"""Module 2 — 样例拆解（PySceneDetect + librosa + ASR + VLM + LLM）。

路由层只负责收请求 + 起 BackgroundTask + SSE 透传；
真流水线在 services/agent/decompose_agent.py。
"""
from __future__ import annotations

import json
import logging
import uuid
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from ..config import get_settings
from ..schemas import DecomposeRequest, DecomposeSubmitResponse, VideoType
from ..services.agent.decompose_agent import decompose
from ..services.jobs import job_store

log = logging.getLogger("seecript.decompose")
router = APIRouter()


# 内置样例视频的物理位置：server/samples/<sample_id>/video.mp4
_SAMPLES_ROOT = Path(__file__).resolve().parents[2] / "samples"

# 用户上传待拆解视频：server/var/uploads/decompose/<sample_id>/video.mp4
_USER_VIDEO_ALLOWED = {"video/mp4", "video/quicktime", "video/webm"}
_USER_VIDEO_MAX_BYTES = 200 * 1024 * 1024  # 单视频 200MB（比通用 material 50MB 宽松：拆解通常吃整段视频）


def _user_uploads_root() -> Path:
    settings = get_settings()
    root = settings.log_dir.parent / "var" / "uploads" / "decompose"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _resolve_video_path(sample_id: str) -> Optional[Path]:
    """先查内置样例，再查用户上传目录；都没命中返回 None，让 agent 走 mock。"""
    if not sample_id:
        return None
    sys_candidate = _SAMPLES_ROOT / sample_id / "video.mp4"
    if sys_candidate.is_file():
        return sys_candidate
    user_candidate = _user_uploads_root() / sample_id / "video.mp4"
    if user_candidate.is_file():
        return user_candidate
    return None


async def _run_decompose(
    job_id: str,
    sample_id: str,
    video_type: VideoType,
    video_path: Optional[str] = None,
    reference_asset_ids: Optional[list[str]] = None,
) -> None:
    try:
        await decompose(
            sample_id,
            job_id=job_id,
            video_type=video_type,
            video_path=video_path,
            reference_asset_ids=reference_asset_ids,
        )
    except Exception as exc:  # pragma: no cover
        log.exception("[%s] decompose failed: %s", job_id, exc)
        job_store.fail(job_id, str(exc))


@router.post("/decompose", response_model=DecomposeSubmitResponse)
async def submit_decompose(req: DecomposeRequest, bg: BackgroundTasks) -> DecomposeSubmitResponse:
    # 命中内置样例或用户已上传视频 → 把磁盘上的 video.mp4 喂给 agent，
    # 让 PySceneDetect 切真实镜头、对齐磁盘上的 shot-NN.jpg / 真实切片。
    real_path = _resolve_video_path(req.sample_id)
    job_id = job_store.create(
        "decompose",
        payload={
            "sample_id": req.sample_id,
            "video_type": req.video_type,
            "video_path": str(real_path) if real_path else None,
            "reference_asset_ids": list(req.reference_asset_ids or []),
        },
    )
    bg.add_task(
        _run_decompose,
        job_id,
        req.sample_id,
        req.video_type,
        str(real_path) if real_path else None,
        list(req.reference_asset_ids or []),
    )
    return DecomposeSubmitResponse(job_id=job_id)


class DecomposeUploadResponse(BaseModel):
    """`POST /api/decompose/upload` 返回——前端拿到 sample_id 后再调 /api/decompose 提交拆解。"""

    sample_id: str
    filename: str
    size_bytes: int
    video_url: str


@router.post("/decompose/upload", response_model=DecomposeUploadResponse)
async def upload_for_decompose(file: UploadFile = File(...)) -> DecomposeUploadResponse:
    """用户上传一段自己的视频，落到 var/uploads/decompose/<sample_id>/video.mp4，
    返回 sample_id 给前端再走 /api/decompose 流水线。

    - 仅接受 video/mp4 | video/quicktime | video/webm
    - 单文件硬上限 200MB
    - sample_id 形如 user-<hex>，决不会碰 server/samples 内置目录
    """
    if file.content_type not in _USER_VIDEO_ALLOWED:
        raise HTTPException(
            status_code=415,
            detail=f"unsupported content-type: {file.content_type}（支持 mp4/mov/webm）",
        )

    data = await file.read()
    if len(data) > _USER_VIDEO_MAX_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"{file.filename} 超过 {_USER_VIDEO_MAX_BYTES // (1024 * 1024)}MB 上限",
        )

    sample_id = f"user-{uuid.uuid4().hex[:10]}"
    target_dir = _user_uploads_root() / sample_id
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = target_dir / "video.mp4"
    target_path.write_bytes(data)
    log.info(
        "[decompose.upload] sample=%s saved %s (%d bytes)",
        sample_id,
        target_path,
        len(data),
    )
    return DecomposeUploadResponse(
        sample_id=sample_id,
        filename=Path(file.filename or "video.mp4").name,
        size_bytes=len(data),
        video_url=f"/uploads/decompose/{sample_id}/video.mp4",
    )


@router.get("/decompose/stream")
async def stream_decompose(job_id: str = Query(...)) -> StreamingResponse:
    if job_store.get(job_id) is None:
        raise HTTPException(status_code=404, detail="job not found")

    async def event_gen():
        async for event in job_store.subscribe(job_id):
            yield f"event: {event['event']}\ndata: {json.dumps(event['data'], ensure_ascii=False)}\n\n"

    return StreamingResponse(event_gen(), media_type="text/event-stream")
