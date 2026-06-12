"""stage-80 (2026-06-12) `_resolve_scene_path` 缩略图过滤回归。

bug：上传 user_material 时同 material_id 同时落 `<id>_thumb.jpg` 和 `<id>_<file>.mov`，
原 `if source_ref in f.name: return f` 在 ext4 readdir 顺序下偶发命中 .jpg → ffmpeg
当成视频去 trim 拿不到流 → preview/render 回退 text_card 占位（用户看到的 0:02 黑屏 +
99% scene 走 text_card 的现象）。

修复：视频后缀优先 + 缩略图扩展名硬过滤。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.schemas import (
    AdaptedSection,
    ComposeSettings,
    Plan,
    Scene,
    ShotPlan,
)


def _make_plan_with_session(session_id: str, source_ref: str) -> Plan:
    return Plan(
        plan_id=f"plan-resolve-{source_ref[:6]}",
        sample_ids=["sample-marketing-01"],
        project_id="proj-resolve",
        session_id=session_id,
        settings=ComposeSettings(),
        adapted_sections=[
            AdaptedSection(
                section_id="sec-0", role="hook", theme="开场",
                content_description="占位", source_shot_indices=[0],
                order=0, duration_seconds=3.0,
                shots=[ShotPlan(order=0, subject="x", visual="x",
                                narration="x", duration_seconds=3.0)],
            ),
        ],
        main_track=[
            Scene(
                scene_id="sc-0", section="hook", parent_section_id="sec-0",
                shot_order=0, shot_subject="x",
                source="user_material", source_ref=source_ref,
                start=0.0, duration=3.0, narration="x",
            ),
        ],
        packaging_track=[],
        duration_seconds=3.0,
        variant="A",
    )


def test_resolve_scene_path_prefers_video_over_thumb(monkeypatch, tmp_path):
    """同 material_id 下 thumb.jpg + .mov 共存：必须返回 .mov，不能返 thumb。"""
    from app.services.render import pipeline as pipeline_svc

    session_id = "sess-001"
    material_id = "abc123def456"
    uploads = tmp_path / "uploads" / session_id
    uploads.mkdir(parents=True)
    # 故意让 thumb 先创建（部分文件系统 readdir 会按 inode 顺序返回）
    (uploads / f"{material_id}_thumb.jpg").write_bytes(b"\xff\xd8\xff")  # JPEG magic
    (uploads / f"{material_id}_VideoFile.mov").write_bytes(b"\x00" * 256)

    monkeypatch.setattr(pipeline_svc, "_uploads_root", lambda: tmp_path / "uploads")

    plan = _make_plan_with_session(session_id, material_id)
    resolved = pipeline_svc._resolve_scene_path(plan, plan.main_track[0])
    assert resolved is not None
    assert resolved.suffix.lower() == ".mov", f"应返回 .mov，实际 {resolved.name}"


def test_resolve_scene_path_skips_all_image_extensions(monkeypatch, tmp_path):
    """material_id 只匹到缩略图（jpg/png/webp 等）→ 应返 None，不让 ffmpeg 试着 trim 图片。"""
    from app.services.render import pipeline as pipeline_svc

    session_id = "sess-002"
    material_id = "onlythumbs99"
    uploads = tmp_path / "uploads" / session_id
    uploads.mkdir(parents=True)
    for ext in (".jpg", ".jpeg", ".png", ".webp", ".gif"):
        (uploads / f"{material_id}_thumb{ext}").write_bytes(b"\x00" * 64)

    monkeypatch.setattr(pipeline_svc, "_uploads_root", lambda: tmp_path / "uploads")

    plan = _make_plan_with_session(session_id, material_id)
    resolved = pipeline_svc._resolve_scene_path(plan, plan.main_track[0])
    assert resolved is None, f"只有缩略图时必须返 None，实际 {resolved}"


def test_resolve_scene_path_returns_none_when_session_id_missing():
    """plan.session_id 为 None → 一律返 None（不要降级到 cross-session 搜）。"""
    from app.services.render import pipeline as pipeline_svc

    plan = _make_plan_with_session("sess-x", "ref-y")
    plan.session_id = None
    assert pipeline_svc._resolve_scene_path(plan, plan.main_track[0]) is None


def test_resolve_scene_path_handles_video_with_unknown_extension(monkeypatch, tmp_path):
    """老素材没扩展名（或扩展名不在白名单）但也不是缩略图 → 退而求其次返第一个候选。"""
    from app.services.render import pipeline as pipeline_svc

    session_id = "sess-003"
    material_id = "legacy00"
    uploads = tmp_path / "uploads" / session_id
    uploads.mkdir(parents=True)
    (uploads / f"{material_id}_thumb.jpg").write_bytes(b"\xff\xd8\xff")
    (uploads / f"{material_id}_oldformat").write_bytes(b"\x00" * 256)

    monkeypatch.setattr(pipeline_svc, "_uploads_root", lambda: tmp_path / "uploads")

    plan = _make_plan_with_session(session_id, material_id)
    resolved = pipeline_svc._resolve_scene_path(plan, plan.main_track[0])
    assert resolved is not None
    assert resolved.name.endswith("_oldformat"), f"应返非缩略图候选，实际 {resolved.name}"
