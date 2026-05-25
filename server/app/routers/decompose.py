"""Module 2 — 样例拆解（PySceneDetect + librosa + ASR + VLM + LLM）。

阶段 1：mock 后台任务推 5 步假进度（scene_detect → bgm → asr → vlm_tag → llm_section），
完结时把 sample_id 回写进 payload，前端拿到后再去 /api/sample/{id}/manifest 取 manifest。
"""
from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query
from fastapi.responses import StreamingResponse

from ..schemas import DecomposeRequest, DecomposeSubmitResponse
from ..services.jobs import job_store

log = logging.getLogger("seecript.decompose")
router = APIRouter()

_STEPS = [
    ("scene_detect", 15, "PySceneDetect 切镜头"),
    ("bgm_analysis", 35, "librosa 抽 BGM 能量"),
    ("asr_transcribe", 55, "豆包 turbo ASR 口播"),
    ("vlm_tag", 80, "VLM 帧打标"),
    ("llm_section", 95, "LLM 分 Hook/Body/CTA"),
]


async def _run_decompose(job_id: str, sample_id: str) -> None:
    try:
        job_store.start(job_id)
        for step, percent, note in _STEPS:
            await asyncio.sleep(0.6)
            job_store.publish(job_id, step=step, percent=percent, payload={"note": note})
        job_store.complete(job_id, payload={"sample_id": sample_id})
    except Exception as exc:  # pragma: no cover
        log.exception("[%s] decompose failed: %s", job_id, exc)
        job_store.fail(job_id, str(exc))


@router.post("/decompose", response_model=DecomposeSubmitResponse)
async def submit_decompose(req: DecomposeRequest, bg: BackgroundTasks) -> DecomposeSubmitResponse:
    job_id = job_store.create("decompose", payload={"sample_id": req.sample_id})
    bg.add_task(_run_decompose, job_id, req.sample_id)
    return DecomposeSubmitResponse(job_id=job_id)


@router.get("/decompose/stream")
async def stream_decompose(job_id: str = Query(...)) -> StreamingResponse:
    if job_store.get(job_id) is None:
        raise HTTPException(status_code=404, detail="job not found")

    async def event_gen():
        async for event in job_store.subscribe(job_id):
            yield f"event: {event['event']}\ndata: {json.dumps(event['data'], ensure_ascii=False)}\n\n"

    return StreamingResponse(event_gen(), media_type="text/event-stream")
