"""Module 6 — 渲染流水线。

`POST /api/render/submit`         提交渲染任务，返回 job_id
`GET  /api/render/stream?job_id`  SSE 推渲染进度（concat → seedance → remotion → overlay）

阶段 3：接 services/render/pipeline.run_pipeline 真实流水线（mock 模式下也能跑完）。
"""
from __future__ import annotations

import json
import logging

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query
from fastapi.responses import StreamingResponse

from ..schemas import RenderSubmitRequest, RenderSubmitResponse
from ..services.jobs import job_store
from ..services.plans import plan_store
from ..services.render import run_pipeline

log = logging.getLogger("seecript.render")
router = APIRouter()


async def _run_render(job_id: str, plan_id: str, variant: str) -> None:
    try:
        plan = plan_store.get(plan_id)
        if plan is None:
            raise ValueError(f"plan not found: {plan_id}")
        # 接收 variant：如果与原 plan 不一致，覆盖一份（不持久化）
        if plan.variant != variant:
            plan = plan.model_copy(update={"variant": variant})
        job_store.start(job_id)
        result = await run_pipeline(job_id, plan)
        job_store.complete(job_id, payload={
            "plan_id": result.plan_id,
            "variant": result.variant,
            "video_url": result.video_url,
            "cover_url": result.cover_url,
            "duration_seconds": result.duration_seconds,
            "timings_ms": result.timings_ms,
            "notes": result.notes,
        })
    except Exception as exc:
        log.exception("[%s] render failed: %s", job_id, exc)
        job_store.fail(job_id, str(exc))


@router.post("/render/submit", response_model=RenderSubmitResponse)
async def submit_render(req: RenderSubmitRequest, bg: BackgroundTasks) -> RenderSubmitResponse:
    if plan_store.get(req.plan_id) is None:
        raise HTTPException(status_code=404, detail=f"plan not found: {req.plan_id}")
    job_id = job_store.create("render", payload={"plan_id": req.plan_id, "variant": req.variant})
    bg.add_task(_run_render, job_id, req.plan_id, req.variant)
    return RenderSubmitResponse(job_id=job_id)


@router.get("/render/stream")
async def stream_render(job_id: str = Query(...)) -> StreamingResponse:
    if job_store.get(job_id) is None:
        raise HTTPException(status_code=404, detail="job not found")

    async def event_gen():
        async for event in job_store.subscribe(job_id):
            yield f"event: {event['event']}\ndata: {json.dumps(event['data'], ensure_ascii=False)}\n\n"

    return StreamingResponse(event_gen(), media_type="text/event-stream")
