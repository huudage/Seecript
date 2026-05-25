"""Stage 3 · 任务 #15 — Seedance 首尾帧长视频拼接。

业务目标：把『FFmpeg 拼好的主轨』继续向后延长，直到达成目标时长（30-60s）。

实现思路：
  1. ffprobe 拿主轨当前时长；若已 ≥ target，直接 trim 到 target 返回。
  2. 否则按 `segment_duration_seconds`（默认 6s）切 N 段，循环：
       a. 抽当前尾帧 → base64 data URL；
       b. T2VClient.submit(prompt, first_frame=tail_frame, duration=segment_duration)；
       c. poll query() 直至 succeeded 或超时；
       d. httpx 拉下来生成的 mp4 / 用 mock URL 落到本地；
       e. ffmpeg concat 主轨 + 新段。
  3. 任何步骤抛错 → 降级回 base_segment（保证主流水线不挂）。

依赖：`services.t2v_client`、`services.video.ffmpeg`、httpx。
"""
from __future__ import annotations

import asyncio
import base64
import logging
import math
import mimetypes
import time
from pathlib import Path
from typing import Optional

import httpx

from ..jobs import job_store
from ..t2v_client import T2VClient, T2VError, get_t2v_client
from ..video import ffmpeg as ffmpeg_svc

log = logging.getLogger("seecript.render.seedance_chain")


class SeedanceChainError(RuntimeError):
    pass


_DEFAULT_PROMPT = "保持画面风格自然延续，构图稳定，无人物突变"


def _image_to_data_url(image_path: Path) -> str:
    mime, _ = mimetypes.guess_type(image_path.name)
    mime = mime or "image/jpeg"
    payload = base64.b64encode(image_path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{payload}"


async def _download_to(url: str, dst: Path, *, timeout: float = 60.0) -> Path:
    dst.parent.mkdir(parents=True, exist_ok=True)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        dst.write_bytes(resp.content)
    return dst


async def _poll_until_done(
    client: T2VClient,
    task_id: str,
    *,
    poll_interval_seconds: float,
    max_wait_seconds: float,
    job_id: Optional[str],
    chunk_index: int,
) -> str:
    """轮询直到 succeeded；返回视频 URL。失败 / 超时抛 SeedanceChainError。"""
    started = time.time()
    while True:
        q = await client.query(task_id)
        if q.status == "succeeded":
            if not q.video_url:
                raise SeedanceChainError(f"seedance task {task_id} succeeded but no video_url")
            return q.video_url
        if q.status == "failed":
            raise SeedanceChainError(f"seedance task {task_id} failed: {q.fail_reason or 'unknown'}")
        if time.time() - started > max_wait_seconds:
            raise SeedanceChainError(f"seedance task {task_id} timeout after {max_wait_seconds:.0f}s")
        if job_id is not None:
            elapsed = time.time() - started
            ratio = min(1.0, elapsed / max_wait_seconds)
            job_store.publish(
                job_id, "seedance_extend",
                48.0 + min(8.0, ratio * 8.0),
                {"note": f"段 {chunk_index} 等待 Seedance（已 {int(elapsed)}s）"},
            )
        await asyncio.sleep(poll_interval_seconds)


async def extend_with_seedance(
    base_segment: Path,
    target_duration_seconds: float,
    *,
    job_id: Optional[str] = None,
    segment_duration_seconds: int = 6,
    poll_interval_seconds: float = 4.0,
    max_wait_seconds: float = 240.0,
    prompt: str = _DEFAULT_PROMPT,
    client: Optional[T2VClient] = None,
) -> Path:
    """主入口：把 base_segment 延长到 target_duration。失败时返回 base_segment。"""
    if not base_segment.exists() or base_segment.stat().st_size == 0:
        log.info("[seedance_chain] base segment empty, skip extension")
        return base_segment

    # ---- 探测当前时长 ----
    try:
        info = ffmpeg_svc.probe(base_segment)
        current = info.duration_seconds
    except (ffmpeg_svc.FFmpegError, FileNotFoundError) as exc:
        log.warning("[seedance_chain] probe failed (%s), skip", exc)
        return base_segment

    if current >= target_duration_seconds - 0.5:
        log.info("[seedance_chain] base already long enough: %.2fs >= %.2fs",
                 current, target_duration_seconds)
        return base_segment

    deficit = target_duration_seconds - current
    n_chunks = max(1, math.ceil(deficit / segment_duration_seconds))
    log.info("[seedance_chain] base=%.2fs target=%.2fs deficit=%.2fs → %d chunks",
             current, target_duration_seconds, deficit, n_chunks)

    if not ffmpeg_svc.ffmpeg_available():
        log.warning("[seedance_chain] ffmpeg unavailable, cannot chain extend")
        return base_segment

    t2v = client or get_t2v_client()
    tmp_dir = base_segment.parent / "seedance_tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)

    tail_path = base_segment
    chunks: list[Path] = [base_segment]

    try:
        for i in range(n_chunks):
            # 抽尾帧
            tail_frame = tmp_dir / f"frame_{i:02d}.jpg"
            try:
                tail_info = ffmpeg_svc.probe(tail_path)
                # 抽倒数 0.5s 处的帧，避免黑场结尾
                t = max(0.0, tail_info.duration_seconds - 0.5)
                await asyncio.to_thread(ffmpeg_svc.extract_frame, tail_path, t, tail_frame)
            except (ffmpeg_svc.FFmpegError, FileNotFoundError) as exc:
                log.warning("[seedance_chain] tail-frame extract failed: %s", exc)
                break

            first_frame_url = _image_to_data_url(tail_frame)

            # 提交 Seedance 任务
            try:
                submit = await t2v.submit(
                    prompt=prompt,
                    first_frame=first_frame_url,
                    duration_seconds=segment_duration_seconds,
                )
            except T2VError as exc:
                log.warning("[seedance_chain] submit failed: %s", exc)
                break

            if job_id is not None:
                job_store.publish(
                    job_id, "seedance_extend",
                    48.0 + (i + 0.1) * (8.0 / n_chunks),
                    {"note": f"Seedance 提交第 {i + 1}/{n_chunks} 段 (task={submit.task_id})"},
                )

            # 轮询
            try:
                video_url = await _poll_until_done(
                    t2v, submit.task_id,
                    poll_interval_seconds=poll_interval_seconds,
                    max_wait_seconds=max_wait_seconds,
                    job_id=job_id, chunk_index=i + 1,
                )
            except SeedanceChainError as exc:
                log.warning("[seedance_chain] poll failed: %s", exc)
                break

            # 下载
            chunk_path = tmp_dir / f"chunk_{i:02d}.mp4"
            try:
                if video_url.startswith("http"):
                    await _download_to(video_url, chunk_path)
                else:
                    # mock 返回 /aigc/xxx.mp4 之类的本地相对路径 → 写占位
                    chunk_path.write_bytes(b"")
                    log.info("[seedance_chain] non-http video_url=%s, mock placeholder", video_url)
            except (httpx.HTTPError, OSError) as exc:
                log.warning("[seedance_chain] download failed: %s", exc)
                break

            if chunk_path.stat().st_size == 0:
                # mock 模式下 chunk 是空文件，无法 concat — 停在这里，使用 base_segment
                log.info("[seedance_chain] empty chunk %d (mock mode), abort extension", i)
                break

            chunks.append(chunk_path)
            tail_path = chunk_path

        if len(chunks) <= 1:
            return base_segment

        # 拼接所有 chunks
        final_path = base_segment.with_name(f"{base_segment.stem}_extended.mp4")
        try:
            await asyncio.to_thread(ffmpeg_svc.concat, chunks, final_path, reencode=True)
        except ffmpeg_svc.FFmpegError as exc:
            log.warning("[seedance_chain] final concat failed: %s", exc)
            return base_segment
        log.info("[seedance_chain] extended %s → %s (%d chunks)",
                 base_segment.name, final_path.name, len(chunks))
        return final_path
    finally:
        # 清理临时帧（保留 chunks 以便排查）
        for fp in tmp_dir.glob("frame_*.jpg"):
            fp.unlink(missing_ok=True)
