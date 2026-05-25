"""Stage 3 · 视频渲染流水线编排。

输入：`Plan`（含 main_track / packaging_track / bgm）
输出：`RenderResult`（final.mp4 路径 + 封面路径 + 各阶段耗时统计）

六步进度条（与前端动画相同节奏）：
  prepare        → 8%
  ffmpeg_concat  → 28%
  seedance       → 48%
  remotion       → 70%
  overlay        → 88%
  finalize       → 99%

每一步都设有 try / fallback：依赖缺失时写 mock 占位文件，让 demo 顺利演到收尾。
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

from ...config import get_settings
from ...schemas import Plan
from ..jobs import job_store
from ..video import ffmpeg as ffmpeg_svc
from ..video import remotion as remotion_svc
from .seedance_chain import extend_with_seedance

log = logging.getLogger("seecript.render.pipeline")


@dataclass
class RenderResult:
    job_id: str
    plan_id: str
    variant: str
    video_path: Path
    cover_path: Path
    video_url: str
    cover_url: str
    duration_seconds: float
    timings_ms: dict[str, int] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


# ----------------------------- 工具 ---------------------------------------

def _outputs_root() -> Path:
    settings = get_settings()
    root = settings.log_dir.parent / "var" / "outputs"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _uploads_root() -> Path:
    settings = get_settings()
    return settings.log_dir.parent / "var" / "uploads"


def _resolve_scene_path(plan: Plan, source_ref: str) -> Path | None:
    """从 source_ref 里反查素材实际路径。session_id 在 Plan 上可选携带。"""
    if plan.session_id:
        d = _uploads_root() / plan.session_id
        if d.exists():
            for f in d.iterdir():
                if source_ref in f.name:
                    return f
    return None


def _touch_placeholder(dst: Path, content: bytes = b"") -> Path:
    """生成 0 字节占位 mp4/webm/jpg，用于 mock 模式下保持流水线串得起来。"""
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_bytes(content)
    return dst


# ----------------------------- 流水线 -------------------------------------

async def run_pipeline(job_id: str, plan: Plan) -> RenderResult:
    """从 Plan → final.mp4。同步耗时操作放线程池，progress 用 job_store 推送。"""
    out_dir = _outputs_root() / job_id
    out_dir.mkdir(parents=True, exist_ok=True)
    timings: dict[str, int] = {}
    notes: list[str] = []

    # ---- Step 1 · prepare ----
    t0 = time.time()
    job_store.publish(job_id, "prepare", 8.0, {"note": "校验 Plan + 准备工作目录"})
    if not plan.main_track:
        raise ValueError("plan has empty main_track")
    notes.append(f"plan_id={plan.plan_id} scenes={len(plan.main_track)} duration={plan.duration_seconds}s")
    timings["prepare_ms"] = int((time.time() - t0) * 1000)

    # ---- Step 2 · ffmpeg concat (main track) ----
    t0 = time.time()
    job_store.publish(job_id, "ffmpeg_concat", 28.0, {"note": "FFmpeg 拼接主轨"})
    main_path = out_dir / "main.mp4"
    inputs: list[Path] = []
    for sc in plan.main_track:
        p = _resolve_scene_path(plan, sc.source_ref)
        if p is not None:
            inputs.append(p)
    if inputs and ffmpeg_svc.ffmpeg_available():
        try:
            await asyncio.to_thread(ffmpeg_svc.concat, inputs, main_path, reencode=True)
        except ffmpeg_svc.FFmpegError as exc:
            log.warning("[%s] concat failed, falling back to mock: %s", job_id, exc)
            notes.append(f"concat fallback: {exc}")
            _touch_placeholder(main_path)
    else:
        notes.append(f"ffmpeg unavailable or no inputs (n={len(inputs)}); mock main.mp4")
        _touch_placeholder(main_path)
    timings["concat_ms"] = int((time.time() - t0) * 1000)

    # ---- Step 3 · Seedance 长视频扩展 ----
    t0 = time.time()
    job_store.publish(job_id, "seedance_extend", 48.0, {"note": "Seedance 首尾帧扩展"})
    try:
        extended_path = await extend_with_seedance(main_path, plan.duration_seconds, job_id=job_id)
    except Exception as exc:  # noqa: BLE001 — graceful
        log.warning("[%s] seedance chain failed: %s", job_id, exc)
        notes.append(f"seedance fallback: {exc}")
        extended_path = main_path
    timings["seedance_ms"] = int((time.time() - t0) * 1000)

    # ---- Step 4 · Remotion 包装轨 ----
    t0 = time.time()
    job_store.publish(job_id, "remotion_render", 70.0, {"note": "Remotion 渲染包装轨"})
    packaging_path = out_dir / "packaging.webm"
    pkg_props = {
        "durationInSeconds": plan.duration_seconds,
        "items": [item.model_dump() for item in plan.packaging_track],
    }
    if plan.packaging_track and remotion_svc.remotion_available():
        try:
            await asyncio.to_thread(remotion_svc.render_packaging_track, pkg_props, packaging_path)
        except (remotion_svc.RemotionError, FileNotFoundError) as exc:
            log.warning("[%s] remotion render failed, falling back: %s", job_id, exc)
            notes.append(f"remotion fallback: {exc}")
            _touch_placeholder(packaging_path)
    else:
        notes.append(f"remotion unavailable or empty packaging (n={len(plan.packaging_track)}); mock packaging.webm")
        _touch_placeholder(packaging_path)
    timings["remotion_ms"] = int((time.time() - t0) * 1000)

    # ---- Step 5 · ffmpeg overlay ----
    t0 = time.time()
    job_store.publish(job_id, "ffmpeg_overlay", 88.0, {"note": "FFmpeg overlay 合成"})
    overlaid_path = out_dir / "overlaid.mp4"
    if (
        ffmpeg_svc.ffmpeg_available()
        and extended_path.exists() and extended_path.stat().st_size > 0
        and packaging_path.exists() and packaging_path.stat().st_size > 0
    ):
        try:
            await asyncio.to_thread(
                ffmpeg_svc.overlay, extended_path, packaging_path, overlaid_path, position="0:0"
            )
        except ffmpeg_svc.FFmpegError as exc:
            log.warning("[%s] overlay failed: %s", job_id, exc)
            notes.append(f"overlay fallback: {exc}")
            _touch_placeholder(overlaid_path)
    else:
        notes.append("overlay skipped (missing inputs or ffmpeg); passthrough")
        # 直接把 extended_path 复制成 overlaid_path（mock 时都是空文件，无所谓）
        if extended_path.exists():
            overlaid_path.write_bytes(extended_path.read_bytes())
        else:
            _touch_placeholder(overlaid_path)

    timings["overlay_ms"] = int((time.time() - t0) * 1000)

    # ---- Step 6 · finalize：BGM 混音 + 封面抽帧 ----
    t0 = time.time()
    job_store.publish(job_id, "finalize", 99.0, {"note": "封面抽帧 + BGM 混音"})
    final_path = out_dir / "final.mp4"
    bgm_track = plan.bgm.track_url if plan.bgm else None
    bgm_local: Path | None = None
    if bgm_track and bgm_track.startswith("/"):
        candidate = Path(_outputs_root().parent.parent) / bgm_track.lstrip("/")
        if candidate.exists():
            bgm_local = candidate

    if (
        ffmpeg_svc.ffmpeg_available()
        and bgm_local is not None
        and overlaid_path.exists() and overlaid_path.stat().st_size > 0
    ):
        try:
            await asyncio.to_thread(
                ffmpeg_svc.mix_bgm, overlaid_path, bgm_local, final_path,
                bgm_volume=plan.bgm.volume if plan.bgm else 0.6,
            )
        except ffmpeg_svc.FFmpegError as exc:
            log.warning("[%s] mix_bgm failed: %s", job_id, exc)
            notes.append(f"mix_bgm fallback: {exc}")
            final_path.write_bytes(overlaid_path.read_bytes() if overlaid_path.exists() else b"")
    else:
        # 无 BGM 或缺 ffmpeg：直接 rename
        notes.append("bgm mix skipped; using overlaid output as final")
        if overlaid_path.exists():
            final_path.write_bytes(overlaid_path.read_bytes())
        else:
            _touch_placeholder(final_path)

    cover_path = out_dir / "cover.jpg"
    if ffmpeg_svc.ffmpeg_available() and final_path.exists() and final_path.stat().st_size > 0:
        try:
            await asyncio.to_thread(ffmpeg_svc.extract_frame, final_path, 0.5, cover_path)
        except ffmpeg_svc.FFmpegError as exc:
            log.warning("[%s] cover extract failed: %s", job_id, exc)
            notes.append(f"cover fallback: {exc}")
            _touch_placeholder(cover_path)
    else:
        notes.append("cover frame skipped; ffmpeg or video missing")
        _touch_placeholder(cover_path)

    timings["finalize_ms"] = int((time.time() - t0) * 1000)

    return RenderResult(
        job_id=job_id,
        plan_id=plan.plan_id,
        variant=plan.variant,
        video_path=final_path,
        cover_path=cover_path,
        video_url=f"/outputs/{job_id}/final.mp4",
        cover_url=f"/outputs/{job_id}/cover.jpg",
        duration_seconds=plan.duration_seconds,
        timings_ms=timings,
        notes=notes,
    )
