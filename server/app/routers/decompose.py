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
from ..services.library import manifest_store
from ..services.video import ffmpeg as ffmpeg_util

log = logging.getLogger("seecript.decompose")
router = APIRouter()


# 内置样例视频的物理位置：server/samples/<sample_id>/video.mp4
_SAMPLES_ROOT = Path(__file__).resolve().parents[2] / "samples"

# 用户上传待拆解视频：server/var/uploads/decompose/<sample_id>/video.mp4
_USER_VIDEO_ALLOWED = {"video/mp4", "video/quicktime", "video/webm"}
_USER_VIDEO_MAX_BYTES = 200 * 1024 * 1024  # 单视频 200MB（比通用 material 50MB 宽松：拆解通常吃整段视频）
# 时长上限：3 分钟 + 20s 余量（容器/封装层可能比真实视频流多几秒，给点宽松）。
# 拒掉超时长视频是为了：① 防 LLM/ASR/T2V 配额浪费 ② 防 _segment_with_roles
# 在 50+ shots 下 token 飙升 ③ 给前端清晰的 UX 反馈（SSE 跑 5 分钟才报错很糟）。
_USER_VIDEO_MAX_DURATION_SECONDS = 200.0


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
    nl_prompt: Optional[str] = None,
    replace_slot: Optional[str] = None,
    persist: bool = False,
) -> None:
    try:
        await decompose(
            sample_id,
            job_id=job_id,
            video_type=video_type,
            video_path=video_path,
            reference_asset_ids=reference_asset_ids,
            nl_prompt=nl_prompt,
            replace_slot=replace_slot,
            persist=persist,
        )
    except Exception as exc:  # pragma: no cover
        log.exception("[%s] decompose failed: %s", job_id, exc)
        job_store.fail(job_id, str(exc))


@router.post("/decompose", response_model=DecomposeSubmitResponse)
async def submit_decompose(req: DecomposeRequest, bg: BackgroundTasks) -> DecomposeSubmitResponse:
    """触发拆解流水线。

    stage-15 起 persist 默认 False(草稿模式):跑完 SSE done 推 manifest 给前端 zustand,
    用户在 Decompose 页点「保存到资产库」时再调 POST /sample/{id}/manifest/save 入版本槽。
    persist=True 走老行为(直接写槽),仅供需要无人值守自动入库的内部场景。

    版本槽预校验仅在 persist=True 时跑(草稿模式根本不写槽):
    - 槽满(=MAX_VERSIONS)且未指定 replace_slot → 409,body 列出现有版本让前端弹「删哪个」
    - 槽未满但指定了 replace_slot → 422,避免无意义覆盖
    """
    real_path = _resolve_video_path(req.sample_id)
    # 草稿模式跳过槽位预校验——不写槽就不用提前拒绝。
    # persist=True 时仍按老逻辑预校验,免得跑完 2 分钟流水线撞墙。
    if req.persist:
        cur_count = manifest_store.version_count(req.sample_id)
        if req.replace_slot is None and cur_count >= manifest_store.MAX_VERSIONS:
            existing = manifest_store.list_versions(req.sample_id)
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "slots_full",
                    "message": f"sample {req.sample_id} 已有 {cur_count} 个版本,请先选一个删除",
                    "max_versions": manifest_store.MAX_VERSIONS,
                    "versions": [
                        {
                            "slot_id": v.slot_id,
                            "updated_at": v.updated_at,
                            "is_active": v.is_active,
                        }
                        for v in existing
                    ],
                },
            )
        if req.replace_slot is not None:
            if cur_count < manifest_store.MAX_VERSIONS:
                raise HTTPException(
                    status_code=422,
                    detail=f"slot 还有空位({cur_count}/{manifest_store.MAX_VERSIONS}),不应传 replace_slot",
                )
            valid_ids = {v.slot_id for v in manifest_store.list_versions(req.sample_id)}
            if req.replace_slot not in valid_ids:
                raise HTTPException(
                    status_code=404,
                    detail=f"replace_slot={req.replace_slot} 不存在",
                )

    job_id = job_store.create(
        "decompose",
        payload={
            "sample_id": req.sample_id,
            "video_type": req.video_type,
            "video_path": str(real_path) if real_path else None,
            "reference_asset_ids": list(req.reference_asset_ids or []),
            "nl_prompt": req.nl_prompt,
            "replace_slot": req.replace_slot,
            "persist": req.persist,
        },
    )
    bg.add_task(
        _run_decompose,
        job_id,
        req.sample_id,
        req.video_type,
        str(real_path) if real_path else None,
        list(req.reference_asset_ids or []),
        req.nl_prompt,
        req.replace_slot,
        req.persist,
    )
    return DecomposeSubmitResponse(job_id=job_id)


class DecomposeUploadResponse(BaseModel):
    """`POST /api/decompose/upload` 返回——前端拿到 sample_id 后再调 /api/decompose 提交拆解。"""

    sample_id: str
    filename: str
    size_bytes: int
    video_url: str


@router.post("/decompose/upload", response_model=DecomposeUploadResponse)
async def upload_for_decompose(
    file: UploadFile = File(...),
    video_type: VideoType = Form(default="marketing"),
    title: Optional[str] = Form(default=None),
) -> DecomposeUploadResponse:
    """用户上传一段自己的视频，落到 var/uploads/decompose/<sample_id>/video.mp4，
    返回 sample_id 给前端再走 /api/decompose 流水线。

    - 仅接受 video/mp4 | video/quicktime | video/webm
    - 单文件硬上限 200MB
    - sample_id 形如 user-<hex>，决不会碰 server/samples 内置目录
    - video_type / title 写入 meta.json，供 /library?source=user 列出
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

    # 时长校验：ffprobe 拿真实秒数（容器头里 metadata 不一定准），>3min+20s 余量直接退还。
    # ffprobe 不可用时（开发机没装 ffmpeg）放过，后续真链路会再撞同样的问题；
    # 比起在上传环节卡死开发者，让流水线自己降级更友好。
    try:
        probe_info = ffmpeg_util.probe(target_path)
        duration = probe_info.duration_seconds
    except (ffmpeg_util.FFmpegError, FileNotFoundError, OSError) as exc:
        log.warning("[decompose.upload] ffprobe failed for %s: %s; 跳过时长校验", target_path, exc)
        duration = None

    if duration is not None and duration > _USER_VIDEO_MAX_DURATION_SECONDS:
        # 删文件 + 空目录回收，防止 var/uploads/decompose 下堆超时长样例
        try:
            target_path.unlink(missing_ok=True)
            target_dir.rmdir()
        except OSError:
            pass
        raise HTTPException(
            status_code=413,
            detail=(
                f"视频时长 {duration:.1f}s 超过 3 分钟上限"
                f"（最长 {_USER_VIDEO_MAX_DURATION_SECONDS:.0f}s）"
            ),
        )

    # 元数据：让 /library?source=user 能列出来（title/video_type/size）
    safe_title = (title or Path(file.filename or "video.mp4").stem)[:80]
    import time
    meta = {
        "sample_id": sample_id,
        "title": safe_title,
        "video_type": video_type,
        "filename": Path(file.filename or "video.mp4").name,
        "size_bytes": len(data),
        "uploaded_at": time.time(),
    }
    (target_dir / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    log.info(
        "[decompose.upload] sample=%s type=%s saved %s (%d bytes)",
        sample_id,
        video_type,
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
