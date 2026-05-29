"""验证 Plan/Gap/Material 重启后从磁盘恢复（模拟新进程加载）。

测试覆盖：
1. plan_store.put → 新建 PlanStore 实例 → get(plan_id) 命中
2. gap_store.put → 新 GapStore → get(gap_id) 命中
3. material_store.put → 新 MaterialStore → list(session_id) 命中
4. plan 在 var/projects/<project_id>/plans/<plan_id>.json 落盘
5. 没 project_id 的 plan 落 __legacy 目录
"""
from __future__ import annotations

import json
import shutil
import time

import pytest

from app.config import get_settings
from app.schemas import (
    AdaptedSection,
    Gap,
    Material,
    Plan,
    Scene,
)
from app.services.materials.store import (
    GapStore,
    MaterialStore,
    gap_store,
    material_store,
)
from app.services.plans.store import PlanStore, plan_store
from app.services.projects import project_store


_TEST_PROJECT_IDS: list[str] = []
_TEST_PLAN_IDS: list[str] = []


def _clean(pid: str) -> None:
    project_store._by_id.pop(pid, None)
    material_store._by_session.pop(pid, None)
    var = get_settings().log_dir.parent / "var"
    for sub in ("projects", "uploads", "assets"):
        target = var / sub / pid
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)


def _clean_plan(plan_id: str) -> None:
    plan_store._plans.pop(plan_id, None)
    gaps = gap_store._by_plan.pop(plan_id, [])
    for g in gaps:
        gap_store._by_gap_id.pop(g.gap_id, None)


@pytest.fixture(autouse=True)
def cleanup():
    yield
    for pid in _TEST_PROJECT_IDS:
        _clean(pid)
    for pid in _TEST_PLAN_IDS:
        _clean_plan(pid)
    _TEST_PROJECT_IDS.clear()
    _TEST_PLAN_IDS.clear()


def _make_plan(project_id: str | None, plan_id: str) -> Plan:
    return Plan(
        plan_id=plan_id,
        sample_id="sample-marketing-01",
        project_id=project_id,
        session_id=project_id,
        adapted_sections=[
            AdaptedSection(
                section_id="adp-opening",
                role="opening",
                theme="开场",
                content_description="hook",
                source_shot_indices=[0],
                order=0,
                duration_seconds=3.0,
            ),
        ],
        main_track=[
            Scene(
                scene_id="sc-1",
                section="opening",
                source="user_material",
                source_ref="m-x",
                start=0.0,
                duration=3.0,
            ),
        ],
        packaging_track=[],
        duration_seconds=3.0,
        variant="A",
    )


def test_plan_persisted_and_restored_from_disk():
    proj = project_store.create(name="单测·PLANRESTORE", sample_id="sample-marketing-01")
    _TEST_PROJECT_IDS.append(proj.project_id)
    plan = _make_plan(proj.project_id, f"plan-restore-{int(time.time() * 1000)}")
    _TEST_PLAN_IDS.append(plan.plan_id)

    plan_store.put(plan)

    # 文件应落在 var/projects/<pid>/plans/<plan_id>.json
    var = get_settings().log_dir.parent / "var"
    path = var / "projects" / proj.project_id / "plans" / f"{plan.plan_id}.json"
    assert path.exists()
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["plan_id"] == plan.plan_id
    assert payload["project_id"] == proj.project_id

    # 新 store 实例（模拟重启）→ 内存里也能查到
    fresh = PlanStore()
    got = fresh.get(plan.plan_id)
    assert got is not None
    assert got.project_id == proj.project_id
    assert got.sample_id == "sample-marketing-01"
    assert len(got.main_track) == 1


def test_plan_without_project_id_falls_to_legacy_dir():
    """没绑 project 的旧 plan 应落 __legacy/plans/。"""
    plan = _make_plan(None, f"plan-legacy-{int(time.time() * 1000)}")
    _TEST_PLAN_IDS.append(plan.plan_id)

    plan_store.put(plan)

    var = get_settings().log_dir.parent / "var"
    legacy_path = var / "projects" / "__legacy" / "plans" / f"{plan.plan_id}.json"
    assert legacy_path.exists()

    fresh = PlanStore()
    assert fresh.get(plan.plan_id) is not None


def test_gap_store_restored_from_disk():
    proj = project_store.create(name="单测·GAPRESTORE", sample_id="sample-marketing-01")
    _TEST_PROJECT_IDS.append(proj.project_id)
    plan_id = f"plan-gaprestore-{int(time.time() * 1000)}"
    _TEST_PLAN_IDS.append(plan_id)

    gap_a = Gap(
        gap_id=f"gap-opening-0-{plan_id}",
        section="opening",
        section_id="adp-opening",
        slot_index=0,
        requirement="hook",
        status="ok",
        impact="high",
        sample_thumbnail_url=None,
        project_id=proj.project_id,
    )
    gap_store.put(plan_id, [gap_a])

    # 应落 var/projects/<pid>/gaps/<plan_id>.json
    var = get_settings().log_dir.parent / "var"
    path = var / "projects" / proj.project_id / "gaps" / f"{plan_id}.json"
    assert path.exists()

    # 新 GapStore 实例从磁盘扫回
    fresh = GapStore()
    got = fresh.get(gap_a.gap_id)
    assert got is not None
    assert got.project_id == proj.project_id
    by_plan = fresh.list_by_plan(plan_id)
    assert len(by_plan) == 1
    assert by_plan[0].gap_id == gap_a.gap_id


def test_material_store_restored_from_disk():
    proj = project_store.create(name="单测·MATRESTORE", sample_id="sample-marketing-01")
    _TEST_PROJECT_IDS.append(proj.project_id)

    materials = [
        Material(
            material_id="m-restore-1",
            filename="a.mp4",
            media_type="video",
            recommended_section="opening",
            sort_order=0,
        ),
        Material(
            material_id="m-restore-2",
            filename="b.mp4",
            media_type="video",
            recommended_section="closing",
            sort_order=1,
        ),
    ]
    material_store.put(proj.project_id, materials)

    # 文件落 var/projects/<pid>/materials/index.json
    var = get_settings().log_dir.parent / "var"
    path = var / "projects" / proj.project_id / "materials" / "index.json"
    assert path.exists()
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert len(raw) == 2

    # 新 store 实例从磁盘扫回
    fresh = MaterialStore()
    items = fresh.list(proj.project_id)
    assert len(items) == 2
    assert {m.material_id for m in items} == {"m-restore-1", "m-restore-2"}


def test_plan_get_404_then_restore_recovers(client):
    """端到端：http POST /plan/build → 取 plan_id → 新 PlanStore() → http GET /plan/<id> 仍 200。

    这就是用户上轮报告的 'plan-2ca06c7165 not found' bug 的回归测试场景：
    重启后端后，前端 localStorage 里的 plan_id 仍能命中。
    """
    # 创建 project
    r = client.post("/api/project", json={"name": "持久化", "sample_id": "sample-marketing-01"})
    assert r.status_code == 200, r.text
    pid = r.json()["project_id"]
    _TEST_PROJECT_IDS.append(pid)

    # build plan
    r = client.post("/api/plan/build", json={
        "sample_id": "sample-marketing-01",
        "project_id": pid,
        "session_id": pid,
        "selected_materials": [],
        "fills": [],
        "variant": "A",
    })
    assert r.status_code == 200, r.text
    plan_id = r.json()["plan_id"]
    _TEST_PLAN_IDS.append(plan_id)

    # 模拟"重启"：清空内存、新建 store、再 lookup
    plan_store._plans.clear()
    fresh = PlanStore()
    assert fresh.get(plan_id) is not None, "重启后应仍能从磁盘扫回 plan"

    # 这里再走真 HTTP GET 也应命中（main 模块导入的 plan_store 是同一个单例，
    # 测试里清空它再 fresh.put 同一份数据回去即可——这一步真实场景不需要，
    # 因为生产环境进程重启时也是 from disk 全量加载）
    plan_store._plans.update(fresh._plans)
    r = client.get(f"/api/plan/{plan_id}")
    assert r.status_code == 200
