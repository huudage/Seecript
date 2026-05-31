"""PATCH /plan/{id}/settings 路由烟测——局部翻转 ComposeSettings 字段。

校验：
1. 单字段 PATCH 仅更新该字段，其他字段保留
2. 多字段 PATCH 同时生效
3. 落盘后 plan_store.get 拿到新值（持久化）
4. 不存在的 plan_id 返回 404
5. 空 body 安全返回当前 plan
"""
from __future__ import annotations

import time

import pytest

from app.schemas import AdaptedSection, ComposeSettings, Plan, Scene
from app.services.plans.store import plan_store


_TEST_PLAN_IDS: list[str] = []


def _make_plan(plan_id: str) -> Plan:
    return Plan(
        plan_id=plan_id,
        sample_id="sample-marketing-01",
        project_id=None,
        session_id=None,
        settings=ComposeSettings(
            voiceover_enabled=True,
            tts_voice="zh_female_qingxin",
            cta="点赞收藏",
        ),
        adapted_sections=[
            AdaptedSection(
                section_id="adp-opening", role="opening", theme="开场",
                content_description="hook", source_shot_indices=[0],
                order=0, duration_seconds=3.0,
            ),
        ],
        main_track=[
            Scene(
                scene_id="sc-0", section="opening", source="user_material",
                source_ref="m-1", start=0.0, duration=3.0, narration="hi",
            ),
        ],
        packaging_track=[],
        duration_seconds=3.0,
        variant="A",
    )


@pytest.fixture(autouse=True)
def cleanup_settings():
    yield
    for plan_id in _TEST_PLAN_IDS:
        plan_store._plans.pop(plan_id, None)
    _TEST_PLAN_IDS.clear()


def test_patch_settings_single_field(client):
    plan = _make_plan(f"plan-set-1-{int(time.time() * 1000)}")
    _TEST_PLAN_IDS.append(plan.plan_id)
    plan_store.put(plan)

    resp = client.patch(
        f"/api/plan/{plan.plan_id}/settings",
        json={"voiceover_enabled": False},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["settings"]["voiceover_enabled"] is False
    # 其他字段保留
    assert body["settings"]["tts_voice"] == "zh_female_qingxin"
    assert body["settings"]["cta"] == "点赞收藏"

    refreshed = plan_store.get(plan.plan_id)
    assert refreshed is not None
    assert refreshed.settings.voiceover_enabled is False
    assert refreshed.settings.tts_voice == "zh_female_qingxin"


def test_patch_settings_multi_field(client):
    plan = _make_plan(f"plan-set-2-{int(time.time() * 1000)}")
    _TEST_PLAN_IDS.append(plan.plan_id)
    plan_store.put(plan)

    resp = client.patch(
        f"/api/plan/{plan.plan_id}/settings",
        json={
            "voiceover_enabled": False,
            "tts_voice": "zh_male_jieshuo",
            "cta": "下载试用",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["settings"]["voiceover_enabled"] is False
    assert body["settings"]["tts_voice"] == "zh_male_jieshuo"
    assert body["settings"]["cta"] == "下载试用"


def test_patch_settings_empty_body_is_noop(client):
    plan = _make_plan(f"plan-set-3-{int(time.time() * 1000)}")
    _TEST_PLAN_IDS.append(plan.plan_id)
    plan_store.put(plan)

    resp = client.patch(f"/api/plan/{plan.plan_id}/settings", json={})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["settings"]["voiceover_enabled"] is True
    assert body["settings"]["tts_voice"] == "zh_female_qingxin"


def test_patch_settings_unknown_plan_404(client):
    resp = client.patch(
        "/api/plan/plan-does-not-exist/settings",
        json={"voiceover_enabled": False},
    )
    assert resp.status_code == 404
