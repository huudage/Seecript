"""Seedance 1.0 Pro 首尾帧长视频拼接（占位）。

阶段 3 · 任务 #14 留出钩子；真实实现见任务 #15。当前实现：原样返回输入片段。

设计思路（#15 时填充）：
  1. 按目标时长拆若干 6s 子段
  2. 每个子段拿『上段尾帧 + 下段首帧』调 Seedance T2V
  3. ffmpeg concat 子段
  4. 失败时降级为 ffmpeg loop / 静止画面拉伸
"""
from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger("seecript.render.seedance_chain")


class SeedanceChainError(RuntimeError):
    pass


async def extend_with_seedance(
    base_segment: Path,
    target_duration_seconds: float,
    *,
    job_id: str | None = None,
) -> Path:
    """占位实现：原样返回 base_segment。

    真实实现会在 task #15 接入 services.t2v_client.DoubaoArkT2VClient，
    用 Seedance 首尾帧模式生成长视频。
    """
    log.info("[seedance_chain] (stub) passthrough %s target=%.2fs", base_segment.name, target_duration_seconds)
    return base_segment
