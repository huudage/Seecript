"""Module · 用户长期素材库（Asset Library）。

与 `material.py` 的 session 级 MaterialStore 严格区分——这里管的是用户在跨 session
反复复用的 BGM / 参考图 / 参考视频。

v2 起 owner = project_id：每个项目独立资产库，跨项目互不可见。

Endpoints（全部 prefix=/api）：
- POST   /asset/upload         multipart：file + kind + project_id[+title+tags] → 落盘 + sha256 去重 + 后台探测
- GET    /asset/library        project_id + kind?/q?/tag? 过滤 + 最近使用倒序
- GET    /asset/{asset_id}     单条详情（owner 由 store 内部反查）
- PATCH  /asset/{asset_id}     改 title/description/tags
- DELETE /asset/{asset_id}     删条目（含文件 + meta + 缩略 + 抽帧）
- POST   /asset/{asset_id}/touch    使用打卡（plan/render 用了哪条就 +1）

Background：上传后 sync 部分仅做"落盘 + status=processing"，重的 ffprobe / Pillow
缩略 / 抽帧都丢到 BackgroundTask，让 UI 立刻看到这条新资产再变 ready/failed。
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, File, Form, HTTPException, Query, UploadFile

from ..config import get_settings
from ..schemas import (
    Asset,
    AssetKind,
    AssetListResponse,
    AssetSaveFromUrlRequest,
    AssetUpdateRequest,
)
from ..services.assets import asset_store
from ..services.video import ffmpeg as ffmpeg_svc
from ..services.video.audio_analysis import backend_name as audio_backend
from ..services.video.bgm_analysis import analyze_bgm

log = logging.getLogger("seecript.asset")
router = APIRouter()


# ---------------------------------------------------------------------------
# MIME / size 校验
# ---------------------------------------------------------------------------
_ALLOWED_BGM = {"audio/mpeg", "audio/mp3", "audio/wav", "audio/x-wav", "audio/aac", "audio/m4a", "audio/mp4"}
_ALLOWED_IMAGE = {"image/jpeg", "image/png", "image/webp"}
_ALLOWED_VIDEO = {"video/mp4", "video/quicktime", "video/webm"}

_MAX_BYTES_BGM = 20 * 1024 * 1024       # 20MB BGM 上限（与前端 BGM 上传面板一致）
_MAX_BYTES_IMAGE = 15 * 1024 * 1024     # 15MB 图
_MAX_BYTES_VIDEO = 100 * 1024 * 1024    # 100MB 视频（比 material 大一点，参考素材常更长）


def _kind_constraints(kind: AssetKind) -> tuple[set[str], int]:
    if kind == "bgm":
        return _ALLOWED_BGM, _MAX_BYTES_BGM
    if kind == "reference_image":
        return _ALLOWED_IMAGE, _MAX_BYTES_IMAGE
    if kind == "reference_video":
        return _ALLOWED_VIDEO, _MAX_BYTES_VIDEO
    raise HTTPException(status_code=400, detail=f"unknown asset kind: {kind}")


def _require_project_id(project_id: Optional[str]) -> str:
    if not project_id or not project_id.strip():
        raise HTTPException(status_code=400, detail="project_id 必填")
    return project_id.strip()


# ---------------------------------------------------------------------------
# 路径辅助
# ---------------------------------------------------------------------------
def _assets_owner_root(owner: str) -> Path:
    settings = get_settings()
    root = settings.log_dir.parent / "var" / "assets" / owner
    root.mkdir(parents=True, exist_ok=True)
    return root


def _kind_dir(owner: str, kind: AssetKind) -> Path:
    d = _assets_owner_root(owner) / kind
    d.mkdir(parents=True, exist_ok=True)
    return d


def _safe_ext(file_name: str, default: str) -> str:
    ext = Path(file_name).suffix.lower().strip(".")
    return ext or default


# ---------------------------------------------------------------------------
# 后台元数据探测
# ---------------------------------------------------------------------------
def _probe_bgm(asset: Asset, file_path: Path) -> None:
    metadata: dict = {}
    try:
        info = ffmpeg_svc.probe(file_path)
        metadata["duration_seconds"] = round(info.duration_seconds, 3)
    except Exception as exc:  # noqa: BLE001
        log.warning("[asset.bgm] ffprobe failed id=%s: %s", asset.asset_id, exc)

    try:
        if audio_backend() == "librosa":
            profile = analyze_bgm(file_path)
            metadata["tempo_bpm"] = round(profile.tempo_bpm, 1)
            if profile.peak_seconds is not None:
                metadata["peak_at_seconds"] = round(profile.peak_seconds, 2)
            metadata.setdefault("duration_seconds", round(profile.duration_seconds, 3))
    except Exception as exc:  # noqa: BLE001
        log.warning("[asset.bgm] librosa analyze failed id=%s: %s", asset.asset_id, exc)

    asset_store.set_status(asset.asset_id, "ready", metadata=metadata)
    log.info("[asset.bgm] ready id=%s meta=%s", asset.asset_id, metadata)


def _probe_image(asset: Asset, file_path: Path) -> None:
    metadata: dict = {}
    try:
        from PIL import Image  # type: ignore
        with Image.open(file_path) as im:
            metadata["width"] = im.width
            metadata["height"] = im.height
            im.thumbnail((256, 256))
            thumb_path = file_path.parent / f"{asset.asset_id}.thumb.jpg"
            if im.mode in ("RGBA", "LA", "P"):
                im = im.convert("RGB")
            im.save(thumb_path, format="JPEG", quality=85)
            metadata["thumbnail_url"] = f"/assets/{asset.owner}/reference_image/{thumb_path.name}"
    except ImportError:
        log.warning("[asset.image] Pillow 未安装，跳过缩略图")
    except Exception as exc:  # noqa: BLE001
        log.warning("[asset.image] thumbnail failed id=%s: %s", asset.asset_id, exc)

    asset_store.set_status(asset.asset_id, "ready", metadata=metadata)
    log.info("[asset.image] ready id=%s meta=%s", asset.asset_id, metadata)


def _probe_video(asset: Asset, file_path: Path) -> None:
    metadata: dict = {}
    parent_dir = file_path.parent

    try:
        info = ffmpeg_svc.probe(file_path)
        metadata["duration_seconds"] = round(info.duration_seconds, 3)
        metadata["width"] = info.width
        metadata["height"] = info.height
        metadata["fps"] = round(info.fps, 2)
    except Exception as exc:  # noqa: BLE001
        log.warning("[asset.video] ffprobe failed id=%s: %s", asset.asset_id, exc)

    try:
        thumb_path = parent_dir / f"{asset.asset_id}.thumb.jpg"
        ffmpeg_svc.extract_frame(file_path, 0.5, thumb_path)
        metadata["thumbnail_url"] = f"/assets/{asset.owner}/reference_video/{thumb_path.name}"
    except Exception as exc:  # noqa: BLE001
        log.warning("[asset.video] thumb extract failed id=%s: %s", asset.asset_id, exc)

    try:
        frames_dir = parent_dir / f"{asset.asset_id}.frames"
        frames = ffmpeg_svc.extract_uniform_frames(file_path, frames_dir, count=8, prefix="frame")
        metadata["frame_urls"] = [
            f"/assets/{asset.owner}/reference_video/{frames_dir.name}/{p.name}" for p in frames
        ]
    except Exception as exc:  # noqa: BLE001
        log.warning("[asset.video] frames extract failed id=%s: %s", asset.asset_id, exc)

    asset_store.set_status(asset.asset_id, "ready", metadata=metadata)
    log.info("[asset.video] ready id=%s frames=%d", asset.asset_id, len(metadata.get("frame_urls", [])))


def _dispatch_probe(asset: Asset, file_path: Path) -> None:
    try:
        if asset.kind == "bgm":
            _probe_bgm(asset, file_path)
        elif asset.kind == "reference_image":
            _probe_image(asset, file_path)
        elif asset.kind == "reference_video":
            _probe_video(asset, file_path)
    except Exception as exc:  # noqa: BLE001
        log.exception("[asset] probe pipeline crashed id=%s: %s", asset.asset_id, exc)
        asset_store.set_status(asset.asset_id, "failed", error=str(exc)[:200])


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
@router.post("/asset/upload", response_model=Asset)
async def upload_asset(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    kind: AssetKind = Form(...),  # type: ignore[valid-type]
    project_id: str = Form(..., description="所属项目 ID"),
    title: Optional[str] = Form(default=None),
    description: Optional[str] = Form(default=None),
    tags: Optional[str] = Form(default=None, description="逗号分隔标签字符串"),
) -> Asset:
    """上传单个资产到指定项目的资产库。"""
    project_id = _require_project_id(project_id)
    allowed_mimes, max_bytes = _kind_constraints(kind)
    if file.content_type not in allowed_mimes:
        raise HTTPException(
            status_code=415,
            detail=f"unsupported content-type {file.content_type} for kind={kind}; allowed={sorted(allowed_mimes)}",
        )

    data = await file.read()
    if len(data) > max_bytes:
        raise HTTPException(status_code=413, detail=f"file exceeds {max_bytes // (1024*1024)}MB")
    if not data:
        raise HTTPException(status_code=400, detail="empty file")

    from ..services.assets.store import sha256_of_bytes
    content_hash = sha256_of_bytes(data)

    # 撞 hash 直接复用，避免磁盘膨胀（去重仅在该项目内生效）
    existing = asset_store.find_by_hash(project_id, content_hash)
    if existing is not None:
        asset_store.touch(existing.asset_id)
        log.info("[asset] dedup hit id=%s kind=%s name=%s project=%s",
                 existing.asset_id, existing.kind, existing.file_name, project_id)
        return existing

    asset_id = asset_store.new_asset_id()
    safe_name = Path(file.filename or "asset").name
    default_ext = {"bgm": "mp3", "reference_image": "jpg", "reference_video": "mp4"}[kind]
    ext = _safe_ext(safe_name, default_ext)
    target_path = _kind_dir(project_id, kind) / f"{asset_id}.{ext}"
    target_path.write_bytes(data)

    tag_list: list[str] = []
    if tags:
        tag_list = [t.strip()[:30] for t in tags.split(",") if t.strip()][:12]

    asset = Asset(
        asset_id=asset_id,
        owner=project_id,
        kind=kind,
        file_name=safe_name,
        file_url=f"/assets/{project_id}/{kind}/{target_path.name}",
        file_size=len(data),
        content_hash=content_hash,
        mime=file.content_type or "application/octet-stream",
        title=(title or safe_name)[:120],
        description=(description or "")[:500],
        tags=tag_list,
        metadata={},
        status="processing",
        error=None,
        created_at=time.time(),
        last_used_at=None,
        use_count=0,
    )
    asset_store.upsert(asset)
    log.info(
        "[asset] uploaded id=%s project=%s kind=%s size=%d name=%s hash=%s...",
        asset_id, project_id, kind, len(data), safe_name, content_hash[:8],
    )

    background_tasks.add_task(_dispatch_probe, asset, target_path)
    return asset


@router.get("/asset/library", response_model=AssetListResponse)
async def list_assets(
    project_id: str = Query(..., description="所属项目 ID"),
    kind: Optional[AssetKind] = None,  # type: ignore[valid-type]
    q: Optional[str] = None,
    tag: Optional[str] = None,
) -> AssetListResponse:
    """列指定项目的资产；按 kind/标题模糊/tag 过滤；最近使用倒序。"""
    project_id = _require_project_id(project_id)
    items = asset_store.list(project_id, kind=kind, query=q, tag=tag)
    return AssetListResponse(items=items, total=len(items))


@router.get("/asset/{asset_id}", response_model=Asset)
async def get_asset(asset_id: str) -> Asset:
    a = asset_store.get(asset_id)
    if a is None:
        raise HTTPException(status_code=404, detail=f"asset not found: {asset_id}")
    return a


@router.patch("/asset/{asset_id}", response_model=Asset)
async def update_asset(asset_id: str, body: AssetUpdateRequest) -> Asset:
    if asset_store.get(asset_id) is None:
        raise HTTPException(status_code=404, detail=f"asset not found: {asset_id}")
    patch = body.model_dump(exclude_unset=True)
    updated = asset_store.update_fields(asset_id, **patch)
    if updated is None:
        raise HTTPException(status_code=404, detail=f"asset not found: {asset_id}")
    return updated


@router.delete("/asset/{asset_id}")
async def delete_asset(asset_id: str) -> dict:
    ok = asset_store.delete(asset_id)
    if not ok:
        raise HTTPException(status_code=404, detail=f"asset not found: {asset_id}")
    return {"deleted": True, "asset_id": asset_id}


@router.post("/asset/{asset_id}/touch", response_model=Asset)
async def touch_asset(asset_id: str) -> Asset:
    a = asset_store.touch(asset_id)
    if a is None:
        raise HTTPException(status_code=404, detail=f"asset not found: {asset_id}")
    return a


@router.post("/asset/save-from-url", response_model=Asset)
async def save_asset_from_url(
    body: AssetSaveFromUrlRequest,
    background_tasks: BackgroundTasks,
) -> Asset:
    """把外部 URL 的图片 / 参考视频下载入库。

    主要场景：
    - Seedream 生成的临时图片 CDN（1h-7d 有效）→ 用户点『保存到素材库』
    - 用户输入一段外站参考视频 URL → 入库当作参考视频素材

    永久落盘 + 复用 _dispatch_probe 流程做缩略图 / 抽帧 / MIME 校验。
    禁 `bgm`：避免被滥用抓取音乐。
    """
    project_id = _require_project_id(body.project_id)
    if body.kind not in ("reference_image", "reference_video"):
        raise HTTPException(
            status_code=400,
            detail="save-from-url 仅支持 reference_image / reference_video",
        )
    url = body.url.strip()
    if not (url.startswith("http://") or url.startswith("https://")):
        raise HTTPException(status_code=400, detail="url 必须是 http/https")

    import httpx
    try:
        async with httpx.AsyncClient(timeout=60.0) as cli:
            resp = await cli.get(url)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"下载失败：{exc}") from exc
    if resp.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"下载失败 HTTP {resp.status_code}")
    data = resp.content
    if not data:
        raise HTTPException(status_code=502, detail="下载到空响应")

    allowed_mimes, max_bytes = _kind_constraints(body.kind)
    if len(data) > max_bytes:
        raise HTTPException(status_code=413, detail=f"file exceeds {max_bytes // (1024*1024)}MB")

    # 优先用响应 content-type；CDN 偶尔返回 application/octet-stream，按扩展名推断兜底。
    ct = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
    if ct not in allowed_mimes:
        # 按 url 后缀兜底
        suffix = Path(url.split("?")[0]).suffix.lower()
        if body.kind == "reference_video":
            ct = {
                ".mp4": "video/mp4",
                ".mov": "video/quicktime",
                ".webm": "video/webm",
                ".m4v": "video/mp4",
            }.get(suffix, "video/mp4")  # Seedance / 豆包 默认 mp4
        else:
            ct = {
                ".jpg": "image/jpeg",
                ".jpeg": "image/jpeg",
                ".png": "image/png",
                ".webp": "image/webp",
            }.get(suffix, "image/jpeg")  # Seedream 默认 jpeg

    from ..services.assets.store import sha256_of_bytes
    content_hash = sha256_of_bytes(data)
    existing = asset_store.find_by_hash(project_id, content_hash)
    if existing is not None:
        asset_store.touch(existing.asset_id)
        log.info("[asset.save-from-url] dedup id=%s project=%s", existing.asset_id, project_id)
        return existing

    asset_id = asset_store.new_asset_id()
    if body.kind == "reference_video":
        ext = {
            "video/mp4": "mp4",
            "video/quicktime": "mov",
            "video/webm": "webm",
        }.get(ct, "mp4")
        name_prefix = f"aigc-{asset_id}"
    else:
        ext = {
            "image/jpeg": "jpg",
            "image/png": "png",
            "image/webp": "webp",
        }.get(ct, "jpg")
        name_prefix = f"seedream-{asset_id}"
    safe_name = (body.title or name_prefix)[:80] + f".{ext}"
    target_path = _kind_dir(project_id, body.kind) / f"{asset_id}.{ext}"
    target_path.write_bytes(data)

    tag_list = body.tags or []

    asset = Asset(
        asset_id=asset_id,
        owner=project_id,
        kind=body.kind,
        file_name=safe_name,
        file_url=f"/assets/{project_id}/{body.kind}/{target_path.name}",
        file_size=len(data),
        content_hash=content_hash,
        mime=ct,
        title=(body.title or safe_name)[:120],
        description=f"来自外部 URL：{url[:200]}",
        tags=tag_list,
        metadata={"source_url": url[:500]},
        status="processing",
        error=None,
        created_at=time.time(),
        last_used_at=None,
        use_count=0,
    )
    asset_store.upsert(asset)
    log.info(
        "[asset.save-from-url] ok id=%s project=%s size=%d hash=%s...",
        asset_id, project_id, len(data), content_hash[:8],
    )
    background_tasks.add_task(_dispatch_probe, asset, target_path)
    return asset
