"""Module 3 — 新内容上传 + VLM 打标。

`POST /api/material/upload`  multipart，落地到 `server/var/uploads/<session_id>/`。

阶段 1：只做文件落盘 + mock 标签，不调真 VLM。阶段 2 接 VLMClient.tag_frames。
"""
from __future__ import annotations

import logging
import uuid
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from ..config import get_settings
from ..schemas import Material, MaterialUploadResponse

log = logging.getLogger("seecript.material")
router = APIRouter()

_ALLOWED_VIDEO = {"video/mp4", "video/quicktime", "video/webm"}
_ALLOWED_IMAGE = {"image/jpeg", "image/png", "image/webp"}
_ALLOWED_AUDIO = {"audio/mpeg", "audio/wav", "audio/x-wav"}
_MAX_BYTES = 50 * 1024 * 1024  # 50MB 单文件硬上限


def _uploads_root() -> Path:
    settings = get_settings()
    root = settings.log_dir.parent / "var" / "uploads"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _detect_media_type(content_type: str | None) -> str | None:
    if not content_type:
        return None
    if content_type in _ALLOWED_VIDEO:
        return "video"
    if content_type in _ALLOWED_IMAGE:
        return "image"
    if content_type in _ALLOWED_AUDIO:
        return "audio"
    return None


@router.post("/material/upload", response_model=MaterialUploadResponse)
async def upload_material(
    files: list[UploadFile] = File(...),
    session_id: str | None = Form(default=None),
) -> MaterialUploadResponse:
    if not files:
        raise HTTPException(status_code=400, detail="no files")
    sid = session_id or uuid.uuid4().hex[:12]
    target_dir = _uploads_root() / sid
    target_dir.mkdir(parents=True, exist_ok=True)

    materials: list[Material] = []
    for f in files:
        media_type = _detect_media_type(f.content_type)
        if media_type is None:
            raise HTTPException(status_code=415, detail=f"unsupported content-type: {f.content_type}")
        data = await f.read()
        if len(data) > _MAX_BYTES:
            raise HTTPException(status_code=413, detail=f"{f.filename} exceeds 50MB")
        safe_name = Path(f.filename or "unnamed").name
        material_id = uuid.uuid4().hex[:12]
        dest = target_dir / f"{material_id}_{safe_name}"
        dest.write_bytes(data)
        log.info("[material] session=%s saved %s (%d bytes, %s)", sid, dest.name, len(data), media_type)
        materials.append(
            Material(
                material_id=material_id,
                filename=safe_name,
                media_type=media_type,  # type: ignore[arg-type]
                duration_seconds=None,
                thumbnail_url=f"/uploads/{sid}/{dest.name}",
                tags=["[mock] 室内", "[mock] 近景", "[mock] 口播"],
                recommended_section="body",
            )
        )

    return MaterialUploadResponse(session_id=sid, materials=materials)
