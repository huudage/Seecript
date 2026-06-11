"""Module 5 — Plan 组装。

`POST /api/plan/build`：
1. 反查样例 manifest（优先真预解析 `_load_real_manifest`，否则 404 让用户先去拆解）
2. 走 `plan_agent.adapt_structure`，基于 brief + video_goal + settings 把样例骨架改编为 AdaptedSection[]
3. 按 AdaptedSection 一段对应一个 Scene 拼主轨——长度由 LLM 给的 duration_seconds 决定
4. 持久化 Plan（含 adapted_sections + video_goal + settings），供 /gap/detect、/render、/edit 复用
"""
from __future__ import annotations

import logging
import uuid
from typing import Any, Literal, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..routers.library import _LIBRARY, _load_real_manifest
from ..schemas import (
    AdaptedSection,
    AnimationSpec,
    AspectRatio,
    BGMConfig,
    ComposeSettings,
    FillResult,
    Material,
    MaterialShot,
    PackagingItem,
    Plan,
    PlanBuildRequest,
    PlanSnapshotCreateRequest,
    PlanSnapshotEntry,
    PlanSnapshotMeta,
    ReferenceVersion,
    SampleManifest,
    Scene,
    SceneTransition,
    TargetPlatform,
    TextCardSpec,
    ToneStyle,
    TransitionStyle,
    TTSVoice,
)
from ..services.agent.plan_agent import adapt_structure, extract_subject_anchors
from ..services.assets import asset_store
from ..services.library import manifest_store
from ..services.materials import gap_store, material_store
from ..services.plans import plan_snapshot_store, plan_store
from ..services.projects import project_store
from ..services.video.bgm_analysis import analyze_bgm_with_llm

log = logging.getLogger("seecript.plan")
router = APIRouter()


def _resolve_manifest(sample_id: str) -> SampleManifest:
    """加载样例真 manifest；找不到 → 404 让前端先跳拆解页。"""
    real = _load_real_manifest(sample_id)
    if real is not None:
        return real
    raise HTTPException(
        status_code=404,
        detail=f"sample {sample_id} 尚未拆解，请先在「视频拆解」页跑一次 decompose。",
    )


def _resolve_manifests(refs: list[ReferenceVersion]) -> list[SampleManifest]:
    """逐个 (sample_id, slot_id) 精确加载——找不到对应槽就回落到默认查找。

    stage-15 起 Plan/Project 按槽粒度引用，不再隐式取 active 槽：
    - 优先 manifest_store.load_version(sample_id, slot_id)，命中即用
    - 槽不存在（slot_id 写错 / 槽被删了）→ 回落 _resolve_manifest(sample_id)，
      让流水线仍能跑下去而不是 422 中断（前端会在 Compose 端给提示）
    """
    if not refs:
        raise HTTPException(status_code=422, detail="reference_versions 不能为空")
    out: list[SampleManifest] = []
    for rv in refs:
        manifest = manifest_store.load_version(rv.sample_id, rv.slot_id)
        if manifest is not None:
            out.append(manifest)
            continue
        log.warning(
            "[plan] reference_version sample=%s slot=%s 未命中，回落默认 manifest",
            rv.sample_id, rv.slot_id,
        )
        out.append(_resolve_manifest(rv.sample_id))
    return out


def _narration_from_content(content: str, *, limit: int = 60) -> str:
    """从 content_description 取首句（中英标点）作为口播种子，截断到 limit 字符。"""
    if not content:
        return ""
    text = content.strip()
    for sep in ("。", "！", "？", ".", "!", "?", "；", ";"):
        idx = text.find(sep)
        if 0 <= idx < limit:
            return text[: idx + 1]
    return text[:limit]


def _rebuild_subtitle_packaging(plan: Plan) -> None:
    """同步刷新 plan.packaging_track 中 kind="subtitle" 的 PackagingItem。

    用户痛报（2026-06-10）："字幕轨有内容但是预览视频中没字幕"——
    根因：scene.narration / scene.duration 在 step2 改了之后，step3 的 packaging_track
    上一批 subtitle item 还是旧 text / 旧时间窗，前端 PackagingLayer 按旧 PackagingItem.start
    绝对定位，时长错位导致字幕落在静默场景外、被画面盖住或干脆 0 duration。

    本函数 in-place 重建 subtitle 列表（与 plan/build & packaging.apply 同源逻辑）：
    - subtitle_enabled=False → 清空所有 subtitle，保留其他包装项
    - 每个 scene 一条 subtitle，沿用 settings.packaging_prefs 展开后的样式
    - scene.text_card_spec 非空跳过（字卡画面已有主副标）
    - narration 空字符串跳过
    其他 kind（cover/title_bar/sticker/transition）原样保留。
    """
    from ..services.agent.packaging_agent import expand_preset

    non_sub = [it for it in plan.packaging_track if it.kind != "subtitle"]
    if not plan.settings.subtitle_enabled:
        plan.packaging_track = non_sub
        return
    prefs_eff = expand_preset(plan.settings.packaging_prefs)
    subs: list[PackagingItem] = []
    for idx, sc in enumerate(plan.main_track):
        if sc.text_card_spec is not None:
            continue
        text = (sc.narration or "").strip()
        if not text:
            continue
        subs.append(PackagingItem(
            item_id=f"pkg-sub-{idx}",
            kind="subtitle",
            start=sc.start,
            end=sc.start + sc.duration,
            text=text,
            style={
                "font_size": prefs_eff.subtitle_font_size,
                "position": prefs_eff.subtitle_position,
                "background": prefs_eff.subtitle_background,
                "bilingual": prefs_eff.subtitle_bilingual,
                "stroke": "#000",
            },
        ))
    plan.packaging_track = subs + non_sub


def _fill_section_lookup(fills: list[FillResult]) -> dict[str, FillResult]:
    """把 FillResult 按其所属 section_id 索引——多段同 role 时不再被压扁。

    路由优先级：
    1. `fill.section_id` 直接给的 —— v2 后 fill_gap 在所有路径都回填，最权威，不依赖进程内存
    2. `gap_store.get(f.gap_id).section_id` —— 兼容老 fill（无 section_id 字段）+ gap 仍在内存
    3. 都没有 → 丢弃 + warn 日志（提示后端可能重启 / fill 来自旧版本）

    PR-L.3：warn / failed 的 aigc_image / aigc / copy fill（产出空但 action 明确）
    必须保留进索引——这样 _pick 看到该 section 已有 fill 就走 text_card 兜底，
    不会让"该段缺生成"的语义变成"该段抢顺位用户素材"导致后面所有段落集体错位。
    """
    out: dict[str, FillResult] = {}
    dropped: list[str] = []
    for f in fills:
        # 只有完全无 action / 无产出 / 无 section_id 的纯垃圾 fill 才丢
        sid = f.section_id
        if not sid:
            gap = gap_store.get(f.gap_id)
            sid = gap.section_id if (gap and gap.section_id) else None
        if sid is None:
            dropped.append(f.gap_id)
            continue
        # 空产出但 action 是 aigc_image / aigc / copy → 保留占位（_pick 据此走 text_card）
        has_output = bool(f.new_material_id or f.video_urls or f.narration
                          or f.aigc_image_url or f.text_card_spec)
        has_intent = f.action in ("aigc", "aigc_image", "copy")
        if not has_output and not has_intent:
            continue
        # stage-43：last-write-wins。原来 setdefault 让旧 rerank fill 永远压制后写的 aigc_image fill，
        # 用户重新选了「AI 生图」却看到旧素材——直接覆盖即可（FillResult 列表本身是按写入顺序的）。
        out[sid] = f
    if dropped:
        log.warning(
            "[plan] %d fill 因无法定位 section_id 被丢弃：%s（fill 来自旧版本或 gap_store 进程内存已失效）",
            len(dropped), dropped,
        )
    log.info("[plan] fill_by_section 路由：%d fills → %d sections（%s）",
             len(fills), len(out), list(out.keys()))
    return out


# 不同段落 role 偏好的镜头特性：
#   - hook / climax / opening 类：偏好高 action_density（强冲击）
#   - closing / outro 类：偏好低 action_density（收束）
#   - development / problem 等：中性，按时长接近度选
# 系数仅作排序权重，不需要严格归一化。
_ROLE_ACTION_PREFERENCE: dict[str, float] = {
    "hook": 0.85,
    "opening": 0.75,
    "climax": 0.85,
    "transition_break": 0.7,
    "cta": 0.6,
    "closing": 0.25,
    "outro": 0.2,
    "ending": 0.2,
    "callback": 0.4,
    "summary": 0.3,
    "development": 0.5,
    "problem": 0.55,
    "twist": 0.8,
    "demonstration": 0.6,
    "tension": 0.75,
    "reveal": 0.85,
}


def _pick_shot_for_section(material: Material, sec: AdaptedSection) -> MaterialShot | None:
    """从 material.shots 里挑一个最适合该 section 的镜头。

    为什么：之前 user_material 只取前 N 秒（in_point=0），开场静止画面会被塞进
    climax 段。预处理把视频切成镜头并打 (caption / action_density / recommended_role)
    后，这里按 role 优先 + action_density 偏好 + 时长接近度 三级排序。

    返回 None 时（shots 空 / 全无效）调用方应回落到老的 truncate 行为。
    """
    if not material.shots:
        return None
    target_role = (sec.role or "").strip().lower()
    target_dur = max(0.5, float(sec.duration_seconds or 4.0))
    pref_action = _ROLE_ACTION_PREFERENCE.get(target_role, 0.5)

    def _score(sh: MaterialShot) -> float:
        # 越小越好
        role_match = 0.0 if (sh.recommended_role or "").lower() == target_role else 1.0
        action_gap = abs((sh.action_density or 0.5) - pref_action)
        dur_gap = abs(sh.duration - target_dur) / max(target_dur, 0.5)
        # role 是硬性优先；action 与 dur 是软性，权重 1:0.7
        return role_match * 10.0 + action_gap + 0.7 * dur_gap

    return min(material.shots, key=_score)


def _build_bgm_config(bgm_asset_id: Optional[str]) -> BGMConfig:
    """把 PlanBuildRequest.bgm_asset_id 解析为 BGMConfig；None / 找不到 / 未 ready → 无 BGM。

    资产层的 status=processing 直接落 None（避免渲染阶段拿到一个不存在的文件），
    前端在 SSE/loading 时会等到 ready 再让用户提交。
    """
    if not bgm_asset_id:
        return BGMConfig()
    asset = asset_store.get(bgm_asset_id)
    if asset is None:
        log.warning("[plan] bgm asset_id=%s 不存在，本次无 BGM", bgm_asset_id)
        return BGMConfig()
    if asset.kind != "bgm":
        log.warning("[plan] asset_id=%s kind=%s 不是 BGM，忽略", bgm_asset_id, asset.kind)
        return BGMConfig()
    if asset.status != "ready":
        log.warning("[plan] bgm asset_id=%s status=%s 未就绪，本次无 BGM", bgm_asset_id, asset.status)
        return BGMConfig()
    # 触发一次使用统计；不阻塞失败
    try:
        asset_store.touch(bgm_asset_id)
    except Exception as exc:  # noqa: BLE001
        log.debug("[plan] bgm touch failed: %s", exc)
    return BGMConfig(
        bgm_asset_id=bgm_asset_id,
        track_url=asset.file_url,
        duration_seconds=float(asset.metadata.get("duration_seconds") or 0.0) or None,
        peak_seconds=(
            float(asset.metadata["peak_at_seconds"])
            if isinstance(asset.metadata.get("peak_at_seconds"), (int, float))
            else None
        ),
        video_anchor_seconds=0.0,
        volume=0.35,
        fade_in=1.5,
        fade_out=2.0,
        duck_with_voice=True,
    )


async def _attach_bgm_llm_analysis(
    bgm: BGMConfig, *, brief: str, video_goal: str,
) -> BGMConfig:
    """绑定 BGM 时跑一次 doubao-seed 音频理解，把结果挂到 bgm.analysis。

    设计：
    - 没绑 BGM → 直接返回（不调 LLM）
    - 已有 analysis 字段且未换曲（外层先 _build_bgm_config 会重置 analysis=None）→ 跳过
    - LLM 失败/超时 → 保持 None，前端兜底走 librosa peak

    LLM 拿的是公网 URL（PUBLIC_AUDIO_BASE_URL + asset.file_url），与 ASR 同一条暴露路径。
    """
    if not bgm.bgm_asset_id or not bgm.track_url:
        return bgm
    if bgm.analysis is not None:
        return bgm
    try:
        raw = await analyze_bgm_with_llm(
            file_url=bgm.track_url,
            duration_seconds=float(bgm.duration_seconds or 0.0),
            brief=brief or "",
            video_goal=video_goal or "",
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("[plan] bgm LLM 分析异常 asset=%s: %s", bgm.bgm_asset_id, exc)
        raw = None
    if raw is None:
        return bgm
    try:
        from ..schemas import BGMAnalysis
        bgm.analysis = BGMAnalysis.model_validate(raw)
    except Exception as exc:  # noqa: BLE001
        log.warning("[plan] bgm LLM 结果 schema 校验失败 asset=%s: %s", bgm.bgm_asset_id, exc)
    return bgm


def _adapted_sections_to_emotion_input(plan: Plan) -> list:
    """把 plan.adapted_sections 包成 emotion_agent 能用的有 start/end 的 duck-type。

    AdaptedSection 自身不带绝对时间——按 main_track 内的 Scene.parent_section_id 反推
    每段在时间线上的实际 start/end。无 main_track 命中时按 order × duration_seconds 累加兜底。
    """
    if not plan.adapted_sections:
        return []
    # 按 parent_section_id 聚合 main_track scenes
    by_parent: dict[str, list[Scene]] = {}
    for sc in plan.main_track:
        if sc.parent_section_id:
            by_parent.setdefault(sc.parent_section_id, []).append(sc)

    out = []
    cursor = 0.0
    for sec in sorted(plan.adapted_sections, key=lambda s: s.order):
        scs = by_parent.get(sec.section_id, [])
        if scs:
            start = min(sc.start for sc in scs)
            end = max(sc.start + sc.duration for sc in scs)
        else:
            start = cursor
            end = cursor + sec.duration_seconds
            cursor = end

        # 用一个 Section-like 简单对象，含 emotion_agent 需要的全部字段
        class _Sec:
            pass

        s = _Sec()
        s.role = sec.role
        s.theme = sec.theme
        s.start = float(start)
        s.end = float(end)
        s.summary = sec.content_description  # emotion_agent 优先取 summary,缺时取 content_description
        s.content_description = sec.content_description
        s.shot_indices = []
        out.append(s)
    return out


def _auto_align_bgm_to_emotion(plan: Plan) -> None:
    """委派给 services.plans.bgm_align——抽到 service 层是为了让单测能绕开 router import 链。

    详见 services/plans/bgm_align.py 的 docstring（含算法、clamp 策略、调用时机）。
    """
    from ..services.plans.bgm_align import auto_align_bgm_to_emotion as _impl
    _impl(plan)


async def _compute_plan_emotion(plan: Plan) -> Optional["EmotionCurve"]:
    """跑一次 LLM 多信号情绪曲线打分；失败回 None（不抛）。

    被 build_plan 收尾、PATCH /plan/{id}/bgm（换曲后自动重算）、
    POST /plan/{id}/recompute-emotion（手动重算）三处复用。
    """
    try:
        from ..services.agent.emotion_agent import (
            PlanIntent as _PlanIntent,
            score_emotion as _score_emotion,
        )
        primary_manifest = None
        if plan.reference_versions:
            rv0 = plan.reference_versions[0]
            primary_manifest = manifest_store.load_version(rv0.sample_id, rv0.slot_id)
        intent = _PlanIntent(
            brief=plan.brief,
            video_goal=plan.video_goal,
            migration_preference=plan.settings.migration_preference,
        )
        pseudo_sections = _adapted_sections_to_emotion_input(plan)
        return await _score_emotion(
            sections=pseudo_sections,
            shots=plan.main_track,
            total_duration=plan.duration_seconds,
            bgm_analysis=plan.bgm.analysis if plan.bgm else None,
            bgm_energy=None,
            understanding=primary_manifest.understanding if primary_manifest else None,
            sample_analysis=primary_manifest.analysis if primary_manifest else None,
            intent=intent,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("[plan] emotion 计算失败 plan=%s: %s", plan.plan_id, exc)
        return None


@router.post("/plan/build", response_model=Plan)
async def build_plan(req: PlanBuildRequest) -> Plan:
    plan_id = f"plan-{uuid.uuid4().hex[:10]}"
    settings = req.settings or ComposeSettings()
    # v2 起 session_id == project_id；老前端只传 session_id 时仍可工作
    effective_project_id = (req.project_id or req.session_id or "").strip() or None
    log.info(
        "[plan] build plan=%s refs=%s project=%s materials=%d fills=%d variant=%s "
        "brief=%s goal=%s target_dur=%.0fs platform=%s tone=%s",
        plan_id,
        [(rv.sample_id, rv.slot_id) for rv in req.reference_versions],
        effective_project_id,
        len(req.selected_materials), len(req.fills),
        req.variant, (req.brief or "")[:30], (req.video_goal or "")[:30],
        settings.target_duration_seconds, settings.target_platform, settings.tone,
    )

    # 1. 取样例 manifests（1-2 个，按 slot 精确加载）
    manifests = _resolve_manifests(req.reference_versions)

    # 2. LLM 改编段落结构（带 settings + 参考素材注入；多样例时段落结构合并参考）
    # 增量构建：若前端透传 reuse_sections（上一版 plan.adapted_sections），跳过 LLM——
    # 修复『5→4 段抖动』bug：每次 runAnalyze 都重跑 LLM，非确定性导致段数飘忽。
    if req.reuse_sections:
        adapted = list(req.reuse_sections)
        log.info("[plan] reuse_sections=%d 跳过 adapt_structure", len(adapted))
    else:
        # stage-58：reference_asset_ids 字段保留 schema 但不再传给 adapt_structure——
        # 结构迁移已改为节奏画像注入（见 plan_agent._build_rhythm_block）。
        adapted = await adapt_structure(
            manifests, req.brief, req.video_goal, settings,
        )
    if not adapted:
        # fallback：用第一份样例的 sections 1:1 兜底
        primary_sections = list(manifests[0].sections)
        adapted = [
            AdaptedSection(
                section_id=f"sec-{i}",
                role=sec.role,
                theme=sec.theme or "段落",
                content_description=f"[fallback] {sec.role} 段，沿用样例结构。",
                source_section_indices=[i],
                source_shot_indices=list(sec.shot_indices or []),
                order=i,
                duration_seconds=4.0,
            )
            for i, sec in enumerate(primary_sections)
        ]

    log.info("[plan] adapted_sections=%d (sample_total_sections=%d) target_total=%.1fs",
             len(adapted), sum(len(m.sections) for m in manifests),
             sum(s.duration_seconds for s in adapted))

    # PR-B：用户素材分镜级匹配。把 selected_materials 里所有 video material 的
    # MaterialShot 池子拿出来，对每个 AdaptedSection.shots 跑 ShotPlan ↔ MaterialShot
    # 文本相似度 + role 加权 + 时长接近度匹配，把结果写回 ShotPlan.matched_material_*。
    # 多镜物化时（plan.py 下方 if n_shots>=2 分支）会优先按这个匹配选材，匹配失败回落
    # 到 cyclic 策略。
    try:
        from ..services.agent.shot_matcher import apply_matches_to_section, match_section_shots
        material_pool: list[Material] = []
        if effective_project_id:
            seen_ids: set[str] = set()
            # 1) 显式 selected_materials 优先（用户在 step1 勾选过的）
            for mid in req.selected_materials:
                if mid in seen_ids:
                    continue
                m = material_store.get(effective_project_id, mid)
                if m is not None:
                    material_pool.append(m)
                    seen_ids.add(mid)
            # 2) stage-60: 兜底——若 selected_materials 为空 / 缺片，把项目里其它已上传的
            #    user material（video / image）也纳入匹配池。用户报障"多镜片段没有打分"
            #    根因：他没把素材加进 selected_materials 但素材库里有 → 不打分。
            #    扩池后每个 sub-shot 都能拿到 match_quality + match_score。
            for m in material_store.list(effective_project_id):
                if m.material_id in seen_ids:
                    continue
                if m.media_type not in ("video", "image"):
                    continue
                material_pool.append(m)
                seen_ids.add(m.material_id)
        if material_pool:
            adapted = [
                apply_matches_to_section(sec, match_section_shots(sec, material_pool))
                for sec in adapted
            ]
            n_match = sum(
                1 for sec in adapted for sh in sec.shots if sh.matched_material_id
            )
            log.info("[plan] PR-B shot match: pool=%d matched=%d/%d shots",
                     len(material_pool), n_match,
                     sum(len(sec.shots) for sec in adapted))
    except Exception as exc:  # noqa: BLE001
        log.warning("[plan] shot_matcher 失败（跳过，不阻塞 plan）：%s", exc)

    # 3. 把 fills 按 section_id 索引——这是修复『多段 development 全被路由到第一段』bug 的关键
    fill_by_section = _fill_section_lookup(req.fills)
    material_cursor = 0

    # stage-58 G3：cursor 跳过 PR-B 已锁的素材，避免段 1 顺位抢走段 3 PR-B 锁定的素材。
    # 先扫一遍所有 sec.shots 收集 matched_material_id 集合。
    locked_material_ids: set[str] = set()
    for _sec in adapted:
        for _sh in (_sec.shots or []):
            if getattr(_sh, "matched_material_id", None):
                locked_material_ids.add(_sh.matched_material_id)
    if locked_material_ids:
        log.info("[plan] stage-58 cursor lockset = %d ids", len(locked_material_ids))

    def _pick(sec: AdaptedSection) -> tuple[str, str, list[str], str | None, str | None, "TextCardSpec | None", str | None, list[str], "AnimationSpec | None"]:
        """返回 (source, source_ref, aigc_video_urls, narration_override, voiceover_url, text_card_spec, aigc_image_url, aigc_image_urls, animation_spec)。

        优先级：本段 fill（aigc / aigc_image / copy / rerank） > 用户素材 > 文字卡兜底。
        - aigc fill：source=aigc_t2v，source_ref=task_id，aigc_video_urls=video_urls
        - aigc_image fill：source=aigc_image，source_ref=new_material_id，aigc_image_url=fill.aigc_image_url
                          多镜头时 aigc_image_urls = fill.aigc_image_urls（path B 拆分用）
                          animation_spec：若 fill 携带则透传，决定 Scene 走 remotion 渲染还是 ffmpeg 静帧
        - copy fill：source=text_card（字卡画面），text_card_spec=fill.text_card_spec；
                     narration 用 main_text + sub_text 拼接（供 TTS 与 LLM 上下文）；
                     若 fill.voiceover_url 已存在（gap 路由自动 TTS 写入），直接透传到 Scene。
        - rerank fill：source=user_material，source_ref=new_material_id（gap_agent 真挑材后的真 material_id）；
                     优先级在顺位 selected_materials 之前，避免"用户挑了素材 A 但 rebuild 用了 B"
        - 用户素材：顺位消费 selected_materials
        - 兜底：source=text_card，文案取自 content_description 首句
        """
        nonlocal material_cursor
        fill = fill_by_section.get(sec.section_id)
        if fill and fill.action == "aigc" and (fill.video_urls or fill.new_material_id):
            return (
                "aigc_t2v",
                fill.new_material_id or (fill.video_urls[0] if fill.video_urls else "aigc"),
                list(fill.video_urls),
                None,
                None,
                None,
                None,
                [],
                None,
            )
        if fill and fill.action == "aigc_image" and fill.aigc_image_url:
            return (
                "aigc_image",
                fill.new_material_id or f"aigc-image-{sec.section_id}",
                [],
                None,
                None,
                None,
                fill.aigc_image_url,
                list(fill.aigc_image_urls or []),
                fill.animation_spec,
            )
        narration_override = None
        voiceover_url = None
        text_card_spec = None
        if fill and fill.action == "copy":
            # copy fill 的主输出是 text_card_spec；narration 字段仅为兼容
            text_card_spec = fill.text_card_spec
            if fill.narration:
                narration_override = fill.narration
            voiceover_url = (fill.voiceover_url or "").strip() or None
            # 有 text_card_spec → 直接走 text_card 分支（即使有 user_material 也不抢；用户用 copy tab 就是要字卡）
            if text_card_spec is not None:
                return (
                    "text_card",
                    f"text-card-fill-{sec.section_id}",
                    [],
                    narration_override,
                    voiceover_url,
                    text_card_spec,
                    None,
                    [],
                    None,
                )
        # PR-L.3：本段已绑定 aigc / aigc_image / copy fill 但产出为空（warn / 任务超时 / Seedream 失败 / LLM 返空）
        # 关键约束：section ↔ scene 严格 1:1，不允许"该段被抢顺位 user_material 后面段落全部错位"。
        # 直接给字卡兜底，文案取自 sec.content_description / fill.narration / fill.note。
        if fill and fill.action in ("aigc", "aigc_image", "copy"):
            fallback_text = (
                (fill.narration or "").strip()
                or (sec.content_description or "").strip()
                or (fill.note or "").strip()
                or sec.role
            )
            return (
                "text_card",
                f"text-card-fill-empty-{sec.section_id}",
                [],
                fallback_text or None,
                None,
                None,
                None,
                [],
                None,
            )
        # stage-57：rerank fill 把用户挑/AI 挑的真 material_id 落到本段——优先级高于顺位消费 selected_materials，
        # 这样用户在工作坊里挑了不同素材后，rebuild 出来的 Scene 真的会用那个素材，而不是默默被顺序覆盖。
        # 空 new_material_id 的 rerank（material_store 空 / 手动模式没选）走顺位 fallback。
        if fill and fill.action == "rerank" and fill.new_material_id:
            return (
                "user_material",
                fill.new_material_id,
                [],
                narration_override,
                voiceover_url,
                None,
                None,
                [],
                None,
            )
        # stage-58 G2：单镜段读 PR-B 匹配——优先用 sec.shots[0].matched_material_id
        # （即使 cursor 还没消费到它），避免 cursor 顺位错位。
        if sec.shots and getattr(sec.shots[0], "matched_material_id", None):
            mid = sec.shots[0].matched_material_id
            return ("user_material", mid, [], narration_override, voiceover_url, None, None, [], None)
        # stage-58 G3：cursor 跳过 PR-B 已锁素材
        while material_cursor < len(req.selected_materials):
            ref = req.selected_materials[material_cursor]
            if ref in locked_material_ids:
                material_cursor += 1
                continue
            material_cursor += 1
            return ("user_material", ref, [], narration_override, voiceover_url, None, None, [], None)
        # 无 AIGC、无 copy 文案、无用户素材 → 文字卡（packaging 字幕负责显示真实文案）
        return ("text_card", f"text-card-{sec.section_id}", [], narration_override, voiceover_url, None, None, [], None)

    main_track: list[Scene] = []
    timeline_cursor = 0.0
    for sec in adapted:
        source, source_ref, aigc_urls, narration_override, voiceover_url, text_card_spec, aigc_image_url, aigc_image_urls, animation_spec = _pick(sec)
        target_duration = float(sec.duration_seconds) if sec.duration_seconds > 0 else 4.0

        # ---- keyframe_morph：多张图保留在同一 Scene 上，由 Remotion 渲染器一次性渐变 ----
        # 不切子 Scene；image_urls 透传给 Remotion AnimatedImage composition。
        if (
            source == "aigc_image"
            and animation_spec is not None
            and getattr(animation_spec, "engine", "ffmpeg") == "remotion"
            and getattr(animation_spec, "animation_type", "") == "keyframe_morph"
            and len(aigc_image_urls) > 1
        ):
            spec_copy = animation_spec.model_copy(update={"image_urls": list(aigc_image_urls)})
            scene = Scene(
                scene_id=f"sc-{sec.order}",
                section=sec.role,  # type: ignore[arg-type]
                parent_section_id=sec.section_id,
                shot_order=0,
                shot_subject=sec.shots[0].subject if sec.shots else "",
                source="aigc_image",  # type: ignore[arg-type]
                source_ref=source_ref,
                start=timeline_cursor,
                duration=target_duration,
                in_point=0.0,
                out_point=None,
                narration=narration_override or "",
                voiceover_url=voiceover_url,
                aigc_video_urls=[],
                aigc_image_url=aigc_image_urls[0],  # 兜底字段：首张图，避免渲染失败时静帧空白
                text_card_spec=None,
                animation_spec=spec_copy,
            )
            main_track.append(scene)
            timeline_cursor += target_duration
            continue

        # ---- stage-24 multi-shot 物化：sec.shots 显式拆分驱动 ----
        # 触发条件：sec.shots 长度 ≥ 2，或老 aigc_image 多图（path B）。两者会走同一路径。
        # 优先级：sec.shots 是结构性事实，aigc_image_urls 是已生成的素材；当 N 张图与
        # N 个 ShotPlan 数量不一致时，按数量小的对齐（保证不漏镜也不漏图）。
        n_shots = len(sec.shots)
        n_imgs = len(aigc_image_urls)
        # stage-60: 拆分前的"视觉差异度"门禁——上游 LLM 把段拆成 N 个 sub-shot 是结构性意图,
        # 但物化时若没有 N 份不同素材撑场, 物理上就是把同一片段连放 N 次（用户看到「砍价反差剧情」
        # 重复 3 遍那种）. 这里检查是否有差异化素材池, 若没有就跳过 multi-shot 路径,
        # 回到下方的单 Scene 路径占满 target_duration.
        # 例外: text_card 即便没素材, 文案逐镜不同, 仍允许拆.
        def _has_visual_variety_for_split(N: int) -> bool:
            if source == "text_card":
                return True
            if source == "aigc_image":
                return n_imgs >= max(2, N)
            if source == "aigc_t2v":
                return len(aigc_urls) >= max(2, N)
            if source == "user_material":
                # stage-76: user_material 一律走拆分路径（如果 sec.shots ≥ 2）—— sub-shot
                # 都是占位（text_card + needs_fill），让用户在 step2 按分镜粒度各自选素材。
                # 不再探测"素材是否够分"——因为 build_plan 根本不自动用素材。
                return True
            return True

        if n_shots >= 2 or (source == "aigc_image" and n_imgs > 1):
            _N_check = n_shots if n_shots >= 2 else n_imgs
            _can_split = _has_visual_variety_for_split(_N_check)
            if not _can_split:
                log.info(
                    "[plan] sec=%s 拆分意图 N=%d 但视觉素材不足→回落单 Scene 占满 %.2fs (source=%s)",
                    sec.section_id, _N_check, target_duration, source,
                )
        else:
            _can_split = False

        if _can_split:
            # 决定本段最终拆分镜头数 N：以 sec.shots 为准（plan_agent 给的拆分意图）；
            # 老 plan / fallback 没给 shots 时退回 aigc_image_urls 长度。
            if n_shots >= 2:
                N = n_shots
                shot_durs = [float(sh.duration_seconds) for sh in sec.shots]
            else:
                N = n_imgs
                shot_durs = [target_duration / N] * N
            sum_dur = sum(shot_durs) or target_duration
            # 归一化到段总时长（plan_agent 已做但兜底）
            shot_durs = [d * target_duration / sum_dur for d in shot_durs]

            # 选材策略：
            # - aigc_image：循环 aigc_image_urls，images 不够时复用最后一张
            # - user_material：所有 shot 用同一 material；in/out 由 _pick_shot_for_section
            #   选定后按比例细分（PR-B 会改成按 shot.subject 选 MaterialShot）
            # - text_card：每个 shot 自成 text_card，main_text=subject 或 narration 首句
            # - aigc_t2v：把 video_urls 按比例分给各 shot（>= N 段时 1:1，少于 N 时复用）
            for shot_idx in range(N):
                shot = sec.shots[shot_idx] if shot_idx < n_shots else None
                shot_dur = shot_durs[shot_idx] if shot_idx < len(shot_durs) else target_duration / N
                shot_subject = shot.subject if shot else ""
                shot_narration = (shot.narration if shot else "") or (narration_override or "" if shot_idx == 0 else "")

                sub_id = f"sc-{sec.order}-sh-{shot_idx}"

                if source == "aigc_image":
                    pick_url = aigc_image_urls[shot_idx] if shot_idx < n_imgs else (aigc_image_urls[-1] if n_imgs else aigc_image_url)
                    # stage-49：每个 sub-scene 用单图独立 AnimationSpec，按本镜 camera_technique 推运镜。
                    # 关键：剥掉父段 spec.image_urls，否则前端 StoryboardLayer 会循环全 N 张图，
                    # 造成"3 张图在每个 sub-scene 都反复切"的观感（用户报障）。
                    from ..services.agent.gap_agent import suggest_animation_spec_for_shot_async
                    sub_anim_spec = await suggest_animation_spec_for_shot_async(shot, sec, None)
                    main_track.append(Scene(
                        scene_id=sub_id,
                        section=sec.role,  # type: ignore[arg-type]
                        parent_section_id=sec.section_id,
                        shot_order=shot_idx,
                        shot_subject=shot_subject,
                        source="aigc_image",  # type: ignore[arg-type]
                        source_ref=f"{source_ref}-sh-{shot_idx}" if n_imgs > 1 else source_ref,
                        start=timeline_cursor,
                        duration=shot_dur,
                        in_point=0.0,
                        out_point=None,
                        narration=shot_narration,
                        voiceover_url=voiceover_url if shot_idx == 0 else None,
                        aigc_video_urls=[],
                        aigc_image_url=pick_url,
                        text_card_spec=None,
                        animation_spec=sub_anim_spec,
                    ))
                elif source == "text_card":
                    # 每个 shot 一张字卡：main_text 取 subject（或 narration 首句），sub_text 取 narration 余文
                    main_t = (shot_subject or (shot_narration.split("。")[0] if shot_narration else "") or "")[:24]
                    sub_t = (shot_narration if shot_subject else "。".join(shot_narration.split("。")[1:]))[:40]
                    base_spec = text_card_spec
                    if base_spec is not None:
                        spec = base_spec.model_copy(update={
                            "main_text": main_t or base_spec.main_text,
                            "sub_text": sub_t or base_spec.sub_text,
                            "duration_seconds": round(max(1.5, min(15.0, shot_dur)), 2),
                        })
                    else:
                        from ..schemas import TextCardSpec  # 延迟避免循环
                        spec = TextCardSpec(main_text=main_t, sub_text=sub_t, duration_seconds=round(max(1.5, min(15.0, shot_dur)), 2))
                    main_track.append(Scene(
                        scene_id=sub_id,
                        section=sec.role,  # type: ignore[arg-type]
                        parent_section_id=sec.section_id,
                        shot_order=shot_idx,
                        shot_subject=shot_subject,
                        source="text_card",  # type: ignore[arg-type]
                        source_ref=f"text-card-{sec.section_id}-sh-{shot_idx}",
                        start=timeline_cursor,
                        duration=shot_dur,
                        in_point=0.0,
                        out_point=None,
                        narration=shot_narration,
                        voiceover_url=voiceover_url if shot_idx == 0 else None,
                        aigc_video_urls=[],
                        aigc_image_url=None,
                        text_card_spec=spec,
                        animation_spec=None,
                    ))
                elif source == "user_material":
                    # stage-76: 用户底线（2026-06-12）："只让你对真实素材做切片，不要做其他处理"。
                    # 之前自动按 matched_material_shot_index / N 等分 / cyclic 三档填用户素材，
                    # 是「腌入味了画面重复」bug 的根因（同 material 被复用 / 切碎）。
                    # 现在 user_material 一律占位：text_card + needs_fill=True，让用户在 step2
                    # 用 SwapSourceDialog 手动选 material + MaterialTrimPanel 裁剪（swap-source
                    # 链路会写真实 in_point/out_point 并 _rebuild_timeline 重铺 plan.duration_seconds）。
                    main_t = (shot_subject or sec.theme or sec.role)[:24]
                    sub_t = (shot_narration[:40] if shot_narration else "请在分镜编辑界面手动选择素材")
                    from ..schemas import TextCardSpec  # 延迟避免循环
                    spec = TextCardSpec(
                        main_text=main_t or "待选素材",
                        sub_text=sub_t,
                        duration_seconds=round(max(1.5, min(15.0, shot_dur)), 2),
                    )
                    main_track.append(Scene(
                        scene_id=sub_id,
                        section=sec.role,  # type: ignore[arg-type]
                        parent_section_id=sec.section_id,
                        shot_order=shot_idx,
                        shot_subject=shot_subject,
                        source="text_card",  # type: ignore[arg-type]
                        source_ref=f"placeholder-{sec.section_id}-sh-{shot_idx}",
                        start=timeline_cursor,
                        duration=shot_dur,
                        in_point=0.0,
                        out_point=None,
                        narration=shot_narration,
                        voiceover_url=voiceover_url if shot_idx == 0 else None,
                        aigc_video_urls=[],
                        aigc_image_url=None,
                        text_card_spec=spec,
                        animation_spec=None,
                        needs_fill=True,
                    ))
                else:  # aigc_t2v / sample / fallback
                    # 简化：aigc_t2v 按 shot 比例切割 video_urls；不够 N 时复用最后一段
                    sub_video_urls: list[str] = []
                    if source == "aigc_t2v" and aigc_urls:
                        if len(aigc_urls) >= N:
                            # 按比例切片：shot_idx 落入 floor(shot_idx * len/N) 那段
                            picked = aigc_urls[min(shot_idx * len(aigc_urls) // N, len(aigc_urls) - 1)]
                            sub_video_urls = [picked]
                        else:
                            sub_video_urls = [aigc_urls[min(shot_idx, len(aigc_urls) - 1)]]
                    main_track.append(Scene(
                        scene_id=sub_id,
                        section=sec.role,  # type: ignore[arg-type]
                        parent_section_id=sec.section_id,
                        shot_order=shot_idx,
                        shot_subject=shot_subject,
                        source=source,  # type: ignore[arg-type]
                        source_ref=f"{source_ref}-sh-{shot_idx}" if N > 1 else source_ref,
                        start=timeline_cursor,
                        duration=shot_dur,
                        in_point=0.0,
                        out_point=None,
                        narration=shot_narration,
                        voiceover_url=voiceover_url if shot_idx == 0 else None,
                        aigc_video_urls=sub_video_urls,
                        aigc_image_url=None,
                        text_card_spec=None,
                        animation_spec=None,
                    ))
                # 推进 cursor 用刚 append 的 scene.duration——user_material 裁剪可能 < shot_dur，
                # 用 shot_dur 会留缝；text_card / aigc 分支 duration == shot_dur，行为不变。
                timeline_cursor += main_track[-1].duration
            continue

        in_point = 0.0
        out_point: float | None = None
        actual_duration = target_duration
        if source == "user_material":
            # stage-76: 单 Scene 路径同样占位——build_plan 不再自动用 user_material 切片。
            # 用 text_card 占位渲染（main_text=section.theme / shot.subject），needs_fill=True
            # 让 FourTrackBoard 高亮"待选素材"；用户在 step2 SwapSourceDialog 手动选 material
            # + MaterialTrimPanel 裁剪后，swap-source 会把 source 改回 user_material 并写
            # 真实 in/out/duration，再由 _rebuild_timeline 顺延 plan.duration_seconds。
            only_subj = sec.shots[0].subject if sec.shots else ""
            main_t = (only_subj or sec.theme or sec.role)[:24]
            sub_t = (narration_override[:40] if narration_override else "请在分镜编辑界面手动选择素材")
            from ..schemas import TextCardSpec  # 延迟避免循环
            placeholder_spec = TextCardSpec(
                main_text=main_t or "待选素材",
                sub_text=sub_t,
                duration_seconds=round(max(1.5, min(15.0, target_duration)), 2),
            )
            # 单 Scene 路径下文不区分 source，统一用占位变量族构造 Scene
            source = "text_card"  # type: ignore[assignment]
            source_ref = f"placeholder-{sec.section_id}"
            text_card_spec = placeholder_spec
            in_point = 0.0
            out_point = None
            actual_duration = target_duration
            _placeholder_needs_fill = True
        else:
            _placeholder_needs_fill = False
        # text_card / aigc_t2v / aigc_image：无 in/out 概念，actual_duration = target_duration

        # 不再用 content_description 自动种 narration——那会让用户在第 2 步看到"全段已有文案"，
        # 误以为系统替他做了 LLM 文案补全。改成：narration 仅在显式 fill (action=copy) 时写入，
        # 其余段落留空，由用户在 Compose UI 主动触发文案 / 配音 / AIGC。
        narration_text = narration_override or ""
        # PR-A：即便 sec.shots 为 0/1，也给单 Scene 写上 parent_section_id + shot_order=0，
        # 让前端 FourTrackBoard 的"按段聚合 → 展开分镜"逻辑统一走一条路径。
        only_shot_subject = sec.shots[0].subject if sec.shots else ""
        scene = Scene(
            scene_id=f"sc-{sec.order}",
            section=sec.role,  # type: ignore[arg-type]
            parent_section_id=sec.section_id,
            shot_order=0,
            shot_subject=only_shot_subject,
            source=source,  # type: ignore[arg-type]
            source_ref=source_ref,
            start=timeline_cursor,
            duration=actual_duration,
            in_point=in_point,
            out_point=out_point,
            narration=narration_text,
            voiceover_url=voiceover_url,
            aigc_video_urls=aigc_urls,
            aigc_image_url=aigc_image_url,
            text_card_spec=text_card_spec,
            animation_spec=animation_spec if source == "aigc_image" else None,
            needs_fill=_placeholder_needs_fill,
        )
        main_track.append(scene)
        timeline_cursor += actual_duration

    # stage-72/74：跨 section 去重 user_material 窗口（含 partial overlap）。
    # 现象：shot_matcher 给不同 section 的 shot 返回同一个 matched_material_shot_index
    # → build 出来两段 Scene 引用同 material；窗口可能完全相同（stage-72 已修），
    # 也可能 partial overlap（同 material 但 [0,2.19] 与 [0,2.63]——两段都包含"腌
    # 入味了"那几个音节，用户感知是"音画都重复了"）。
    # 修复：seen 用 list[(in,out)] 而非精确点；每个后续段 shift in_point 直到与所有
    # seen 区间无重叠；找不到位置就标 needs_fill。
    # 重叠判定：a 与 b 重叠 iff max(a_in,b_in) < min(a_out,b_out) - 0.05s
    # （允许 50ms 之内的瞬时碰边作为"相邻不算重叠"）。
    if effective_project_id:
        seen_intervals: dict[str, list[tuple[float, float, str]]] = {}
        OVERLAP_TOL = 0.05

        def _overlaps(a_in: float, a_out: float, intervals: list[tuple[float, float, str]]) -> str | None:
            for b_in, b_out, b_scene in intervals:
                if max(a_in, b_in) < min(a_out, b_out) - OVERLAP_TOL:
                    return b_scene
            return None

        for idx, sc in enumerate(main_track):
            if sc.source != "user_material" or sc.out_point is None or not sc.source_ref:
                continue
            intervals = seen_intervals.setdefault(sc.source_ref, [])
            hit = _overlaps(sc.in_point, sc.out_point, intervals)
            if hit is None:
                intervals.append((sc.in_point, sc.out_point, sc.scene_id))
                continue
            mat = material_store.get(effective_project_id, sc.source_ref)
            mat_dur = float(mat.duration_seconds or 0.0) if mat is not None else 0.0
            win_len = float(sc.out_point - sc.in_point)
            if mat_dur <= 0 or win_len <= 0 or mat_dur <= win_len + OVERLAP_TOL:
                # 素材太短或没有时长 → 没法挪，标 needs_fill 让前端提醒
                main_track[idx] = sc.model_copy(update={"needs_fill": True})
                continue
            shifted = False
            # 跳步：先按 win_len 跳，找不到再用 0.5s 细步扫一遍兜底
            trial_offsets: list[float] = []
            step_coarse = max(win_len * 0.5, 0.5)
            t = step_coarse
            while t < mat_dur:
                trial_offsets.append(t)
                t += step_coarse
            trial_offsets.append(max(0.0, mat_dur - win_len))  # 尾段兜底
            seen_offsets: set[int] = set()
            for offset in trial_offsets:
                trial_in = round(min(max(0.0, offset), max(0.0, mat_dur - win_len)), 3)
                key = int(trial_in * 1000)
                if key in seen_offsets:
                    continue
                seen_offsets.add(key)
                trial_out = round(min(trial_in + win_len, mat_dur), 3)
                if trial_out - trial_in < 0.5:
                    continue
                if _overlaps(trial_in, trial_out, intervals) is not None:
                    continue
                main_track[idx] = sc.model_copy(update={
                    "in_point": trial_in,
                    "out_point": trial_out,
                    "duration": round(trial_out - trial_in, 3),
                })
                intervals.append((trial_in, trial_out, sc.scene_id))
                log.info(
                    "[plan] stage-74 dedup: %s 与 %s 窗口重叠 (%s,%.2f,%.2f) → shift 到 (%.2f,%.2f)",
                    sc.scene_id, hit, sc.source_ref, sc.in_point, sc.out_point,
                    trial_in, trial_out,
                )
                shifted = True
                break
            if not shifted:
                main_track[idx] = sc.model_copy(update={"needs_fill": True})

        # dedup 可能改了 scene.duration（trial_out 触底时），重铺 scene.start
        new_cursor = 0.0
        for idx, sc in enumerate(main_track):
            if abs(sc.start - new_cursor) > 1e-3:
                main_track[idx] = sc.model_copy(update={"start": round(new_cursor, 3)})
            new_cursor += main_track[idx].duration

    actual_total = sum(sc.duration for sc in main_track) or 1.0

    # 4. 包装轨：仅生成每段口播字幕；title_bar/sticker/cover/transition 全部走
    # V2 流程（PackagingPanel 推荐→挑选→/packaging/apply 落盘）。
    packaging_track: list[PackagingItem] = []

    # 每个 Scene 烧一条字幕（用 scene.narration 而不是单条 placeholder）
    # subtitle_enabled=False 时跳过：用户没在字幕轨打开开关时不该自动出字幕。
    # scene.text_card_spec 非空的段也跳过：字卡画面已经显示主副标，字幕会与之打架。
    # 保留 scene.narration 文本，供 LLM 改编上下文 / 后续 TTS 使用。
    if settings.subtitle_enabled:
        prefs = settings.packaging_prefs
        # custom 时直接用 prefs 字段；非 custom 走预设展开（确保 plan/build 落盘的 subtitle 样式
        # 与 PackagingPanel 默认预设一致，不必等用户先点一次"一键包装"才生效）。
        from ..services.agent.packaging_agent import expand_preset
        effective = expand_preset(prefs)
        for idx, scene in enumerate(main_track):
            if scene.text_card_spec is not None:
                continue
            sub_text = (scene.narration or "").strip()
            if not sub_text:
                continue
            packaging_track.append(PackagingItem(
                item_id=f"pkg-sub-{idx}",
                kind="subtitle",
                start=scene.start,
                end=scene.start + scene.duration,
                text=sub_text,
                style={
                    "font_size": effective.subtitle_font_size,
                    "position": effective.subtitle_position,
                    "background": effective.subtitle_background,
                    "bilingual": effective.subtitle_bilingual,
                    "stroke": "#000",
                },
            ))

    plan = Plan(
        plan_id=plan_id,
        reference_versions=list(req.reference_versions),
        project_id=effective_project_id,
        session_id=effective_project_id,
        brief=req.brief,
        video_goal=req.video_goal,
        subject_anchors=extract_subject_anchors(req.brief),
        adapted_sections=adapted,
        variant=req.variant,
        duration_seconds=actual_total,
        main_track=main_track,
        packaging_track=packaging_track,
        bgm=await _attach_bgm_llm_analysis(
            _build_bgm_config(req.bgm_asset_id),
            brief=req.brief or "",
            video_goal=req.video_goal or "",
        ),
        settings=settings,
    )
    # 个性知识库注入统计：本次 plan/build 实际"看到"了多少条 KB 规则。
    # 与 plan_agent 内部注入逻辑独立计算同一份数据——前端徽标用此字段。
    try:
        from ..services.profile import collect_active_rules, count_applied_rules
        plan.kb_rules_applied = count_applied_rules(collect_active_rules())
    except Exception as exc:  # noqa: BLE001
        log.warning("[plan] kb_rules_applied 统计失败 plan=%s: %s", plan_id, exc)
    # 蒸馏初版 snapshot：render commit 时与 v1 做 diff 落 Trace A 用。
    # 后续 PATCH（scene 编辑 / gap fill 重建）不重写本字段，确保 v0 基准稳定。
    try:
        from ..services.profile import to_snapshot as _profile_to_snapshot
        plan.initial_snapshot = _profile_to_snapshot(plan)
    except Exception as exc:  # noqa: BLE001
        log.warning("[plan] 写 initial_snapshot 失败 plan=%s: %s", plan_id, exc)

    # stage-28 LLM 多信号情绪曲线 ----
    plan.emotion_curve = await _compute_plan_emotion(plan)
    if plan.emotion_curve:
        log.info(
            "[plan] emotion_curve plan=%s backend=%s anchors=%d peaks=%d",
            plan_id, plan.emotion_curve.backend,
            len(plan.emotion_curve.anchors), len(plan.emotion_curve.peaks),
        )
    # stage-60：BGM 高潮自动切片对齐内容高潮（用户上传 BGM 后听不到好听段的根因）
    _auto_align_bgm_to_emotion(plan)

    # stage-59：素材适配度打分。给所有 user_material scene 写 fit_score / fit_reason，
    # 让用户在 Compose 卡上看到"这条素材跟段意 NN% 搭"，便于人工挑替换。
    if effective_project_id:
        try:
            from ..services.materials.fit import annotate_plan_fit_scores
            mats_list = material_store.list(effective_project_id)
            mats_by_id = {m.material_id: m for m in mats_list}
            written = annotate_plan_fit_scores(
                main_track=plan.main_track,
                adapted_sections=plan.adapted_sections or [],
                materials_by_id=mats_by_id,
            )
            log.info("[plan] fit_scores annotated plan=%s scenes=%d", plan_id, written)
        except Exception as exc:  # noqa: BLE001
            log.warning("[plan] fit_scores 计算失败 plan=%s: %s", plan_id, exc)

    plan_store.put(plan)

    # 回写到 Project，让首页/项目详情能拿到 last_plan_id
    if effective_project_id:
        try:
            project_store.mark_planned(effective_project_id, plan_id)
        except Exception as exc:  # noqa: BLE001
            log.warning("[plan] mark_planned(%s, %s) 失败：%s", effective_project_id, plan_id, exc)

    # 诊断日志：主轨各 source 占比——『渲染结果与样例无异』时第一眼就能看出是不是全 fallback 到 sample
    source_counts: dict[str, int] = {}
    for sc in main_track:
        source_counts[sc.source] = source_counts.get(sc.source, 0) + 1
    log.info(
        "[plan] plan=%s 主轨 source 分布：%s（共 %d 段，总 %.1fs，fills 输入 %d/路由命中 %d）",
        plan_id, source_counts, len(main_track), actual_total,
        len(req.fills), len(fill_by_section),
    )
    return plan


@router.get("/plan/{plan_id}", response_model=Plan)
async def get_plan(plan_id: str) -> Plan:
    """Plan 详情查询。包装/编辑动作回写后，前端用它把 store 同步成最新版本。"""
    plan = plan_store.get(plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail=f"plan_id 不存在：{plan_id}")
    return plan


class PlanBgmPatch(BaseModel):
    """PATCH /plan/{plan_id}/bgm：BGM 替换 / 锚点拖动 / 音量调节。

    `bgm_asset_id` 给值则换 BGM（重新分析、重置 anchor=0）；
    给值为空字符串等同 DELETE（清空 BGM 引用）；
    不给值仅修改可选字段。
    """
    bgm_asset_id: Optional[str] = Field(default=None)
    video_anchor_seconds: Optional[float] = None
    volume: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    fade_in: Optional[float] = Field(default=None, ge=0.0, le=10.0)
    fade_out: Optional[float] = Field(default=None, ge=0.0, le=10.0)
    duck_with_voice: Optional[bool] = None


@router.patch("/plan/{plan_id}/bgm", response_model=Plan)
async def patch_plan_bgm(plan_id: str, body: PlanBgmPatch) -> Plan:
    """更新 plan 的 BGM 配置——支持换曲 / 拖动锚点 / 调音量等。"""
    plan = plan_store.get(plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail=f"plan_id 不存在：{plan_id}")

    bgm = plan.bgm.model_copy(deep=True) if plan.bgm else BGMConfig()
    patch = body.model_dump(exclude_unset=True)

    if "bgm_asset_id" in patch:
        new_id = (patch["bgm_asset_id"] or "").strip() or None
        if new_id is None:
            # 清空 BGM
            bgm = BGMConfig()
        else:
            # 替换 BGM：重新从 asset 拉 duration/peak，并重置 anchor + 重跑 LLM 分析
            bgm = _build_bgm_config(new_id)
            bgm = await _attach_bgm_llm_analysis(
                bgm, brief=plan.brief or "", video_goal=plan.video_goal or "",
            )

    for field in ("video_anchor_seconds", "volume", "fade_in", "fade_out", "duck_with_voice"):
        if field in patch and patch[field] is not None:
            setattr(bgm, field, patch[field])

    plan.bgm = bgm
    # BGM 切换 / 锚点改变 → 情绪曲线过期，自动重算（失败回 None 不阻塞）
    if "bgm_asset_id" in patch:
        plan.emotion_curve = await _compute_plan_emotion(plan)
        # 换 BGM 后 anchor 已被 _build_bgm_config 重置为 0，安全地按高潮重新对齐；
        # 若同一 PATCH 又显式设了 video_anchor_seconds（手拖），用户意图覆盖自动对齐
        if "video_anchor_seconds" not in patch or patch.get("video_anchor_seconds") is None:
            _auto_align_bgm_to_emotion(plan)
    plan_store.put(plan)
    log.info(
        "[plan] bgm patched plan=%s asset=%s anchor=%.2fs vol=%.2f duck=%s",
        plan_id, bgm.bgm_asset_id, bgm.video_anchor_seconds, bgm.volume, bgm.duck_with_voice,
    )
    return plan


@router.delete("/plan/{plan_id}/bgm", response_model=Plan)
async def delete_plan_bgm(plan_id: str) -> Plan:
    """清空 plan 的 BGM 引用（保留资产库里的文件本身）。"""
    plan = plan_store.get(plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail=f"plan_id 不存在：{plan_id}")
    plan.bgm = BGMConfig()
    plan_store.put(plan)
    log.info("[plan] bgm cleared plan=%s", plan_id)
    return plan


@router.post("/plan/{plan_id}/recompute-emotion", response_model=Plan)
async def recompute_emotion(plan_id: str) -> Plan:
    """手动重算情绪曲线——前端 EmotionCurveCard 的 ↻ 重算按钮。

    BGM 切换走 PATCH /plan/{id}/bgm 自动重算；本接口用于：
    - main_track 编辑后用户主动刷新
    - migration_preference 切到 amp_emotion 后想立即看到曲线整体抬高
    """
    plan = plan_store.get(plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail=f"plan_id 不存在：{plan_id}")
    plan.emotion_curve = await _compute_plan_emotion(plan)
    plan_store.put(plan)
    log.info(
        "[plan] emotion 手动重算 plan=%s backend=%s",
        plan_id, plan.emotion_curve.backend if plan.emotion_curve else "-",
    )
    return plan


@router.post("/plan/{plan_id}/refresh-fit-scores", response_model=Plan)
async def refresh_fit_scores(plan_id: str) -> Plan:
    """手动重算所有 user_material scene 的素材-段落 适配度评分（stage-59）。

    plan.build / scene.swap-source 都已自动调一次；本接口给用户在 Compose
    手改了 section.theme / content_description / scene.duration 后想刷新评分用。
    """
    plan = plan_store.get(plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail=f"plan_id 不存在：{plan_id}")
    proj_id = plan.project_id
    if not proj_id:
        # 无 project 的老 plan 也允许跑——但 materials_by_id 必为空，全部 scene 会被清空 fit
        mats_by_id: dict[str, Material] = {}
    else:
        mats_by_id = {m.material_id: m for m in material_store.list(proj_id)}
    from ..services.materials.fit import annotate_plan_fit_scores
    written = annotate_plan_fit_scores(
        main_track=plan.main_track,
        adapted_sections=plan.adapted_sections or [],
        materials_by_id=mats_by_id,
    )
    plan_store.put(plan)
    log.info("[plan] refresh_fit_scores plan=%s scenes=%d", plan_id, written)
    return plan


class PlanSettingsPatch(BaseModel):
    """PATCH /plan/{plan_id}/settings：在轨道板等位置直接翻转单个设置项。

    所有字段可选；只更新前端实际传入的键（`exclude_unset`），未传字段保持现值。
    主要用法：字幕轨/口播开关翻转 subtitle_enabled / voiceover_enabled、
    Compose 设置面板切换 tts_voice。
    """
    subtitle_enabled: Optional[bool] = None
    voiceover_enabled: Optional[bool] = None
    tts_voice: Optional[TTSVoice] = None
    target_platform: Optional[TargetPlatform] = None
    aspect_ratio: Optional[AspectRatio] = None
    tone: Optional[ToneStyle] = None
    cta: Optional[str] = Field(default=None, max_length=20)
    keywords: Optional[list[str]] = Field(default=None, max_length=5)
    target_duration_seconds: Optional[float] = Field(default=None, ge=10.0, le=120.0)


@router.patch("/plan/{plan_id}/settings", response_model=Plan)
async def patch_plan_settings(plan_id: str, body: PlanSettingsPatch) -> Plan:
    """部分更新 plan.settings；不重跑 LLM，仅落盘 + 返回最新 Plan。

    不触发结构重排——voiceover_enabled 由 voice/render 阶段读取生效，
    subtitle_enabled 由 字幕轨展示 / burn_packaging_track 读取生效，
    tts_voice 由 /voice/synthesize 阶段读取。前端切换后页面用返回的 Plan 同步 store。
    """
    plan = plan_store.get(plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail=f"plan_id 不存在：{plan_id}")
    patch = body.model_dump(exclude_unset=True)
    if not patch:
        return plan
    current = plan.settings.model_dump()
    current.update(patch)
    plan.settings = ComposeSettings(**current)
    # 字幕开关翻转：False → 清空字幕项；True → 按当前 narration / 时间窗 重建字幕项。
    # 用户痛报：开关切到 True 时不自动建 subtitle，预览看不到字幕——只能依赖 PackagingPanel
    # 二次点击。这里直接重建，让"打开 = 立刻看见"成为默认行为。
    if "subtitle_enabled" in patch:
        before = len(plan.packaging_track)
        _rebuild_subtitle_packaging(plan)
        log.info("[plan] settings subtitle=%s → 字幕项 %d→%d",
                 patch["subtitle_enabled"], before, len(plan.packaging_track))
    plan_store.put(plan)
    log.info("[plan] settings patched plan=%s keys=%s", plan_id, list(patch.keys()))
    return plan


class RegenerateNarrationsResponse(BaseModel):
    """POST /plan/{plan_id}/regenerate-narrations 返回。"""
    plan: Plan
    updated_scene_ids: list[str]
    skipped_scene_ids: list[str] = Field(default_factory=list)
    note: str = ""


@router.post("/plan/{plan_id}/regenerate-narrations", response_model=RegenerateNarrationsResponse)
async def regenerate_plan_narrations(plan_id: str) -> RegenerateNarrationsResponse:
    """step3 入口调：综合段长+内容直接给出每段口播，禁止复述凑时长。

    设计意图：
    - plan_agent 在 step1/step2 给的 narration 是"还没定稿时的估算"——段长会随用户调整而变。
    - 进 step3 之前段长已稳，需要按"每秒 5 字"的预算重新出一份**严丝合缝**的口播。
    - LLM 失败时不抹掉旧文案（避免回退灾难），仅记 skipped。
    - 不在这里调 TTS——前端拿到新 narration 后再触发 /voice/synthesize-all（如果开了 voiceover）。
    """
    plan = plan_store.get(plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail=f"plan_id 不存在：{plan_id}")

    from ..services.agent.narration_agent import regenerate_narrations

    new_narrations = await regenerate_narrations(plan)
    if not new_narrations:
        return RegenerateNarrationsResponse(
            plan=plan,
            updated_scene_ids=[],
            skipped_scene_ids=[s.scene_id for s in plan.main_track],
            note="LLM 暂不可用 / 返回不合法；保留旧 narration",
        )

    updated: list[str] = []
    skipped: list[str] = []
    for i, sc in enumerate(plan.main_track):
        new_text = new_narrations.get(sc.scene_id)
        if new_text is None:
            skipped.append(sc.scene_id)
            continue
        # 同步清空已合成的 voiceover_url：文案变了，旧 wav 已失效，强制重新合成
        plan.main_track[i] = sc.model_copy(update={
            "narration": new_text,
            "voiceover_url": None,
        })
        updated.append(sc.scene_id)

    # 字幕轨同步：批量改完口播后，所有 PackagingItem(subtitle) 必须按新 narration 重建
    if updated:
        _rebuild_subtitle_packaging(plan)

    plan_store.put(plan)
    log.info(
        "[plan] regenerate narrations plan=%s updated=%d skipped=%d",
        plan_id, len(updated), len(skipped),
    )
    return RegenerateNarrationsResponse(
        plan=plan,
        updated_scene_ids=updated,
        skipped_scene_ids=skipped,
        note=f"已重写 {len(updated)} 段口播",
    )


class SceneEditPatch(BaseModel):
    """PATCH /plan/{plan_id}/scene/{scene_id}：用户在四轨板"内容轨"上直接编辑段落内容。

    所有字段可选：
    - narration：改 Scene.narration（口播文案；后续合成 TTS 走的就是这一行）
    - theme / content_description：改对应 AdaptedSection（结构层），后续重排或 LLM 复用以此为锚定

    注意：不重跑 plan/build——只是把用户的手改落盘。改完通常紧跟着 /gap/fill 让用户再补补缺。
    """
    narration: Optional[str] = Field(default=None, max_length=2000)
    theme: Optional[str] = Field(default=None, max_length=80)
    content_description: Optional[str] = Field(default=None, max_length=400)


@router.patch("/plan/{plan_id}/scene/{scene_id}", response_model=Plan)
async def patch_plan_scene(plan_id: str, scene_id: str, body: SceneEditPatch) -> Plan:
    """更新 plan 中某 scene 的可编辑文本字段 + 同步对应 AdaptedSection 的 theme/content_description。

    AdaptedSection ↔ Scene 的关联：scene_id 形如 `sc-<order>`，order 与 section.order 对齐。
    用户在内容轨上看到的"段标题/段描述"就是 AdaptedSection 那两个字段，所以联动改。
    narration 是 Scene 自己的字段，只在 Scene 上改。
    """
    plan = plan_store.get(plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail=f"plan_id 不存在：{plan_id}")

    patch = body.model_dump(exclude_unset=True)
    if not patch:
        return plan

    scene_idx = next((i for i, s in enumerate(plan.main_track) if s.scene_id == scene_id), None)
    if scene_idx is None:
        raise HTTPException(status_code=404, detail=f"scene_id 不存在：{scene_id}")
    scene = plan.main_track[scene_idx]

    # 通过 scene_id 推断 section.order；老数据可能不是 sc-<order> 形式，回落到 scene_idx
    section_order: Optional[int] = None
    if scene_id.startswith("sc-"):
        try:
            section_order = int(scene_id.split("-", 1)[1])
        except ValueError:
            section_order = None
    if section_order is None:
        section_order = scene_idx

    section_idx = next(
        (i for i, sec in enumerate(plan.adapted_sections) if sec.order == section_order),
        None,
    )

    # Trace B 用：在 mutation 之前先抓 before 快照（Scene 是 immutable，model_copy 不会改原引用，
    # 但 section_idx 处会被原地替换，所以这里必须先拍）
    _before = {
        "narration": scene.narration or "",
        "theme": plan.adapted_sections[section_idx].theme if section_idx is not None else "",
        "content_description": plan.adapted_sections[section_idx].content_description if section_idx is not None else "",
    }

    if "narration" in patch:
        # narration 改了 → 旧 voiceover_url 指向的 wav 是旧文案合成的，必须废弃；
        # step3 PlanPlayer 才不会播放对不上嘴的旧音频。下次 /voice/synthesize 会按新 narration 重合。
        # stage-61：用户改文本视为对本镜的手动确认，user_edited 翻成 True。
        plan.main_track[scene_idx] = scene.model_copy(update={
            "narration": patch["narration"],
            "voiceover_url": None,
            "user_edited": True,
        })

    if section_idx is not None and any(k in patch for k in ("theme", "content_description")):
        sec = plan.adapted_sections[section_idx]
        update: dict[str, str] = {}
        if "theme" in patch:
            update["theme"] = patch["theme"]
        if "content_description" in patch:
            update["content_description"] = patch["content_description"]
        plan.adapted_sections[section_idx] = sec.model_copy(update=update)
        # stage-61：用户改了 section 描述，整段所有 scene 都视为人工已审过——
        # narration 没动也要落 user_edited=True（仅当本 scene 还没被前一分支翻过）。
        cur_sc = plan.main_track[scene_idx]
        if not cur_sc.user_edited:
            plan.main_track[scene_idx] = cur_sc.model_copy(update={"user_edited": True})

    # 字幕轨同步刷新：narration / duration 变更后，packaging_track 上的 subtitle 必须重生
    # 否则 step3 预览仍按旧 text + 旧时间窗 渲染，用户看到画面有字幕条但内容对不上
    if "narration" in patch:
        _rebuild_subtitle_packaging(plan)

    plan_store.put(plan)
    log.info(
        "[plan] scene patched plan=%s scene=%s keys=%s",
        plan_id, scene_id, list(patch.keys()),
    )
    # Trace B：自然语言编辑事件——只有用户真的在 narration / theme / content_description 上写了字才记。
    # 失败仅 warn 不影响 plan 持久化。
    try:
        user_input_parts = [v for v in (
            patch.get("narration"), patch.get("theme"), patch.get("content_description"),
        ) if v]
        user_input = " | ".join(user_input_parts).strip()
        if user_input:
            from ..services.profile import DEFAULT_USER_ID, TraceB, append_trace_b
            import time as _time
            scene_after = plan.main_track[scene_idx]
            sec_after = plan.adapted_sections[section_idx] if section_idx is not None else None
            trace = TraceB(
                ts=int(_time.time()),
                project_id=plan.project_id or "__legacy",
                plan_id=plan.plan_id,
                user_id=DEFAULT_USER_ID,
                context="scene_edit",
                scene_id=scene_id,
                section_role=scene_after.section,
                user_input=user_input,
                before=_before,
                after={
                    "narration": scene_after.narration or "",
                    "theme": sec_after.theme if sec_after else "",
                    "content_description": sec_after.content_description if sec_after else "",
                },
            )
            append_trace_b(DEFAULT_USER_ID, trace)
    except Exception as exc:  # noqa: BLE001
        log.warning("[plan] profile.trace_b (scene_edit) write failed: %s", exc)
    return plan


class SceneTransitionPatch(BaseModel):
    """PATCH /plan/{plan_id}/scene/{scene_id}/transition：更新某分镜的入场转场样式。

    Scene.transition_in 表示与上一段的衔接方式；sc-0 永远忽略此字段。
    style=hard_cut 时直接清空 transition_in（concat demuxer 走硬切）；其余值写一条 SceneTransition。
    """
    style: TransitionStyle
    duration: Optional[float] = Field(default=None, ge=0.1, le=1.5, description="转场持续秒数；缺省走 SceneTransition 默认 0.4s")


@router.patch("/plan/{plan_id}/scene/{scene_id}/transition", response_model=Plan)
async def patch_scene_transition(plan_id: str, scene_id: str, body: SceneTransitionPatch) -> Plan:
    """更新某 scene 的 transition_in：用户在包装轨上点击转场节点后选了一个新样式。"""
    plan = plan_store.get(plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail=f"plan_id 不存在：{plan_id}")
    scene_idx = next((i for i, s in enumerate(plan.main_track) if s.scene_id == scene_id), None)
    if scene_idx is None:
        raise HTTPException(status_code=404, detail=f"scene_id 不存在：{scene_id}")
    if scene_idx == 0:
        raise HTTPException(status_code=400, detail="首个分镜不能设置入场转场")
    scene = plan.main_track[scene_idx]
    if body.style == "hard_cut":
        new_transition = None
    else:
        duration = body.duration if body.duration is not None else (
            scene.transition_in.duration if scene.transition_in else 0.4
        )
        new_transition = SceneTransition(style=body.style, duration=duration)
    plan.main_track[scene_idx] = scene.model_copy(update={"transition_in": new_transition})
    plan_store.put(plan)
    log.info(
        "[plan] scene transition patched plan=%s scene=%s style=%s dur=%.2f",
        plan_id, scene_id, body.style, (new_transition.duration if new_transition else 0.0),
    )
    return plan


class ShotSubjectPatch(BaseModel):
    """PATCH /plan/{plan_id}/scene/{scene_id}/shot-subject：用户编辑分镜「对象」字段。

    会同步写两处：
    - Scene.shot_subject（直接显示 / 文字卡兜底用）
    - 父 AdaptedSection.shots[shot_order].subject（plan_agent / aigc_prompt_agent 读这里）

    禁比喻、上位词——前端 placeholder 已提示用户写具象名词，后端只做长度校验，不做语义判断。
    """
    subject: str = Field(default="", max_length=40)


@router.patch("/plan/{plan_id}/scene/{scene_id}/shot-subject", response_model=Plan)
async def patch_shot_subject(plan_id: str, scene_id: str, body: ShotSubjectPatch) -> Plan:
    """单镜「对象/主体」编辑：双写 Scene.shot_subject + parent_section.shots[order].subject。

    设计动机：用户在 Compose 分镜清单里发现 LLM 给的 subject 是比喻词（『国宝碎片』）→
    生图阶段被同义化成『新品潮酷碎片』，需要直接改成具象名词（『青铜器残片』）。
    改完下游 aigc_prompt_agent 重读 plan，subject 锚点立即生效，下次 swap-source 出图就准了。
    """
    plan = plan_store.get(plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail=f"plan_id 不存在：{plan_id}")
    scene_idx = next((i for i, s in enumerate(plan.main_track) if s.scene_id == scene_id), None)
    if scene_idx is None:
        raise HTTPException(status_code=404, detail=f"scene_id 不存在：{scene_id}")
    scene = plan.main_track[scene_idx]
    new_subject = (body.subject or "").strip()[:40]

    plan.main_track[scene_idx] = scene.model_copy(update={
        "shot_subject": new_subject,
        "user_edited": True,
    })

    # 同步父 AdaptedSection.shots[order].subject——下游 aigc_prompt_agent 读这里
    if scene.parent_section_id:
        sec_idx = next(
            (i for i, sec in enumerate(plan.adapted_sections) if sec.section_id == scene.parent_section_id),
            None,
        )
        if sec_idx is not None:
            sec = plan.adapted_sections[sec_idx]
            if sec.shots:
                shot_idx = next(
                    (i for i, sh in enumerate(sec.shots) if sh.order == scene.shot_order),
                    None,
                )
                if shot_idx is not None:
                    new_shots = list(sec.shots)
                    new_shots[shot_idx] = sec.shots[shot_idx].model_copy(update={"subject": new_subject})
                    plan.adapted_sections[sec_idx] = sec.model_copy(update={"shots": new_shots})

    plan_store.put(plan)
    log.info(
        "[plan] shot subject patched plan=%s scene=%s subject=%r",
        plan_id, scene_id, new_subject[:32],
    )
    return plan


# ---------------------------------------------------------------------------
# stage-37：单镜（Scene + 父 ShotPlan）多字段编辑——弹窗用
# ---------------------------------------------------------------------------

class ShotFieldsPatch(BaseModel):
    """PATCH /plan/{plan_id}/scene/{scene_id}/shot-fields：弹窗里改单镜的多个字段。

    - subject：双写 Scene.shot_subject + ShotPlan.subject
    - visual：只写父 ShotPlan.visual（Scene 上没这字段；aigc_prompt_agent / 字卡兜底读 ShotPlan）
    - narration：双写 Scene.narration + ShotPlan.narration（口播 / 字幕都跟着）

    duration_seconds 故意不在这条 patch 里——改时长要重排下游所有 Scene.start，
    放到独立路由 / Plan rebuild 流程里更安全。
    """

    subject: Optional[str] = Field(default=None, max_length=40)
    visual: Optional[str] = Field(default=None, max_length=200)
    narration: Optional[str] = Field(default=None, max_length=200)
    camera_technique: Optional[str] = Field(default=None, max_length=80)


@router.patch("/plan/{plan_id}/scene/{scene_id}/shot-fields", response_model=Plan)
async def patch_shot_fields(plan_id: str, scene_id: str, body: ShotFieldsPatch) -> Plan:
    """单镜级多字段编辑：写 Scene 上的 shot_subject / narration，同时同步父 ShotPlan
    上的 subject / visual / narration（aigc_prompt_agent 重读 plan 时立即生效）。

    与现有 patch_shot_subject 的区别：那个只改 subject；这个一次性提交弹窗里所有改动。
    """
    plan = plan_store.get(plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail=f"plan_id 不存在：{plan_id}")
    patch = body.model_dump(exclude_unset=True)
    if not patch:
        return plan

    scene_idx = next((i for i, s in enumerate(plan.main_track) if s.scene_id == scene_id), None)
    if scene_idx is None:
        raise HTTPException(status_code=404, detail=f"scene_id 不存在：{scene_id}")
    scene = plan.main_track[scene_idx]

    scene_update: dict[str, Any] = {}
    if "subject" in patch:
        scene_update["shot_subject"] = (patch["subject"] or "").strip()[:40]
    if "narration" in patch:
        # Scene.narration 可空——纯画面镜头允许无口播
        new_narr = (patch["narration"] or "").strip()
        scene_update["narration"] = new_narr or None
        # narration 改了 → 旧 voiceover_url 指向的 wav 是旧文案合成的，必须废弃；
        # 不清掉 step3 PlanPlayer 会用旧音频对不上新字幕，用户报『改了文案 step3 没刷』就是这个。
        scene_update["voiceover_url"] = None
    # stage-61：弹窗里改任何字段 → 整个 scene 视为人工已审过；step2 缺口检查不再把它当作待补
    scene_update["user_edited"] = True
    if scene_update:
        plan.main_track[scene_idx] = scene.model_copy(update=scene_update)

    # 同步父 AdaptedSection.shots[order] —— aigc_prompt_agent / Seedance 重新读 plan 时拿到的就是这里
    if scene.parent_section_id:
        sec_idx = next(
            (i for i, sec in enumerate(plan.adapted_sections) if sec.section_id == scene.parent_section_id),
            None,
        )
        if sec_idx is not None:
            sec = plan.adapted_sections[sec_idx]
            if sec.shots:
                shot_idx = next(
                    (i for i, sh in enumerate(sec.shots) if sh.order == scene.shot_order),
                    None,
                )
                if shot_idx is not None:
                    shot_update: dict[str, Any] = {}
                    if "subject" in patch:
                        shot_update["subject"] = (patch["subject"] or "").strip()[:40]
                    if "visual" in patch:
                        shot_update["visual"] = (patch["visual"] or "").strip()[:200]
                    if "narration" in patch:
                        shot_update["narration"] = (patch["narration"] or "").strip()[:200]
                    if "camera_technique" in patch:
                        shot_update["camera_technique"] = (patch["camera_technique"] or "").strip()[:80]
                    if shot_update:
                        new_shots = list(sec.shots)
                        new_shots[shot_idx] = sec.shots[shot_idx].model_copy(update=shot_update)
                        plan.adapted_sections[sec_idx] = sec.model_copy(update={"shots": new_shots})

    # narration 改了 → step3 字幕轨同步重建（否则预览/渲染都还按旧 text）
    if "narration" in patch:
        _rebuild_subtitle_packaging(plan)

    plan_store.put(plan)
    log.info(
        "[plan] shot fields patched plan=%s scene=%s keys=%s",
        plan_id, scene_id, list(patch.keys()),
    )
    return plan


@router.get("/plan", response_model=list[Plan])
async def list_plans(project_id: str) -> list[Plan]:
    """按 project_id 列出该项目所有 plans。前端进 Compose 时根据 step snapshot
    拿单个 plan_id；用本接口可在调试/历史回看时拉全量。"""
    return plan_store.list_by_project(project_id)


# ---------------------------------------------------------------------------
# stage-26 PR-N.4：单镜（Scene）换源
# ---------------------------------------------------------------------------

class SceneSwapSourceRequest(BaseModel):
    """POST /plan/{plan_id}/scene/{scene_id}/swap-source 入参。

    把单个 Scene 的 source 切到指定类型，让用户精修『匹配不上的某一镜』而不必整段换源。
    每种 source 走对应的同步生成路径——返回时 Scene 已就绪可立即预览。
    """
    source: Literal["user_material", "aigc_image", "aigc_t2v", "text_card"] = Field(
        ..., description="目标 source 类型"
    )
    material_id: Optional[str] = Field(default=None, description="source=user_material 必填")
    material_shot_index: Optional[int] = Field(
        default=None, description="source=user_material 时指定 MaterialShot.index；缺省走首镜"
    )
    material_in_point: Optional[float] = Field(
        default=None, ge=0,
        description="source=user_material 时手动裁剪起点（秒）；与 material_shot_index 互斥，"
        "out > in 且需 ≥ 0.5s。给了 in+out 就走手动裁剪路径，scene.duration 跟随用户。",
    )
    material_out_point: Optional[float] = Field(
        default=None, gt=0,
        description="source=user_material 时手动裁剪终点（秒）；out>in，自动 clamp 到素材时长。",
    )
    prompt_hint: Optional[str] = Field(
        default=None, max_length=200,
        description="source=aigc_image / aigc_t2v 时给 LLM 的额外提示；缺省走 shot.subject + visual",
    )
    main_text: Optional[str] = Field(
        default=None, max_length=24,
        description="source=text_card 时主文案；缺省走 shot.subject",
    )
    sub_text: Optional[str] = Field(default=None, max_length=40, description="source=text_card 时副文案")


@router.post("/plan/{plan_id}/scene/{scene_id}/swap-source", response_model=Plan)
async def swap_scene_source(
    plan_id: str, scene_id: str, body: SceneSwapSourceRequest
) -> Plan:
    """单镜换源：把某 Scene 的 source 切到 user_material / aigc_image / aigc_t2v / text_card。

    与 PATCH /scene/{id} 区别：那是改文本字段（narration/theme/content_description），
    本接口是改素材来源——会同步调 Seedream / Seedance / 切素材入出点 / 装 TextCardSpec，
    返回 plan 时该 Scene 已就绪。

    成功后清掉 needs_fill。失败抛 502 / 500，plan 不变。
    """
    plan = plan_store.get(plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail=f"plan_id 不存在：{plan_id}")
    scene_idx = next((i for i, s in enumerate(plan.main_track) if s.scene_id == scene_id), None)
    if scene_idx is None:
        raise HTTPException(status_code=404, detail=f"scene_id 不存在：{scene_id}")
    scene = plan.main_track[scene_idx]

    # 反查归属 AdaptedSection / ShotPlan，给 aigc 路径喂上下文
    section: Optional[AdaptedSection] = None
    shot_plan = None
    if scene.parent_section_id:
        section = next(
            (sec for sec in plan.adapted_sections if sec.section_id == scene.parent_section_id),
            None,
        )
        if section is not None and section.shots:
            shot_plan = next(
                (sh for sh in section.shots if sh.order == scene.shot_order),
                None,
            )

    shot_dur = scene.duration

    if body.source == "text_card":
        # 直接装 TextCardSpec，不调外部
        main_t = (body.main_text or scene.shot_subject or (scene.narration or "").split("。")[0] or "")[:24]
        if not main_t and section is not None:
            main_t = (section.theme or section.role)[:24]
        sub_t = (body.sub_text or "")[:40]
        spec = TextCardSpec(
            main_text=main_t or "（待补全）",
            sub_text=sub_t,
            duration_seconds=round(max(1.5, min(15.0, shot_dur)), 2),
        )
        new_scene = scene.model_copy(update={
            "source": "text_card",
            "source_ref": f"text-card-swap-{scene_id}",
            "in_point": 0.0,
            "out_point": None,
            "aigc_video_urls": [],
            "aigc_image_url": None,
            "text_card_spec": spec,
            "animation_spec": None,
            "needs_fill": False,
            "user_edited": True,
        })
    elif body.source == "user_material":
        if not body.material_id:
            raise HTTPException(status_code=400, detail="source=user_material 必须传 material_id")
        proj_id = plan.project_id
        if not proj_id:
            raise HTTPException(status_code=400, detail="plan 缺少 project_id，无法定位用户素材")
        mat = material_store.get(proj_id, body.material_id)
        if mat is None:
            raise HTTPException(status_code=404, detail=f"material_id 不存在：{body.material_id}")
        # 优先级 1：用户手动裁剪（in + out 都给）→ 分镜时长跟随用户
        if body.material_in_point is not None and body.material_out_point is not None:
            in_pt = float(body.material_in_point)
            out_pt = float(body.material_out_point)
            mat_dur = float(mat.duration_seconds or 0.0)
            if mat_dur > 0:
                in_pt = max(0.0, min(in_pt, mat_dur))
                out_pt = max(in_pt + 0.5, min(out_pt, mat_dur))
            if out_pt - in_pt < 0.5:
                raise HTTPException(status_code=400, detail="裁剪窗口太短，至少 0.5s")
            new_dur = round(out_pt - in_pt, 3)
        # 优先级 2：传了 material_shot_index，按 PySceneDetect 切片取窗口
        elif mat.shots:
            target_idx = body.material_shot_index
            mshot = None
            if target_idx is not None:
                mshot = next((ms for ms in mat.shots if ms.index == target_idx), None)
            if mshot is None:
                mshot = mat.shots[0]
            in_pt = float(mshot.start)
            out_pt = min(float(mshot.end), in_pt + shot_dur)
            # scene.duration 严格 = out_pt - in_pt，禁止渲染端 freeze/slow-mo 凑长度。
            new_dur = round(max(0.5, out_pt - in_pt), 3)
        # 优先级 3：素材没切镜，按 scene.duration 从头取
        else:
            in_pt = 0.0
            mat_dur = float(mat.duration_seconds or 0.0)
            if mat_dur > 0:
                out_pt = min(shot_dur, mat_dur)
            else:
                out_pt = shot_dur
            new_dur = round(max(0.5, out_pt - in_pt), 3)
        new_scene = scene.model_copy(update={
            "source": "user_material",
            "source_ref": mat.material_id,
            "in_point": in_pt,
            "out_point": out_pt,
            "duration": new_dur,
            "aigc_video_urls": [],
            "aigc_image_url": None,
            "text_card_spec": None,
            "animation_spec": None,
            "needs_fill": False,
            "user_edited": True,
        })
    elif body.source == "aigc_image":
        # 同步走 Seedream 单图：用 shot 主题 + 用户 hint
        from ..services.agent.aigc_prompt_agent import _fallback_prompt
        from ..services.agent.gap_agent import _persist_aigc_image, suggest_animation_spec_async
        from ..services.seedream_client import SeedreamError, get_seedream_client
        from ..schemas import Gap
        # 拼 prompt：优先用 shot.visual / subject，hint 拼到尾巴
        if shot_plan is not None:
            base_text = " ".join(s for s in (shot_plan.subject, shot_plan.visual) if s).strip()
        else:
            base_text = scene.shot_subject or (scene.narration or "")
        if body.prompt_hint:
            base_text = f"{base_text}（{body.prompt_hint}）" if base_text else body.prompt_hint
        if not base_text:
            base_text = section.content_description if section else f"短视频画面：{scene.section}"
        prompt = base_text[:200]
        ratio_pref = (plan.settings.aspect_ratio if plan.settings else None) or "9:16"
        try:
            seedream = get_seedream_client()
            images = await seedream.generate(prompt, ratio=ratio_pref, n=1, watermark=False)
        except SeedreamError as exc:
            raise HTTPException(status_code=502, detail=f"Seedream 出图失败：{exc}") from exc
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=502, detail=f"Seedream 出图异常：{exc}") from exc
        if not images:
            raise HTTPException(status_code=502, detail="Seedream 返回 0 张图")
        persisted = await _persist_aigc_image(images[0].url, scene_id)
        final_url = persisted or images[0].url
        # stage-43：单图换源同样要 Remotion 动效（否则 step3 渲染只看到静态贴图）。
        # stage-58：suggest_animation_spec_async 先调 LLM 直选运镜，失败回落到 camera_technique 字典。
        try:
            anim_spec = await suggest_animation_spec_async(section, 1, None)
        except Exception as exc:  # noqa: BLE001
            log.warning("[scene-swap] %s aigc_image 推荐动效失败 → 走 ken-burns 兜底：%s", scene_id, exc)
            anim_spec = AnimationSpec(
                engine="remotion", animation_type="ken-burns",
                motion_direction="in", intensity=0.35,
                transition="cross-fade", transition_duration=0.4,
            )
        new_scene = scene.model_copy(update={
            "source": "aigc_image",
            "source_ref": f"img-swap-{scene_id}",
            "in_point": 0.0,
            "out_point": None,
            "aigc_video_urls": [],
            "aigc_image_url": final_url,
            "text_card_spec": None,
            "animation_spec": anim_spec,
            "needs_fill": False,
            "user_edited": True,
        })
        log.info(
            "[scene-swap] %s → aigc_image url=%s prompt='%s' anim=%s/%s",
            scene_id, final_url[:80], prompt[:60],
            anim_spec.animation_type, anim_spec.motion_direction,
        )
    elif body.source == "aigc_t2v":
        # 同步走 Seedance 单段（per scene 时长 ≤ 12s 时 1 次提交即可，超过则切链；本接口不切链，直接最长 12s）
        from ..services.t2v_client import T2VError, get_t2v_client
        from ..services.agent.gap_agent import SEEDANCE_MAX_SECONDS, _persist_aigc_video
        if shot_plan is not None:
            base_text = " ".join(s for s in (shot_plan.subject, shot_plan.visual) if s).strip()
        else:
            base_text = scene.shot_subject or (scene.narration or "")
        if body.prompt_hint:
            base_text = f"{base_text}（{body.prompt_hint}）" if base_text else body.prompt_hint
        if not base_text:
            base_text = section.content_description if section else f"短视频画面：{scene.section}"
        prompt = base_text[:200]
        ratio_pref = (plan.settings.aspect_ratio if plan.settings else None) or "9:16"
        # Seedance 2.0 拒绝 duration<5；shot_dur 可能短于 5（用户裁剪到 2-3s），floor 上去
        per_chunk_seconds = int(round(min(float(SEEDANCE_MAX_SECONDS), max(5.0, shot_dur))))
        t2v = get_t2v_client()
        try:
            submit = await t2v.submit(
                prompt=prompt, duration_seconds=per_chunk_seconds, ratio=ratio_pref,
            )
        except T2VError as exc:
            raise HTTPException(status_code=502, detail=f"Seedance 提交失败：{exc}") from exc
        # 简化：循环 query 等待完成，超时拍 502
        import asyncio as _asyncio, time as _time
        deadline = _time.time() + 180.0
        video_url = ""
        while _time.time() < deadline:
            try:
                q = await t2v.query(submit.task_id)
            except T2VError as exc:
                raise HTTPException(status_code=502, detail=f"Seedance 轮询失败：{exc}") from exc
            if q.status == "succeeded" and q.video_url:
                video_url = q.video_url
                break
            if q.status in ("failed", "cancelled"):
                raise HTTPException(
                    status_code=502,
                    detail=f"Seedance 任务失败：{q.fail_reason or 'unknown'}",
                )
            await _asyncio.sleep(4.0)
        if not video_url:
            raise HTTPException(status_code=504, detail="Seedance 任务超时（>180s）")
        persisted = await _persist_aigc_video(video_url, scene_id)
        final_url = persisted or video_url
        new_scene = scene.model_copy(update={
            "source": "aigc_t2v",
            "source_ref": submit.task_id,
            "in_point": 0.0,
            "out_point": None,
            "aigc_video_urls": [final_url],
            "aigc_image_url": None,
            "text_card_spec": None,
            "animation_spec": None,
            "needs_fill": False,
            "user_edited": True,
        })
        log.info(
            "[scene-swap] %s → aigc_t2v url=%s prompt='%s'",
            scene_id, final_url[:80], prompt[:60],
        )
    else:
        raise HTTPException(status_code=400, detail=f"不支持的 source 类型：{body.source}")

    plan.main_track[scene_idx] = new_scene
    # 只有 scene.duration 真改了（手动裁剪路径）才重铺 timeline。其它换源（text_card / aigc /
    # 自动 shot）保留原 duration，跳过 _rebuild_timeline 避免清空全片字幕。
    duration_changed = abs(new_scene.duration - scene.duration) > 0.001
    if duration_changed:
        from ..services.agent.compose_edit_agent import _rebuild_timeline
        _rebuild_timeline(plan)
    # 字幕轨同步：swap 到 text_card 时该段不该再叠字幕；从 text_card 换回视频/AIGC 时
    # 又要把字幕加回来。统一走 _rebuild_subtitle_packaging（按 narration / text_card_spec
    # 重新判定每段是否出字幕），避免 source 变了但 subtitle 残留导致字幕错位。
    _rebuild_subtitle_packaging(plan)
    # stage-59：换源后给被改动的 scene 重算 fit_score（其它 scene 维持原值不动）
    try:
        from ..services.materials.fit import annotate_plan_fit_scores
        proj_id = plan.project_id
        if proj_id:
            mats_by_id = {m.material_id: m for m in material_store.list(proj_id)}
            annotate_plan_fit_scores(
                main_track=plan.main_track,
                adapted_sections=plan.adapted_sections or [],
                materials_by_id=mats_by_id,
            )
    except Exception as exc:  # noqa: BLE001
        log.warning("[scene-swap] fit_score 重算失败 plan=%s scene=%s: %s", plan_id, scene_id, exc)
    plan_store.put(plan)
    log.info(
        "[plan] scene source swapped plan=%s scene=%s %s → %s",
        plan_id, scene_id, scene.source, body.source,
    )
    return plan


# ---------------------------------------------------------------------------
# stage-77 切片适配度评分（2026-06-12）
# 用户原话：「在内容轨生成之后，基于不同分镜的内容要求，对每个真实素材切片
# 对每一个分镜的适配程度进行打分，在分镜编辑的切片选择界面展示分数」
#
# 复用 shot_matcher._score_pair：score = 0.55*bigram_jaccard + 0.20*role_match
# + 0.15*action_density_fit + 0.10*duration_fit，跟 build_plan 自动匹配同一份
# 评分函数——避免 UI 显示的分跟物化时挑切片的依据不一致。
# ---------------------------------------------------------------------------

class ShotFitScore(BaseModel):
    """单个 MaterialShot 对当前 Scene 的适配度。"""
    shot_index: int = Field(..., description="MaterialShot.index")
    score: float = Field(..., ge=0.0, le=1.0, description="0-1，越高越适配")
    score_pct: int = Field(..., ge=0, le=100, description="UI 显示用 0-100 整数")
    quality: Literal["good", "weak", "missing"] = Field(
        ..., description="good ≥ 0.30 / weak ≥ 0.10 / missing < 0.10",
    )


class ShotFitScoresResponse(BaseModel):
    """GET /plan/{plan_id}/scene/{scene_id}/material/{material_id}/shot-scores 返回。"""
    plan_id: str
    scene_id: str
    material_id: str
    section_role: str = Field(..., description="评分用到的段位 role")
    scene_shot_subject: str = Field(..., description="评分用到的分镜主体（来自 ShotPlan）")
    scene_duration: float = Field(..., description="评分用到的目标时长（秒）")
    scores: list[ShotFitScore]


def _quality_from_score(score: float) -> Literal["good", "weak", "missing"]:
    """与 shot_matcher.ShotMatch.quality 保持一致的三档分级。"""
    if score >= 0.30:
        return "good"
    if score >= 0.10:
        return "weak"
    return "missing"


@router.get(
    "/plan/{plan_id}/scene/{scene_id}/material/{material_id}/shot-scores",
    response_model=ShotFitScoresResponse,
)
async def get_material_shot_fit_scores(
    plan_id: str, scene_id: str, material_id: str
) -> ShotFitScoresResponse:
    """给当前 scene × 指定 material 的每个 shot 打适配度分。

    用于 step2 换源弹窗：用户选了一个 video material 后，前端拉这个接口在
    每个 shot 卡上显示分数 + 颜色标签，把"靠瞎猜"换成"靠数据挑"。

    评分跟 shot_matcher 用的是同一个 `_score_pair`，避免 UI 跟物化层用两套尺。
    """
    plan = plan_store.get(plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail=f"plan_id 不存在：{plan_id}")
    scene = next((s for s in plan.main_track if s.scene_id == scene_id), None)
    if scene is None:
        raise HTTPException(status_code=404, detail=f"scene_id 不在 plan 中：{scene_id}")

    # 在 plan.adapted_sections 里找当前 scene 所属 section + ShotPlan
    sec_by_id = {s.section_id: s for s in (plan.adapted_sections or [])}
    section = sec_by_id.get(scene.parent_section_id or "")
    if section is None:
        # 老 plan 没 parent_section_id 时按 role fallback
        section = next(
            (s for s in (plan.adapted_sections or []) if s.role == scene.section),
            None,
        )
    if section is None:
        raise HTTPException(
            status_code=400,
            detail=f"scene {scene_id} 没法回查 AdaptedSection（plan.adapted_sections 空或不一致）",
        )

    # ShotPlan：按 scene.shot_order 在 section.shots 里取；缺时合成一个最小占位（
    # subject 用 scene.shot_subject，visual=narration，duration=scene.duration）让 _score_pair
    # 仍可跑——比 400 友好，且这种情况只在老 plan 上出现。
    from ..schemas import ShotPlan
    plan_shot: Optional[ShotPlan] = None
    if section.shots:
        plan_shot = next(
            (sh for sh in section.shots if sh.order == scene.shot_order),
            None,
        )
        if plan_shot is None:
            plan_shot = section.shots[0]
    if plan_shot is None:
        plan_shot = ShotPlan(
            order=scene.shot_order,
            subject=scene.shot_subject or section.theme or section.role,
            visual=scene.narration or section.content_description or "",
            narration=scene.narration or "",
            duration_seconds=float(scene.duration or 0.0),
        )

    # 取 material —— 必须属于本 plan 的 project；防跨项目泄漏
    proj_id = plan.project_id
    if not proj_id:
        raise HTTPException(status_code=400, detail="plan 没绑 project，无法定位素材")
    mats = material_store.list(proj_id)
    mat = next((m for m in mats if m.material_id == material_id), None)
    if mat is None:
        raise HTTPException(
            status_code=404,
            detail=f"material_id 不在本项目素材库：{material_id}",
        )

    from ..services.agent.shot_matcher import _score_pair
    scores: list[ShotFitScore] = []
    if mat.media_type == "video" and mat.shots:
        for ms in mat.shots:
            s = _score_pair(plan_shot, ms, section.role)
            scores.append(ShotFitScore(
                shot_index=ms.index,
                score=round(s, 4),
                score_pct=int(round(s * 100)),
                quality=_quality_from_score(s),
            ))
    elif mat.media_type == "image":
        # 图片素材合成虚拟 shot 给一个分（与 match_section_shots 同口径）
        virt_dur = max(1.0, float(mat.duration_seconds or 3.0))
        virt_cap = (mat.highlight_reason or "").strip() or " ".join(
            [*(mat.subjects or []), *(mat.tags or [])[:4]]
        ).strip() or mat.filename
        virt_shot = MaterialShot(
            index=0,
            start=0.0,
            end=virt_dur,
            duration=virt_dur,
            caption=virt_cap or None,
            action_density=0.5,
            recommended_role=mat.recommended_section,
        )
        s = _score_pair(plan_shot, virt_shot, section.role)
        scores.append(ShotFitScore(
            shot_index=0,
            score=round(s, 4),
            score_pct=int(round(s * 100)),
            quality=_quality_from_score(s),
        ))
    # video 没切镜 / audio 类 → 返回空 scores（前端只在长度 > 0 时显示徽章）

    return ShotFitScoresResponse(
        plan_id=plan_id,
        scene_id=scene_id,
        material_id=material_id,
        section_role=section.role,
        scene_shot_subject=plan_shot.subject or "",
        scene_duration=float(scene.duration or 0.0),
        scores=scores,
    )



# ---------------------------------------------------------------------------
# Plan 命名快照（撤销栈以外的、用户主动保存的版本点）
# ---------------------------------------------------------------------------

@router.post("/plan/{plan_id}/snapshot", response_model=PlanSnapshotMeta)
async def create_plan_snapshot(plan_id: str, body: PlanSnapshotCreateRequest) -> PlanSnapshotMeta:
    """保存当前 plan 为一条命名快照；返回 meta（不含 plan 体）。"""
    plan = plan_store.get(plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail=f"plan_id 不存在：{plan_id}")
    meta = plan_snapshot_store.create(plan, name=body.name, user_id=None)
    return PlanSnapshotMeta(**meta)


@router.get("/plan/{plan_id}/snapshots", response_model=list[PlanSnapshotMeta])
async def list_plan_snapshots(plan_id: str) -> list[PlanSnapshotMeta]:
    plan = plan_store.get(plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail=f"plan_id 不存在：{plan_id}")
    items = plan_snapshot_store.list(plan_id, project_id=plan.project_id)
    return [PlanSnapshotMeta(**it) for it in items]


@router.post("/plan/{plan_id}/snapshot/{snapshot_id}/restore", response_model=Plan)
async def restore_plan_snapshot(plan_id: str, snapshot_id: str) -> Plan:
    """把快照里的 Plan 写回 plan_store——同 plan_id 覆盖。前端拿到 Plan 后自行 push 撤销栈。"""
    plan = plan_store.get(plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail=f"plan_id 不存在：{plan_id}")
    record = plan_snapshot_store.get(plan_id, snapshot_id, project_id=plan.project_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"snapshot 不存在：{snapshot_id}")
    snap_plan = Plan.model_validate(record["plan"])
    plan_store.replace(snap_plan)
    return snap_plan


@router.delete("/plan/{plan_id}/snapshot/{snapshot_id}")
async def delete_plan_snapshot(plan_id: str, snapshot_id: str) -> dict:
    plan = plan_store.get(plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail=f"plan_id 不存在：{plan_id}")
    ok = plan_snapshot_store.delete(plan_id, snapshot_id, project_id=plan.project_id)
    if not ok:
        raise HTTPException(status_code=404, detail=f"snapshot 不存在：{snapshot_id}")
    return {"ok": True}
