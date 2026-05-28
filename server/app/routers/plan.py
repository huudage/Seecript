"""Module 5 — Plan 组装。

`POST /api/plan/build`：
1. 反查样例 manifest（优先真预解析 `_load_real_manifest`，回落 `_stub_manifest`）
2. 走 `plan_agent.adapt_structure`，基于 brief + video_goal 把样例骨架改编为 AdaptedSection[]
3. 按 AdaptedSection 一段对应一个 Scene 拼主轨——长度不再硬编码 5
4. 持久化 Plan（含 adapted_sections + video_goal），供 /gap/detect、/render、/edit 复用
"""
from __future__ import annotations

import logging
import re
import uuid

from fastapi import APIRouter, HTTPException

from ..routers.library import _LIBRARY, _load_real_manifest, _stub_manifest
from ..schemas import (
    AdaptedSection,
    BGMConfig,
    PackagingItem,
    Plan,
    PlanBuildRequest,
    SampleManifest,
    Scene,
)
from ..services.agent.plan_agent import adapt_structure
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


def _section_duration_from_shots(
    manifest: SampleManifest,
    shot_indices: list[int],
    *,
    default: float = 4.0,
) -> float:
    """取该段在样例 manifest 中对应镜头的时长之和；没有命中给 default。"""
    if not shot_indices:
        return default
    by_idx = {sh.index: sh for sh in manifest.shots}
    total = 0.0
    hit = False
    for i in shot_indices:
        sh = by_idx.get(i)
        if sh:
            total += max(0.5, sh.duration)
            hit = True
    return total if hit else default


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


@router.post("/plan/build", response_model=Plan)
async def build_plan(req: PlanBuildRequest) -> Plan:
    plan_id = f"plan-{uuid.uuid4().hex[:10]}"
    log.info(
        "[plan] build plan=%s sample=%s materials=%d fills=%d variant=%s session=%s brief=%s goal=%s",
        plan_id, req.sample_id, len(req.selected_materials), len(req.fills),
        req.variant, req.session_id, (req.brief or "")[:30], (req.video_goal or "")[:30],
    )

    # 1. 取样例 manifest
    manifest = _resolve_manifest(req.sample_id)

    # 2. LLM 改编段落结构
    adapted = await adapt_structure(manifest, req.brief, req.video_goal)
    if not adapted:
        # 极端兜底：plan_agent 也失败时按 manifest.sections 1:1 包一层
        adapted = [
            AdaptedSection(
                section_id=f"sec-{i}",
                role=sec.role,
                theme=sec.theme or "段落",
                content_description=f"[fallback] {sec.role} 段，沿用样例结构。",
                source_section_indices=[i],
                source_shot_indices=list(sec.shot_indices or []),
                order=i,
            )
            for i, sec in enumerate(manifest.sections)
        ]

    log.info("[plan] adapted_sections=%d (sample_sections=%d)",
             len(adapted), len(manifest.sections))

    # 3. 按 adapted 段落生成 main_track Scene[]
    # gap fills 按 role 索引；多段同 role 时只覆盖第一个（保持与旧 gap_id 路由兼容）。
    fill_by_role = {
        f.gap_id.split("-")[1] if "-" in f.gap_id else "development": f
        for f in req.fills if f.new_material_id
    }
    material_cursor = 0  # 顺位消费 selected_materials

    def _pick(role: str, sample_shot_idx: int) -> tuple[str, str]:
        nonlocal material_cursor
        fill = fill_by_role.get(role)
        if fill and fill.new_material_id:
            return ("aigc_t2v", fill.new_material_id)
        if material_cursor < len(req.selected_materials):
            ref = req.selected_materials[material_cursor]
            material_cursor += 1
            return ("user_material", ref)
        return ("sample", f"sample-shot-{sample_shot_idx:02d}")

    main_track: list[Scene] = []
    timeline_cursor = 0.0
    for sec in adapted:
        sample_shot_idx = sec.source_shot_indices[0] if sec.source_shot_indices else 0
        source, source_ref = _pick(sec.role, sample_shot_idx)
        target_duration = _section_duration_from_shots(manifest, sec.source_shot_indices)

        in_point = 0.0
        out_point: float | None = None
        actual_duration = target_duration
        if source == "sample":
            m = _SAMPLE_SHOT_RE.search(source_ref)
            if m:
                shot_idx = int(m.group(1))
                in_point, shot_duration = _sample_shot_window(req.sample_id, shot_idx)
                actual_duration = min(target_duration, shot_duration)
                out_point = in_point + actual_duration

        scene = Scene(
            scene_id=f"sc-{sec.order}",
            section=sec.role,  # type: ignore[arg-type]
            source=source,  # type: ignore[arg-type]
            source_ref=source_ref,
            start=timeline_cursor,
            duration=actual_duration,
            in_point=in_point,
            out_point=out_point,
            narration=_narration_from_content(sec.content_description),
        )
        main_track.append(scene)
        timeline_cursor += actual_duration

    actual_total = sum(sc.duration for sc in main_track) or 1.0

    # 4. 包装轨（仍按段数生成最小骨架——packaging_agent 后续会按真实 plan 补全）
    packaging_track: list[PackagingItem] = []
    if main_track:
        opening = main_track[0]
        packaging_track.append(PackagingItem(
            item_id="pkg-title", kind="title_bar",
            start=opening.start, end=opening.start + opening.duration,
            text=adapted[0].theme or "开场",
            style={"size": 64, "color": "#FFF"},
        ))
    if len(main_track) >= 2:
        body_start = main_track[0].duration
        body_end = max(body_start, actual_total - main_track[-1].duration)
        if body_end > body_start:
            packaging_track.append(PackagingItem(
                item_id="pkg-sub-1", kind="subtitle",
                start=body_start, end=body_end,
                text="动态字幕跟随口播",
                style={"size": 48, "stroke": "#000"},
            ))
        closing = main_track[-1]
        packaging_track.append(PackagingItem(
            item_id="pkg-cta", kind="sticker",
            start=closing.start, end=closing.start + closing.duration,
            text=adapted[-1].theme or "点赞收藏",
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
        bgm=BGMConfig(track_url=None, volume=0.6),
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
