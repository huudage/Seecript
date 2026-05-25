"""Module 6 — 渲染流水线。

`POST /api/render/submit`         提交渲染任务，返回 job_id
`GET  /api/render/stream?job_id`  SSE 推渲染进度（concat → seedance → remotion → overlay）

阶段 1：mock 6 步后台进度。阶段 3 接 FFmpeg + Seedance + Remotion 真实流水线。
"""
from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query
from fastapi.responses import StreamingResponse

from ..schemas import RenderSubmitRequest, RenderSubmitResponse
from ..services.jobs import job_store

log = logging.getLogger("seecript.render")
router = APIRouter()

_STEPS = [
    ("prepare", 8, "校验 Plan + 准备素材"),
    ("ffmpeg_concat", 30, "FFmpeg 拼接主轨"),
    ("seedance_extend", 55, "Seedance 首尾帧扩展长视频"),
    ("remotion_render", 80, "Remotion 渲染包装轨"),
    ("ffmpeg_overlay", 95, "FFmpeg overlay 合成最终 MP4"),
    ("finalize", 99, "封面抽帧 + 元数据"),
]


async def _run_render(job_id: str, plan_id: str, variant: str) -> None:
    try:
        job_store.start(job_id)
        for step, percent, note in _STEPS:
            await asyncio.sleep(0.8)
            job_store.publish(job_id, step=step, percent=percent, payload={"note": note})
        job_store.complete(job_id, payload={
            "plan_id": plan_id,
            "variant": variant,
            "video_url": f"/outputs/{job_id}/final.mp4",
            "cover_url": f"/outputs/{job_id}/cover.jpg",
        })
    except Exception as exc:  # pragma: no cover
        log.exception("[%s] render failed: %s", job_id, exc)
        job_store.fail(job_id, str(exc))


@router.post("/render/submit", response_model=RenderSubmitResponse)
async def submit_render(req: RenderSubmitRequest, bg: BackgroundTasks) -> RenderSubmitResponse:
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
