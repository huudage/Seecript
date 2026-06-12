"""stage-80 (2026-06-12) POST /plan/{plan_id}/preview-mainline 回归。

用户原话："实在不行视频预览这里换个技术栈"——把 Remotion <Video> 浏览器侧 currentTime
seek 抖动导致的「单镜头内复读前 0.X 秒」彻底切掉，后端实时合成主轨 mp4，前端单 video 播。

覆盖：
1. 正常 plan：返回 url + signature + duration_seconds，磁盘 mp4 落地非空
2. signature 稳定：同 plan 调两次 → 第二次命中缓存（mtime 不变，避免重跑 ffmpeg）
3. plan 变更（main_track 改） → signature 变，合新 mp4
4. plan 不存在 → 404
5. signature 字段：影响主轨画面/时长/顺序的字段才参与（BGM / 字幕 / packaging 改动不影响）
"""
from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import patch

import pytest

from app.schemas import (
    AdaptedSection,
    BGMConfig,
    ComposeSettings,
    Plan,
    Scene,
    ShotPlan,
)
from app.services.plans.store import plan_store


_TEST_PLAN_IDS: list[str] = []


def _make_minimal_plan(plan_id: str) -> Plan:
    return Plan(
        plan_id=plan_id,
        sample_ids=["sample-marketing-01"],
        project_id="proj-preview-test",
        session_id=None,
        settings=ComposeSettings(),
        adapted_sections=[
            AdaptedSection(
                section_id="sec-0",
                role="hook",
                theme="开场",
                content_description="主播口播",
                source_shot_indices=[0],
                order=0,
                duration_seconds=3.0,
                shots=[ShotPlan(order=0, subject="hook 主体", visual="hook 画面",
                                narration="hook 旁白", duration_seconds=3.0)],
            ),
        ],
        main_track=[
            Scene(
                scene_id="sc-0-sh-0",
                section="hook",
                parent_section_id="sec-0",
                shot_order=0,
                shot_subject="hook 主体",
                source="text_card",  # text_card 路径不依赖外部素材，最稳
                source_ref="placeholder-sec-0-sh-0",
                start=0.0,
                duration=3.0,
                narration="hook 旁白",
            ),
        ],
        packaging_track=[],
        duration_seconds=3.0,
        variant="A",
    )


@pytest.fixture(autouse=True)
def _cleanup():
    yield
    for pid in _TEST_PLAN_IDS:
        plan_store._plans.pop(pid, None)
    _TEST_PLAN_IDS.clear()


def test_signature_stable_across_calls(client):
    """compute_signature 必须可复现：同 plan 两次调用产同样 signature → 缓存命中。"""
    from app.services.render.preview import compute_signature

    plan = _make_minimal_plan(f"plan-prev-sig-{int(time.time() * 1000)}")
    _TEST_PLAN_IDS.append(plan.plan_id)
    sig1 = compute_signature(plan)
    sig2 = compute_signature(plan)
    assert sig1 == sig2
    assert len(sig1) == 16  # sha1[:16]


def test_signature_changes_on_main_track_edit(client):
    """改了 scene.duration → signature 变 → 必须重渲。"""
    from app.services.render.preview import compute_signature

    plan = _make_minimal_plan(f"plan-prev-edit-{int(time.time() * 1000)}")
    _TEST_PLAN_IDS.append(plan.plan_id)
    sig_before = compute_signature(plan)

    plan.main_track[0] = plan.main_track[0].model_copy(update={"duration": 5.0})
    sig_after = compute_signature(plan)
    assert sig_before != sig_after


def test_signature_ignores_bgm_and_packaging(client):
    """改 BGM / packaging_track → signature 不变（这些不进主轨预览）。"""
    from app.schemas import PackagingItem
    from app.services.render.preview import compute_signature

    plan = _make_minimal_plan(f"plan-prev-ign-{int(time.time() * 1000)}")
    _TEST_PLAN_IDS.append(plan.plan_id)
    sig_before = compute_signature(plan)

    # 改 BGM 不影响 signature
    plan.bgm = BGMConfig(bgm_asset_id="any-bgm", volume=0.5)
    # 加 packaging item 不影响 signature
    plan.packaging_track = [
        PackagingItem(
            item_id="pkg-0", kind="subtitle", layer="bottom",
            start=0.0, end=2.0, text="测试字幕",
        ),
    ]
    sig_after = compute_signature(plan)
    assert sig_before == sig_after


def test_post_preview_returns_url_and_writes_mp4(client, monkeypatch, tmp_path):
    """端到端：返回 url + signature + duration；磁盘文件非空。

    用 monkeypatch 把 ffmpeg.concat / _render_text_card 短路成写假文件，避免依赖系统 ffmpeg。
    """
    from app.services.render import preview as preview_svc
    from app.services.video import ffmpeg as ffmpeg_svc

    plan = _make_minimal_plan(f"plan-prev-end-{int(time.time() * 1000)}")
    _TEST_PLAN_IDS.append(plan.plan_id)
    plan_store.put(plan)

    # mock _render_text_card → 写一个假 mp4 进 segments_dir
    def fake_render_text_card(scene, segments_dir, idx, *, width, height):
        out = Path(segments_dir) / f"text-{idx:02d}.mp4"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"\x00" * 256)
        return out

    # mock ffmpeg.concat → 写假 mp4 到 dst
    def fake_concat(inputs, dst, *, reencode=False):
        Path(dst).parent.mkdir(parents=True, exist_ok=True)
        Path(dst).write_bytes(b"\x00" * 1024)
        return Path(dst)

    monkeypatch.setattr(preview_svc, "_render_text_card", fake_render_text_card)
    monkeypatch.setattr(ffmpeg_svc, "concat", fake_concat)
    monkeypatch.setattr(ffmpeg_svc, "ffmpeg_available", lambda: True)

    resp = client.post(f"/api/plan/{plan.plan_id}/preview-mainline")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["plan_id"] == plan.plan_id
    assert len(body["signature"]) == 16
    assert body["url"].startswith("/preview/")
    assert body["url"].endswith(".mp4")
    assert body["duration_seconds"] == pytest.approx(3.0)

    # 实际磁盘文件落地了
    expected_path = preview_svc._preview_path(plan.plan_id, body["signature"])
    assert expected_path.exists()
    assert expected_path.stat().st_size > 0


def test_post_preview_uses_cache_on_second_call(client, monkeypatch):
    """第二次调（plan 没改）→ ffmpeg.concat 不应再被调用，文件 mtime 不变。"""
    from app.services.render import preview as preview_svc
    from app.services.video import ffmpeg as ffmpeg_svc

    plan = _make_minimal_plan(f"plan-prev-cache-{int(time.time() * 1000)}")
    _TEST_PLAN_IDS.append(plan.plan_id)
    plan_store.put(plan)

    def fake_render_text_card(scene, segments_dir, idx, *, width, height):
        out = Path(segments_dir) / f"text-{idx:02d}.mp4"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"\x00" * 256)
        return out

    concat_calls = {"count": 0}

    def counting_concat(inputs, dst, *, reencode=False):
        concat_calls["count"] += 1
        Path(dst).parent.mkdir(parents=True, exist_ok=True)
        Path(dst).write_bytes(b"\x00" * 1024)
        return Path(dst)

    monkeypatch.setattr(preview_svc, "_render_text_card", fake_render_text_card)
    monkeypatch.setattr(ffmpeg_svc, "concat", counting_concat)
    monkeypatch.setattr(ffmpeg_svc, "ffmpeg_available", lambda: True)

    # 第一次：合成
    r1 = client.post(f"/api/plan/{plan.plan_id}/preview-mainline")
    assert r1.status_code == 200
    sig = r1.json()["signature"]
    first_path = preview_svc._preview_path(plan.plan_id, sig)
    first_mtime = first_path.stat().st_mtime
    assert concat_calls["count"] == 1

    # 第二次：命中缓存
    r2 = client.post(f"/api/plan/{plan.plan_id}/preview-mainline")
    assert r2.status_code == 200
    assert r2.json()["signature"] == sig
    assert first_path.stat().st_mtime == first_mtime, "缓存命中不能重写文件"
    assert concat_calls["count"] == 1, "缓存命中不该再调 ffmpeg.concat"


def test_post_preview_unknown_plan_returns_404(client):
    resp = client.post("/api/plan/no-such-plan/preview-mainline")
    assert resp.status_code == 404
