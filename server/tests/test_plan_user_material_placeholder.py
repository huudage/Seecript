"""stage-76 回归：build_plan 对 user_material section 一律占位，禁自动选切片。

用户原话（2026-06-12）：
> "我现在只让你对真实素材做切片，不要做其他处理，就不会发生这种情况了"
> "step2 一开始不要直接填入素材了，都让用户自己去选择"

测试目标：
1. 单 Scene 路径：section 选 user_material → main_track 对应 scene 是 text_card 占位 + needs_fill=True
2. 拆分路径：section.shots ≥ 2 + source=user_material → 每个 sub-scene 都是 text_card 占位 + needs_fill=True
3. 占位 scene 仍带 plan 规划的 duration（≠ 0），用户可在 step2 看到时间轴
4. 占位 scene 的 source_ref 是 placeholder-* 前缀（便于追踪）
5. 占位 scene 没有 in_point/out_point 残留（in=0, out=None）
6. swap-source 链路依然能把占位 scene 转回真正的 user_material（hazy-dreaming-finch 已验证）
"""
from __future__ import annotations

import pytest


_TEST_PROJECT_IDS: list[str] = []
_TEST_PLAN_IDS: list[str] = []


@pytest.fixture(autouse=True)
def _cleanup(client):
    yield
    from app.services.plans.store import plan_store
    from app.services.projects.store import project_store
    for pid in _TEST_PLAN_IDS:
        plan_store._plans.pop(pid, None)
    _TEST_PLAN_IDS.clear()
    for prj in _TEST_PROJECT_IDS:
        try:
            project_store.delete(prj)
        except Exception:
            pass
    _TEST_PROJECT_IDS.clear()


def _build_plan_via_http(client, project_id: str) -> dict:
    """走真 HTTP POST /api/plan/build，模拟前端调用。"""
    r = client.post("/api/plan/build", json={
        "sample_ids": ["sample-marketing-01"],
        "project_id": project_id,
        "session_id": project_id,
        "selected_materials": [],  # 无真实素材：plan_agent 仍可能给 user_material section
        "fills": [],
        "variant": "A",
    })
    assert r.status_code == 200, r.text
    return r.json()


def test_build_plan_user_material_sections_become_placeholders(client):
    """build_plan 对所有 source=user_material 的 scene 应输出 text_card 占位。

    既覆盖单 Scene 路径，也覆盖拆分路径（sec.shots ≥ 2）。
    """
    r = client.post("/api/project", json={
        "name": "stage76-占位回归",
        "sample_ids": ["sample-marketing-01"],
    })
    assert r.status_code == 200, r.text
    pid = r.json()["project_id"]
    _TEST_PROJECT_IDS.append(pid)

    plan = _build_plan_via_http(client, pid)
    _TEST_PLAN_IDS.append(plan["plan_id"])

    main_track = plan["main_track"]
    assert len(main_track) > 0, "plan 应至少有一个 scene"

    # 找原本会被 plan_agent 标 source=user_material 的段：no real materials → adapted_sections
    # 里如果出现 user_material role 的 section，那对应 scene 应该都是占位。
    # 实现侧：build_plan 已经把所有 user_material scene 转成 source=text_card + needs_fill=True。
    # 我们直接断言「main_track 里不应有 source=user_material 的 scene（无素材场景）」。
    user_material_scenes = [s for s in main_track if s["source"] == "user_material"]
    assert user_material_scenes == [], (
        "build_plan 不应再自动产 source=user_material 的 scene；"
        f"实测剩余 {len(user_material_scenes)} 个: "
        f"{[s['scene_id'] for s in user_material_scenes]}"
    )


def test_placeholder_scenes_carry_text_card_spec_and_needs_fill(client):
    """占位 scene：source=text_card + needs_fill=True + text_card_spec 非空 + in/out 清零。"""
    r = client.post("/api/project", json={
        "name": "stage76-占位字段",
        "sample_ids": ["sample-marketing-01"],
    })
    assert r.status_code == 200
    pid = r.json()["project_id"]
    _TEST_PROJECT_IDS.append(pid)

    plan = _build_plan_via_http(client, pid)
    _TEST_PLAN_IDS.append(plan["plan_id"])

    # 找 source_ref 是 placeholder-* 前缀的 scene
    placeholders = [
        s for s in plan["main_track"]
        if isinstance(s.get("source_ref"), str) and s["source_ref"].startswith("placeholder-")
    ]
    if not placeholders:
        pytest.skip("此样例下 plan_agent 没产 user_material section，跳过")

    for sc in placeholders:
        assert sc["source"] == "text_card", f"占位应是 text_card，但 {sc['scene_id']} = {sc['source']}"
        assert sc["needs_fill"] is True, f"{sc['scene_id']} 占位应 needs_fill=True"
        assert sc["text_card_spec"] is not None, f"{sc['scene_id']} 应带 text_card_spec"
        assert sc["text_card_spec"]["main_text"], f"{sc['scene_id']} main_text 不能空"
        assert sc["in_point"] == 0.0
        assert sc["out_point"] is None
        assert sc["duration"] > 0, f"{sc['scene_id']} duration 必须保留 plan 规划值"


def test_placeholder_duration_preserves_plan_target(client):
    """占位 scene 的 duration 应保留 plan_agent 规划的 sub_shot 时长（而非 0 或 1）。

    用户底线：「内容轨时长要先生成」—— 即便没填真实素材，时间轴仍要长出 plan 规划的轮廓，
    让用户在 step2 能看到时间结构再去填。
    """
    r = client.post("/api/project", json={
        "name": "stage76-占位时长",
        "sample_ids": ["sample-marketing-01"],
    })
    assert r.status_code == 200
    pid = r.json()["project_id"]
    _TEST_PROJECT_IDS.append(pid)

    plan = _build_plan_via_http(client, pid)
    _TEST_PLAN_IDS.append(plan["plan_id"])

    placeholders = [
        s for s in plan["main_track"]
        if isinstance(s.get("source_ref"), str) and s["source_ref"].startswith("placeholder-")
    ]
    if not placeholders:
        pytest.skip("此样例下 plan_agent 没产 user_material section，跳过")

    total_dur = sum(s["duration"] for s in placeholders)
    assert total_dur >= 1.0, f"占位 scene 总时长应 ≥ 1s（实测 {total_dur:.2f}s）"

    # plan.duration_seconds 应等于 sum(scene.duration)（含占位）
    assert plan["duration_seconds"] == pytest.approx(
        sum(s["duration"] for s in plan["main_track"]),
        abs=0.5,
    )
