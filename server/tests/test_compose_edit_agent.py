"""compose_edit_agent mutator 单测——重点覆盖 stage-62 新增的 delete_shot。

用户原话（2026-06-11）：
    "现在自然语言编辑连个分镜都删不了，step2的功能介绍保守一点，修复这个bug"

复现：用户输入"删除第二段第三镜" → LLM 误选 update_shot_duration 把镜时长压到 1s。
根因：工具列表里没有 delete_shot，LLM 只能在 update_shot_duration / delete_section
之间硬选最近的——必须新增 delete_shot 工具 + mutator + 关键词兜底。
"""
from __future__ import annotations

import time

from app.schemas import (
    AdaptedSection,
    ComposeSettings,
    Plan,
    Scene,
    ShotPlan,
)
from app.services.agent.compose_edit_agent import (
    _mock_intent,
    _mut_delete_shot,
    _rebuild_timeline,
)


def _make_plan_with_shots() -> Plan:
    """造一个 2 段 / 段内多镜的 plan：sec-0 有 2 镜，sec-1 有 3 镜。"""
    plan_id = f"plan-edit-shot-{int(time.time() * 1000)}"

    sec0_shots = [
        ShotPlan(order=0, subject="主播", visual="近景出镜", narration="开场口播", duration_seconds=2.0),
        ShotPlan(order=1, subject="logo", visual="logo 浮现", narration="片头点题", duration_seconds=2.0),
    ]
    sec1_shots = [
        ShotPlan(order=0, subject="痛点 A", visual="A 场景", narration="痛点一", duration_seconds=3.0),
        ShotPlan(order=1, subject="痛点 B", visual="B 场景", narration="痛点二", duration_seconds=3.0),
        ShotPlan(order=2, subject="痛点 C", visual="C 场景", narration="痛点三", duration_seconds=4.0),
    ]

    return Plan(
        plan_id=plan_id,
        sample_ids=["sample-x"],
        project_id=None,
        session_id=None,
        settings=ComposeSettings(),
        adapted_sections=[
            AdaptedSection(
                section_id="sec-0",
                role="opening",
                theme="开场",
                content_description="开场段",
                source_shot_indices=[0],
                order=0,
                duration_seconds=4.0,
                shots=sec0_shots,
            ),
            AdaptedSection(
                section_id="sec-1",
                role="development",
                theme="发展",
                content_description="痛点段",
                source_shot_indices=[1],
                order=1,
                duration_seconds=10.0,
                shots=sec1_shots,
            ),
        ],
        main_track=[
            Scene(
                scene_id="sc-0-0", section="opening", parent_section_id="sec-0", shot_order=0,
                source="user_material", source_ref="m-0", start=0.0, duration=2.0,
                narration="开场口播",
            ),
            Scene(
                scene_id="sc-0-1", section="opening", parent_section_id="sec-0", shot_order=1,
                source="user_material", source_ref="m-1", start=2.0, duration=2.0,
                narration="片头点题",
            ),
            Scene(
                scene_id="sc-1-0", section="development", parent_section_id="sec-1", shot_order=0,
                source="user_material", source_ref="m-2", start=4.0, duration=3.0,
                narration="痛点一",
            ),
            Scene(
                scene_id="sc-1-1", section="development", parent_section_id="sec-1", shot_order=1,
                source="user_material", source_ref="m-3", start=7.0, duration=3.0,
                narration="痛点二",
            ),
            Scene(
                scene_id="sc-1-2", section="development", parent_section_id="sec-1", shot_order=2,
                source="user_material", source_ref="m-4", start=10.0, duration=4.0,
                narration="痛点三",
            ),
        ],
        packaging_track=[],
        duration_seconds=14.0,
        variant="A",
    )


def test_delete_shot_removes_shot_and_scene_and_renumbers():
    """删 sec-1 第 3 镜（shot_order=2）：sec-1.shots 变 2 镜，对应 scene 消失，整片缩短。"""
    plan = _make_plan_with_shots()
    diff = _mut_delete_shot(plan, {"section_id": "sec-1", "shot_order": 2})

    assert diff is not None
    assert diff.op == "delete_shot"

    # sec-1 还剩 2 镜，order 重排成 0..1
    sec1 = next(s for s in plan.adapted_sections if s.section_id == "sec-1")
    assert len(sec1.shots) == 2
    assert [sh.order for sh in sec1.shots] == [0, 1]
    assert sec1.shots[0].subject == "痛点 A"
    assert sec1.shots[1].subject == "痛点 B"
    # 段时长 = 剩余 shot 之和 = 6.0
    assert abs(sec1.duration_seconds - 6.0) < 0.01

    # main_track 中 sc-1-2 已消失
    scene_ids = {sc.scene_id for sc in plan.main_track}
    assert "sc-1-2" not in scene_ids
    assert "sc-1-0" in scene_ids and "sc-1-1" in scene_ids

    # 整片时长 = 4 (sec-0) + 6 (sec-1) = 10
    actual_total = sum(sc.duration for sc in plan.main_track)
    assert abs(plan.duration_seconds - actual_total) < 0.01
    assert abs(plan.duration_seconds - 10.0) < 0.01


def test_delete_shot_renumbers_following_scene_shot_order():
    """删 sec-1 第 1 镜（shot_order=0）：原 sc-1-1/sc-1-2 的 shot_order 应顺移到 0/1。"""
    plan = _make_plan_with_shots()
    _mut_delete_shot(plan, {"section_id": "sec-1", "shot_order": 0})

    sec1 = next(s for s in plan.adapted_sections if s.section_id == "sec-1")
    assert len(sec1.shots) == 2
    # 剩下原来的『痛点 B / 痛点 C』，重新编号为 0/1
    assert sec1.shots[0].subject == "痛点 B"
    assert sec1.shots[0].order == 0
    assert sec1.shots[1].subject == "痛点 C"
    assert sec1.shots[1].order == 1

    # main_track 上同段后续 scene 的 shot_order 也要 -1
    sec1_scenes = [sc for sc in plan.main_track if sc.parent_section_id == "sec-1"]
    assert len(sec1_scenes) == 2
    assert sorted(sc.shot_order for sc in sec1_scenes) == [0, 1]


def test_delete_shot_cascades_to_section_when_only_one_left():
    """段内只剩一镜：删它等于整段删除——避免留空段死区。"""
    plan = _make_plan_with_shots()
    # 先把 sec-0 删到只剩 1 镜
    _mut_delete_shot(plan, {"section_id": "sec-0", "shot_order": 1})
    sec0 = next((s for s in plan.adapted_sections if s.section_id == "sec-0"), None)
    assert sec0 is not None and len(sec0.shots) == 1

    # 再删 sec-0 仅剩的 1 镜 → 应 cascade 整段删除
    diff = _mut_delete_shot(plan, {"section_id": "sec-0", "shot_order": 0})
    assert diff is not None
    assert diff.op == "delete_shot"  # op 名保留，summary 注明 cascade
    assert "整段删除" in diff.summary or "cascade" in diff.summary.lower()

    # sec-0 整段消失
    assert all(s.section_id != "sec-0" for s in plan.adapted_sections)
    # main_track 也不再有 parent_section_id="sec-0" 的 scene
    assert all(sc.parent_section_id != "sec-0" for sc in plan.main_track)


def test_delete_shot_unknown_section_returns_none():
    plan = _make_plan_with_shots()
    diff = _mut_delete_shot(plan, {"section_id": "sec-not-exist", "shot_order": 0})
    assert diff is None


def test_delete_shot_unknown_shot_order_reports_not_found():
    plan = _make_plan_with_shots()
    diff = _mut_delete_shot(plan, {"section_id": "sec-1", "shot_order": 99})
    assert diff is not None
    assert diff.op == "delete_shot"
    assert "没有第" in diff.summary  # "段 sec-1 没有第 100 镜"


def test_mock_intent_recognizes_delete_shot_natural_language():
    """用户原话『删除第二段第三镜』必须落到 delete_shot，不能被 delete_section 抢走。"""
    plan = _make_plan_with_shots()
    intents = _mock_intent(plan, "删除第二段第三镜", step="step2")
    assert len(intents) == 1
    assert intents[0]["name"] == "delete_shot"
    assert intents[0]["arguments"]["section_id"] == "sec-1"
    assert intents[0]["arguments"]["shot_order"] == 2  # "第三镜" → shot_order=2


def test_mock_intent_delete_section_still_works_when_no_shot_mention():
    """『删除第 2 段』纯段删除路径必须保留——不能被新加的 delete_shot 误吃。"""
    plan = _make_plan_with_shots()
    intents = _mock_intent(plan, "删除第 2 段", step="step2")
    assert len(intents) == 1
    assert intents[0]["name"] == "delete_section"
    assert intents[0]["arguments"]["section_id"] == "sec-1"


def test_rebuild_timeline_writes_plan_duration_seconds():
    """既有冒烟：_rebuild_timeline 末尾必须把 sum(scene.duration) 回写到 plan.duration_seconds。
    delete_shot 借这条路径同步整片时长，所以这个不变量必须永远成立。"""
    plan = _make_plan_with_shots()
    plan.duration_seconds = 999.0  # 人为不一致
    info = _rebuild_timeline(plan)
    expected = sum(sc.duration for sc in plan.main_track)
    assert abs(plan.duration_seconds - expected) < 0.001
    assert abs(info["total"] - expected) < 0.001
