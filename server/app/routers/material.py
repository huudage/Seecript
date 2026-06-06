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
from typing import Any, Optional

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from ..config import get_settings
from ..schemas import Material, MaterialUploadResponse, SectionRole, VideoType, all_role_names
from ..services.llm_client import LLMError, get_llm_client
from ..services.materials import material_store
from ..services.materials.preprocess import dispatch as dispatch_preprocess
from ..services.video.ffmpeg import FFmpegError, extract_frame, ffmpeg_available

log = logging.getLogger("seecript.material")
router = APIRouter()

_ALLOWED_VIDEO = {"video/mp4", "video/quicktime", "video/webm"}
_ALLOWED_IMAGE = {"image/jpeg", "image/png", "image/webp"}
_ALLOWED_AUDIO = {"audio/mpeg", "audio/wav", "audio/x-wav"}
_MAX_BYTES = 50 * 1024 * 1024  # 50MB 单文件硬上限

# Stage-16：允许 5 模式下任何静态 role 名（step_N/item_N 走正则兜底）。
# 上传时不知道用户最终用哪个 pattern，所以只过滤"明显非法"的字符串。
_ALLOWED_ROLES: tuple[str, ...] = tuple(all_role_names())

_MATERIAL_TAG_SYSTEM = (
    "你是短视频素材打标 Agent。看一帧画面，返回 JSON：\n"
    "{\"tags\": [string]（3-5 个，物体/场景/构图/风格关键词），"
    "\"recommended_section\": string（必须从 allowed_sections 里选一个 role；"
    "若 allowed_sections 含动态后缀如 step_*/item_*，可输出 step_1/item_2 这种带序号形式；"
    "若不确定使用第一个 target_role 作为默认值），"
    "\"highlight_score\": number（0.0-1.0；0.8+ 强冲击/可做开场或峰值，"
    "0.5-0.8 标准镜头适合中段，<0.5 仅 B-roll），"
    "\"highlight_reason\": string（一句话理由：构图/动作/情绪/光线，≤20 字）}。\n"
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


def _placeholder_tags(media_type: str, video_type: VideoType) -> tuple[list[str], SectionRole, float, str]:
    """LLM 不可用 / audio / 调用失败 时的兜底标。返回 (tags, role, highlight_score, highlight_reason).

    role 默认 development（主体段），video_type 仅作日志/语义提示保留，不再决定 role 集合。
    """
    if media_type == "audio":
        return ["[auto] 音频素材", "[auto] 口播/BGM 候选"], "development", 0.3, "[auto] 音频无画面评分"
    return ["[auto] 待打标", "[auto] 通用素材"], "development", 0.5, "[auto] LLM 不可用，给中位分"


async def _tag_with_llm(
    image_path: Path,
    media_type: str,
    video_type: VideoType,
) -> tuple[list[str], SectionRole, float, str]:
    """单帧 → LLM 多模态打标。失败回落 placeholder。返回 (tags, role, highlight_score, highlight_reason)."""
    user_text = (
        f"video_type={video_type}\n"
        f"allowed_sections={list(_ALLOWED_ROLES)}\n"
        f"media_type={media_type}\n"
        "请按 system 中的 schema 返回 JSON，highlight_score 必须给一个 0.0-1.0 的数。"
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

    # 兼容两种形态：直接 {tags, recommended_section, highlight_score} 或 mock 的 {frame_tags: [{tags, ...}]}
    raw_tags: list = []
    raw_section: Optional[str] = None
    raw_score: Any = None
    raw_reason: Optional[str] = None
    if isinstance(data.get("tags"), list):
        raw_tags = data["tags"]
        raw_section = data.get("recommended_section")
        raw_score = data.get("highlight_score")
        raw_reason = data.get("highlight_reason")
    elif isinstance(data.get("frame_tags"), list) and data["frame_tags"]:
        first = data["frame_tags"][0]
        if isinstance(first, dict):
            raw_tags = first.get("tags") or []
            raw_section = first.get("recommended_section")
            raw_score = first.get("highlight_score")
            raw_reason = first.get("highlight_reason")

    tags = [str(t)[:30] for t in raw_tags if t][:5]
    if not tags:
        return _placeholder_tags(media_type, video_type)

    # role 校验：必须是 17 个静态 role 之一或 step_N/item_N 形式；否则回落 development
    import re as _re
    role: SectionRole
    if isinstance(raw_section, str):
        cleaned = _re.sub(r"^(step|item)\s*0*(\d+)$", r"\1_\2", raw_section.strip())
        if cleaned in _ALLOWED_ROLES or _re.match(r"^(step|item)_\d+$", cleaned):
            role = cleaned
        else:
            role = "development"
    else:
        role = "development"

    # highlight_score 容错：可能是字符串 "0.8" 或越界数；兜成 [0,1] float
    try:
        score = float(raw_score) if raw_score is not None else 0.5
    except (TypeError, ValueError):
        score = 0.5
    score = max(0.0, min(1.0, score))
    reason = str(raw_reason)[:60] if isinstance(raw_reason, str) and raw_reason.strip() else "LLM 未给理由"

    return tags, role, score, reason


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
        tags, recommended_section, highlight_score, highlight_reason = await _tag_with_llm(
            thumbnail_path, media_type, video_type
        )
    else:
        tags, recommended_section, highlight_score, highlight_reason = _placeholder_tags(media_type, video_type)

    return Material(
        material_id=material_id,
        filename=safe_name,
        media_type=media_type,  # type: ignore[arg-type]
        duration_seconds=None,
        thumbnail_url=thumbnail_url,
        file_url=f"/uploads/{sid}/{dest.name}",
        tags=tags,
        recommended_section=recommended_section,
        highlight_score=highlight_score,
        highlight_reason=highlight_reason,
        sort_order=sort_order,
        preprocess_status="pending" if media_type == "video" else "skipped",
    )


@router.post("/material/upload", response_model=MaterialUploadResponse)
async def upload_material(
    files: list[UploadFile] = File(...),
    project_id: str | None = Form(default=None),
    session_id: str | None = Form(default=None),  # 兼容老前端：等价 project_id
    video_type: VideoType = Form(default="marketing"),
) -> MaterialUploadResponse:
    if not files:
        raise HTTPException(status_code=400, detail="no files")
    # v2 起 session_id == project_id；老前端只传 session_id 时仍可工作，
    # 但不再 mint 随机 sid——必须有显式 project_id / session_id，避免跨项目串货。
    sid = (project_id or session_id or "").strip()
    if not sid:
        raise HTTPException(status_code=400, detail="project_id 必填（session_id 作为兼容别名亦可）")
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

    # 视频素材入队预处理（PySceneDetect + VLM caption）；非视频跳过。
    # 不 await——预处理是后台任务，几十秒到几分钟，前端走 GET /api/material/{id}/preprocess 轮询。
    for m in materials:
        if m.media_type == "video":
            local = target_dir / f"{m.material_id}_{m.filename}"
            try:
                dispatch_preprocess(sid, m.material_id, local)
            except Exception as exc:  # noqa: BLE001
                log.warning("[material] dispatch preprocess failed material=%s: %s",
                            m.material_id, exc)

    return MaterialUploadResponse(session_id=sid, materials=list(materials))


@router.get("/material/{material_id}/preprocess", response_model=Material)
async def get_material_preprocess(material_id: str, project_id: str) -> Material:
    """前端轮询视频预处理进度（preprocess_status / shots）。

    project_id 必填——MaterialStore 按 project 分区，跨 project 不允许查询。
    返回 200 + 完整 Material；status 字段是 pending / running / ready / failed / skipped。
    """
    sid = project_id.strip()
    if not sid:
        raise HTTPException(status_code=400, detail="project_id 必填")
    m = material_store.get(sid, material_id)
    if m is None:
        raise HTTPException(status_code=404, detail=f"material {material_id} not found in project {sid}")
    return m


@router.get("/material", response_model=list[Material])
async def list_materials(project_id: str) -> list[Material]:
    """列出某 project 已上传的素材。

    刷新页面后前端用它回灌素材库（in-memory MaterialStore，进程重启清空——本期接受）。
    project_id 在 store 内即 session_id 别名（详见 upload_material）。
    """
    sid = project_id.strip()
    if not sid:
        raise HTTPException(status_code=400, detail="project_id 必填")
    return material_store.list(sid)
