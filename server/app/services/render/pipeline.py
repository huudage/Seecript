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
import hashlib
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from ...config import get_settings
from ...schemas import Plan, Scene
from ..jobs import job_store
from ..video import ffmpeg as ffmpeg_svc
from ..video import remotion as remotion_svc

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


def _samples_root() -> Path:
    """server/samples/<sample_id>/video.mp4 —— 系统内置样例视频根目录。"""
    settings = get_settings()
    return settings.log_dir.parent / "samples"


def _aigc_cache_root() -> Path:
    """AIGC chunks 下载缓存：var/aigc_cache/<url-hash>.mp4。"""
    settings = get_settings()
    root = settings.log_dir.parent / "var" / "aigc_cache"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _aigc_chunk_local_path(url: str) -> Path:
    """同一 URL 命中本地缓存，避免重复下载。"""
    h = hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]
    return _aigc_cache_root() / f"chunk-{h}.mp4"


async def _download_aigc_chunk(url: str, *, timeout: float = 120.0) -> Path | None:
    """把 CDN URL 下到本地缓存。失败返回 None（caller 兜底处理）。"""
    dst = _aigc_chunk_local_path(url)
    if dst.exists() and dst.stat().st_size > 0:
        return dst
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            dst.write_bytes(resp.content)
    except Exception as exc:  # noqa: BLE001
        log.warning("[render] aigc chunk download failed url=%s err=%s", url, exc)
        return None
    return dst if dst.stat().st_size > 0 else None


async def _resolve_aigc_scene(scene: Scene, segments_dir: Path, idx: int) -> Path | None:
    """下载 scene.aigc_video_urls 中的所有 chunk，按时序 ffmpeg concat 成单个 scene-XX.mp4。

    返回 None → 上层走兜底（_resolve_scene_path 会返回 None，scene 被跳过）。
    """
    urls = list(scene.aigc_video_urls or [])
    if not urls:
        return None
    locals_: list[Path] = []
    for url in urls:
        p = await _download_aigc_chunk(url)
        if p is None:
            log.warning("[render] aigc scene %d 第 %d 段下载失败，跳过整段", idx, len(locals_) + 1)
            return None
        locals_.append(p)
    if not locals_:
        return None
    if len(locals_) == 1:
        return locals_[0]
    # 多 chunk → concat
    if not ffmpeg_svc.ffmpeg_available():
        # 没 ffmpeg 时只用第一段，至少 demo 能播
        log.warning("[render] ffmpeg 不可用，aigc scene %d 仅用第 1 段", idx)
        return locals_[0]
    dst = segments_dir / f"aigc-scene-{idx:02d}.mp4"
    try:
        await asyncio.to_thread(ffmpeg_svc.concat, locals_, dst, reencode=True)
        return dst
    except ffmpeg_svc.FFmpegError as exc:
        log.warning("[render] aigc concat scene=%d failed: %s → 仅用第 1 段", idx, exc)
        return locals_[0]


def _aigc_fallback_to_sample(
    plan: Plan, scene: Scene, idx: int, segments_dir: Path,
) -> tuple[Path | None, str]:
    """AIGC scene URL 缺失/下载失败时，回落到样例视频对应段落的切片。

    返回 (path, note)。path=None 表示连兜底都没成（样例视频也找不到 / ffmpeg 不可用）。
    """
    sample_video = _samples_root() / plan.sample_id / "video.mp4"
    if not sample_video.is_file():
        sample_video = _uploads_root() / "decompose" / plan.sample_id / "video.mp4"
    if not sample_video.is_file() or not ffmpeg_svc.ffmpeg_available():
        return None, f"scene {idx} aigc 兜底失败：样例视频缺失或 ffmpeg 不可用"

    # 直接用 scene 的 timeline 起点对样例时长取模，给一个稳定但不重复的入点；
    # 比按 source_shot_indices[0] 反查更鲁棒——同 shot 多段不会撞同一窗口。
    from ...routers.library import _LIBRARY
    sample = next((s for s in _LIBRARY if s.id == plan.sample_id), None)
    sample_total = float(sample.duration_seconds) if sample else 18.0
    duration = max(1.0, float(scene.duration or 3.0))
    in_point = float(scene.start or 0.0) % max(1.0, sample_total - duration + 0.1)
    duration = min(duration, max(0.5, sample_total - in_point))

    seg_dst = segments_dir / f"aigc-fallback-{idx:02d}.mp4"
    try:
        ffmpeg_svc.trim(sample_video, seg_dst, start=in_point, duration=duration, reencode=True)
        return seg_dst, (
            f"scene {idx} ({scene.section}/aigc_t2v) URL 缺失，回落样例切片 "
            f"window=({in_point:.1f}s, {duration:.1f}s)"
        )
    except (AttributeError, ffmpeg_svc.FFmpegError) as exc:
        return None, f"scene {idx} aigc 兜底 trim 失败：{exc}"


def _resolve_scene_path(plan: Plan, scene: Scene) -> Path | None:
    """从 Scene 反查素材实际路径。

    - source="user_material": session_id 隔离的上传目录里按 material_id 子串匹配
    - source="sample"        : plan.sample_id 对应 server/samples/<id>/video.mp4
                                （后续 _trim_segments 会按 in_point/out_point 切片）
    - source="aigc_t2v"      : 通过 _resolve_aigc_scene 异步下载 + concat，不在这里处理

    本同步函数对 aigc_t2v 返回 None，让上层 pipeline 的异步分支自己跑。
    """
    source_ref = scene.source_ref
    if scene.source == "user_material" and plan.session_id:
        d = _uploads_root() / plan.session_id
        if d.exists():
            for f in d.iterdir():
                if source_ref in f.name:
                    return f
        return None

    if scene.source == "sample":
        sample_id = plan.sample_id
        sample_video = _samples_root() / sample_id / "video.mp4"
        if sample_video.is_file():
            return sample_video
        user_video = _uploads_root() / "decompose" / sample_id / "video.mp4"
        if user_video.is_file():
            return user_video
        return None

    return None


def _trim_segment(src: Path, scene: Scene, dst: Path) -> Path | None:
    """对 source="sample" 的整段视频按 in_point/out_point 切片，
    避免把整段样例 video.mp4 直接当成一个 scene 拼进主轨——那会导致每段都是相同长视频。

    切片用 ffmpeg -ss/-t 复制流（reencode=False），失败则返回 None 让上层走 mock。
    """
    if not ffmpeg_svc.ffmpeg_available():
        return None

    duration = max(0.5, float(scene.duration or 1.0))
    in_point = max(0.0, float(scene.in_point or 0.0))

    # 没有 out_point 的场景：直接按 duration 切；out_point 给定时按区间切。
    if scene.out_point is not None and scene.out_point > in_point:
        duration = float(scene.out_point) - in_point

    try:
        ffmpeg_svc.trim(src, dst, start=in_point, duration=duration, reencode=True)  # type: ignore[attr-defined]
        return dst
    except (AttributeError, ffmpeg_svc.FFmpegError) as exc:
        # ffmpeg.trim 不存在或调用失败：回落到整段 src（让 concat 至少能跑）
        log.warning("[trim] segment trim failed for %s: %s", src.name, exc)
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
    segments_dir = out_dir / "segments"
    segments_dir.mkdir(parents=True, exist_ok=True)

    inputs: list[Path] = []
    for i, sc in enumerate(plan.main_track):
        if sc.source == "aigc_t2v":
            aigc_path = await _resolve_aigc_scene(sc, segments_dir, i)
            if aigc_path is not None:
                inputs.append(aigc_path)
            else:
                # AIGC 下载失败 → 不再 silent skip，回落样例切片，让段落不丢
                fb_path, fb_note = await asyncio.to_thread(
                    _aigc_fallback_to_sample, plan, sc, i, segments_dir,
                )
                notes.append(fb_note)
                if fb_path is not None:
                    inputs.append(fb_path)
                else:
                    log.warning("[%s] %s", job_id, fb_note)
            continue

        src = _resolve_scene_path(plan, sc)
        if src is None:
            notes.append(f"scene {i} ({sc.section}/{sc.source}/{sc.source_ref}) unresolved")
            continue
        # 所有非 AIGC 来源都按 in_point/out_point 切片：
        # - sample：plan 已分配独立子窗口，避免多段共享相同片段
        # - user_material：限制到 scene.duration，避免一条长视频霸占整段
        # 注意：trim 失败时不再回落整段 src（会造成『同一片段复读 N 遍 ≈ 原视频』）
        seg_dst = segments_dir / f"scene-{i:02d}.mp4"
        trimmed = _trim_segment(src, sc, seg_dst)
        if trimmed is not None:
            inputs.append(trimmed)
        else:
            notes.append(f"scene {i} ({sc.source}) trim 失败，跳过该段")

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

    # ---- Step 3 · 主轨直通 ----
    # 渲染按 plan 主轨结构走，时长以 plan.duration_seconds 为准；
    # 需要 AIGC 补齐请走「一键 AI 生成全部缺口」(/api/gap/fill-all)，
    # 不再在渲染阶段隐式跑 Seedance 首尾帧扩展。
    t0 = time.time()
    job_store.publish(
        job_id, "seedance_extend", 48.0,
        {"note": "主轨直通（按 plan 主轨结构渲染，长度补齐请走缺口生成）"},
    )
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
        # /assets/... 和 /uploads/... 都映射到 server/var/<assets|uploads>/...
        # _outputs_root() == server/var/outputs，往上两层是 server/
        candidate = _outputs_root().parent.parent / bgm_track.lstrip("/")
        if candidate.exists():
            bgm_local = candidate
        else:
            log.warning("[%s] bgm path %s 不存在，跳过混音", job_id, candidate)

    if (
        ffmpeg_svc.ffmpeg_available()
        and bgm_local is not None
        and overlaid_path.exists() and overlaid_path.stat().st_size > 0
    ):
        try:
            bgm_cfg = plan.bgm
            await asyncio.to_thread(
                ffmpeg_svc.mix_bgm, overlaid_path, bgm_local, final_path,
                bgm_volume=bgm_cfg.volume if bgm_cfg else 0.35,
                fade_in=bgm_cfg.fade_in if bgm_cfg else 1.5,
                fade_out=bgm_cfg.fade_out if bgm_cfg else 2.0,
                start_offset=bgm_cfg.start_offset if bgm_cfg else 0.0,
                duck_with_voice=bgm_cfg.duck_with_voice if bgm_cfg else True,
                duck_attenuation_db=bgm_cfg.duck_attenuation_db if bgm_cfg else -9.0,
                video_has_voice=True,  # 暂保守假设主轨有口播；后续可由 ASR 结果回填
            )
            notes.append(
                f"bgm mixed: vol={bgm_cfg.volume:.2f} duck={bgm_cfg.duck_with_voice} "
                f"fade={bgm_cfg.fade_in}/{bgm_cfg.fade_out}"
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
