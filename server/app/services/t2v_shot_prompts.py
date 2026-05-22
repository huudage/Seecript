"""Preset copy for T2V「分镜演示」模式 — single source of truth for server-side merge.

Why a dedicated module (vs inline in router):
  - Router stays thin (SRP); prompt wording can evolve without touching HTTP mapping.
  - The video API only accepts one `prompt` string — we emulate a "system" layer by
    prepending a fixed instruction block before the user-selected shot body.

Keep in sync with user-facing copy on `feature-5.html` (same intent, wording may differ).
"""

from __future__ import annotations

# 控制在约 120 字以内，为「分镜描述」留出 t2v_max_prompt_chars 余量（默认 500）。
SHOT_PREVIEW_SYSTEM_PREFIX = (
    "【分镜演示·系统指令】生成约10秒竖屏短视频预览：突出镜头运动、光影与场景氛围，写实清晰；"
    "勿将口播稿逐字当作完整对白字幕铺满画面；仅用于创作前效果预期，非最终成片。"
)


def merge_shot_preview_prompt(body: str, max_chars: int) -> str:
    """Prepend the preset instruction to the shot-specific body and hard-cap length.

    If the combined string exceeds max_chars, the tail of *body* is truncated so the
    instruction prefix always remains intact (better than losing moderation hints).
    """
    body = (body or "").strip()
    prefix = SHOT_PREVIEW_SYSTEM_PREFIX + "\n\n【分镜描述】\n"
    if not body:
        return prefix.strip()
    combined = prefix + body
    if len(combined) <= max_chars:
        return combined
    room = max(0, max_chars - len(prefix))
    return prefix + body[:room]
