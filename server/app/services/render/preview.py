"""stage-80 (2026-06-12)：step2/step3 主轨预览的后端合成服务。

背景
====
原 step2/step3 用 Remotion <Player> + PlanComposition 内的 <Video startFrom endAt> 实时
预览，浏览器 HTMLVideoElement 的 currentTime seek 不是 frame-accurate ——Remotion 每帧
重设 currentTime，落到关键帧附近会偶发回退几帧，表现为「单镜头内突然复读前 0.X 秒
内容」。stage-79 的 ffmpeg.trim 组合 seek 修了渲染输出（`npx remotion render` / pipeline.run_render
最终 mp4），但浏览器侧的 <Video> 走另一条路径，无法消除。

方案
====
后端把 plan.main_track 实时拼成单个 480p mp4，前端只用一个 <video src=...> 播——
零 currentTime 抖动，零关键帧回退。复用 pipeline 已有的 _resolve_*/_trim_segment/concat
逻辑，但跳过：包装轨（字幕/封面/标题）、转场（用 hard cut 代替）、BGM、口播 mux。
预览的核心诉求只是验证主轨内容——快、不复读。

缓存
====
预览文件命名为 `var/preview/{plan_id}-{signature}.mp4`，signature 是 main_track 关键
字段（scene_id / source / source_ref / in_point / out_point / duration）的稳定 hash。
plan 没改 → signature 没变 → 命中缓存直接返回 URL，不重跑 ffmpeg。

并发
====
同一 plan_id 同时只允许一个合成任务（asyncio.Lock per plan_id），第二个请求等第一个
完成后命中缓存返回。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from pathlib import Path

from ...config import get_settings
from ...schemas import Plan
from ..video import ffmpeg as ffmpeg_svc
from ..video.aspect import aspect_for_settings
from .pipeline import (
    _resolve_aigc_image_scene,
    _resolve_aigc_scene,
    _normalize_to_canvas,
    _render_text_card,
    _resolve_scene_path,
    _trim_segment,
)

log = logging.getLogger("seecript.render.preview")


# 预览画布：480p 9:16 / 16:9 自动跟随 plan.settings.aspect_ratio，但缩放到长边 ≤ 854 / 短边 ≤ 480
# 用低码率 fast preset → 5-15 秒主轨大概 3-8 秒合成时间
PREVIEW_LONG_EDGE = 854   # 16:9 时是宽，9:16 时是高
PREVIEW_SHORT_EDGE = 480  # 16:9 时是高，9:16 时是宽


def _preview_root() -> Path:
    settings = get_settings()
    root = settings.log_dir.parent / "var" / "preview"
    root.mkdir(parents=True, exist_ok=True)
    return root


def compute_signature(plan: Plan) -> str:
    """主轨内容签名：plan 没改 → signature 不变 → 缓存命中。

    只 hash 影响视频画面/时长/顺序的字段；忽略包装、字幕、BGM、口播 URL（这些
    不参与主轨预览渲染）。
    """
    payload = {
        "plan_id": plan.plan_id,
        "duration": round(plan.duration_seconds, 3),
        "ratio": plan.settings.aspect_ratio,
        "platform": plan.settings.target_platform,
        "scenes": [
            {
                "id": sc.scene_id,
                "src": sc.source,
                "ref": sc.source_ref,
                "in": round(sc.in_point or 0.0, 3),
                "out": round(sc.out_point, 3) if sc.out_point is not None else None,
                "dur": round(sc.duration, 3),
                "text_card": sc.text_card_spec.model_dump() if sc.text_card_spec else None,
                "aigc_videos": list(sc.aigc_video_urls or []),
                "aigc_image": sc.aigc_image_url,
                "anim": sc.animation_spec.model_dump() if sc.animation_spec else None,
            }
            for sc in plan.main_track
        ],
    }
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha1(blob).hexdigest()[:16]


def _preview_path(plan_id: str, signature: str) -> Path:
    return _preview_root() / f"{plan_id}-{signature}.mp4"


def preview_url_for(plan_id: str, signature: str) -> str:
    """供 router 返给前端的相对 URL（前端会和 API base 拼接）。"""
    return f"/preview/{plan_id}-{signature}.mp4"


# 同一 plan_id 同时只允许一个合成任务，避免重复调度 ffmpeg / 撞缓存写入
_locks: dict[str, asyncio.Lock] = {}


def _get_lock(plan_id: str) -> asyncio.Lock:
    lock = _locks.get(plan_id)
    if lock is None:
        lock = asyncio.Lock()
        _locks[plan_id] = lock
    return lock


def _preview_canvas(plan: Plan) -> tuple[int, int]:
    """480p 预览画布。保持 plan 的宽高比，长边 ≤ 854，短边 ≤ 480。"""
    aspect = aspect_for_settings(plan.settings)
    if aspect.width >= aspect.height:
        # 横屏：长边宽
        w = PREVIEW_LONG_EDGE
        h = max(2, round(w * aspect.height / aspect.width / 2) * 2)  # 偶数对齐
    else:
        # 竖屏：长边高
        h = PREVIEW_LONG_EDGE
        w = max(2, round(h * aspect.width / aspect.height / 2) * 2)
    return w, h


async def build_mainline_preview(plan: Plan) -> Path:
    """合成（或命中缓存返回）主轨预览 mp4。

    返回值：磁盘路径，调用方自行根据 plan_id+signature 计算 URL。
    异常：合成失败 → RuntimeError，由 router 转 500。
    """
    sig = compute_signature(plan)
    target = _preview_path(plan.plan_id, sig)

    if target.exists() and target.stat().st_size > 0:
        log.info("[preview] cache hit plan=%s sig=%s size=%dKB",
                 plan.plan_id, sig, target.stat().st_size // 1024)
        return target

    if not ffmpeg_svc.ffmpeg_available():
        raise RuntimeError("ffmpeg 未安装，无法生成预览")

    if not plan.main_track:
        raise RuntimeError("plan 主轨为空，无可预览内容")

    lock = _get_lock(plan.plan_id)
    async with lock:
        # 进锁后再检一次（前一个等锁的任务可能已经合好了同 sig 的文件）
        if target.exists() and target.stat().st_size > 0:
            return target

        canvas_w, canvas_h = _preview_canvas(plan)
        work_dir = _preview_root() / f".work-{plan.plan_id}-{sig}"
        work_dir.mkdir(parents=True, exist_ok=True)
        segments_dir = work_dir / "segments"
        segments_dir.mkdir(parents=True, exist_ok=True)

        try:
            inputs = await _build_segments(plan, segments_dir, canvas_w, canvas_h)
            if not inputs:
                raise RuntimeError("预览合成：所有 scene 段落生成失败")

            # 一律走 hard cut concat（reencode）：预览不要 xfade，省时间也避免转场叠加
            # 引入新的复杂度。最终视频带不带转场用户在 step4 渲染时再确认。
            tmp_out = work_dir / "preview.mp4"
            await asyncio.to_thread(
                ffmpeg_svc.concat, [str(p) for p in inputs], tmp_out, reencode=True,
            )
            if not tmp_out.exists() or tmp_out.stat().st_size == 0:
                raise RuntimeError("预览合成 concat 失败：输出文件为空")

            # 原子替换到最终文件名
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp_out.replace(target)
            log.info("[preview] built plan=%s sig=%s scenes=%d size=%dKB canvas=%dx%d",
                     plan.plan_id, sig, len(inputs),
                     target.stat().st_size // 1024, canvas_w, canvas_h)
            return target
        finally:
            # 清掉中间产物（单镜片段不再需要；最终 mp4 已经原子搬走）
            try:
                for p in segments_dir.glob("*"):
                    p.unlink(missing_ok=True)
                segments_dir.rmdir()
                # work_dir 里可能还有 preview.mp4（如果 replace 还没跑），保险删一遍
                for p in work_dir.glob("*"):
                    p.unlink(missing_ok=True)
                work_dir.rmdir()
            except OSError:
                pass


async def _build_segments(plan, segments_dir: Path, canvas_w: int, canvas_h: int) -> list[Path]:
    """复刻 pipeline.run_pipeline Step 2 的 main_track 段落生成逻辑，但用 480p 画布。

    与 run_pipeline 的差异：
    - 不发 job_store 进度（预览是同步阻塞接口）
    - 失败的 scene 落 text_card 占位（不抛错），保证整体能合成
    - 不做 _normalize_to_canvas 之外的额外处理
    """
    inputs: list[Path] = []
    for i, sc in enumerate(plan.main_track):
        try:
            if sc.source == "text_card":
                tc = _render_text_card(sc, segments_dir, i, width=canvas_w, height=canvas_h)
                if tc is not None:
                    inputs.append(tc)
                continue

            if sc.source == "aigc_t2v":
                aigc = await _resolve_aigc_scene(sc, segments_dir, i)
                if aigc is not None:
                    normed = _normalize_to_canvas(aigc, segments_dir / f"aigc-norm-{i:02d}.mp4",
                                                  width=canvas_w, height=canvas_h)
                    inputs.append(normed if normed is not None else aigc)
                    continue
                tc = await asyncio.to_thread(_render_text_card, sc, segments_dir, i,
                                             width=canvas_w, height=canvas_h)
                if tc is not None:
                    inputs.append(tc)
                continue

            if sc.source == "aigc_image":
                img = await _resolve_aigc_image_scene(sc, segments_dir, i,
                                                      width=canvas_w, height=canvas_h)
                if img is not None:
                    inputs.append(img)
                    continue
                tc = await asyncio.to_thread(_render_text_card, sc, segments_dir, i,
                                             width=canvas_w, height=canvas_h)
                if tc is not None:
                    inputs.append(tc)
                continue

            # user_material / sample
            src = _resolve_scene_path(plan, sc)
            if src is not None:
                seg_dst = segments_dir / f"scene-{i:02d}.mp4"
                trimmed = _trim_segment(src, sc, seg_dst, canvas_w, canvas_h)
                if trimmed is not None:
                    inputs.append(trimmed)
                    continue
            # 解析失败 → text_card 占位
            tc = _render_text_card(sc, segments_dir, i, width=canvas_w, height=canvas_h)
            if tc is not None:
                inputs.append(tc)
        except Exception as exc:  # noqa: BLE001
            log.warning("[preview] scene %d 生成失败：%s，跳过", i, exc)
    return inputs
