"""Unit tests for T2V shot-preview prompt merge."""
from __future__ import annotations

from app.services.t2v_shot_prompts import SHOT_PREVIEW_SYSTEM_PREFIX, merge_shot_preview_prompt


def test_merge_keeps_prefix_when_truncating() -> None:
    body = "x" * 600
    out = merge_shot_preview_prompt(body, max_chars=500)
    assert out.startswith(SHOT_PREVIEW_SYSTEM_PREFIX)
    assert len(out) == 500


def test_merge_short_body_unchanged_length() -> None:
    body = "咖啡杯特写，暖光木桌。"
    out = merge_shot_preview_prompt(body, max_chars=500)
    assert SHOT_PREVIEW_SYSTEM_PREFIX in out
    assert body in out
    assert len(out) < 500
