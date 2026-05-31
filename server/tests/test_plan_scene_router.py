"""PATCH /plan/{id}/scene/{scene_id} 路由烟测——直接编辑 Scene + 联动 AdaptedSection。

校验：
1. 仅改 narration：Scene.narration 更新；AdaptedSection 不动
2. 改 theme + content_description：联动到对应 AdaptedSection（按 sc-<order> 解析 order）
3. 多字段同时改
4. 不存在的 plan_id 返回 404
5. 不存在的 scene_id 返回 404
6. 空 body 安全返回当前 plan
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
        sample_ids=["sample-marketing-01"],
        project_id=None,
        session_id=None,
        settings=ComposeSettings(voiceover_enabled=True, tts_voice="zh_female_qingxin"),
        adapted_sections=[
            AdaptedSection(
                section_id="adp-opening",
                role="opening",
                theme="原开场",
                content_description="原描述-开场",
                source_shot_indices=[0],
                order=0,
                duration_seconds=3.0,
            ),
            AdaptedSection(
                section_id="adp-development",
                role="development",
                theme="原发展",
                content_description="原描述-发展",
                source_shot_indices=[1],
                order=1,
                duration_seconds=4.0,
            ),
        ],
        main_track=[
            Scene(
                scene_id="sc-0", section="opening", source="user_material",
                source_ref="m-1", start=0.0, duration=3.0, narration="原口播0",
            ),
            Scene(
                scene_id="sc-1", section="development", source="user_material",
                source_ref="m-2", start=3.0, duration=4.0, narration="原口播1",
            ),
        ],
        packaging_track=[],
        duration_seconds=7.0,
        variant="A",
    )


@pytest.fixture(autouse=True)
def cleanup_scene_plans():
    yield
    for plan_id in _TEST_PLAN_IDS:
        plan_store._plans.pop(plan_id, None)
    _TEST_PLAN_IDS.clear()


def test_patch_scene_narration_only(client):
    plan = _make_plan(f"plan-scene-1-{int(time.time() * 1000)}")
    _TEST_PLAN_IDS.append(plan.plan_id)
    plan_store.put(plan)

    resp = client.patch(
        f"/api/plan/{plan.plan_id}/scene/sc-0",
        json={"narration": "改后的口播"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    sc0 = next(s for s in body["main_track"] if s["scene_id"] == "sc-0")
    assert sc0["narration"] == "改后的口播"

    # AdaptedSection 不动
    adp0 = next(a for a in body["adapted_sections"] if a["order"] == 0)
    assert adp0["theme"] == "原开场"
    assert adp0["content_description"] == "原描述-开场"


def test_patch_scene_theme_and_content_updates_section(client):
    plan = _make_plan(f"plan-scene-2-{int(time.time() * 1000)}")
    _TEST_PLAN_IDS.append(plan.plan_id)
    plan_store.put(plan)

    resp = client.patch(
        f"/api/plan/{plan.plan_id}/scene/sc-1",
        json={"theme": "新发展", "content_description": "新描述-发展"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    # AdaptedSection.order==1 应被更新
    adp1 = next(a for a in body["adapted_sections"] if a["order"] == 1)
    assert adp1["theme"] == "新发展"
    assert adp1["content_description"] == "新描述-发展"

    # order==0 不动
    adp0 = next(a for a in body["adapted_sections"] if a["order"] == 0)
    assert adp0["theme"] == "原开场"

    # Scene.narration 不动
    sc1 = next(s for s in body["main_track"] if s["scene_id"] == "sc-1")
    assert sc1["narration"] == "原口播1"


def test_patch_scene_multi_field(client):
    plan = _make_plan(f"plan-scene-3-{int(time.time() * 1000)}")
    _TEST_PLAN_IDS.append(plan.plan_id)
    plan_store.put(plan)

    resp = client.patch(
        f"/api/plan/{plan.plan_id}/scene/sc-0",
        json={
            "narration": "全新口播",
            "theme": "全新主题",
            "content_description": "全新描述",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    sc0 = next(s for s in body["main_track"] if s["scene_id"] == "sc-0")
    assert sc0["narration"] == "全新口播"
    adp0 = next(a for a in body["adapted_sections"] if a["order"] == 0)
    assert adp0["theme"] == "全新主题"
    assert adp0["content_description"] == "全新描述"


def test_patch_scene_unknown_plan_404(client):
    resp = client.patch(
        "/api/plan/plan-not-exist/scene/sc-0",
        json={"narration": "x"},
    )
    assert resp.status_code == 404


def test_patch_scene_unknown_scene_404(client):
    plan = _make_plan(f"plan-scene-4-{int(time.time() * 1000)}")
    _TEST_PLAN_IDS.append(plan.plan_id)
    plan_store.put(plan)

    resp = client.patch(
        f"/api/plan/{plan.plan_id}/scene/sc-999",
        json={"narration": "x"},
    )
    assert resp.status_code == 404


def test_patch_scene_empty_body_noop(client):
    plan = _make_plan(f"plan-scene-5-{int(time.time() * 1000)}")
    _TEST_PLAN_IDS.append(plan.plan_id)
    plan_store.put(plan)

    resp = client.patch(f"/api/plan/{plan.plan_id}/scene/sc-0", json={})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    sc0 = next(s for s in body["main_track"] if s["scene_id"] == "sc-0")
    assert sc0["narration"] == "原口播0"
