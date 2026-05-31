"""StepStore 状态机 + /api/.../step 路由测试。

覆盖：
1. save：被 commit 的步骤 → saved；下游 saved→dirty / pending 保持
2. 顺序 commit：current_step 推进；末步停留
3. 回退后重新 commit：下游再次打 dirty，产物（snapshot）保留
4. get/list：从未 commit → None / 空
5. HTTP：commit/get/list + step 不一致 400 + 未知 project 404
"""
from __future__ import annotations

import shutil

import pytest

from app.config import get_settings
from app.schemas import StepSnapshot
from app.services.projects import project_store, step_store


_TEST_PROJECT_IDS: list[str] = []


def _clean_project(pid: str) -> None:
    project_store._by_id.pop(pid, None)
    var = get_settings().log_dir.parent / "var"
    for sub in ("projects", "uploads", "assets"):
        target = var / sub / pid
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)


@pytest.fixture(autouse=True)
def cleanup_projects():
    yield
    for pid in _TEST_PROJECT_IDS:
        _clean_project(pid)
    _TEST_PROJECT_IDS.clear()


def _track(pid: str) -> str:
    _TEST_PROJECT_IDS.append(pid)
    return pid


def _snap(step: str, **payload) -> StepSnapshot:
    return StepSnapshot(step=step, saved_at=0.0, payload=payload)


# -------- StepStore 单元 --------

def test_commit_marks_step_saved_and_advances_current():
    proj = project_store.create(name="单测·STEP A", sample_id="sample-marketing-01")
    _track(proj.project_id)
    assert proj.step_states.library == "pending"
    assert proj.current_step == "library"

    updated = step_store.save(proj.project_id, _snap("library", sample_id="sample-marketing-01"))
    assert updated.step_states.library == "saved"
    # 下游保持 pending（之前没 saved 过）
    assert updated.step_states.decompose == "pending"
    assert updated.step_states.compose == "pending"
    # current_step 推进到下一步
    assert updated.current_step == "decompose"


def test_sequential_commit_progresses_to_render_and_stays():
    proj = project_store.create(name="单测·STEP SEQ", sample_id="sample-marketing-01")
    _track(proj.project_id)
    for step in ("library", "decompose", "compose", "render"):
        payload = {"plan_id": "plan-x"} if step == "compose" else (
            {"job_id": "job-x"} if step == "render" else {"sample_id": "sample-marketing-01"}
        )
        updated = step_store.save(proj.project_id, _snap(step, **payload))
    # 全部 saved
    assert updated.step_states.model_dump() == {
        "library": "saved", "decompose": "saved", "compose": "saved", "render": "saved",
    }
    # 末步 commit 后 current_step 停在 render
    assert updated.current_step == "render"


def test_recommit_upstream_marks_downstream_dirty_but_keeps_snapshot():
    proj = project_store.create(name="单测·STEP DIRTY", sample_id="sample-marketing-01")
    _track(proj.project_id)
    # 全流程跑一遍
    step_store.save(proj.project_id, _snap("library", sample_id="sample-marketing-01"))
    step_store.save(proj.project_id, _snap("decompose", sample_id="sample-marketing-01"))
    step_store.save(proj.project_id, _snap("compose", plan_id="plan-1"))
    step_store.save(proj.project_id, _snap("render", job_id="job-1"))

    # 回到 decompose 重新 commit → compose/render 应变 dirty
    updated = step_store.save(proj.project_id, _snap("decompose", sample_id="sample-marketing-01"))
    assert updated.step_states.decompose == "saved"
    assert updated.step_states.compose == "dirty"
    assert updated.step_states.render == "dirty"
    assert updated.step_states.library == "saved"  # 上游不动

    # 下游 snapshot 文件保留（产物不删）
    compose_snap = step_store.get(proj.project_id, "compose")
    assert compose_snap is not None
    assert compose_snap.payload["plan_id"] == "plan-1"


def test_get_returns_none_when_never_committed():
    proj = project_store.create(name="单测·STEP NONE", sample_id="sample-marketing-01")
    _track(proj.project_id)
    assert step_store.get(proj.project_id, "compose") is None
    assert step_store.list(proj.project_id) == []


# -------- HTTP --------

def test_http_commit_get_list(client):
    r = client.post("/api/project", json={"name": "单测·STEP HTTP", "sample_id": "sample-marketing-01"})
    pid = r.json()["project_id"]
    _track(pid)

    # commit library
    rc = client.post(f"/api/project/{pid}/step/library/commit", json={
        "step": "library", "saved_at": 0.0, "payload": {"sample_id": "sample-marketing-01"},
    })
    assert rc.status_code == 200, rc.text
    body = rc.json()
    assert body["step_states"]["library"] == "saved"
    assert body["current_step"] == "decompose"

    # get single
    rg = client.get(f"/api/project/{pid}/step/library")
    assert rg.status_code == 200
    assert rg.json()["payload"]["sample_id"] == "sample-marketing-01"

    # get never-committed → null
    rnull = client.get(f"/api/project/{pid}/step/render")
    assert rnull.status_code == 200
    assert rnull.json() is None

    # list
    rl = client.get(f"/api/project/{pid}/steps")
    assert rl.status_code == 200
    assert len(rl.json()) == 1


def test_http_commit_step_mismatch_400(client):
    r = client.post("/api/project", json={"name": "单测·STEP MM", "sample_id": "sample-marketing-01"})
    pid = r.json()["project_id"]
    _track(pid)
    rc = client.post(f"/api/project/{pid}/step/library/commit", json={
        "step": "compose", "saved_at": 0.0, "payload": {},
    })
    assert rc.status_code == 400


def test_http_commit_unknown_project_404(client):
    rc = client.post("/api/project/bogus-pid/step/library/commit", json={
        "step": "library", "saved_at": 0.0, "payload": {},
    })
    assert rc.status_code == 404


def test_http_compose_commit_marks_planned(client):
    r = client.post("/api/project", json={"name": "单测·STEP PLAN", "sample_id": "sample-marketing-01"})
    pid = r.json()["project_id"]
    _track(pid)
    rc = client.post(f"/api/project/{pid}/step/compose/commit", json={
        "step": "compose", "saved_at": 0.0, "payload": {"plan_id": "plan-abc", "fill_ids": []},
    })
    assert rc.status_code == 200, rc.text
    body = rc.json()
    assert body["last_plan_id"] == "plan-abc"
    assert body["status"] == "planned"
