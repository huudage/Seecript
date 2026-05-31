"""画幅参数：从 TargetPlatform 推导出 ratio 字符串 + 像素维度。

用于把 ComposeSettings.target_platform 串到：
- Seedance T2V 提交（用 ratio 字符串，如 "9:16"）
- ffmpeg color_clip / scale 滤镜（用像素 width/height）
- 渲染 pipeline 的整体画布尺寸（concat 前所有 scene 统一到这个分辨率）

平台 → 画幅对应（与 schemas.py:588-594 注释保持一致）：
- douyin / wechat / xiaohongshu  → 9:16 竖屏（1080×1920）
- bilibili                        → 16:9 横屏（1920×1080）

设计取舍：不在 ComposeSettings 上加单独的 aspect_ratio 字段——平台已经隐含了画幅，
让 LLM/Seedance/ffmpeg 三条线都查这一个映射表，保持单一真源。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AspectSpec:
    ratio: str      # Seedance 提交字段
    width: int      # ffmpeg 渲染像素
    height: int


_PLATFORM_TO_SPEC: dict[str, AspectSpec] = {
    "douyin":      AspectSpec(ratio="9:16",  width=1080, height=1920),
    "wechat":      AspectSpec(ratio="9:16",  width=1080, height=1920),
    "xiaohongshu": AspectSpec(ratio="9:16",  width=1080, height=1920),
    "bilibili":    AspectSpec(ratio="16:9",  width=1920, height=1080),
}


_DEFAULT = AspectSpec(ratio="9:16", width=1080, height=1920)


def aspect_for_platform(platform: str | None) -> AspectSpec:
    """返回该平台的 (ratio, width, height)。未知平台回落到 9:16（默认抖音）。"""
    if not platform:
        return _DEFAULT
    return _PLATFORM_TO_SPEC.get(platform.strip().lower(), _DEFAULT)
