"""ProjectStore CRUD + 持久化往返测试。

覆盖：
1. create → get：新建项目能立刻取回
2. update：部分字段更新，updated_at 自动刷新
3. list 倒序
4. delete 级联清盘
5. 重启（重建 store 实例）后从磁盘恢复
6. CRUD API 端点（POST/GET/PATCH/DELETE）
"""
from __future__ import annotations

import shutil
import time

import pytest

from app.config import get_settings
from app.schemas import ComposeSettings
from app.services.projects import project_store
from app.services.projects.store import ProjectStore


_TEST_PROJECT_IDS: list[str] = []


def _clean_project(pid: str) -> None:
    """清掉 store 内存 + var/projects/<pid>/、uploads、assets。"""
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


def test_create_then_get_roundtrip():
    proj = project_store.create(name="单测·CRUD A", sample_id="sample-marketing-01")
    _track(proj.project_id)
    assert proj.project_id
    assert proj.status == "draft"
    fetched = project_store.get(proj.project_id)
    assert fetched is not None
    assert fetched.name == "单测·CRUD A"
    assert fetched.sample_id == "sample-marketing-01"


def test_update_changes_updated_at():
    proj = project_store.create(name="单测·UPD", sample_id="sample-marketing-01")
    _track(proj.project_id)
    t0 = proj.updated_at
    time.sleep(0.01)
    updated = project_store.update(proj.project_id, brief="新简报")
    assert updated.brief == "新简报"
    assert updated.updated_at > t0
    # 第二次 update 无变化时 updated_at 应保持
    same = project_store.update(proj.project_id, brief="新简报")
    assert same.updated_at == updated.updated_at


def test_update_unknown_field_raises():
    from app.services.projects.store import ProjectStoreError
    proj = project_store.create(name="单测·BADFIELD", sample_id="sample-marketing-01")
    _track(proj.project_id)
    with pytest.raises(ProjectStoreError):
        project_store.update(proj.project_id, weird_unknown_field="x")


def test_list_returns_in_updated_at_desc():
    a = project_store.create(name="单测·LIST a", sample_id="sample-marketing-01")
    _track(a.project_id)
    time.sleep(0.01)
    b = project_store.create(name="单测·LIST b", sample_id="sample-marketing-01")
    _track(b.project_id)
    items = project_store.list()
    # b 比 a 晚建 → 应排在 a 之前
    ids = [p.project_id for p in items]
    assert ids.index(b.project_id) < ids.index(a.project_id)


def test_delete_clears_disk_and_memory():
    proj = project_store.create(name="单测·DEL", sample_id="sample-marketing-01")
    pid = proj.project_id
    var = get_settings().log_dir.parent / "var"
    assert (var / "projects" / pid / "project.json").exists()

    project_store.delete(pid)
    assert project_store.get(pid) is None
    assert not (var / "projects" / pid).exists()
    # 不需要 _track，自己删过了


def test_restart_rebuilds_from_disk():
    """新建 ProjectStore 实例（模拟重启）应能从磁盘扫回内存。"""
    proj = project_store.create(name="单测·RESTART", sample_id="sample-marketing-01")
    _track(proj.project_id)
    # 直接 new 一个 store 实例 → 走 __init__._load() → 扫盘
    fresh = ProjectStore()
    fetched = fresh.get(proj.project_id)
    assert fetched is not None
    assert fetched.name == "单测·RESTART"


# -------- HTTP API --------

def test_post_project_creates_and_get_returns_404_for_unknown(client):
    resp = client.post("/api/project", json={
        "name": "单测·HTTP",
        "sample_id": "sample-marketing-01",
    })
    assert resp.status_code == 200, resp.text
    body = resp.json()
    pid = body["project_id"]
    _track(pid)
    assert body["name"] == "单测·HTTP"
    assert body["status"] == "draft"

    # GET 详情
    r = client.get(f"/api/project/{pid}")
    assert r.status_code == 200
    assert r.json()["project_id"] == pid

    # GET 不存在
    r404 = client.get("/api/project/proj-bogus-xxx")
    assert r404.status_code == 404


def test_post_project_rejects_bad_sample(client):
    resp = client.post("/api/project", json={
        "name": "单测·BADSAMPLE",
        "sample_id": "sample-nonexistent",
    })
    assert resp.status_code == 404


def test_post_project_rejects_empty_name(client):
    resp = client.post("/api/project", json={
        "name": "   ",
        "sample_id": "sample-marketing-01",
    })
    assert resp.status_code == 400


def test_list_project_endpoint_returns_items(client):
    r1 = client.post("/api/project", json={
        "name": "单测·LIST1", "sample_id": "sample-marketing-01",
    })
    _track(r1.json()["project_id"])

    r = client.get("/api/project")
    assert r.status_code == 200
    body = r.json()
    assert "items" in body
    assert any(p["name"] == "单测·LIST1" for p in body["items"])


def test_patch_project_updates_fields(client):
    r1 = client.post("/api/project", json={
        "name": "单测·PATCH 原", "sample_id": "sample-marketing-01",
    })
    pid = r1.json()["project_id"]
    _track(pid)

    settings = ComposeSettings().model_dump()
    settings["target_duration_seconds"] = 60
    settings["cta"] = "立即抢购"
    settings["keywords"] = ["关键词1"]

    r = client.patch(f"/api/project/{pid}", json={
        "name": "单测·PATCH 改",
        "brief": "patch 测试简报",
        "settings": settings,
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["name"] == "单测·PATCH 改"
    assert body["brief"] == "patch 测试简报"
    assert body["settings"]["target_duration_seconds"] == 60
    assert body["settings"]["cta"] == "立即抢购"


def test_delete_project_endpoint_cascades(client):
    r1 = client.post("/api/project", json={
        "name": "单测·DEL HTTP", "sample_id": "sample-marketing-01",
    })
    pid = r1.json()["project_id"]
    # 不 _track —— 自己删

    r = client.delete(f"/api/project/{pid}")
    assert r.status_code == 200
    assert r.json()["deleted"] is True

    # 再 GET 应 404
    r404 = client.get(f"/api/project/{pid}")
    assert r404.status_code == 404
