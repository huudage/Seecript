"""Module 5 — Plan 组装。

`POST /api/plan/build`：
1. 反查样例 manifest（优先真预解析 `_load_real_manifest`，回落 `_stub_manifest`）
2. 走 `plan_agent.adapt_structure`，基于 brief + video_goal + settings 把样例骨架改编为 AdaptedSection[]
3. 按 AdaptedSection 一段对应一个 Scene 拼主轨——长度由 LLM 给的 duration_seconds 决定
4. 持久化 Plan（含 adapted_sections + video_goal + settings），供 /gap/detect、/render、/edit 复用
"""
from __future__ import annotations

import logging
import re
import uuid
from typing import Optional

from fastapi import APIRouter, HTTPException

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
)
from ..services.agent.plan_agent import adapt_structure
from ..services.assets import asset_store
from ..services.materials import gap_store
from ..services.plans import plan_store

log = logging.getLogger("seecript.plan")
router = APIRouter()


_SAMPLE_SHOT_RE = re.compile(r"sample-shot-(\d+)")


def _sample_shot_window(sample_id: str, shot_idx: int) -> tuple[float, float]:
    """根据样例 LibraryItem.duration_seconds + shot_count 估算第 shot_idx 个镜头的 (in_point, duration)。"""
    sample = next((s for s in _LIBRARY if s.id == sample_id), None)
    if sample is None or sample.shot_count <= 0:
        return (0.0, 3.0)
    avg = sample.duration_seconds / sample.shot_count
    idx = max(0, min(shot_idx, sample.shot_count - 1))
    return (idx * avg, avg)


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
    """把 FillResult 按其 gap_id 对应的 section_id 索引——多段同 role 时不再被压扁。

    流程：fills 里的 gap_id 反查 gap_store 拿 Gap.section_id；
    section_id → 该段第一个有效 fill 的 FillResult。
    """
    out: dict[str, FillResult] = {}
    for f in fills:
        if not f.new_material_id and not f.video_urls and not f.narration:
            continue
        gap = gap_store.get(f.gap_id)
        sid = gap.section_id if (gap and gap.section_id) else None
        if sid is None:
            # 老 gap_id 走兜底：用 role + section_seq 反推 sec-N 不可靠，留给 plan 端忽略
            continue
        out.setdefault(sid, f)
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
        volume=0.35,
        fade_in=1.5,
        fade_out=2.0,
        start_offset=0.0,
        duck_with_voice=True,
    )


@router.post("/plan/build", response_model=Plan)
async def build_plan(req: PlanBuildRequest) -> Plan:
    plan_id = f"plan-{uuid.uuid4().hex[:10]}"
    settings = req.settings or ComposeSettings()
    log.info(
        "[plan] build plan=%s sample=%s materials=%d fills=%d variant=%s session=%s "
        "brief=%s goal=%s target_dur=%.0fs platform=%s tone=%s",
        plan_id, req.sample_id, len(req.selected_materials), len(req.fills),
        req.variant, req.session_id, (req.brief or "")[:30], (req.video_goal or "")[:30],
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

    def _pick(sec: AdaptedSection, sample_shot_idx: int) -> tuple[str, str, list[str], str | None]:
        """返回 (source, source_ref, aigc_video_urls, narration_override)。

        优先级：本段 fill（aigc / copy） > 用户素材 > 样例镜头。
        - aigc fill：source=aigc_t2v，source_ref=task_id，aigc_video_urls=video_urls
        - copy fill：source 仍走素材/样例兜底，narration 用 fill.narration 覆盖
        - 用户素材：顺位消费 selected_materials
        - 样例镜头：兜底引用 sample-shot-XX
        """
        nonlocal material_cursor
        fill = fill_by_section.get(sec.section_id)
        if fill and fill.action == "aigc" and (fill.video_urls or fill.new_material_id):
            return (
                "aigc_t2v",
                fill.new_material_id or (fill.video_urls[0] if fill.video_urls else "aigc"),
                list(fill.video_urls),
                None,
            )
        narration_override = None
        if fill and fill.action == "copy" and fill.narration:
            narration_override = fill.narration
        if material_cursor < len(req.selected_materials):
            ref = req.selected_materials[material_cursor]
            material_cursor += 1
            return ("user_material", ref, [], narration_override)
        return ("sample", f"sample-shot-{sample_shot_idx:02d}", [], narration_override)

    main_track: list[Scene] = []
    timeline_cursor = 0.0
    for sec in adapted:
        sample_shot_idx = sec.source_shot_indices[0] if sec.source_shot_indices else 0
        source, source_ref, aigc_urls, narration_override = _pick(sec, sample_shot_idx)
        target_duration = float(sec.duration_seconds) if sec.duration_seconds > 0 else 4.0

        in_point = 0.0
        out_point: float | None = None
        actual_duration = target_duration
        if source == "sample":
            m = _SAMPLE_SHOT_RE.search(source_ref)
            if m:
                shot_idx = int(m.group(1))
                in_point, shot_duration = _sample_shot_window(req.sample_id, shot_idx)
                # 样例镜头有多长就放多长（target 是目标，但不超过实际可用素材）
                actual_duration = min(target_duration, shot_duration) if shot_duration > 0 else target_duration
                out_point = in_point + actual_duration

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
            style={"size": 48, "stroke": "#000"},
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
        session_id=req.session_id,
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
    return plan


@router.get("/plan/{plan_id}", response_model=Plan)
async def get_plan(plan_id: str) -> Plan:
    """Plan 详情查询。包装/编辑动作回写后，前端用它把 store 同步成最新版本。"""
    plan = plan_store.get(plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail=f"plan_id 不存在：{plan_id}")
    return plan
