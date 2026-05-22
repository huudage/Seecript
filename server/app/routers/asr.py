"""Module 1 prequel — Audio upload + ASR transcription (极速版 / turbo).

Flow:
  Browser (ffmpeg.wasm extracts mp3) → POST multipart /api/asr/transcribe (audio bytes)
  → backend base64-encodes the bytes inline
  → backend calls Doubao 极速版 endpoint (one HTTP request, 1-5s response)
  → returns transcript text

Why no temp file (changed in v0.4):
  极速版 accepts `audio.data` (base64) inline. Standard async版 needed `audio.url`
  pointing to a publicly-reachable file → forced us to write to disk + expose
  via nginx /asr-tmp/. None of that is needed anymore.

Local dev:
  Just set ASR_PROVIDER=doubao + DOUBAO_API_KEY in .env. No ngrok required.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, Request, UploadFile

from ..config import get_settings
from ..schemas import ASRResponse
from ..services.asr_client import ASRError, get_asr_client


router = APIRouter()
log = logging.getLogger("seecript.asr_route")


# Constants — extracted to avoid magic numbers.
# 极速版上限 100MB，但 base64 编码后 +33% → 实际 HTTP body 约 27MB；25MB 是安全阈值。
MAX_UPLOAD_BYTES = 25 * 1024 * 1024
ALLOWED_EXTENSIONS = {".mp3", ".m4a", ".wav", ".aac", ".ogg", ".opus"}
ALLOWED_MIME_PREFIXES = ("audio/",)


@router.post("/transcribe", response_model=ASRResponse)
async def transcribe(request: Request, file: UploadFile = File(...)) -> ASRResponse:
    """Accept an audio blob, run ASR (极速版), return transcript."""
    trace_id = getattr(request.state, "trace_id", "-")
    started = time.perf_counter()
    settings = get_settings()  # noqa: F841  (kept for future per-request logging hooks)

    # ---- 1. Validate file ----
    filename = file.filename or "upload.mp3"
    suffix = Path(filename).suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=415,
            detail=f"暂不支持 {suffix} 格式；请上传 mp3 / m4a / wav / aac / ogg / opus",
        )
    if file.content_type and not any(file.content_type.startswith(p) for p in ALLOWED_MIME_PREFIXES):
        # Browsers sometimes send octet-stream; we only reject when it claims a non-audio type.
        if not file.content_type.startswith("application/octet-stream"):
            raise HTTPException(
                status_code=415,
                detail=f"MIME 类型 {file.content_type} 不是音频。",
            )

    # ---- 2. Read & size-cap ----
    blob = await file.read()
    if not blob:
        raise HTTPException(status_code=400, detail="上传内容为空")
    if len(blob) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=(
                f"文件过大（{len(blob)//1024//1024} MB）。极速版建议 ≤ "
                f"{MAX_UPLOAD_BYTES//1024//1024} MB（base64 后会 +33%），"
                "更长的视频请先在浏览器端用 ffmpeg.wasm 抽取低码率单声道 16kHz mp3。"
            ),
        )

    log.info("[%s] asr upload received | bytes=%d | suffix=%s", trace_id, len(blob), suffix)

    # ---- 3. Dispatch to provider (synchronous one-shot) ----
    provider = get_asr_client()
    try:
        transcript = await provider.transcribe_bytes(blob, audio_format=suffix.lstrip("."))
    except ASRError as e:
        log.error("[%s] ASR error: code=%s upstream=%s msg=%s", trace_id, e.code, e.upstream_status, e)
        # 4xxxxxxx upstream codes → 422 (client-fixable, e.g. invalid params / silent audio)
        # 5xxxxxxx upstream codes → 502 (upstream service error)
        if e.upstream_status and 40000000 <= e.upstream_status < 50000000:
            http_status = 422
        else:
            http_status = 502
        raise HTTPException(status_code=http_status, detail=str(e)) from e
    except Exception as e:  # pragma: no cover - last-resort net
        log.exception("[%s] ASR unexpected error", trace_id)
        raise HTTPException(status_code=500, detail=f"ASR 失败：{e}") from e

    elapsed_ms = int((time.perf_counter() - started) * 1000)
    log.info(
        "[%s] asr ok | provider=%s | %dms | chars=%d",
        trace_id,
        provider.name,
        elapsed_ms,
        len(transcript),
    )
    return ASRResponse(
        transcript=transcript,
        duration_seconds=0.0,  # 极速版返回了 audio_info.duration（毫秒），后续可填，本期暂不使用
        provider=provider.name,
        elapsed_ms=elapsed_ms,
    )
