"""Module 5b — Packaging Agent HTTP 入口（V2：5 维度多候选）。

`POST /api/packaging/recommend` —— 拿 plan_id 跑 packaging_agent V2，
返回 PackagingRecommendationV2（subtitle_styles / title_bars / stickers /
transition_bundles / covers），不 mutate plan，只 persist 偏好。

`POST /api/packaging/apply` —— 用户挑完 candidate_id 后调用，把 selection
写到 plan.packaging_track + Scene.transition_in，返回最新 Plan。

偏好持久化：
- 入参 preferences 与 plan.settings.packaging_prefs 合并（请求体覆盖）
- 调用 agent 用合并后的 prefs
- 写回 plan.settings.packaging_prefs，下次 PackagingPanel 打开能反显
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException

from ..schemas import (
    PackagingItem,
    PackagingItemDraftRequest,
    PackagingItemDraftResponse,
    PackagingItemPlaceRequest,
    PackagingRecommendationV2,
    PackagingRecommendRequest,
    PackagingSceneRecommendRequest,
    PackagingSelection,
    Plan,
)
from ..services.agent.packaging_agent import (
    apply_selection_to_plan,
    recommend_packaging_for_scene,
    recommend_packaging_v2,
)
from ..services.llm_client import LLMError
from ..services.plans import plan_store

log = logging.getLogger("seecript.packaging")
router = APIRouter()


@router.post("/packaging/recommend", response_model=PackagingRecommendationV2)
async def recommend(req: PackagingRecommendRequest) -> PackagingRecommendationV2:
    plan = plan_store.get(req.plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail=f"plan_id 不存在：{req.plan_id}")

    effective_prefs = req.preferences or plan.settings.packaging_prefs

    log.info(
        "[packaging] recommend-v2 plan=%s scenes=%d preset=%s allowed=%s",
        plan.plan_id, len(plan.main_track),
        effective_prefs.preset,
        ",".join(effective_prefs.allowed_transition_styles),
    )

    rec = await recommend_packaging_v2(plan, preferences=effective_prefs)

    if req.preferences is not None:
        plan.settings = plan.settings.model_copy(
            update={"packaging_prefs": req.preferences}
        )
        plan_store.replace(plan)

    return rec


@router.post("/packaging/apply", response_model=Plan)
async def apply(req: PackagingSelection) -> Plan:
    plan = plan_store.get(req.plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail=f"plan_id 不存在：{req.plan_id}")
    if req.recommendation.plan_id != req.plan_id:
        raise HTTPException(
            status_code=400,
            detail="recommendation.plan_id 与请求 plan_id 不一致",
        )
    log.info(
        "[packaging] apply plan=%s subs=%s tbs=%d stickers=%d transitions=%d cover=%s",
        plan.plan_id,
        req.subtitle_style_id or "none",
        len(req.title_bar_ids),
        len(req.sticker_ids),
        len(req.transition_selections),
        req.cover_id or "none",
    )
    return apply_selection_to_plan(plan, req)


# ---------------------------------------------------------------------------
# F2 · 单组件 picker 增量接口
# ---------------------------------------------------------------------------

def _next_item_id(plan: Plan, kind: str) -> str:
    prefix = {"title_bar": "pkg-tb", "sticker": "pkg-st", "cover": "pkg-cv"}.get(kind, f"pkg-{kind}")
    used = {it.item_id for it in plan.packaging_track}
    i = 1
    while f"{prefix}-u{i}" in used:
        i += 1
    return f"{prefix}-u{i}"


def _candidate_to_item(plan: Plan, kind: str, rec: PackagingRecommendationV2) -> tuple[PackagingItem, str] | None:
    """从 V2 推荐里挑 kind 第一个候选，转成 PackagingItem。无候选返回 None。"""
    if kind == "title_bar":
        c = rec.title_bars[0] if rec.title_bars else None
        if c is None:
            return None
        item = PackagingItem(
            item_id=_next_item_id(plan, "title_bar"),
            kind="title_bar",
            start=c.start,
            end=c.end,
            text=c.text,
            style={
                "font_size": c.font_size,
                "color": c.color,
                "background_color": c.background_color,
                "position": c.position,
            },
        )
        return item, c.rationale
    if kind == "sticker":
        c = rec.stickers[0] if rec.stickers else None
        if c is None:
            return None
        item = PackagingItem(
            item_id=_next_item_id(plan, "sticker"),
            kind="sticker",
            start=c.start,
            end=c.end,
            text=c.text,
            style={
                "color": c.color,
                "background_color": c.background_color,
                "position": c.position,
            },
        )
        return item, c.rationale
    if kind == "cover":
        c = rec.covers[0] if rec.covers else None
        if c is None or not plan.main_track:
            return None
        first_dur = plan.main_track[0].duration
        cover_end = max(0.6, min(plan.settings.packaging_prefs.cover_duration, first_dur))
        item = PackagingItem(
            item_id=_next_item_id(plan, "cover"),
            kind="cover",
            start=0.0,
            end=cover_end,
            text=c.title,
            style={
                "subtitle": c.subtitle,
                "palette": c.palette,
                "layout": c.layout,
                "style_note": c.style_note,
            },
        )
        return item, c.rationale
    return None


@router.post("/packaging/items/draft", response_model=PackagingItemDraftResponse)
async def draft_item(req: PackagingItemDraftRequest) -> PackagingItemDraftResponse:
    """按 kind 跑一次 V2 推荐，挑同 kind 的首个候选转成 PackagingItem。

    不写 plan——前端拿草稿放进 staging slot，用户编辑后再调 place。
    """
    plan = plan_store.get(req.plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail=f"plan_id 不存在：{req.plan_id}")
    rec = await recommend_packaging_v2(plan)
    out = _candidate_to_item(plan, req.kind, rec)
    if out is None:
        raise HTTPException(
            status_code=502,
            detail=f"AI 暂时给不出 {req.kind} 候选，请稍后再试或换一个组件类型。",
        )
    item, rationale = out
    log.info("[packaging] draft plan=%s kind=%s item_id=%s", plan.plan_id, req.kind, item.item_id)
    return PackagingItemDraftResponse(item=item, rationale=rationale)


@router.post("/packaging/items/place", response_model=Plan)
async def place_item(req: PackagingItemPlaceRequest) -> Plan:
    """把 staging slot 里的 PackagingItem 单独 append 到 plan.packaging_track。"""
    plan = plan_store.get(req.plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail=f"plan_id 不存在：{req.plan_id}")
    if req.item.kind not in ("title_bar", "sticker", "cover"):
        raise HTTPException(
            status_code=400,
            detail="只能用此接口添加 title_bar/sticker/cover；字幕由字幕轨开关控制，转场由 main_track.transition_in 内化。",
        )
    # 时间裁剪：保证 item 不超过 plan 总时长
    total = float(plan.duration_seconds or 0.0) or sum(s.duration for s in plan.main_track)
    item = req.item.model_copy()
    if total > 0:
        if item.end > total:
            item.end = total
        if item.start >= item.end:
            item.start = max(0.0, item.end - 1.0)
    # 同 item_id 已存在 → 替换；否则 append
    existing = next((i for i, it in enumerate(plan.packaging_track) if it.item_id == item.item_id), None)
    if existing is not None:
        plan.packaging_track[existing] = item
    else:
        plan.packaging_track.append(item)
    plan_store.replace(plan)
    log.info("[packaging] place plan=%s kind=%s item_id=%s start=%.2f end=%.2f",
             plan.plan_id, item.kind, item.item_id, item.start, item.end)
    return plan


@router.delete("/packaging/items/{plan_id}/{item_id}", response_model=Plan)
async def delete_item(plan_id: str, item_id: str) -> Plan:
    plan = plan_store.get(plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail=f"plan_id 不存在：{plan_id}")
    before = len(plan.packaging_track)
    plan.packaging_track = [it for it in plan.packaging_track if it.item_id != item_id]
    if len(plan.packaging_track) == before:
        raise HTTPException(status_code=404, detail=f"item_id 不存在：{item_id}")
    plan_store.replace(plan)
    log.info("[packaging] delete plan=%s item=%s", plan_id, item_id)
    return plan


@router.post("/packaging/recommend-for-scene", response_model=PackagingItemDraftResponse)
async def recommend_for_scene(req: PackagingSceneRecommendRequest) -> PackagingItemDraftResponse:
    """单 scene + 自然语言 hint → 单个 PackagingItem 草稿。

    前端拿到后可直接调 /packaging/items/place 落进 plan.packaging_track；
    或先放进 staging slot 让用户调整文字/时间再 place。
    """
    plan = plan_store.get(req.plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail=f"plan_id 不存在：{req.plan_id}")
    try:
        item, rationale = await recommend_packaging_for_scene(
            plan, scene_id=req.scene_id, kind=req.kind, hint=req.hint,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except LLMError as exc:
        raise HTTPException(status_code=502, detail=f"AI 暂时给不出建议：{exc}")
    log.info(
        "[packaging] scene-recommend plan=%s scene=%s kind=%s item=%s",
        plan.plan_id, req.scene_id, req.kind, item.item_id,
    )
    return PackagingItemDraftResponse(item=item, rationale=rationale)
