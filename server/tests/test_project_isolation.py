"""项目隔离测试：两个项目独立持有素材/资产/Plan/Gap，互不可见。

用真 HTTP 端点跑：
1. 新建两个项目 A、B，sample_id 相同（都 marketing-01）
2. A 上传 2 段 material → /material/upload；B 上传 1 段
3. A 上传 1 个 BGM → /asset/upload；B 上传 1 个 BGM（同字节，但 owner 不同 ≠ dedup）
4. A 走 /plan/build → 持有 plan_A；B 同样得 plan_B（独立 plan_id）
5. 用 /gap/detect 给两个 plan 各产 gaps，互查彼此 gap_id 应 404
6. asset_store.list(owner=A) 不含 B 的 BGM，反之亦然
7. material_store.list(A.project_id) 仅含 A 的 2 段
"""
from __future__ import annotations

import io
import shutil

import pytest

from app.config import get_settings
from app.services.assets import asset_store
from app.services.materials.store import material_store
from app.services.plans.store import plan_store
from app.services.projects import project_store


_TEST_PROJECT_IDS: list[str] = []


def _clean_project(pid: str) -> None:
    project_store._by_id.pop(pid, None)
    material_store._by_session.pop(pid, None)
    asset_store._states.pop(pid, None)
    asset_store._owner_by_asset = {
        aid: owner for aid, owner in asset_store._owner_by_asset.items() if owner != pid
    }
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


_FAKE_VIDEO = b"\x00\x00\x00\x18ftypisom" + b"\x00" * 1024
_FAKE_MP3_A = b"\xff\xfb\x90\x00" + b"A" * 1020
_FAKE_MP3_B = b"\xff\xfb\x90\x00" + b"B" * 1020


def _create(client, name: str) -> str:
    r = client.post("/api/project", json={"name": name, "sample_ids": ["sample-marketing-01"]})
    assert r.status_code == 200, r.text
    pid = r.json()["project_id"]
    _TEST_PROJECT_IDS.append(pid)
    return pid


def test_materials_isolated_between_projects(client):
    pid_a = _create(client, "隔离·A")
    pid_b = _create(client, "隔离·B")

    # A 上传 2 段
    r = client.post(
        "/api/material/upload",
        files=[
            ("files", ("a1.mp4", io.BytesIO(_FAKE_VIDEO), "video/mp4")),
            ("files", ("a2.mp4", io.BytesIO(_FAKE_VIDEO), "video/mp4")),
        ],
        data={"project_id": pid_a},
    )
    assert r.status_code == 200, r.text
    a_materials = r.json()["materials"]
    assert len(a_materials) == 2

    # B 上传 1 段
    r = client.post(
        "/api/material/upload",
        files=[("files", ("b1.mp4", io.BytesIO(_FAKE_VIDEO), "video/mp4"))],
        data={"project_id": pid_b},
    )
    assert r.status_code == 200, r.text
    b_materials = r.json()["materials"]
    assert len(b_materials) == 1

    # 直接查 store：两边互不可见
    assert len(material_store.list(pid_a)) == 2
    assert len(material_store.list(pid_b)) == 1

    a_ids = {m["material_id"] for m in a_materials}
    b_ids = {m["material_id"] for m in b_materials}
    assert a_ids.isdisjoint(b_ids), "material_id 不应跨项目复用"

    # B 的 store 不能看到 A 的素材
    b_store_ids = {m.material_id for m in material_store.list(pid_b)}
    assert b_store_ids.isdisjoint(a_ids)


def test_assets_isolated_between_projects(client):
    pid_a = _create(client, "资产隔离·A")
    pid_b = _create(client, "资产隔离·B")

    # A 上传 BGM
    r = client.post(
        "/api/asset/upload",
        files={"file": ("a-hook.mp3", io.BytesIO(_FAKE_MP3_A), "audio/mpeg")},
        data={"kind": "bgm", "title": "A 主题曲", "project_id": pid_a},
    )
    assert r.status_code == 200, r.text
    asset_a = r.json()
    aid_a = asset_a["asset_id"]
    assert asset_a["file_url"].startswith(f"/assets/{pid_a}/bgm/")

    # B 上传不同字节的 BGM（防 dedup 把它们合并）
    r = client.post(
        "/api/asset/upload",
        files={"file": ("b-hook.mp3", io.BytesIO(_FAKE_MP3_B), "audio/mpeg")},
        data={"kind": "bgm", "title": "B 主题曲", "project_id": pid_b},
    )
    assert r.status_code == 200, r.text
    asset_b = r.json()
    aid_b = asset_b["asset_id"]
    assert asset_b["file_url"].startswith(f"/assets/{pid_b}/bgm/")

    assert aid_a != aid_b

    # GET A 的库 → 不含 B 的 BGM
    r = client.get(f"/api/asset/library?project_id={pid_a}&kind=bgm")
    assert r.status_code == 200
    a_lib_ids = {it["asset_id"] for it in r.json()["items"]}
    assert aid_a in a_lib_ids
    assert aid_b not in a_lib_ids

    # GET B 的库 → 不含 A 的 BGM
    r = client.get(f"/api/asset/library?project_id={pid_b}&kind=bgm")
    assert r.status_code == 200
    b_lib_ids = {it["asset_id"] for it in r.json()["items"]}
    assert aid_b in b_lib_ids
    assert aid_a not in b_lib_ids


def test_same_bytes_uploaded_to_two_projects_dedup_per_owner(client):
    """同字节内容在不同 project 下应各自产生独立 asset_id —— dedup 是 per-owner 的。"""
    pid_a = _create(client, "Dedup·A")
    pid_b = _create(client, "Dedup·B")

    payload = b"\xff\xfb\x90\x00" + b"X" * 1024

    r1 = client.post(
        "/api/asset/upload",
        files={"file": ("same.mp3", io.BytesIO(payload), "audio/mpeg")},
        data={"kind": "bgm", "project_id": pid_a},
    )
    aid1 = r1.json()["asset_id"]

    r2 = client.post(
        "/api/asset/upload",
        files={"file": ("same.mp3", io.BytesIO(payload), "audio/mpeg")},
        data={"kind": "bgm", "project_id": pid_b},
    )
    aid2 = r2.json()["asset_id"]

    assert aid1 != aid2, "不同项目下同字节内容应各自登记"


def test_plans_and_gaps_isolated(client):
    pid_a = _create(client, "Plan 隔离·A")
    pid_b = _create(client, "Plan 隔离·B")

    # 给 A、B 各传素材
    r = client.post(
        "/api/material/upload",
        files=[("files", ("a.mp4", io.BytesIO(_FAKE_VIDEO), "video/mp4"))],
        data={"project_id": pid_a},
    )
    a_mats = [m["material_id"] for m in r.json()["materials"]]
    r = client.post(
        "/api/material/upload",
        files=[("files", ("b.mp4", io.BytesIO(_FAKE_VIDEO), "video/mp4"))],
        data={"project_id": pid_b},
    )
    b_mats = [m["material_id"] for m in r.json()["materials"]]

    # A 构 Plan
    r = client.post(
        "/api/plan/build",
        json={
            "sample_ids": ["sample-marketing-01"],
            "project_id": pid_a,
            "session_id": pid_a,
            "selected_materials": a_mats,
            "fills": [],
            "variant": "A",
        },
    )
    assert r.status_code == 200, r.text
    plan_a = r.json()
    assert plan_a["project_id"] == pid_a

    # B 构 Plan
    r = client.post(
        "/api/plan/build",
        json={
            "sample_ids": ["sample-marketing-01"],
            "project_id": pid_b,
            "session_id": pid_b,
            "selected_materials": b_mats,
            "fills": [],
            "variant": "A",
        },
    )
    assert r.status_code == 200, r.text
    plan_b = r.json()
    assert plan_b["project_id"] == pid_b

    assert plan_a["plan_id"] != plan_b["plan_id"]

    # plan_store 内 Plan.project_id 各自归属正确
    pa = plan_store.get(plan_a["plan_id"])
    pb = plan_store.get(plan_b["plan_id"])
    assert pa is not None and pa.project_id == pid_a
    assert pb is not None and pb.project_id == pid_b

    # gap/detect 各跑各的
    r = client.post(
        "/api/gap/detect",
        json={"plan_id": plan_a["plan_id"], "project_id": pid_a, "session_id": pid_a},
    )
    assert r.status_code == 200, r.text
    gaps_a = r.json()
    r = client.post(
        "/api/gap/detect",
        json={"plan_id": plan_b["plan_id"], "project_id": pid_b, "session_id": pid_b},
    )
    assert r.status_code == 200, r.text
    gaps_b = r.json()

    a_gap_ids = {g["gap_id"] for g in gaps_a}
    b_gap_ids = {g["gap_id"] for g in gaps_b}
    assert a_gap_ids.isdisjoint(b_gap_ids), "不同 plan 的 gap_id 不应碰撞"

    # 每条 gap 都带正确的 project_id
    assert all(g["project_id"] == pid_a for g in gaps_a if g.get("project_id"))
    assert all(g["project_id"] == pid_b for g in gaps_b if g.get("project_id"))


def test_delete_project_a_does_not_touch_project_b(client):
    """删除项目 A 后，B 的素材/资产/var 目录完整存活。"""
    pid_a = _create(client, "级联·A")
    pid_b = _create(client, "级联·B")

    # 各传一段素材 + 一个 BGM
    client.post(
        "/api/material/upload",
        files=[("files", ("a.mp4", io.BytesIO(_FAKE_VIDEO), "video/mp4"))],
        data={"project_id": pid_a},
    )
    client.post(
        "/api/material/upload",
        files=[("files", ("b.mp4", io.BytesIO(_FAKE_VIDEO), "video/mp4"))],
        data={"project_id": pid_b},
    )
    r = client.post(
        "/api/asset/upload",
        files={"file": ("a.mp3", io.BytesIO(_FAKE_MP3_A), "audio/mpeg")},
        data={"kind": "bgm", "project_id": pid_a},
    )
    aid_a = r.json()["asset_id"]
    r = client.post(
        "/api/asset/upload",
        files={"file": ("b.mp3", io.BytesIO(_FAKE_MP3_B), "audio/mpeg")},
        data={"kind": "bgm", "project_id": pid_b},
    )
    aid_b = r.json()["asset_id"]

    var = get_settings().log_dir.parent / "var"
    # 删 A
    r = client.delete(f"/api/project/{pid_a}")
    assert r.status_code == 200
    _TEST_PROJECT_IDS.remove(pid_a)  # 自己删过了

    # A 的盘 + store 都没了
    assert not (var / "projects" / pid_a).exists()
    assert not (var / "uploads" / pid_a).exists()
    assert not (var / "assets" / pid_a).exists()
    assert project_store.get(pid_a) is None
    assert asset_store.get(aid_a) is None

    # B 完好
    assert (var / "projects" / pid_b).exists()
    assert (var / "uploads" / pid_b).exists()
    assert (var / "assets" / pid_b).exists()
    assert project_store.get(pid_b) is not None
    assert asset_store.get(aid_b) is not None
    assert len(material_store.list(pid_b)) == 1
