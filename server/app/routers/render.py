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
from ..services.materials import gap_store
from ..services.plans import plan_store
from ..services.render import run_pipeline

log = logging.getLogger("seecript.render")
router = APIRouter()


def _unfilled_gap_summary(plan_id: str) -> str | None:
    """汇总未补齐的缺口数量；提交渲染时不阻塞，只把 'miss=2 warn=1' 写进 notes。

    plan.py 里 _pick() 已经把没有 fill 的槽位回落成 source="sample"，pipeline 又能把
    sample 整段视频按 in_point/out_point 切片——所以缺口存在不会让渲染产出 0 字节文件，
    只是用样例镜头 + 静态 narration 兜底。这里做的就是把这个事实告诉用户。
    """
    gaps = gap_store.list_by_plan(plan_id)
    if not gaps:
        return None
    miss = sum(1 for g in gaps if g.status == "miss")
    warn = sum(1 for g in gaps if g.status == "warn")
    if miss == 0 and warn == 0:
        return None
    parts = []
    if miss:
        parts.append(f"miss={miss}")
    if warn:
        parts.append(f"warn={warn}")
    return " ".join(parts)


async def _run_render(job_id: str, plan_id: str, variant: str) -> None:
    try:
        plan = plan_store.get(plan_id)
        if plan is None:
            raise ValueError(f"plan not found: {plan_id}")
        # 接收 variant：如果与原 plan 不一致，覆盖一份（不持久化）
        if plan.variant != variant:
            plan = plan.model_copy(update={"variant": variant})
        unfilled = _unfilled_gap_summary(plan_id)
        if unfilled:
            log.info(
                "[%s] render with unfilled gaps: %s（已用样例镜头 + 静态 narration 兜底）",
                job_id, unfilled,
            )
        job_store.start(job_id)
        result = await run_pipeline(job_id, plan)
        if unfilled:
            result.notes.insert(
                0, f"unfilled gaps fallback: {unfilled}（已用样例镜头 + 静态 narration 兜底）",
            )
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
