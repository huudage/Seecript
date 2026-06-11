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


def test_patch_scene_narration_clears_voiceover_and_rebuilds_subtitle(client):
    """narration 改了：旧 voiceover_url 必须清掉（指向旧文案合成的 wav，再播就对不上嘴），
    同时 packaging_track 上的 subtitle item 要按新 text 重生（否则 step3 预览还是旧字幕）。"""
    plan = _make_plan(f"plan-scene-narr-{int(time.time() * 1000)}")
    plan.settings.subtitle_enabled = True
    # 模拟 step2 已经合过一次 TTS：sc-0 上挂了一个 voiceover_url
    plan.main_track[0].voiceover_url = "/voiceovers/legacy/sc-0.wav"
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
    assert sc0["voiceover_url"] is None, "narration 改了 voiceover_url 必须清空，避免播放旧音频"

    # subtitle item 已重建，text 是新 narration
    subs = [it for it in body["packaging_track"] if it["kind"] == "subtitle"]
    assert any(it["text"] == "改后的口播" for it in subs), \
        f"subtitle 应按新 narration 重建，实际：{[it['text'] for it in subs]}"


# ---------------------------------------------------------------------------
# stage-29 swap-source 手动裁剪：用户在 step2 拖手柄选 in/out → scene.duration 跟随
# ---------------------------------------------------------------------------

def _make_plan_with_project(plan_id: str, project_id: str) -> Plan:
    p = _make_plan(plan_id)
    return p.model_copy(update={"project_id": project_id})


def _put_test_material(project_id: str, material_id: str, duration: float) -> None:
    """往 material_store 临时写一条 video material，测试结束 cleanup 一并清。"""
    from app.schemas import Material
    from app.services.materials.store import material_store

    mat = Material(
        material_id=material_id,
        filename=f"{material_id}.mp4",
        media_type="video",
        duration_seconds=duration,
        file_url=f"/uploads/test/{material_id}.mp4",
        tags=[],
    )
    material_store.put(project_id, [mat])


def _drop_test_material(project_id: str) -> None:
    from app.services.materials.store import material_store
    with material_store._lock:
        material_store._by_session.pop(project_id, None)


def test_swap_source_user_material_manual_trim_overrides_duration(client):
    """用户原话："分镜时长要跟着用户裁剪结果走，完全听用户的"——后端必须按 in/out 写
    scene.duration，并把 plan.duration_seconds 同步到 sum(scene.duration)。"""
    project_id = f"proj-swap-trim-{int(time.time() * 1000)}"
    plan_id = f"plan-swap-trim-{int(time.time() * 1000)}"
    _put_test_material(project_id, "mat-trim-1", duration=10.0)
    plan = _make_plan_with_project(plan_id, project_id)
    _TEST_PLAN_IDS.append(plan_id)
    plan_store.put(plan)

    try:
        # 原 sc-0 duration=3.0；用户裁剪窗口 [2.0, 7.5] → 期望 duration=5.5
        resp = client.post(
            f"/api/plan/{plan_id}/scene/sc-0/swap-source",
            json={
                "source": "user_material",
                "material_id": "mat-trim-1",
                "material_in_point": 2.0,
                "material_out_point": 7.5,
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        sc0 = next(s for s in body["main_track"] if s["scene_id"] == "sc-0")
        assert sc0["in_point"] == 2.0
        assert sc0["out_point"] == 7.5
        assert abs(sc0["duration"] - 5.5) < 0.001, \
            f"scene.duration 必须跟随 out-in，期望 5.5，实际 {sc0['duration']}"

        # 后续 sc-1 顺移：sc-0 占 [0, 5.5] → sc-1.start=5.5
        sc1 = next(s for s in body["main_track"] if s["scene_id"] == "sc-1")
        assert abs(sc1["start"] - 5.5) < 0.001, f"sc-1.start 应顺移到 5.5，实际 {sc1['start']}"

        # plan.duration_seconds 必须等于 sum(main_track.duration)
        actual_total = sum(s["duration"] for s in body["main_track"])
        assert abs(body["duration_seconds"] - actual_total) < 0.001, \
            f"plan.duration_seconds 必须 = sum(scenes)；期望 {actual_total}，实际 {body['duration_seconds']}"
    finally:
        _drop_test_material(project_id)


def test_swap_source_user_material_manual_trim_clamps_to_material_duration(client):
    """out_point 超出素材时长：后端 clamp 到 mat.duration_seconds，不报 400。"""
    project_id = f"proj-swap-clamp-{int(time.time() * 1000)}"
    plan_id = f"plan-swap-clamp-{int(time.time() * 1000)}"
    _put_test_material(project_id, "mat-clamp", duration=4.0)
    plan = _make_plan_with_project(plan_id, project_id)
    _TEST_PLAN_IDS.append(plan_id)
    plan_store.put(plan)

    try:
        resp = client.post(
            f"/api/plan/{plan_id}/scene/sc-0/swap-source",
            json={
                "source": "user_material",
                "material_id": "mat-clamp",
                "material_in_point": 1.0,
                "material_out_point": 999.0,  # 远超素材时长
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        sc0 = next(s for s in body["main_track"] if s["scene_id"] == "sc-0")
        assert sc0["out_point"] == 4.0, f"out_point 应被 clamp 到素材时长 4.0，实际 {sc0['out_point']}"
        assert abs(sc0["duration"] - 3.0) < 0.001
    finally:
        _drop_test_material(project_id)


def test_swap_source_user_material_manual_trim_window_widens_to_min(client):
    """用户给的窗口太窄（0.2s）：后端友好 clamp 到 in+0.5s 下限，不报 400。
    保留用户的 in_point 不动，只把 out_point 抬到合理位置——比硬拒绝更顺手。"""
    project_id = f"proj-swap-short-{int(time.time() * 1000)}"
    plan_id = f"plan-swap-short-{int(time.time() * 1000)}"
    _put_test_material(project_id, "mat-short", duration=10.0)
    plan = _make_plan_with_project(plan_id, project_id)
    _TEST_PLAN_IDS.append(plan_id)
    plan_store.put(plan)

    try:
        resp = client.post(
            f"/api/plan/{plan_id}/scene/sc-0/swap-source",
            json={
                "source": "user_material",
                "material_id": "mat-short",
                "material_in_point": 1.0,
                "material_out_point": 1.2,  # 窗口仅 0.2s，会被 clamp 到 1.5
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        sc0 = next(s for s in body["main_track"] if s["scene_id"] == "sc-0")
        assert sc0["in_point"] == 1.0
        assert sc0["out_point"] == 1.5, f"out_point 应被 clamp 到 in+0.5=1.5，实际 {sc0['out_point']}"
        assert abs(sc0["duration"] - 0.5) < 0.001
    finally:
        _drop_test_material(project_id)


def test_rebuild_timeline_writes_plan_duration_seconds():
    """_rebuild_timeline 必须在末尾把 sum(scene.duration) 回写到 plan.duration_seconds，
    所有走重铺路径的 mutator（NL 编辑改时长 / 删段 / 重排 / 手动裁剪 / regenerate_fill）
    都靠这一处收束统一更新。"""
    from app.services.agent.compose_edit_agent import _rebuild_timeline

    plan = _make_plan(f"plan-rebuild-{int(time.time() * 1000)}")
    # 人为把 plan.duration_seconds 改成不一致的值，模拟 mutator 改了 scene.duration 后未同步
    plan.duration_seconds = 999.0
    plan.main_track[0].duration = 5.0
    plan.main_track[1].duration = 6.0

    _rebuild_timeline(plan)

    expected = 5.0 + 6.0
    assert abs(plan.duration_seconds - expected) < 0.001, \
        f"_rebuild_timeline 必须把 plan.duration_seconds 同步到 sum(scenes)；期望 {expected}，实际 {plan.duration_seconds}"


# ---------------------------------------------------------------------------
# stage-61 user_edited sticky flag：用户原话『手动调整过的分镜无论如何视作已补齐』
# ---------------------------------------------------------------------------

def test_patch_scene_narration_sets_user_edited(client):
    plan = _make_plan(f"plan-edited-narr-{int(time.time() * 1000)}")
    _TEST_PLAN_IDS.append(plan.plan_id)
    plan.main_track[0].needs_fill = True  # 模拟 build_plan 标了"待补"
    plan_store.put(plan)

    resp = client.patch(
        f"/api/plan/{plan.plan_id}/scene/sc-0",
        json={"narration": "用户改的口播"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    sc0 = next(s for s in body["main_track"] if s["scene_id"] == "sc-0")
    assert sc0["user_edited"] is True, "PATCH /scene narration 必须翻 user_edited=True"
    # needs_fill 不被这条接口动；前端按 user_edited 自行覆盖显示
    assert sc0["needs_fill"] is True, "patch-scene 不动 needs_fill；user_edited 由前端覆盖"


def test_patch_scene_theme_only_also_sets_user_edited(client):
    """用户只改了 section.theme（没改 narration），整段所有 scene 也算人工已审。"""
    plan = _make_plan(f"plan-edited-theme-{int(time.time() * 1000)}")
    _TEST_PLAN_IDS.append(plan.plan_id)
    plan_store.put(plan)

    resp = client.patch(
        f"/api/plan/{plan.plan_id}/scene/sc-0",
        json={"theme": "新主题"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    sc0 = next(s for s in body["main_track"] if s["scene_id"] == "sc-0")
    assert sc0["user_edited"] is True


def test_swap_source_user_material_sets_user_edited(client):
    project_id = f"proj-edited-{int(time.time() * 1000)}"
    plan_id = f"plan-edited-swap-{int(time.time() * 1000)}"
    _put_test_material(project_id, "mat-edited", duration=8.0)
    plan = _make_plan_with_project(plan_id, project_id)
    _TEST_PLAN_IDS.append(plan_id)
    plan.main_track[0].needs_fill = True
    plan_store.put(plan)

    try:
        resp = client.post(
            f"/api/plan/{plan_id}/scene/sc-0/swap-source",
            json={
                "source": "user_material",
                "material_id": "mat-edited",
                "material_in_point": 1.0,
                "material_out_point": 4.0,
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        sc0 = next(s for s in body["main_track"] if s["scene_id"] == "sc-0")
        assert sc0["user_edited"] is True, "swap-source 必须把 user_edited 翻到 True"
        assert sc0["needs_fill"] is False, "swap-source 也清掉 needs_fill"
    finally:
        _drop_test_material(project_id)


def test_patch_shot_fields_sets_user_edited(client):
    plan = _make_plan(f"plan-edited-shot-{int(time.time() * 1000)}")
    _TEST_PLAN_IDS.append(plan.plan_id)
    plan_store.put(plan)

    resp = client.patch(
        f"/api/plan/{plan.plan_id}/scene/sc-0/shot-fields",
        json={"subject": "改后主体"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    sc0 = next(s for s in body["main_track"] if s["scene_id"] == "sc-0")
    assert sc0["user_edited"] is True
