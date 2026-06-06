"""Module 5 — Plan 组装。

`POST /api/plan/build`：
1. 反查样例 manifest（优先真预解析 `_load_real_manifest`，回落 `_stub_manifest`）
2. 走 `plan_agent.adapt_structure`，基于 brief + video_goal + settings 把样例骨架改编为 AdaptedSection[]
3. 按 AdaptedSection 一段对应一个 Scene 拼主轨——长度由 LLM 给的 duration_seconds 决定
4. 持久化 Plan（含 adapted_sections + video_goal + settings），供 /gap/detect、/render、/edit 复用
"""
from __future__ import annotations

import logging
import uuid
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..routers.library import _LIBRARY, _load_real_manifest, _stub_manifest
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
    TargetPlatform,
    TextCardSpec,
    ToneStyle,
    TTSVoice,
)
from ..services.agent.plan_agent import adapt_structure
from ..services.assets import asset_store
from ..services.library import manifest_store
from ..services.materials import gap_store, material_store
from ..services.plans import plan_snapshot_store, plan_store
from ..services.projects import project_store
from ..services.video.bgm_analysis import analyze_bgm_with_llm

log = logging.getLogger("seecript.plan")
router = APIRouter()


def _resolve_manifest(sample_id: str) -> SampleManifest:
    """先尝试真预解析 manifest，没有则回落 stub。"""
    real = _load_real_manifest(sample_id)
    if real is not None:
        return real
    sample = next((s for s in _LIBRARY if s.id == sample_id), _LIBRARY[0])
    return _stub_manifest(sample.id, sample)


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


def _fill_section_lookup(fills: list[FillResult]) -> dict[str, FillResult]:
    """把 FillResult 按其所属 section_id 索引——多段同 role 时不再被压扁。

    路由优先级：
    1. `fill.section_id` 直接给的 —— v2 后 fill_gap 在所有路径都回填，最权威，不依赖进程内存
    2. `gap_store.get(f.gap_id).section_id` —— 兼容老 fill（无 section_id 字段）+ gap 仍在内存
    3. 都没有 → 丢弃 + warn 日志（提示后端可能重启 / fill 来自旧版本）
    """
    out: dict[str, FillResult] = {}
    dropped: list[str] = []
    for f in fills:
        if not f.new_material_id and not f.video_urls and not f.narration:
            continue
        sid = f.section_id
        if not sid:
            gap = gap_store.get(f.gap_id)
            sid = gap.section_id if (gap and gap.section_id) else None
        if sid is None:
            dropped.append(f.gap_id)
            continue
        out.setdefault(sid, f)
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
        adapted = await adapt_structure(
            manifests, req.brief, req.video_goal, settings,
            reference_asset_ids=req.reference_asset_ids,
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

    # 3. 把 fills 按 section_id 索引——这是修复『多段 development 全被路由到第一段』bug 的关键
    fill_by_section = _fill_section_lookup(req.fills)
    material_cursor = 0

    def _pick(sec: AdaptedSection) -> tuple[str, str, list[str], str | None, str | None, "TextCardSpec | None", str | None, list[str], "AnimationSpec | None"]:
        """返回 (source, source_ref, aigc_video_urls, narration_override, voiceover_url, text_card_spec, aigc_image_url, aigc_image_urls, animation_spec)。

        优先级：本段 fill（aigc / aigc_image / copy） > 用户素材 > 文字卡兜底。
        - aigc fill：source=aigc_t2v，source_ref=task_id，aigc_video_urls=video_urls
        - aigc_image fill：source=aigc_image，source_ref=new_material_id，aigc_image_url=fill.aigc_image_url
                          多镜头时 aigc_image_urls = fill.aigc_image_urls（path B 拆分用）
                          animation_spec：若 fill 携带则透传，决定 Scene 走 remotion 渲染还是 ffmpeg 静帧
        - copy fill：source=text_card（字卡画面），text_card_spec=fill.text_card_spec；
                     narration 用 main_text + sub_text 拼接（供 TTS 与 LLM 上下文）；
                     若 fill.voiceover_url 已存在（gap 路由自动 TTS 写入），直接透传到 Scene。
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
        if material_cursor < len(req.selected_materials):
            ref = req.selected_materials[material_cursor]
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

        # ---- Path B: 多镜头 aigc_image 拆分 ----
        # 当 fill 返回多张图（aigc_image_urls 非空且 >1），把一个 AdaptedSection
        # 等长拆成 N 个 Scene（每个一张图）。narration 留在第一张图 Scene 上以便
        # 后续 TTS / 字幕生成；其余子 Scene 留空 narration（packaging_track 阶段
        # 默认会跳过空 narration，所以不会产生重复字幕）。
        if source == "aigc_image" and len(aigc_image_urls) > 1:
            n_shots = len(aigc_image_urls)
            per_shot = target_duration / n_shots
            for shot_idx, shot_url in enumerate(aigc_image_urls):
                scene = Scene(
                    scene_id=f"sc-{sec.order}-shot{shot_idx+1}",
                    section=sec.role,  # type: ignore[arg-type]
                    source="aigc_image",  # type: ignore[arg-type]
                    source_ref=f"{source_ref}-shot{shot_idx+1}",
                    start=timeline_cursor,
                    duration=per_shot,
                    in_point=0.0,
                    out_point=None,
                    # 仅第一张图 Scene 承载 narration / voiceover；其余留空避免重复字幕。
                    narration=(narration_override or "") if shot_idx == 0 else "",
                    voiceover_url=voiceover_url if shot_idx == 0 else None,
                    aigc_video_urls=[],
                    aigc_image_url=shot_url,
                    text_card_spec=None,
                    animation_spec=animation_spec,
                )
                main_track.append(scene)
                timeline_cursor += per_shot
            continue

        in_point = 0.0
        out_point: float | None = None
        actual_duration = target_duration
        if source == "user_material":
            # 默认：取前 target_duration 秒。若该 material 有预处理 shots，
            # 走 _pick_shot_for_section 选最配的一段（role / action_density / 时长）。
            in_point = 0.0
            actual_duration = target_duration
            out_point = actual_duration
            if effective_project_id:
                mat = material_store.get(effective_project_id, source_ref)
                if mat is not None and mat.shots:
                    chosen = _pick_shot_for_section(mat, sec)
                    if chosen is not None:
                        # 镜头本身长度可能不够目标时长——取镜头起点为 in_point，
                        # 终点取 min(镜头终点, in_point + target_duration)，
                        # 短了由 render pipeline 的 _align_to_scene_duration（slowmo/freeze）补齐。
                        in_point = float(chosen.start)
                        shot_end = float(chosen.end)
                        cut_end = min(shot_end, in_point + target_duration)
                        out_point = cut_end
                        actual_duration = target_duration  # 保持 timeline 槽位长度不变
                        log.info(
                            "[plan] sec=%s role=%s 选中 shot#%d (%.2fs-%.2fs role=%s ad=%.2f)",
                            sec.section_id, sec.role, chosen.index,
                            chosen.start, chosen.end,
                            chosen.recommended_role, chosen.action_density,
                        )
        # text_card / aigc_t2v / aigc_image：无 in/out 概念，actual_duration = target_duration

        # 不再用 content_description 自动种 narration——那会让用户在第 2 步看到"全段已有文案"，
        # 误以为系统替他做了 LLM 文案补全。改成：narration 仅在显式 fill (action=copy) 时写入，
        # 其余段落留空，由用户在 Compose UI 主动触发文案 / 配音 / AIGC。
        narration_text = narration_override or ""
        scene = Scene(
            scene_id=f"sc-{sec.order}",
            section=sec.role,  # type: ignore[arg-type]
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
        )
        main_track.append(scene)
        timeline_cursor += actual_duration

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
    # 翻到 subtitle_enabled=False 时，把已经烧好的字幕 PackagingItem 清掉——
    # 不然 render 还会把字幕画进画面，与"无字幕"语义打架。
    # 翻到 True 时不自动重生成 subtitle（让用户去 PackagingPanel 主动选样式），
    # 老 plan 兼容：plan.packaging_track 里如已有 subtitle 项保留不动。
    if patch.get("subtitle_enabled") is False:
        before = len(plan.packaging_track)
        plan.packaging_track = [it for it in plan.packaging_track if it.kind != "subtitle"]
        if before != len(plan.packaging_track):
            log.info("[plan] settings subtitle off → 移除字幕项 %d→%d",
                     before, len(plan.packaging_track))
    plan_store.put(plan)
    log.info("[plan] settings patched plan=%s keys=%s", plan_id, list(patch.keys()))
    return plan


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
        plan.main_track[scene_idx] = scene.model_copy(update={"narration": patch["narration"]})

    if section_idx is not None and any(k in patch for k in ("theme", "content_description")):
        sec = plan.adapted_sections[section_idx]
        update: dict[str, str] = {}
        if "theme" in patch:
            update["theme"] = patch["theme"]
        if "content_description" in patch:
            update["content_description"] = patch["content_description"]
        plan.adapted_sections[section_idx] = sec.model_copy(update=update)

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


@router.get("/plan", response_model=list[Plan])
async def list_plans(project_id: str) -> list[Plan]:
    """按 project_id 列出该项目所有 plans。前端进 Compose 时根据 step snapshot
    拿单个 plan_id；用本接口可在调试/历史回看时拉全量。"""
    return plan_store.list_by_project(project_id)


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
