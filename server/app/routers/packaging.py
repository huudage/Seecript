"""Module 5b — Packaging Agent HTTP 入口。

`POST /api/packaging/recommend` —— 拿 plan_id 跑 packaging_agent，
返回 PackagingRecommendation 给前端；apply=True 时同时落地到 plan.packaging_track。

落地路径：Plan.packaging_track 写入 kind="cover" 的 PackagingItem，
转场则直接写到 main_track 各 Scene 的 transition_in（不再走包装轨）。

偏好持久化：
- 入参 preferences 与 plan.settings.packaging_prefs 合并（请求体覆盖；body 整体提供时整体替换）
- 调用 agent 用合并后的 prefs
- 写回 plan.settings.packaging_prefs，下次 PackagingPanel 打开能反显
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException

from ..schemas import (
    PackagingRecommendation,
    PackagingRecommendRequest,
)
from ..services.agent.packaging_agent import recommend_packaging
from ..services.plans import plan_store

log = logging.getLogger("seecript.packaging")
router = APIRouter()


@router.post("/packaging/recommend", response_model=PackagingRecommendation)
async def recommend(req: PackagingRecommendRequest) -> PackagingRecommendation:
    plan = plan_store.get(req.plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail=f"plan_id 不存在：{req.plan_id}")

    # 合并：请求体 preferences 优先；为空则用 plan.settings.packaging_prefs
    effective_prefs = req.preferences or plan.settings.packaging_prefs

    log.info(
        "[packaging] recommend plan=%s apply=%s scenes=%d preset=%s allowed=%s",
        plan.plan_id, req.apply, len(plan.main_track),
        effective_prefs.preset,
        ",".join(effective_prefs.allowed_transition_styles),
    )

    rec = await recommend_packaging(plan, apply=req.apply, preferences=effective_prefs)

    # 把合并后的 prefs 持久化回 plan.settings.packaging_prefs（即使 apply=False 也写，
    # 因为用户已经表达了配置意图；render 阶段烧字幕样式直接读这里）
    if req.preferences is not None:
        plan.settings = plan.settings.model_copy(
            update={"packaging_prefs": req.preferences}
        )
        plan_store.replace(plan)

    return rec
