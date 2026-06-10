"""BGM 高潮自动切片对齐内容高潮——决定 BGMConfig.video_anchor_seconds。

抽到 service 层而非 router 层，方便单测（router 模块 import 会触发 FastAPI 版本相关的
兼容性副作用）。逻辑由 routers.plan 在 build_plan 收尾 + PATCH /plan/{id}/bgm 换曲后调用。

用户上传 BGM 后常见"听不见好听的段"的根因：BGM 自带 20s 铺垫，但视频只有 15s，
用户从头听到尾全是铺垫。本函数把 BGM 高潮自动落到内容情绪高潮时刻：

- bgm_peak_t：优先 `analysis.climaxes` 里 kind=climax → drop → release → build_start → break
  的最早一个；都没有时回落到 librosa `peak_seconds`；再没有就放弃自动对齐
- content_peak_t：`emotion_curve.peaks` 里 intensity 最高的；没有就放弃
- `video_anchor_seconds = content_peak_t - bgm_peak_t`
    正 → BGM 入场延迟 anchor 秒（前段静音）
    负 → 跳过 BGM 开头 |anchor| 秒（"切片"语义）
- clamp 到 `[-(bgm_dur - 1), max(0, video_dur - 1)]`，留 1s 余量防整曲被切完或落到视频外
"""
from __future__ import annotations

import logging
from typing import Optional

from ...schemas import Plan

log = logging.getLogger(__name__)

_CLIMAX_KIND_PRIORITY: dict[str, int] = {
    "climax": 0,
    "drop": 1,
    "release": 2,
    "build_start": 3,
    "break": 4,
}


def auto_align_bgm_to_emotion(plan: Plan) -> None:
    """把 BGM 能量高潮"切片"对齐到内容情绪高潮——决定 `BGMConfig.video_anchor_seconds`。

    调用时机：build_plan 收尾、PATCH /bgm 换曲后（anchor 已 reset=0 再算）。
    用户手拖锚点的 PATCH 不触发本函数（不会覆盖手动微调）。
    """
    if not plan.bgm or not plan.bgm.track_url:
        return
    bgm = plan.bgm

    bgm_peak_t: Optional[float] = None
    bgm_peak_kind = "peak_seconds"
    if bgm.analysis and bgm.analysis.climaxes:
        sorted_clx = sorted(
            bgm.analysis.climaxes,
            key=lambda c: (_CLIMAX_KIND_PRIORITY.get(c.kind, 9), c.at_seconds),
        )
        bgm_peak_t = float(sorted_clx[0].at_seconds)
        bgm_peak_kind = sorted_clx[0].kind
    if bgm_peak_t is None and bgm.peak_seconds is not None:
        bgm_peak_t = float(bgm.peak_seconds)
    if bgm_peak_t is None:
        log.info("[plan] bgm auto-align skipped plan=%s reason=no_bgm_peak", plan.plan_id)
        return

    if not plan.emotion_curve or not plan.emotion_curve.peaks:
        log.info("[plan] bgm auto-align skipped plan=%s reason=no_content_peak", plan.plan_id)
        return
    content_peak = max(plan.emotion_curve.peaks, key=lambda p: p.intensity)
    content_peak_t = float(content_peak.t)

    raw_anchor = content_peak_t - bgm_peak_t
    video_dur = float(plan.duration_seconds or 0.0)
    bgm_dur = float(bgm.duration_seconds or 0.0)
    lower = -(bgm_dur - 1.0) if bgm_dur > 1.0 else 0.0
    upper = max(0.0, video_dur - 1.0)
    anchor = max(lower, min(raw_anchor, upper))
    bgm.video_anchor_seconds = round(anchor, 2)

    log.info(
        "[plan] bgm auto-aligned plan=%s bgm_peak=%.2fs(%s) ↔ video_peak=%.2fs(intensity=%.2f) "
        "→ anchor=%.2fs (clamped from %.2fs, bgm_dur=%.1f video_dur=%.1f)",
        plan.plan_id, bgm_peak_t, bgm_peak_kind, content_peak_t, content_peak.intensity,
        bgm.video_anchor_seconds, raw_anchor, bgm_dur, video_dur,
    )
