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
    BGMConfig,
    ComposeSettings,
    FillResult,
    PackagingItem,
    Plan,
    PlanBuildRequest,
    SampleManifest,
    Scene,
    TargetPlatform,
    ToneStyle,
    TTSVoice,
)
from ..services.agent.plan_agent import adapt_structure
from ..services.assets import asset_store
from ..services.materials import gap_store
from ..services.plans import plan_store
from ..services.projects import project_store

log = logging.getLogger("seecript.plan")
router = APIRouter()


def _resolve_manifest(sample_id: str) -> SampleManifest:
    """先尝试真预解析 manifest，没有则回落 stub。"""
    real = _load_real_manifest(sample_id)
    if real is not None:
        return real
    sample = next((s for s in _LIBRARY if s.id == sample_id), _LIBRARY[0])
    return _stub_manifest(sample.id, sample)


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


@router.post("/plan/build", response_model=Plan)
async def build_plan(req: PlanBuildRequest) -> Plan:
    plan_id = f"plan-{uuid.uuid4().hex[:10]}"
    settings = req.settings or ComposeSettings()
    # v2 起 session_id == project_id；老前端只传 session_id 时仍可工作
    effective_project_id = (req.project_id or req.session_id or "").strip() or None
    log.info(
        "[plan] build plan=%s sample=%s project=%s materials=%d fills=%d variant=%s "
        "brief=%s goal=%s target_dur=%.0fs platform=%s tone=%s",
        plan_id, req.sample_id, effective_project_id,
        len(req.selected_materials), len(req.fills),
        req.variant, (req.brief or "")[:30], (req.video_goal or "")[:30],
        settings.target_duration_seconds, settings.target_platform, settings.tone,
    )

    # 1. 取样例 manifest
    manifest = _resolve_manifest(req.sample_id)

    # 2. LLM 改编段落结构（带 settings + 参考素材注入）
    adapted = await adapt_structure(
        manifest, req.brief, req.video_goal, settings,
        reference_asset_ids=req.reference_asset_ids,
    )
    if not adapted:
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
            for i, sec in enumerate(manifest.sections)
        ]

    log.info("[plan] adapted_sections=%d (sample_sections=%d) target_total=%.1fs",
             len(adapted), len(manifest.sections),
             sum(s.duration_seconds for s in adapted))

    # 3. 把 fills 按 section_id 索引——这是修复『多段 development 全被路由到第一段』bug 的关键
    fill_by_section = _fill_section_lookup(req.fills)
    material_cursor = 0

    def _pick(sec: AdaptedSection) -> tuple[str, str, list[str], str | None, str | None]:
        """返回 (source, source_ref, aigc_video_urls, narration_override, voiceover_url)。

        优先级：本段 fill（aigc / copy） > 用户素材 > 文字卡兜底。
        - aigc fill：source=aigc_t2v，source_ref=task_id，aigc_video_urls=video_urls
        - copy fill：source=text_card（无画面素材），narration 用 fill.narration；
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
            )
        narration_override = None
        voiceover_url = None
        if fill and fill.action == "copy" and fill.narration:
            narration_override = fill.narration
            voiceover_url = (fill.voiceover_url or "").strip() or None
        if material_cursor < len(req.selected_materials):
            ref = req.selected_materials[material_cursor]
            material_cursor += 1
            return ("user_material", ref, [], narration_override, voiceover_url)
        # 无 AIGC、无 copy 文案、无用户素材 → 文字卡（packaging 字幕负责显示真实文案）
        return ("text_card", f"text-card-{sec.section_id}", [], narration_override, voiceover_url)

    main_track: list[Scene] = []
    timeline_cursor = 0.0
    for sec in adapted:
        source, source_ref, aigc_urls, narration_override, voiceover_url = _pick(sec)
        target_duration = float(sec.duration_seconds) if sec.duration_seconds > 0 else 4.0

        in_point = 0.0
        out_point: float | None = None
        actual_duration = target_duration
        if source == "user_material":
            # user_material 也要切片（不切 ffmpeg 会把整段长视频塞进 4s scene 槽）
            in_point = 0.0
            actual_duration = target_duration
            out_point = actual_duration
        # text_card / aigc_t2v：无 in/out 概念，actual_duration = target_duration

        narration_text = narration_override or _narration_from_content(sec.content_description)
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
        )
        main_track.append(scene)
        timeline_cursor += actual_duration

    actual_total = sum(sc.duration for sc in main_track) or 1.0

    # 4. 包装轨：标题 + 每段独立字幕 + CTA
    packaging_track: list[PackagingItem] = []
    if main_track:
        opening = main_track[0]
        packaging_track.append(PackagingItem(
            item_id="pkg-title", kind="title_bar",
            start=opening.start, end=opening.start + opening.duration,
            text=adapted[0].theme or "开场",
            style={"size": 64, "color": "#FFF"},
        ))

    # 每个 Scene 烧一条字幕（用 scene.narration 而不是单条 placeholder）
    # voiceover_enabled=False 时跳过：纯 BGM 视频不该自动出字幕，
    # 但仍保留 scene.narration 文本，供 LLM 改编上下文使用。
    if settings.voiceover_enabled:
        prefs = settings.packaging_prefs
        # custom 时直接用 prefs 字段；非 custom 走预设展开（确保 plan/build 落盘的 subtitle 样式
        # 与 PackagingPanel 默认预设一致，不必等用户先点一次"一键包装"才生效）。
        from ..services.agent.packaging_agent import expand_preset
        effective = expand_preset(prefs)
        for idx, scene in enumerate(main_track):
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

    if len(main_track) >= 2:
        closing = main_track[-1]
        cta_text = (settings.cta or "").strip() or adapted[-1].theme or "点赞收藏"
        packaging_track.append(PackagingItem(
            item_id="pkg-cta", kind="sticker",
            start=closing.start, end=closing.start + closing.duration,
            text=cta_text,
            style={"size": 56, "color": "#FFE600"},
        ))

    plan = Plan(
        plan_id=plan_id,
        sample_id=req.sample_id,
        project_id=effective_project_id,
        session_id=effective_project_id,
        brief=req.brief,
        video_goal=req.video_goal,
        adapted_sections=adapted,
        variant=req.variant,
        duration_seconds=actual_total,
        main_track=main_track,
        packaging_track=packaging_track,
        bgm=_build_bgm_config(req.bgm_asset_id),
        settings=settings,
    )
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
            # 替换 BGM：重新从 asset 拉 duration/peak，并重置 anchor
            bgm = _build_bgm_config(new_id)

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
    主要用法：四轨板左侧开关翻转 voiceover_enabled、Compose 设置面板切换 tts_voice。
    """
    voiceover_enabled: Optional[bool] = None
    tts_voice: Optional[TTSVoice] = None
    target_platform: Optional[TargetPlatform] = None
    tone: Optional[ToneStyle] = None
    cta: Optional[str] = Field(default=None, max_length=20)
    keywords: Optional[list[str]] = Field(default=None, max_length=5)
    target_duration_seconds: Optional[float] = Field(default=None, ge=10.0, le=120.0)


@router.patch("/plan/{plan_id}/settings", response_model=Plan)
async def patch_plan_settings(plan_id: str, body: PlanSettingsPatch) -> Plan:
    """部分更新 plan.settings；不重跑 LLM，仅落盘 + 返回最新 Plan。

    不触发结构重排——voiceover_enabled 由 voice/render 阶段读取生效，
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
    # 翻到 voiceover_enabled=False 时，把已经烧好的字幕 PackagingItem 清掉——
    # 不然 render 还会把字幕画进画面，与"纯 BGM 视频"语义打架。
    if patch.get("voiceover_enabled") is False:
        before = len(plan.packaging_track)
        plan.packaging_track = [it for it in plan.packaging_track if it.kind != "subtitle"]
        if before != len(plan.packaging_track):
            log.info("[plan] settings voiceover off → 移除字幕项 %d→%d",
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

    if "narration" in patch:
        plan.main_track[scene_idx] = scene.model_copy(update={"narration": patch["narration"]})

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
    return plan


@router.get("/plan", response_model=list[Plan])
async def list_plans(project_id: str) -> list[Plan]:
    """按 project_id 列出该项目所有 plans。前端进 Compose 时根据 step snapshot
    拿单个 plan_id；用本接口可在调试/历史回看时拉全量。"""
    return plan_store.list_by_project(project_id)
