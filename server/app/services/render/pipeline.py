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
from ..video.aspect import aspect_for_platform

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
    """无画面素材的 scene 落「文字卡」：纯色背景持续 scene.duration，
    真实文案由 packaging 字幕在上层叠加。

    覆盖 4 类情形：
    - source=="text_card"（plan 显式安排）
    - source=="aigc_t2v" URL 下载失败
    - source=="user_material" 找不到文件或 trim 失败
    - source=="sample" 老 plan 残留（新 plan 已不再产生）

    返回 None → 连 ffmpeg 都不可用，上层走 mock。
    """
    if not ffmpeg_svc.ffmpeg_available():
        return None
    dur = max(0.5, float(scene.duration or 1.0))
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

    # 从 plan.settings.target_platform 推导画幅
    aspect = aspect_for_platform(plan.settings.target_platform)
    canvas_w, canvas_h = aspect.width, aspect.height
    notes.append(f"canvas={canvas_w}×{canvas_h} (platform={plan.settings.target_platform})")

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

        # source == "user_material" 或老 plan 残留的 "sample"：尝试解析+切片，
        # 失败一律回落到文字卡（不再回落整段 src，避免『同一片段复读 N 遍 ≈ 原视频』）
        src = _resolve_scene_path(plan, sc)
        trimmed: Path | None = None
        if src is not None:
            seg_dst = segments_dir / f"scene-{i:02d}.mp4"
            trimmed = _trim_segment(src, sc, seg_dst, canvas_w, canvas_h)
        if trimmed is not None:
            inputs.append(trimmed)
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
            _touch_placeholder(packaging_path)
    else:
        if plan.packaging_track:
            notes.append(
                f"remotion unavailable (n={len(plan.packaging_track)} items); "
                "走 ffmpeg drawtext fallback"
            )
        else:
            notes.append("empty packaging_track; skip remotion")
        _touch_placeholder(packaging_path)
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
            try:
                await asyncio.to_thread(
                    ffmpeg_svc.burn_packaging_track,
                    extended_path,
                    items_dict,
                    overlaid_path,
                )
                kinds_used = sorted({str(it.get("kind")) for it in items_dict})
                notes.append(
                    f"packaging burned via drawtext ({len(items_dict)} items, kinds={kinds_used})"
                )
            except ffmpeg_svc.FFmpegError as exc:
                log.warning("[%s] drawtext burn failed: %s", job_id, exc)
                notes.append(f"drawtext burn fallback: {exc}")
                if extended_ok:
                    overlaid_path.write_bytes(extended_path.read_bytes())
                else:
                    _touch_placeholder(overlaid_path)
        else:
            notes.append("overlay skipped (missing inputs or ffmpeg); passthrough")
            if extended_ok:
                overlaid_path.write_bytes(extended_path.read_bytes())
            else:
                _touch_placeholder(overlaid_path)

    timings["overlay_ms"] = int((time.time() - t0) * 1000)

    # ---- Step 5b · voice mix：把各 scene 的 TTS 口播按 scene.start 偏移混入主轨 ----
    # voiceover_enabled=False 时跳过（纯 BGM 视频）
    voice_clips: list[tuple[Path, float]] = []
    if plan.settings.voiceover_enabled:
        for sc in plan.main_track:
            url = (sc.voiceover_url or "").strip()
            if not url:
                continue
            if url.startswith("/"):
                candidate = _outputs_root().parent.parent / url.lstrip("/")
            else:
                candidate = Path(url)
            if candidate.exists() and candidate.stat().st_size > 0:
                voice_clips.append((candidate, float(sc.start)))
            else:
                log.warning("[%s] voiceover 文件不存在 url=%s", job_id, url)
    voice_mixed_path = overlaid_path
    if (
        voice_clips
        and ffmpeg_svc.ffmpeg_available()
        and overlaid_path.exists() and overlaid_path.stat().st_size > 0
    ):
        voice_mixed_path = out_dir / "voiced.mp4"
        try:
            await asyncio.to_thread(
                ffmpeg_svc.mix_voiceovers, overlaid_path, voice_clips, voice_mixed_path
            )
            notes.append(f"voiceover mixed: {len(voice_clips)} clips")
        except ffmpeg_svc.FFmpegError as exc:
            log.warning("[%s] mix_voiceovers failed: %s", job_id, exc)
            notes.append(f"voiceover fallback: {exc}")
            voice_mixed_path = overlaid_path

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
            final_path.write_bytes(voice_mixed_path.read_bytes() if voice_mixed_path.exists() else b"")
    else:
        # 无 BGM 或缺 ffmpeg：直接 rename
        notes.append("bgm mix skipped; using overlaid output as final")
        if voice_mixed_path.exists():
            final_path.write_bytes(voice_mixed_path.read_bytes())
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
