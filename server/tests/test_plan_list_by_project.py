"""PlanStore.list_by_project + GET /plan / /gap 查询接口测试。"""
from __future__ import annotations

import shutil

import pytest

from app.config import get_settings
from app.schemas import Gap, Plan, Scene
from app.services.materials import gap_store
from app.services.plans import plan_store
from app.services.projects import project_store


_TEST_PROJECT_IDS: list[str] = []
_TEST_PLAN_IDS: list[str] = []


def _clean_project(pid: str) -> None:
    project_store._by_id.pop(pid, None)
    var = get_settings().log_dir.parent / "var"
    for sub in ("projects", "uploads", "assets"):
        target = var / sub / pid
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)


def _clean_plan(plan_id: str) -> None:
    plan_store._plans.pop(plan_id, None)
    gap_store._by_plan.pop(plan_id, None)


@pytest.fixture(autouse=True)
def cleanup():
    yield
    for pid in _TEST_PROJECT_IDS:
        _clean_project(pid)
    for plan_id in _TEST_PLAN_IDS:
        _clean_plan(plan_id)
    _TEST_PROJECT_IDS.clear()
    _TEST_PLAN_IDS.clear()


def _mk_plan(plan_id: str, project_id: str) -> Plan:
    return Plan(
        plan_id=plan_id,
        sample_ids=["sample-marketing-01"],
        project_id=project_id,
        session_id=project_id,
        variant="A",
        duration_seconds=4.0,
        main_track=[Scene(scene_id="sc-0", section="opening", source="text_card",
                          source_ref="t", start=0.0, duration=4.0)],
        packaging_track=[],
    )


def test_list_by_project_returns_only_owned_plans():
    a = project_store.create(name="单测·LIST-PLAN A", sample_ids=["sample-marketing-01"])
    b = project_store.create(name="单测·LIST-PLAN B", sample_ids=["sample-marketing-01"])
    _TEST_PROJECT_IDS.extend([a.project_id, b.project_id])

    plan_a1 = _mk_plan("plan-aaa-001", a.project_id)
    plan_a2 = _mk_plan("plan-aaa-002", a.project_id)
    plan_b1 = _mk_plan("plan-bbb-001", b.project_id)
    for p in (plan_a1, plan_a2, plan_b1):
        plan_store.put(p)
        _TEST_PLAN_IDS.append(p.plan_id)

    a_plans = plan_store.list_by_project(a.project_id)
    assert {p.plan_id for p in a_plans} == {"plan-aaa-001", "plan-aaa-002"}
    b_plans = plan_store.list_by_project(b.project_id)
    assert [p.plan_id for p in b_plans] == ["plan-bbb-001"]


def test_get_plan_endpoint_filters_by_project(client):
    a = project_store.create(name="单测·HTTP-PLAN", sample_ids=["sample-marketing-01"])
    _TEST_PROJECT_IDS.append(a.project_id)
    p = _mk_plan("plan-zzz-9", a.project_id)
    plan_store.put(p)
    _TEST_PLAN_IDS.append(p.plan_id)

    r = client.get(f"/api/plan?project_id={a.project_id}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert any(item["plan_id"] == "plan-zzz-9" for item in body)


def test_get_gap_endpoint_returns_by_plan(client):
    a = project_store.create(name="单测·HTTP-GAP", sample_ids=["sample-marketing-01"])
    _TEST_PROJECT_IDS.append(a.project_id)
    plan_id = "plan-gap-test-1"
    _TEST_PLAN_IDS.append(plan_id)
    gap = Gap(
        gap_id=f"gap-opening-0-0-{plan_id}",
        section_id="sec-0",
        section="opening",
        slot_index=0,
        requirement="开场",
        status="miss",
        project_id=a.project_id,
    )
    gap_store.put(plan_id, [gap])

    r = client.get(f"/api/gap?plan_id={plan_id}")
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 1
    assert body[0]["section_id"] == "sec-0"
