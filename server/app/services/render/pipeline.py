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
from ..video.aspect import aspect_for_platform, aspect_for_settings

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


def _render_text_card(
    scene: Scene,
    segments_dir: Path,
    idx: int,
    *,
    width: int = 1080,
    height: int = 1920,
    fps: int = 30,
) -> Path | None:
    """无画面素材的 scene 落「文字卡」：

    - 若 scene.text_card_spec 存在 → 用 ffmpeg drawtext 渲染个性字卡（主标 + 副标 +
      情绪化配色 + 动画 + emoji）。这条路径覆盖 copy fill 的 action=copy 产出。
    - 否则 → 退回 color_clip 纯色底，由 packaging 字幕在上层叠文案。

    覆盖 4 类情形：
    - source=="text_card" + spec → 个性字卡（stage-19+）
    - source=="text_card" 无 spec → 纯色底片（兼容旧 plan）
    - source=="aigc_t2v" URL 下载失败 → 纯色兜底
    - source=="user_material" 找不到文件或 trim 失败 → 纯色兜底

    返回 None → 连 ffmpeg 都不可用，上层走 mock。
    """
    if not ffmpeg_svc.ffmpeg_available():
        return None
    dur = max(0.5, float(scene.duration or 1.0))

    if scene.text_card_spec is not None:
        dst = segments_dir / f"text-card-{idx:02d}.mp4"
        # spec 的 duration_seconds 可能比 scene.duration 短/长——以 scene.duration 为准，
        # 因为 main_track 上 scene.duration 已经被 timeline 锁死。
        spec_dict = scene.text_card_spec.model_dump()
        spec_dict["duration_seconds"] = dur
        try:
            return ffmpeg_svc.text_card_clip(
                spec_dict, dst, width=width, height=height, fps=fps,
            )
        except ffmpeg_svc.FFmpegError as exc:
            log.warning("[render] text_card_spec scene=%d failed (fallback to color): %s",
                        idx, exc)
            # 渲染失败 → 仍走纯色兜底，不让段落丢失

    dst = segments_dir / f"text-card-{idx:02d}.mp4"
    try:
        return ffmpeg_svc.color_clip(dur, dst, width=width, height=height, fps=fps)
    except ffmpeg_svc.FFmpegError as exc:
        log.warning("[render] text_card scene=%d failed: %s", idx, exc)
        return None


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
        single = locals_[0]
        return await _maybe_extend_freeze(single, scene, segments_dir, idx)
    # 多 chunk → concat
    if not ffmpeg_svc.ffmpeg_available():
        # 没 ffmpeg 时只用第一段，至少 demo 能播
        log.warning("[render] ffmpeg 不可用，aigc scene %d 仅用第 1 段", idx)
        return locals_[0]
    dst = segments_dir / f"aigc-scene-{idx:02d}.mp4"
    try:
        await asyncio.to_thread(ffmpeg_svc.concat, locals_, dst, reencode=True)
        return await _maybe_extend_freeze(dst, scene, segments_dir, idx)
    except ffmpeg_svc.FFmpegError as exc:
        log.warning("[render] aigc concat scene=%d failed: %s → 仅用第 1 段", idx, exc)
        return locals_[0]


async def _align_to_scene_duration(
    src: Path,
    scene: Scene,
    segments_dir: Path,
    idx: int,
    *,
    label: str = "scene",
    allow_speed_change: bool = True,
) -> Path:
    """统一对齐策略：把任意视频片段对齐到 scene.duration。

    分支（Δ = target - actual）：
      |Δ| < 0.3                    → 误差内，原片直通
      Δ ≥ 0.3 且 actual < target/1.5 → 加长 ≥ 1.5×：变速 setpts/atempo（保留所有内容）
      Δ ≥ 0.3                     → 轻度短缺：tpad 冻结尾帧补足
      Δ ≤ -0.3 且 target < actual/1.5 → 减短 ≤ 0.667×：变速加快（让画面紧凑）
      Δ ≤ -0.3                    → 轻度过长：head trim 截到 target

    allow_speed_change=False 时退回纯 freeze/trim（不变速）——
    AIGC 视频时长偏差通常 < 1s，强行变速反而失真，调用方关掉这条；
    user_material 偏差大、可控，开启变速。

    任意分支失败均返回原片，让 concat 自动按短的来——demo 不阻塞。
    """
    target = float(scene.duration or 0.0)
    if target <= 0.5 or not ffmpeg_svc.ffmpeg_available():
        return src
    try:
        info = await asyncio.to_thread(ffmpeg_svc.probe, src)
    except (ffmpeg_svc.FFmpegError, FileNotFoundError) as exc:
        log.warning("[render] probe failed for align scene=%d: %s", idx, exc)
        return src
    actual = float(info.duration_seconds or 0.0)
    if actual <= 0.05:
        return src
    delta = target - actual
    if abs(delta) < 0.3:
        return src

    # ---- 加长分支 ----
    if delta > 0:
        # 短缺过多 → 变速放慢（保留全部内容）；否则 tpad 冻结尾帧
        if allow_speed_change and actual < target / 1.5 and target / actual <= 4.0:
            dst = segments_dir / f"{label}-slow-{idx:02d}.mp4"
            try:
                await asyncio.to_thread(
                    ffmpeg_svc.change_speed, src, dst, target_duration=target,
                )
                log.info(
                    "[render] %s %d slowmo %.2fs → %.2fs (×%.2f)",
                    label, idx, actual, target, target / actual,
                )
                return dst
            except ffmpeg_svc.FFmpegError as exc:
                log.warning("[render] slowmo scene=%d failed (fallback freeze): %s", idx, exc)
        dst = segments_dir / f"{label}-extend-{idx:02d}.mp4"
        try:
            await asyncio.to_thread(
                ffmpeg_svc.extend_freeze_tail, src, dst, target_duration=target,
            )
            log.info(
                "[render] %s %d freeze-extend %.2fs → %.2fs (Δ=%.2fs)",
                label, idx, actual, target, delta,
            )
            return dst
        except ffmpeg_svc.FFmpegError as exc:
            log.warning("[render] freeze-extend scene=%d failed: %s", idx, exc)
            return src

    # ---- 截短分支（delta < 0）----
    if allow_speed_change and target < actual / 1.5 and actual / target <= 4.0:
        dst = segments_dir / f"{label}-fast-{idx:02d}.mp4"
        try:
            await asyncio.to_thread(
                ffmpeg_svc.change_speed, src, dst, target_duration=target,
            )
            log.info(
                "[render] %s %d speedup %.2fs → %.2fs (×%.2f)",
                label, idx, actual, target, actual / target,
            )
            return dst
        except ffmpeg_svc.FFmpegError as exc:
            log.warning("[render] speedup scene=%d failed (fallback trim): %s", idx, exc)
    dst = segments_dir / f"{label}-trim-{idx:02d}.mp4"
    try:
        await asyncio.to_thread(
            ffmpeg_svc.trim, src, dst,
            start=0.0, duration=target, reencode=True,
        )
        log.info(
            "[render] %s %d head-trim %.2fs → %.2fs",
            label, idx, actual, target,
        )
        return dst
    except ffmpeg_svc.FFmpegError as exc:
        log.warning("[render] head-trim scene=%d failed: %s", idx, exc)
        return src


async def _maybe_extend_freeze(src: Path, scene: Scene, segments_dir: Path, idx: int) -> Path:
    """AIGC 段落：仅做 freeze 补尾 / 轻度 trim（不变速，避免 Seedance 美感被破坏）。"""
    return await _align_to_scene_duration(
        src, scene, segments_dir, idx,
        label="aigc", allow_speed_change=False,
    )


async def _resolve_aigc_image_scene(
    scene: Scene,
    segments_dir: Path,
    idx: int,
    *,
    width: int,
    height: int,
) -> Path | None:
    """source=aigc_image 的 scene：解析本地 /aigc-images/... 路径，
    用 ffmpeg.image_to_video loop 成 scene.duration 长度的 mp4（静帧 + 静音）。

    aigc_image_url 形如 `/aigc-images/<gap_id>-<ts>.png`——直接拼 var/aigc_images/<filename>。
    历史/兜底兼容：也接受完整 http(s) URL（如落盘失败时回落的原 CDN URL）。
    """
    src_url = (scene.aigc_image_url or "").strip()
    if not src_url:
        return None
    if not ffmpeg_svc.ffmpeg_available():
        return None

    settings = get_settings()
    images_dir = settings.log_dir.parent / "var" / "aigc_images"
    uploads_dir = _uploads_root()

    local_path: Path | None = None
    if src_url.startswith("/aigc-images/"):
        local_path = images_dir / src_url[len("/aigc-images/"):]
        if not local_path.exists():
            log.warning("[render] aigc_image scene %d 本地缺失 %s", idx, local_path)
            local_path = None
    elif src_url.startswith("/uploads/"):
        # user_material kind=image 走 aigc_image 路径时，src 是 /uploads/<sid>/<file>
        local_path = uploads_dir / src_url[len("/uploads/"):]
        if not local_path.exists():
            log.warning("[render] aigc_image (uploads) scene %d 本地缺失 %s", idx, local_path)
            local_path = None
    elif src_url.startswith("http"):
        # 兜底：URL 没落盘成功；现下载到 aigc_cache 临时目录
        h = hashlib.sha1(src_url.encode("utf-8")).hexdigest()[:16]
        suffix = ".png"
        for ext in (".png", ".jpg", ".jpeg", ".webp"):
            if src_url.lower().split("?", 1)[0].endswith(ext):
                suffix = ext if ext != ".jpeg" else ".jpg"
                break
        dl = _aigc_cache_root() / f"img-{h}{suffix}"
        if not dl.exists() or dl.stat().st_size == 0:
            try:
                async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
                    resp = await client.get(src_url)
                    resp.raise_for_status()
                    dl.write_bytes(resp.content)
            except Exception as exc:  # noqa: BLE001
                log.warning("[render] aigc_image scene %d 下载失败 %s: %s", idx, src_url[:60], exc)
                return None
        local_path = dl

    if local_path is None:
        return None

    dur = max(0.5, float(scene.duration or 1.0))
    dst = segments_dir / f"aigc-image-{idx:02d}.mp4"

    # Remotion 动效路径：scene.animation_spec.engine == 'remotion' 时优先尝试
    spec = getattr(scene, "animation_spec", None)
    if spec is not None and getattr(spec, "engine", "ffmpeg") == "remotion":
        # 推断 ratio：根据 width/height 反推 9:16/16:9/1:1
        if width > height:
            ratio = "16:9"
        elif width < height:
            ratio = "9:16"
        else:
            ratio = "1:1"

        # 多图（keyframe_morph / storyboard）：从 spec.image_urls 解析所有本地路径
        multi_urls = list(getattr(spec, "image_urls", []) or [])
        image_paths: list[Path] = []
        if multi_urls:
            for u in multi_urls:
                u = u.strip()
                if u.startswith("/aigc-images/"):
                    candidate = images_dir / u[len("/aigc-images/"):]
                    if candidate.exists():
                        image_paths.append(candidate)
                elif u.startswith("/uploads/"):
                    candidate = uploads_dir / u[len("/uploads/"):]
                    if candidate.exists():
                        image_paths.append(candidate)
                elif u.startswith("http"):
                    h = hashlib.sha1(u.encode("utf-8")).hexdigest()[:16]
                    suffix = ".png"
                    for ext in (".png", ".jpg", ".jpeg", ".webp"):
                        if u.lower().split("?", 1)[0].endswith(ext):
                            suffix = ext if ext != ".jpeg" else ".jpg"
                            break
                    dl = _aigc_cache_root() / f"img-{h}{suffix}"
                    if not dl.exists() or dl.stat().st_size == 0:
                        try:
                            async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
                                resp = await client.get(u)
                                resp.raise_for_status()
                                dl.write_bytes(resp.content)
                        except Exception as exc:  # noqa: BLE001
                            log.warning("[render] multi-image 下载失败 %s: %s", u[:60], exc)
                            continue
                    image_paths.append(dl)
        if not image_paths:
            image_paths = [local_path]

        try:
            from .remotion_renderer import render_animated_image, remotion_available
            if remotion_available():
                out = await render_animated_image(
                    image_paths=image_paths,
                    duration_seconds=dur,
                    output_path=dst,
                    animation_type=getattr(spec, "animation_type", "ken-burns") or "ken-burns",
                    ratio=ratio,
                    intensity=float(getattr(spec, "intensity", 0.3) or 0.3),
                    motion_direction=getattr(spec, "motion_direction", "in") or "in",
                    transition=getattr(spec, "transition", "cross-fade") or "cross-fade",
                    transition_duration=float(getattr(spec, "transition_duration", 0.4) or 0.4),
                )
                if out is not None and out.exists() and out.stat().st_size > 0:
                    return out
                log.warning(
                    "[render] aigc_image scene=%d remotion 渲染失败，回落 ffmpeg 静帧",
                    idx,
                )
            else:
                log.info("[render] aigc_image scene=%d 请求 remotion 但环境未就绪，回落 ffmpeg", idx)
        except Exception as exc:  # noqa: BLE001
            log.warning("[render] aigc_image scene=%d remotion 调用异常: %s", idx, exc)

    try:
        # stage-58：ffmpeg fallback 也跑通已有运镜（与 Remotion 6 方向对齐）。
        # 没有 spec 或 spec 标了 ffmpeg/static → 退回静帧；否则走 zoompan 运镜版本。
        if spec is not None:
            anim_t = (getattr(spec, "animation_type", "") or "").lower()
            motion = (getattr(spec, "motion_direction", "") or "").lower()
            inten = float(getattr(spec, "intensity", 0.4) or 0.4)
            if anim_t in ("ken-burns", "parallax"):
                return await asyncio.to_thread(
                    ffmpeg_svc.image_to_video_with_motion,
                    local_path, dur, dst,
                    width=width, height=height,
                    animation_type=anim_t,
                    motion_direction=motion or "in",
                    intensity=inten,
                )
        return await asyncio.to_thread(
            ffmpeg_svc.image_to_video,
            local_path, dur, dst,
            width=width, height=height,
        )
    except ffmpeg_svc.FFmpegError as exc:
        log.warning("[render] aigc_image scene=%d ffmpeg failed: %s", idx, exc)
        return None


def _resolve_scene_path(plan: Plan, scene: Scene) -> Path | None:
    """从 Scene 反查素材实际路径。

    - source="user_material": session_id 隔离的上传目录里按 material_id 子串匹配
    - source="sample"        : 已弃用——老 plan 残留时直接返回 None，让上层走文字卡兜底
                                （不再从 samples/<id>/video.mp4 取素材）
    - source="aigc_t2v"      : 通过 _resolve_aigc_scene 异步下载 + concat，不在这里处理

    本同步函数对 aigc_t2v / sample 返回 None，让上层 pipeline 的异步分支或文字卡兜底处理。
    """
    source_ref = scene.source_ref
    if scene.source == "user_material" and plan.session_id:
        d = _uploads_root() / plan.session_id
        if d.exists():
            for f in d.iterdir():
                if source_ref in f.name:
                    return f
        return None

    # source == "sample"（老 plan 残留）/ 其它未知 source：一律走文字卡
    return None


def _trim_segment(src: Path, scene: Scene, dst: Path, canvas_w: int, canvas_h: int) -> Path | None:
    """对 source="sample" 的整段视频按 in_point/out_point 切片，
    避免把整段样例 video.mp4 直接当成一个 scene 拼进主轨——那会导致每段都是相同长视频。

    切片同时按 (canvas_w, canvas_h) 做 scale+pad+setsar——保证不同来源的素材
    最终都对齐到同一画布尺寸（concat 才能 -c copy 不报错）。
    """
    if not ffmpeg_svc.ffmpeg_available():
        return None

    duration = max(0.5, float(scene.duration or 1.0))
    in_point = max(0.0, float(scene.in_point or 0.0))

    # 没有 out_point 的场景：直接按 duration 切；out_point 给定时按区间切。
    if scene.out_point is not None and scene.out_point > in_point:
        duration = float(scene.out_point) - in_point

    try:
        ffmpeg_svc.trim(
            src, dst,
            start=in_point, duration=duration, reencode=True,
            canvas=(canvas_w, canvas_h),
        )
        return dst
    except (AttributeError, ffmpeg_svc.FFmpegError) as exc:
        # ffmpeg.trim 不存在或调用失败：回落到整段 src（让 concat 至少能跑）
        log.warning("[trim] segment trim failed for %s: %s", src.name, exc)
        return None


def _normalize_to_canvas(src: Path, dst: Path, *, width: int, height: int) -> Path | None:
    """把任意输入视频缩放并 letterbox 填充到 (width, height)。

    用于 aigc 段：Seedance 返回的 ratio 与 plan 设定理论上一致，但实际偶发
    略偏分辨率（如 1088×1920 而非 1080×1920），concat 会拒绝；统一过一遍画布。
    """
    if not ffmpeg_svc.ffmpeg_available():
        return None
    try:
        return ffmpeg_svc.normalize_canvas(src, dst, width=width, height=height)
    except ffmpeg_svc.FFmpegError as exc:
        log.warning("[normalize] %s → %dx%d failed: %s", src.name, width, height, exc)
        return None


def _touch_placeholder(dst: Path, content: bytes = b"") -> Path:
    """Deprecated: 渲染流水线已禁用 0 字节占位 fallback——失败硬抛 RuntimeError。
    保留函数签名仅为兼容老 import；不应再被调用。
    """
    raise RuntimeError(
        f"_touch_placeholder({dst.name}) 被调用——渲染流水线已禁用占位 fallback，"
        "请检查上游 ffmpeg/remotion 失败原因。",
    )


# ----------------------------- 流水线 -------------------------------------

async def run_pipeline(job_id: str, plan: Plan) -> RenderResult:
    """从 Plan → final.mp4。同步耗时操作放线程池，progress 用 job_store 推送。"""
    out_dir = _outputs_root() / job_id
    out_dir.mkdir(parents=True, exist_ok=True)
    timings: dict[str, int] = {}
    notes: list[str] = []

    # 画幅优先看 settings.aspect_ratio，回退到 target_platform，再回落 9:16
    aspect = aspect_for_settings(plan.settings)
    canvas_w, canvas_h = aspect.width, aspect.height
    notes.append(
        f"canvas={canvas_w}×{canvas_h} "
        f"(ratio={plan.settings.aspect_ratio} platform={plan.settings.target_platform})"
    )

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
        # source == "text_card"：无画面素材的段落，直接落纯色底片，
        # 真实文案靠 packaging 字幕在上层叠加。
        if sc.source == "text_card":
            tc = _render_text_card(sc, segments_dir, i, width=canvas_w, height=canvas_h)
            if tc is not None:
                inputs.append(tc)
            else:
                notes.append(f"scene {i} text_card 渲染失败（ffmpeg 不可用）")
            continue

        if sc.source == "aigc_t2v":
            aigc_path = await _resolve_aigc_scene(sc, segments_dir, i)
            if aigc_path is not None:
                # 把 aigc 段尺寸校正到画布——Seedance 偶发返回略偏分辨率，concat 会色彩错位
                normed = _normalize_to_canvas(aigc_path, segments_dir / f"aigc-norm-{i:02d}.mp4",
                                              width=canvas_w, height=canvas_h)
                inputs.append(normed if normed is not None else aigc_path)
                continue
            # AIGC URL 下载/拼接失败 → 落文字卡（不再回落到样例切片）
            tc = await asyncio.to_thread(_render_text_card, sc, segments_dir, i,
                                         width=canvas_w, height=canvas_h)
            if tc is not None:
                inputs.append(tc)
                notes.append(f"scene {i} ({sc.section}/aigc_t2v) URL 缺失，落文字卡")
            else:
                notes.append(f"scene {i} aigc 落文字卡失败（ffmpeg 不可用），跳过")
            continue

        if sc.source == "aigc_image":
            img_path = await _resolve_aigc_image_scene(
                sc, segments_dir, i, width=canvas_w, height=canvas_h,
            )
            if img_path is not None:
                inputs.append(img_path)
                continue
            # Seedream 图缺失/解码失败 → 落文字卡，不让段落丢失
            tc = await asyncio.to_thread(_render_text_card, sc, segments_dir, i,
                                         width=canvas_w, height=canvas_h)
            if tc is not None:
                inputs.append(tc)
                notes.append(f"scene {i} ({sc.section}/aigc_image) 图缺失，落文字卡")
            else:
                notes.append(f"scene {i} aigc_image 落文字卡失败（ffmpeg 不可用），跳过")
            continue

        # source == "user_material" 或老 plan 残留的 "sample"：尝试解析+切片，
        # 失败一律回落到文字卡（不再回落整段 src，避免『同一片段复读 N 遍 ≈ 原视频』）
        src = _resolve_scene_path(plan, sc)
        trimmed: Path | None = None
        if src is not None:
            seg_dst = segments_dir / f"scene-{i:02d}.mp4"
            trimmed = _trim_segment(src, sc, seg_dst, canvas_w, canvas_h)
        if trimmed is not None:
            # 切片完成后再做一次时长对齐：素材实际时长不一定等于 scene.duration
            # （out_point 缺失 / 浮点误差 / 镜头切片选段比 scene 短）。允许变速。
            aligned = await _align_to_scene_duration(
                trimmed, sc, segments_dir, i,
                label="user", allow_speed_change=True,
            )
            inputs.append(aligned)
            continue
        # 解析失败或切片失败：落文字卡
        reason = "素材未解析" if src is None else "切片失败"
        tc = _render_text_card(sc, segments_dir, i, width=canvas_w, height=canvas_h)
        if tc is not None:
            inputs.append(tc)
            notes.append(f"scene {i} ({sc.section}/{sc.source}/{sc.source_ref}) {reason}，落文字卡")
        else:
            notes.append(f"scene {i} 文字卡渲染失败（ffmpeg 不可用），跳过该段")

    if inputs and ffmpeg_svc.ffmpeg_available():
        # 收集每段的 transition_in（首段强制 None）。任何非 hard_cut → 走 xfade 滤镜分支。
        transitions: list[dict | None] = []
        for i, sc in enumerate(plan.main_track[: len(inputs)]):
            if i == 0:
                transitions.append(None)
                continue
            tr = sc.transition_in
            if tr is None:
                transitions.append(None)
            else:
                transitions.append({"style": tr.style, "duration": tr.duration})
        has_real_transition = any(
            t is not None and (t.get("style") or "hard_cut") != "hard_cut"
            for t in transitions
        )
        try:
            if has_real_transition:
                await asyncio.to_thread(
                    ffmpeg_svc.concat_with_transitions,
                    inputs, transitions, main_path,
                    canvas=(canvas_w, canvas_h),
                )
            else:
                await asyncio.to_thread(ffmpeg_svc.concat, inputs, main_path, reencode=True)
        except ffmpeg_svc.FFmpegError as exc:
            raise RuntimeError(f"主轨 concat 失败：{exc}") from exc
    else:
        raise RuntimeError(
            f"主轨为空或 ffmpeg 不可用（n={len(inputs)}）——无法渲染主视频，"
            "请检查上游素材落地与 ffmpeg 安装。",
        )
    timings["concat_ms"] = int((time.time() - t0) * 1000)

    # ---- Step 3 · 主轨直通 ----
    # 渲染按 plan 主轨结构走，时长以 plan.duration_seconds 为准；
    # 需要 AIGC 补齐请在 step2 单段触发 /gap/fill (action=aigc / aigc_image / copy)，
    # 不再在渲染阶段隐式跑 Seedance 首尾帧扩展。
    t0 = time.time()
    job_store.publish(
        job_id, "seedance_extend", 48.0,
        {"note": "主轨直通（按 plan 主轨结构渲染，长度补齐请走缺口生成）"},
    )
    extended_path = main_path
    timings["seedance_ms"] = int((time.time() - t0) * 1000)

    # ---- Step 4 · Remotion 包装轨（不可用时跳过，Step 5 走 drawtext fallback）----
    t0 = time.time()
    job_store.publish(job_id, "remotion_render", 70.0, {"note": "Remotion 渲染包装轨"})
    packaging_path = out_dir / "packaging.webm"
    pkg_props = {
        "duration_seconds": plan.duration_seconds,
        "packaging_track": [item.model_dump() for item in plan.packaging_track],
    }
    remotion_ok = False
    if plan.packaging_track and remotion_svc.remotion_available():
        try:
            await asyncio.to_thread(remotion_svc.render_packaging_track, pkg_props, packaging_path)
            remotion_ok = packaging_path.exists() and packaging_path.stat().st_size > 0
        except (remotion_svc.RemotionError, FileNotFoundError) as exc:
            log.warning("[%s] remotion render failed, falling back to drawtext: %s", job_id, exc)
            notes.append(f"remotion fallback: {exc}")
    else:
        if plan.packaging_track:
            notes.append(
                f"remotion unavailable (n={len(plan.packaging_track)} items); "
                "走 ffmpeg drawtext fallback"
            )
        else:
            notes.append("empty packaging_track; skip remotion")
    timings["remotion_ms"] = int((time.time() - t0) * 1000)

    # ---- Step 5 · 包装合成：remotion overlay 或 drawtext burn ----
    t0 = time.time()
    job_store.publish(job_id, "ffmpeg_overlay", 88.0, {"note": "FFmpeg overlay / drawtext 合成"})
    overlaid_path = out_dir / "overlaid.mp4"
    extended_ok = extended_path.exists() and extended_path.stat().st_size > 0

    if remotion_ok and extended_ok and ffmpeg_svc.ffmpeg_available():
        # 真链路：透明 webm overlay
        try:
            await asyncio.to_thread(
                ffmpeg_svc.overlay, extended_path, packaging_path, overlaid_path, position="0:0"
            )
        except ffmpeg_svc.FFmpegError as exc:
            log.warning("[%s] overlay failed, fallback to drawtext: %s", job_id, exc)
            notes.append(f"overlay fallback: {exc}")
            remotion_ok = False  # 让后面 drawtext 接住

    if not (overlaid_path.exists() and overlaid_path.stat().st_size > 0):
        # 没走成 remotion overlay → 尝试 drawtext fallback 把包装项烧到主轨上
        if (
            ffmpeg_svc.ffmpeg_available()
            and extended_ok
            and plan.packaging_track
        ):
            items_dict = [item.model_dump() for item in plan.packaging_track]
            # frame.md typography_display → 字体文件路径；命中就让 drawtext 用差异化字体
            # （服务器没装就 None，burn_packaging_track 自动回落 find_cjk_font()）
            frame_typo = (
                getattr(plan.settings.frame_design, "typography_display", "") or ""
            )
            font_path = ffmpeg_svc.resolve_typography_font(frame_typo)
            try:
                await asyncio.to_thread(
                    ffmpeg_svc.burn_packaging_track,
                    extended_path,
                    items_dict,
                    overlaid_path,
                    font_path=font_path,
                )
                kinds_used = sorted({str(it.get("kind")) for it in items_dict})
                notes.append(
                    f"packaging burned via drawtext ({len(items_dict)} items, kinds={kinds_used}"
                    + (f", font={Path(font_path).name}" if font_path else "")
                    + ")"
                )
            except ffmpeg_svc.FFmpegError as exc:
                log.warning("[%s] drawtext burn failed: %s", job_id, exc)
                notes.append(f"drawtext burn fallback: {exc}")
                if extended_ok:
                    overlaid_path.write_bytes(extended_path.read_bytes())
                else:
                    raise RuntimeError(
                        f"drawtext 烧字失败且无可用主轨：{exc}",
                    ) from exc
        else:
            notes.append("overlay skipped (missing inputs or ffmpeg); passthrough")
            if extended_ok:
                overlaid_path.write_bytes(extended_path.read_bytes())
            else:
                raise RuntimeError(
                    "overlay 阶段无可用主轨，且 ffmpeg 不可用——无法继续渲染。",
                )

    timings["overlay_ms"] = int((time.time() - t0) * 1000)

    # ---- Step 5a · frame.md 视觉风格化（grain / vignette）----
    # frame_design.grain_overlay / vignette 在此之前都只是 LLM prompt 文本提示；
    # 这里真烧到 ffmpeg 滤镜链：noise=alls + vignette=angle。下游 mix_voiceovers /
    # mix_bgm 都是 -c:v copy，所以这一步是视频流唯一最后一次重编码点。
    frame = getattr(plan.settings, "frame_design", None)
    grain_on = bool(getattr(frame, "grain_overlay", False)) if frame else False
    vignette_on = bool(getattr(frame, "vignette", False)) if frame else False
    styled_path = overlaid_path
    if (
        (grain_on or vignette_on)
        and ffmpeg_svc.ffmpeg_available()
        and overlaid_path.exists() and overlaid_path.stat().st_size > 0
    ):
        styled_dst = out_dir / "styled.mp4"
        t_style = time.time()
        try:
            await asyncio.to_thread(
                ffmpeg_svc.apply_frame_styling,
                overlaid_path, styled_dst,
                grain=grain_on, vignette=vignette_on,
            )
            styled_path = styled_dst
            notes.append(
                f"frame styled: grain={grain_on} vignette={vignette_on}"
            )
        except ffmpeg_svc.FFmpegError as exc:
            log.warning("[%s] apply_frame_styling failed: %s", job_id, exc)
            notes.append(f"frame styling fallback: {exc}")
        timings["frame_styling_ms"] = int((time.time() - t_style) * 1000)

    # ---- Step 5b · voice mix：把各 scene 的 TTS 口播按 scene.start 偏移混入主轨 ----
    # voiceover_enabled=False 时跳过（纯 BGM 视频）
    # stage-49 居中策略：当口播音频自然时长 > scene.duration（atempo 出区间没强对齐），
    # 起始时间向前平移 (audio_dur - scene_dur)/2，让口播在 scene 视觉窗口居中——
    # 用户底线："严禁重复凑时长，太长就居中"。第一段越界部分 clip 到 0，最后一段允许溢出。
    import wave as _wave
    voice_clips: list[tuple[Path, float]] = []
    if plan.settings.voiceover_enabled:
        for sc in plan.main_track:
            url = (sc.voiceover_url or "").strip()
            if not url:
                continue
            # stage-49：URL 末尾带 cache-buster `?v=<ts>` 让浏览器换新文件，本地解析时要剥掉
            local_rel = url.split("?", 1)[0]
            if local_rel.startswith("/"):
                candidate = _outputs_root().parent.parent / local_rel.lstrip("/")
            else:
                candidate = Path(local_rel)
            if candidate.exists() and candidate.stat().st_size > 0:
                start_at = float(sc.start)
                try:
                    with _wave.open(str(candidate), "rb") as wf:
                        audio_dur = wf.getnframes() / float(wf.getframerate() or 1)
                except Exception:  # noqa: BLE001
                    audio_dur = 0.0
                scene_dur = float(sc.duration or 0.0)
                if audio_dur > scene_dur > 0:
                    overflow = audio_dur - scene_dur
                    start_at = max(0.0, float(sc.start) - overflow / 2.0)
                    log.info(
                        "[%s] voice clip overflows scene=%s audio=%.2fs scene=%.2fs → centered start %.2f→%.2f",
                        job_id, sc.scene_id, audio_dur, scene_dur, sc.start, start_at,
                    )
                voice_clips.append((candidate, start_at))
            else:
                log.warning("[%s] voiceover 文件不存在 url=%s", job_id, url)
    voice_mixed_path = styled_path
    if (
        voice_clips
        and ffmpeg_svc.ffmpeg_available()
        and styled_path.exists() and styled_path.stat().st_size > 0
    ):
        voice_mixed_path = out_dir / "voiced.mp4"
        try:
            await asyncio.to_thread(
                ffmpeg_svc.mix_voiceovers, styled_path, voice_clips, voice_mixed_path
            )
            notes.append(f"voiceover mixed: {len(voice_clips)} clips")
        except ffmpeg_svc.FFmpegError as exc:
            log.warning("[%s] mix_voiceovers failed: %s", job_id, exc)
            notes.append(f"voiceover fallback: {exc}")
            voice_mixed_path = styled_path

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
        and voice_mixed_path.exists() and voice_mixed_path.stat().st_size > 0
    ):
        try:
            bgm_cfg = plan.bgm
            anchor = float(bgm_cfg.video_anchor_seconds if bgm_cfg else 0.0)
            bgm_skip = max(0.0, -anchor)
            video_delay = max(0.0, anchor)
            # 口播开关关掉时强制禁 duck，否则原视频里的环境声会触发不必要的衰减
            voiceover_enabled = bool(plan.settings.voiceover_enabled)
            await asyncio.to_thread(
                ffmpeg_svc.mix_bgm, voice_mixed_path, bgm_local, final_path,
                bgm_volume=bgm_cfg.volume if bgm_cfg else 0.35,
                fade_in=bgm_cfg.fade_in if bgm_cfg else 1.5,
                fade_out=bgm_cfg.fade_out if bgm_cfg else 2.0,
                bgm_skip_seconds=bgm_skip,
                video_delay_seconds=video_delay,
                duck_with_voice=(
                    (bgm_cfg.duck_with_voice if bgm_cfg else True) and voiceover_enabled
                ),
                duck_attenuation_db=bgm_cfg.duck_attenuation_db if bgm_cfg else -9.0,
                video_has_voice=voiceover_enabled,
            )
            notes.append(
                f"bgm mixed: vol={bgm_cfg.volume:.2f} duck={bgm_cfg.duck_with_voice and voiceover_enabled} "
                f"fade={bgm_cfg.fade_in}/{bgm_cfg.fade_out} anchor={anchor:.2f}s "
                f"(skip={bgm_skip:.2f} delay={video_delay:.2f})"
            )
        except ffmpeg_svc.FFmpegError as exc:
            log.warning("[%s] mix_bgm failed: %s", job_id, exc)
            notes.append(f"mix_bgm fallback: {exc}")
            if voice_mixed_path.exists():
                final_path.write_bytes(voice_mixed_path.read_bytes())
            else:
                raise RuntimeError(f"BGM 混音失败且无可用 voice_mixed：{exc}") from exc
    else:
        # 无 BGM 或缺 ffmpeg：直接 rename
        notes.append("bgm mix skipped; using overlaid output as final")
        if voice_mixed_path.exists():
            final_path.write_bytes(voice_mixed_path.read_bytes())
        else:
            raise RuntimeError(
                "finalize 阶段无可用 voice_mixed 输出——无法生成 final.mp4。",
            )

    cover_path = out_dir / "cover.jpg"
    if ffmpeg_svc.ffmpeg_available() and final_path.exists() and final_path.stat().st_size > 0:
        try:
            await asyncio.to_thread(ffmpeg_svc.extract_frame, final_path, 0.5, cover_path)
        except ffmpeg_svc.FFmpegError as exc:
            log.warning("[%s] cover extract failed: %s", job_id, exc)
            notes.append(f"cover fallback: {exc}")
    else:
        notes.append("cover frame skipped; ffmpeg or video missing")

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
