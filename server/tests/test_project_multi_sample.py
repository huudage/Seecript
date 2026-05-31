"""多样例项目创建 + library step 提交测试。

覆盖：
1. POST /api/project {name, sample_ids:[s1,s2]} → 200，project.sample_ids 与请求一致
2. POST /api/project/{pid}/step/library/commit {step:'library', payload:{sample_ids:[s1,s2]}}
   → project.sample_ids 被刷新成新的两份
3. ProjectCreateRequest pydantic 校验：sample_ids=[] / 3 项 → ValidationError
4. POST /api/project 单样例（兼容）→ 仍能成功
"""
from __future__ import annotations

import shutil

import pytest
from pydantic import ValidationError

from app.config import get_settings
from app.schemas import ProjectCreateRequest
from app.services.projects import project_store


_TEST_PROJECT_IDS: list[str] = []


def _clean_project(pid: str) -> None:
    project_store._by_id.pop(pid, None)
    var = get_settings().log_dir.parent / "var"
    for sub in ("projects", "uploads", "assets"):
        target = var / sub / pid
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)


@pytest.fixture(autouse=True)
def cleanup():
    yield
    for pid in _TEST_PROJECT_IDS:
        _clean_project(pid)
    _TEST_PROJECT_IDS.clear()


def test_project_create_accepts_two_sample_ids(client):
    r = client.post("/api/project", json={
        "name": "单测·MULTI-SAMPLE A",
        "sample_ids": ["sample-marketing-01", "sample-vlog-01"],
    })
    assert r.status_code == 200, r.text
    body = r.json()
    pid = body["project_id"]
    _TEST_PROJECT_IDS.append(pid)
    assert body["sample_ids"] == ["sample-marketing-01", "sample-vlog-01"]

    # 落盘后 store 也能反查到
    proj = project_store.get(pid)
    assert proj is not None
    assert proj.sample_ids == ["sample-marketing-01", "sample-vlog-01"]


def test_project_create_rejects_three_sample_ids(client):
    r = client.post("/api/project", json={
        "name": "单测·MULTI-SAMPLE 超限",
        "sample_ids": ["sample-marketing-01", "sample-vlog-01", "sample-motion-01"],
    })
    # pydantic v2 把 max_length 超限报 422
    assert r.status_code in (400, 422), r.text


def test_project_create_rejects_empty_sample_ids(client):
    r = client.post("/api/project", json={
        "name": "单测·空 sample_ids",
        "sample_ids": [],
    })
    assert r.status_code in (400, 422), r.text


def test_project_create_rejects_unknown_sample_id(client):
    """任一 sample_id 不存在 → 404，且不会落盘半成品。"""
    r = client.post("/api/project", json={
        "name": "单测·MULTI 不存在",
        "sample_ids": ["sample-marketing-01", "sample-nonexistent-zzz"],
    })
    assert r.status_code == 404, r.text


def test_project_create_request_pydantic_constraints():
    """直接构造 ProjectCreateRequest 也应受 min_length=1 / max_length=2 约束。"""
    with pytest.raises(ValidationError):
        ProjectCreateRequest(name="x", sample_ids=[])
    with pytest.raises(ValidationError):
        ProjectCreateRequest(name="x", sample_ids=["a", "b", "c"])
    # 1 / 2 合法
    ok1 = ProjectCreateRequest(name="x", sample_ids=["a"])
    assert ok1.sample_ids == ["a"]
    ok2 = ProjectCreateRequest(name="x", sample_ids=["a", "b"])
    assert ok2.sample_ids == ["a", "b"]


def test_library_step_commit_refreshes_sample_ids(client):
    """先建单样例项目，再走 library step commit 把 sample_ids 改成两份 → project 被刷新。"""
    r = client.post("/api/project", json={
        "name": "单测·LIB STEP MULTI",
        "sample_ids": ["sample-marketing-01"],
    })
    pid = r.json()["project_id"]
    _TEST_PROJECT_IDS.append(pid)
    assert project_store.get(pid).sample_ids == ["sample-marketing-01"]

    # commit library step with two samples
    rc = client.post(f"/api/project/{pid}/step/library/commit", json={
        "step": "library",
        "saved_at": 0.0,
        "payload": {"sample_ids": ["sample-marketing-01", "sample-vlog-01"]},
    })
    assert rc.status_code == 200, rc.text

    # project.sample_ids 已被刷成两份
    refreshed = project_store.get(pid)
    assert refreshed is not None
    assert refreshed.sample_ids == ["sample-marketing-01", "sample-vlog-01"]


def test_library_step_commit_ignores_invalid_payload(client):
    """payload 既没 sample_ids 也没合法长度时，不应崩，也不应改 project.sample_ids。"""
    r = client.post("/api/project", json={
        "name": "单测·LIB STEP BAD",
        "sample_ids": ["sample-marketing-01"],
    })
    pid = r.json()["project_id"]
    _TEST_PROJECT_IDS.append(pid)

    # 空 payload → 不改 sample_ids
    rc = client.post(f"/api/project/{pid}/step/library/commit", json={
        "step": "library", "saved_at": 0.0, "payload": {},
    })
    assert rc.status_code == 200, rc.text
    assert project_store.get(pid).sample_ids == ["sample-marketing-01"]

    # 超过 2 个 → side effect 不生效（store 保持单样例）
    rc = client.post(f"/api/project/{pid}/step/library/commit", json={
        "step": "library", "saved_at": 0.0,
        "payload": {"sample_ids": ["s1", "s2", "s3"]},
    })
    assert rc.status_code == 200, rc.text
    assert project_store.get(pid).sample_ids == ["sample-marketing-01"]
