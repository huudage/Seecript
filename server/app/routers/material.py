"""Module 3 — 新内容上传 + 多模态 LLM 打标 + MaterialStore 落地。

`POST /api/material/upload`  multipart，落地到 `server/var/uploads/<session_id>/`，
                              做多模态 LLM 打标（tags + recommended_section），
                              结果存进 MaterialStore 供 /gap/detect 反查。

- video：ffmpeg 抽首帧（t=0.5s）→ 给 LLM 看图打标 → 缩略图同时挂到 thumbnail_url
- image：原图直接喂 LLM
- audio：跳过 LLM，给一组 placeholder 标
- 任何 LLM 失败：fallback 到 mock 标，不阻断上传
- 并发：files > 1 时用 asyncio.gather 并行打标
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from ..config import get_settings
from ..schemas import Material, MaterialUploadResponse, SectionKind, VideoType, kinds_for_video_type
from ..services.llm_client import LLMError, get_llm_client
from ..services.materials import material_store
from ..services.video.ffmpeg import FFmpegError, extract_frame, ffmpeg_available

log = logging.getLogger("seecript.material")
router = APIRouter()

_ALLOWED_VIDEO = {"video/mp4", "video/quicktime", "video/webm"}
_ALLOWED_IMAGE = {"image/jpeg", "image/png", "image/webp"}
_ALLOWED_AUDIO = {"audio/mpeg", "audio/wav", "audio/x-wav"}
_MAX_BYTES = 50 * 1024 * 1024  # 50MB 单文件硬上限

_MATERIAL_TAG_SYSTEM = (
    "你是短视频素材打标 Agent。看一帧画面，返回 JSON：\n"
    "{\"tags\": [string]（3-5 个，物体/场景/构图/风格关键词），"
    "\"recommended_section\": string（必须从 allowed_sections 里选一个）}。\n"
    "字段名 frame_tags / material_tag 是 mock 路由用，不要漏。"
)


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


def _placeholder_tags(media_type: str, video_type: VideoType) -> tuple[list[str], SectionKind]:
    """LLM 不可用 / audio / 调用失败 时的兜底标。"""
    allowed = kinds_for_video_type(video_type)
    fallback_section = allowed[1] if len(allowed) >= 2 else allowed[0]  # 主体段
    if media_type == "audio":
        return ["[auto] 音频素材", "[auto] 口播/BGM 候选"], fallback_section  # type: ignore[return-value]
    return ["[auto] 待打标", "[auto] 通用素材"], fallback_section  # type: ignore[return-value]


async def _tag_with_llm(
    image_path: Path,
    media_type: str,
    video_type: VideoType,
) -> tuple[list[str], SectionKind]:
    """单帧 → LLM 多模态打标。失败回落 placeholder。"""
    allowed = kinds_for_video_type(video_type)
    user_text = (
        f"video_type={video_type}\n"
        f"allowed_sections={list(allowed)}\n"
        f"media_type={media_type}\n"
        "请按 system 中的 schema 返回 JSON。"
    )
    try:
        client = get_llm_client()
        text = await client.complete_multimodal(
            _MATERIAL_TAG_SYSTEM, user_text, [image_path],
        )
    except LLMError as exc:
        log.warning("[material] LLM tagging failed (%s) → placeholder", exc)
        return _placeholder_tags(media_type, video_type)
    except Exception as exc:  # 网络抖动等
        log.warning("[material] LLM tagging unexpected error: %s → placeholder", exc)
        return _placeholder_tags(media_type, video_type)

    try:
        data = json.loads(text) if text.strip().startswith(("{", "[")) else None
    except json.JSONDecodeError:
        data = None
    if not isinstance(data, dict):
        # mock 路径可能返回 frame_tags 包裹的结构
        return _placeholder_tags(media_type, video_type)

    # 兼容两种形态：直接 {tags, recommended_section} 或 mock 的 {frame_tags: [{tags, ...}]}
    raw_tags: list = []
    raw_section: Optional[str] = None
    if isinstance(data.get("tags"), list):
        raw_tags = data["tags"]
        raw_section = data.get("recommended_section")
    elif isinstance(data.get("frame_tags"), list) and data["frame_tags"]:
        first = data["frame_tags"][0]
        if isinstance(first, dict):
            raw_tags = first.get("tags") or []
            raw_section = first.get("recommended_section")

    tags = [str(t)[:30] for t in raw_tags if t][:5]
    if not tags:
        tags, default_section = _placeholder_tags(media_type, video_type)
        return tags, default_section
    section: SectionKind
    if isinstance(raw_section, str) and raw_section in allowed:
        section = raw_section  # type: ignore[assignment]
    else:
        section = allowed[1] if len(allowed) >= 2 else allowed[0]  # type: ignore[assignment]
    return tags, section


async def _build_material(
    file: UploadFile,
    target_dir: Path,
    sid: str,
    sort_order: int,
    video_type: VideoType,
) -> Material:
    media_type = _detect_media_type(file.content_type)
    if media_type is None:
        raise HTTPException(status_code=415, detail=f"unsupported content-type: {file.content_type}")
    data = await file.read()
    if len(data) > _MAX_BYTES:
        raise HTTPException(status_code=413, detail=f"{file.filename} exceeds 50MB")
    safe_name = Path(file.filename or "unnamed").name
    material_id = uuid.uuid4().hex[:12]
    dest = target_dir / f"{material_id}_{safe_name}"
    dest.write_bytes(data)
    log.info("[material] session=%s saved %s (%d bytes, %s)",
             sid, dest.name, len(data), media_type)

    # 缩略图：image 直接用自身；video 抽首帧到 .jpg；audio 没有缩略图
    thumbnail_path: Optional[Path] = None
    thumbnail_url: Optional[str] = None
    if media_type == "image":
        thumbnail_path = dest
        thumbnail_url = f"/uploads/{sid}/{dest.name}"
    elif media_type == "video":
        thumb = target_dir / f"{material_id}_thumb.jpg"
        if ffmpeg_available():
            try:
                await asyncio.to_thread(extract_frame, dest, 0.5, thumb)
                thumbnail_path = thumb
                thumbnail_url = f"/uploads/{sid}/{thumb.name}"
            except FFmpegError as exc:
                log.warning("[material] extract_frame failed for %s: %s", dest.name, exc)
        else:
            log.info("[material] ffmpeg unavailable; skip video thumbnail for %s", dest.name)

    # LLM 打标：有可读帧时走多模态，否则 placeholder
    if thumbnail_path is not None and media_type in ("image", "video"):
        tags, recommended_section = await _tag_with_llm(thumbnail_path, media_type, video_type)
    else:
        tags, recommended_section = _placeholder_tags(media_type, video_type)

    return Material(
        material_id=material_id,
        filename=safe_name,
        media_type=media_type,  # type: ignore[arg-type]
        duration_seconds=None,
        thumbnail_url=thumbnail_url,
        tags=tags,
        recommended_section=recommended_section,
        sort_order=sort_order,
    )


@router.post("/material/upload", response_model=MaterialUploadResponse)
async def upload_material(
    files: list[UploadFile] = File(...),
    session_id: str | None = Form(default=None),
    video_type: VideoType = Form(default="marketing"),
) -> MaterialUploadResponse:
    if not files:
        raise HTTPException(status_code=400, detail="no files")
    sid = session_id or uuid.uuid4().hex[:12]
    target_dir = _uploads_root() / sid
    target_dir.mkdir(parents=True, exist_ok=True)

    # 先看 store 里已有几条，sort_order 接在后面
    base_order = len(material_store.list(sid))
    tasks = [
        _build_material(f, target_dir, sid, base_order + idx, video_type)
        for idx, f in enumerate(files)
    ]
    materials = await asyncio.gather(*tasks)
    material_store.put(sid, list(materials))
    return MaterialUploadResponse(session_id=sid, materials=list(materials))
