"""Module 5b — Packaging Agent HTTP 入口。

`POST /api/packaging/recommend` —— 拿 plan_id 跑 packaging_agent，
返回 PackagingRecommendation 给前端；apply=True 时同时落地到 plan.packaging_track。

落地路径：Plan.packaging_track 写入 kind="transition" 和 kind="cover" 两类 PackagingItem，
后续 /api/render 把它们交给 Remotion 渲成透明 WebM 叠到主轨上。
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException

from ..schemas import PackagingRecommendation, PackagingRecommendRequest
from ..services.agent.packaging_agent import recommend_packaging
from ..services.plans import plan_store

log = logging.getLogger("seecript.packaging")
router = APIRouter()


@router.post("/packaging/recommend", response_model=PackagingRecommendation)
async def recommend(req: PackagingRecommendRequest) -> PackagingRecommendation:
    plan = plan_store.get(req.plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail=f"plan_id 不存在：{req.plan_id}")
    log.info("[packaging] recommend plan=%s apply=%s scenes=%d",
             plan.plan_id, req.apply, len(plan.main_track))
    rec = await recommend_packaging(plan, apply=req.apply)
    return rec
