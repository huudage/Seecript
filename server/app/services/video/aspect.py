"""画幅参数：从 ComposeSettings 推导出 ratio 字符串 + 像素维度。

用于把 ComposeSettings.aspect_ratio（v2，独立字段，user 可选）串到：
- Seedance T2V 提交（用 ratio 字符串，如 "9:16"）
- ffmpeg color_clip / scale 滤镜（用像素 width/height）
- 渲染 pipeline 的整体画布尺寸（concat 前所有 scene 统一到这个分辨率）

v2 起：『目标平台』管节奏/字幕风格，『画面比例』独立字段。常见场景如"B 站发竖屏"
不再被强制硬绑。老 plan 没 aspect_ratio 时回落到 platform→ratio 旧映射，保兼容。

回落映射（与 schemas.py 旧映射一致）：
- douyin / wechat / xiaohongshu  → 9:16 竖屏（1080×1920）
- bilibili                        → 16:9 横屏（1920×1080）
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ...schemas import ComposeSettings


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


_RATIO_TO_SPEC: dict[str, AspectSpec] = {
    "9:16": AspectSpec(ratio="9:16", width=1080, height=1920),
    "16:9": AspectSpec(ratio="16:9", width=1920, height=1080),
    "1:1":  AspectSpec(ratio="1:1",  width=1080, height=1080),
}


_DEFAULT = AspectSpec(ratio="9:16", width=1080, height=1920)


def aspect_for_platform(platform: str | None) -> AspectSpec:
    """老入口：仅按平台推断（v1 兼容）。新代码用 aspect_for_settings。"""
    if not platform:
        return _DEFAULT
    return _PLATFORM_TO_SPEC.get(platform.strip().lower(), _DEFAULT)


def aspect_for_ratio(ratio: str | None) -> AspectSpec:
    """按 '9:16'/'16:9'/'1:1' 字面值返回 AspectSpec。未知值回落 9:16。"""
    if not ratio:
        return _DEFAULT
    return _RATIO_TO_SPEC.get(ratio.strip(), _DEFAULT)


def aspect_for_settings(settings: "ComposeSettings | None") -> AspectSpec:
    """从 ComposeSettings 解析画幅。优先 settings.aspect_ratio（v2 显式字段），
    缺失时回落到 aspect_for_platform(target_platform)（兼容老 plan）。"""
    if settings is None:
        return _DEFAULT
    ratio = getattr(settings, "aspect_ratio", None)
    if ratio:
        return aspect_for_ratio(ratio)
    return aspect_for_platform(getattr(settings, "target_platform", None))

