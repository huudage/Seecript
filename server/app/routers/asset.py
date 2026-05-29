"""Module · 用户长期素材库（Asset Library）。

与 `material.py` 的 session 级 MaterialStore 严格区分——这里管的是用户在跨 session
反复复用的 BGM / 参考图 / 参考视频。

Endpoints（全部 prefix=/api）：
- POST   /asset/upload         multipart：file + kind[+title+tags] → 落盘 + sha256 去重 + 后台探测
- GET    /asset/library        kind?/q?/tag? 过滤 + 最近使用倒序
- GET    /asset/{asset_id}     单条详情
- PATCH  /asset/{asset_id}     改 title/description/tags
- DELETE /asset/{asset_id}     删条目（含文件 + meta + 缩略 + 抽帧）
- POST   /asset/{asset_id}/touch    使用打卡（plan/render 用了哪条就 +1）

Background：上传后 sync 部分仅做"落盘 + status=processing"，重的 ffprobe / Pillow
缩略 / 抽帧都丢到 BackgroundTask，让 UI 立刻看到这条新资产再变 ready/failed。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, File, Form, HTTPException, UploadFile

from ..config import get_settings
from ..schemas import (
    Asset,
    AssetKind,
    AssetListResponse,
    AssetUpdateRequest,
)
from ..services.assets import asset_store
from ..services.video import ffmpeg as ffmpeg_svc
from ..services.video.audio_analysis import analyze_audio, backend_name as audio_backend

log = logging.getLogger("seecript.asset")
router = APIRouter()


# ---------------------------------------------------------------------------
# MIME / size 校验
# ---------------------------------------------------------------------------
_ALLOWED_BGM = {"audio/mpeg", "audio/mp3", "audio/wav", "audio/x-wav", "audio/aac", "audio/m4a", "audio/mp4"}
_ALLOWED_IMAGE = {"image/jpeg", "image/png", "image/webp"}
_ALLOWED_VIDEO = {"video/mp4", "video/quicktime", "video/webm"}

_MAX_BYTES_BGM = 30 * 1024 * 1024       # 30MB BGM 上限
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


# ---------------------------------------------------------------------------
# 路径辅助
# ---------------------------------------------------------------------------
def _assets_local_root() -> Path:
    settings = get_settings()
    root = settings.log_dir.parent / "var" / "assets" / "local"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _kind_dir(kind: AssetKind) -> Path:
    d = _assets_local_root() / kind
    d.mkdir(parents=True, exist_ok=True)
    return d


def _public_url(asset: Asset, sub_path: str = "") -> str:
    """生成 `/assets/local/<kind>/<file>` 形式 URL。sub_path 为空则用 asset.file_url。"""
    if not sub_path:
        return asset.file_url
    return f"/assets/{asset.owner}/{asset.kind}/{sub_path}"


def _safe_ext(file_name: str, default: str) -> str:
    ext = Path(file_name).suffix.lower().strip(".")
    return ext or default


# ---------------------------------------------------------------------------
# 后台元数据探测
# ---------------------------------------------------------------------------
def _probe_bgm(asset_id: str, file_path: Path) -> None:
    """BGM 后台：ffprobe duration / sample_rate / channels + librosa tempo（可选）。"""
    metadata: dict = {}
    try:
        info = ffmpeg_svc.probe(file_path)
        metadata["duration_seconds"] = round(info.duration_seconds, 3)
    except Exception as exc:  # noqa: BLE001
        log.warning("[asset.bgm] ffprobe failed id=%s: %s", asset_id, exc)

    # tempo / peak 探测：librosa 缺失时跳过，不阻塞 ready
    try:
        if audio_backend() == "librosa":
            profile = analyze_audio(file_path)
            metadata["tempo_bpm"] = round(profile.tempo_bpm, 1)
            # 峰值时刻：rms_energy 最大值对应的 times
            if profile.rms_energy and profile.times:
                peak_idx = max(range(len(profile.rms_energy)), key=lambda i: profile.rms_energy[i])
                metadata["peak_at_seconds"] = round(profile.times[peak_idx], 2)
            metadata.setdefault("duration_seconds", round(profile.duration_seconds, 3))
    except Exception as exc:  # noqa: BLE001
        log.warning("[asset.bgm] librosa analyze failed id=%s: %s", asset_id, exc)

    asset_store.set_status(asset_id, "ready", metadata=metadata)
    log.info("[asset.bgm] ready id=%s meta=%s", asset_id, metadata)


def _probe_image(asset_id: str, file_path: Path) -> None:
    """图片后台：Pillow 读宽高 + 生成 256px 缩略图。"""
    metadata: dict = {}
    try:
        from PIL import Image  # type: ignore
        with Image.open(file_path) as im:
            metadata["width"] = im.width
            metadata["height"] = im.height
            # 缩略图（同目录，<asset_id>.thumb.jpg）
            im.thumbnail((256, 256))
            thumb_path = file_path.parent / f"{asset_id}.thumb.jpg"
            # JPEG 不支持 alpha，转 RGB 落盘
            if im.mode in ("RGBA", "LA", "P"):
                im = im.convert("RGB")
            im.save(thumb_path, format="JPEG", quality=85)
            metadata["thumbnail_url"] = f"/assets/local/reference_image/{thumb_path.name}"
    except ImportError:
        log.warning("[asset.image] Pillow 未安装，跳过缩略图")
    except Exception as exc:  # noqa: BLE001
        log.warning("[asset.image] thumbnail failed id=%s: %s", asset_id, exc)

    asset_store.set_status(asset_id, "ready", metadata=metadata)
    log.info("[asset.image] ready id=%s meta=%s", asset_id, metadata)


def _probe_video(asset_id: str, file_path: Path) -> None:
    """参考视频后台：ffprobe + 首帧缩略 + 均匀抽 8 帧供多模态参考。"""
    metadata: dict = {}
    parent_dir = file_path.parent

    try:
        info = ffmpeg_svc.probe(file_path)
        metadata["duration_seconds"] = round(info.duration_seconds, 3)
        metadata["width"] = info.width
        metadata["height"] = info.height
        metadata["fps"] = round(info.fps, 2)
    except Exception as exc:  # noqa: BLE001
        log.warning("[asset.video] ffprobe failed id=%s: %s", asset_id, exc)

    # 首帧缩略图
    try:
        thumb_path = parent_dir / f"{asset_id}.thumb.jpg"
        ffmpeg_svc.extract_frame(file_path, 0.5, thumb_path)
        metadata["thumbnail_url"] = f"/assets/local/reference_video/{thumb_path.name}"
    except Exception as exc:  # noqa: BLE001
        log.warning("[asset.video] thumb extract failed id=%s: %s", asset_id, exc)

    # 多模态参考帧：均匀抽 8 帧到子目录
    try:
        frames_dir = parent_dir / f"{asset_id}.frames"
        frames = ffmpeg_svc.extract_uniform_frames(file_path, frames_dir, count=8, prefix="frame")
        metadata["frame_urls"] = [
            f"/assets/local/reference_video/{frames_dir.name}/{p.name}" for p in frames
        ]
    except Exception as exc:  # noqa: BLE001
        log.warning("[asset.video] frames extract failed id=%s: %s", asset_id, exc)

    asset_store.set_status(asset_id, "ready", metadata=metadata)
    log.info("[asset.video] ready id=%s frames=%d", asset_id, len(metadata.get("frame_urls", [])))


def _dispatch_probe(asset: Asset, file_path: Path) -> None:
    """按 kind 路由到具体后台 probe。捕获顶层异常并落 failed 状态。"""
    try:
        if asset.kind == "bgm":
            _probe_bgm(asset.asset_id, file_path)
        elif asset.kind == "reference_image":
            _probe_image(asset.asset_id, file_path)
        elif asset.kind == "reference_video":
            _probe_video(asset.asset_id, file_path)
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
    title: Optional[str] = Form(default=None),
    description: Optional[str] = Form(default=None),
    tags: Optional[str] = Form(default=None, description="逗号分隔标签字符串"),
) -> Asset:
    """上传单个资产。

    流程：
    1. MIME / 大小校验（按 kind 路由）
    2. 读字节 → sha256 dedup（撞 hash 直接返回老 asset）
    3. 写盘 + 入 manifest（status=processing）
    4. 注册 BackgroundTask 跑探测
    5. 同步返回 Asset（前端立刻显示 loading 中卡片，SSE 后续可加）
    """
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

    # 撞 hash 直接复用，避免磁盘膨胀（前端会看到老 asset 直接出现）
    existing = asset_store.find_by_hash(content_hash)
    if existing is not None:
        asset_store.touch(existing.asset_id)
        log.info("[asset] dedup hit id=%s kind=%s name=%s", existing.asset_id, existing.kind, existing.file_name)
        return existing

    asset_id = asset_store.new_asset_id()
    safe_name = Path(file.filename or "asset").name
    # 文件名格式 ass-xxxxxxxx.ext，便于按 id 查找
    default_ext = {"bgm": "mp3", "reference_image": "jpg", "reference_video": "mp4"}[kind]
    ext = _safe_ext(safe_name, default_ext)
    target_path = _kind_dir(kind) / f"{asset_id}.{ext}"
    target_path.write_bytes(data)

    tag_list: list[str] = []
    if tags:
        tag_list = [t.strip()[:30] for t in tags.split(",") if t.strip()][:12]

    asset = Asset(
        asset_id=asset_id,
        owner="local",
        kind=kind,
        file_name=safe_name,
        file_url=f"/assets/local/{kind}/{target_path.name}",
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
        "[asset] uploaded id=%s kind=%s size=%d name=%s hash=%s...",
        asset_id, kind, len(data), safe_name, content_hash[:8],
    )

    # 后台跑重活；BackgroundTasks 在 response 发出后才执行
    background_tasks.add_task(_dispatch_probe, asset, target_path)
    return asset


@router.get("/asset/library", response_model=AssetListResponse)
async def list_assets(
    kind: Optional[AssetKind] = None,  # type: ignore[valid-type]
    q: Optional[str] = None,
    tag: Optional[str] = None,
) -> AssetListResponse:
    """列资产；按 kind/标题模糊/tag 过滤；最近使用倒序。"""
    items = asset_store.list(kind=kind, query=q, tag=tag)
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
    """plan/render 选用该资产时调一次，更新 last_used_at + use_count。"""
    a = asset_store.touch(asset_id)
    if a is None:
        raise HTTPException(status_code=404, detail=f"asset not found: {asset_id}")
    return a
