"""Module 2 — 样例拆解（PySceneDetect + librosa + ASR + VLM + LLM）。

路由层只负责收请求 + 起 BackgroundTask + SSE 透传；
真流水线在 services/agent/decompose_agent.py。
"""
from __future__ import annotations

import json
import logging

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query
from fastapi.responses import StreamingResponse

from ..schemas import DecomposeRequest, DecomposeSubmitResponse
from ..services.agent.decompose_agent import decompose
from ..services.jobs import job_store

log = logging.getLogger("seecript.decompose")
router = APIRouter()


async def _run_decompose(job_id: str, sample_id: str) -> None:
    try:
        await decompose(sample_id, job_id=job_id)
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
