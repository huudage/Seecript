"""v2 D4 · AI 收编：初稿定稿 + 配口播按已确认原文落盘。"""
from __future__ import annotations

import time

import pytest

from app.schemas import Plan
from app.services.plans.store import plan_store
from tests.test_plan_canvas_ops import _make_v2_plan

_TEST_PLAN_IDS: list[str] = []


@pytest.fixture(autouse=True)
def cleanup_d4_plans():
    yield
    for plan_id in _TEST_PLAN_IDS:
        plan_store._plans.pop(plan_id, None)
    _TEST_PLAN_IDS.clear()


def _put(plan: Plan) -> Plan:
    _TEST_PLAN_IDS.append(plan.plan_id)
    plan_store.put(plan)
    return plan


def test_legacy_plan_defaults_to_confirmed():
    plan = _make_v2_plan("plan-d4-legacy")
    assert plan.structure_confirmed is True


def test_confirm_structure_is_idempotent(client):
    plan = _put(_make_v2_plan(f"plan-d4-confirm-{int(time.time() * 1000)}"))
    plan.structure_confirmed = False
    plan_store.put(plan)

    first = client.post(f"/api/plan/{plan.plan_id}/confirm-structure")
    assert first.status_code == 200, first.text
    assert first.json()["structure_confirmed"] is True
    assert plan_store.get(plan.plan_id).structure_confirmed is True

    second = client.post(f"/api/plan/{plan.plan_id}/confirm-structure")
    assert second.status_code == 200
    assert second.json()["structure_confirmed"] is True


def test_append_blank_video_block_is_empty_slot(client):
    plan = _put(_make_v2_plan(f"plan-d4-append-{int(time.time() * 1000)}"))
    before = len(plan.main_track)
    resp = client.post(
        f"/api/plan/{plan.plan_id}/sections/append",
        json={"source": "user_material"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["main_track"]) == before + 1
    added = body["main_track"][-1]
    assert added["needs_fill"] is True
    assert added["source_ref"].startswith("text-card-fill-empty-")


def test_confirm_structure_missing_plan(client):
    resp = client.post("/api/plan/plan-d4-missing/confirm-structure")
    assert resp.status_code == 404


def test_apply_voiceover_writes_confirmed_text_without_llm(client, monkeypatch):
    plan = _put(_make_v2_plan(f"plan-d4-vo-{int(time.time() * 1000)}"))

    async def _boom(*_args, **_kwargs):
        raise AssertionError("确认后的配口播不应再调 LLM")

    monkeypatch.setattr("app.services.agent.narration_agent.regenerate_narrations", _boom)

    resp = client.post(
        f"/api/plan/{plan.plan_id}/regenerate-narrations",
        json={
            "section_ids": ["sec-a"],
            "apply": True,
            "proposals": [
                {
                    "scene_id": "sc-a-0",
                    "old_narration": "口播-sc-a-0",
                    "new_narration": "确认后的这一句",
                }
            ],
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["applied"] is True
    assert body["updated_scene_ids"] == ["sc-a-0"]
    written = next(sc for sc in body["plan"]["main_track"] if sc["scene_id"] == "sc-a-0")
    assert written["narration"] == "确认后的这一句"
    assert written["voiceover_url"] is None
    untouched = next(sc for sc in body["plan"]["main_track"] if sc["scene_id"] == "sc-b-0")
    assert untouched["narration"] == "口播-sc-b-0"


def test_apply_voiceover_rejects_scene_outside_section(client):
    plan = _put(_make_v2_plan(f"plan-d4-vo-scope-{int(time.time() * 1000)}"))
    resp = client.post(
        f"/api/plan/{plan.plan_id}/regenerate-narrations",
        json={
            "section_ids": ["sec-a"],
            "apply": True,
            "proposals": [
                {"scene_id": "sc-b-0", "new_narration": "不该写入"},
            ],
        },
    )
    assert resp.status_code == 422
    stored = plan_store.get(plan.plan_id)
    assert stored.main_track[1].narration == "口播-sc-b-0"
