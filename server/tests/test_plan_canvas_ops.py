"""v2 结构画布即时操作路由烟测（PRD-v2 F4 · Epic-3 D2）。

覆盖：
1. POST /plan/{id}/sections/reorder —— parent_section_id 分组重排（含 v2 同 role 段、
   老 plan sc-N 正则兜底物化）、时间轴重铺、字幕清理、排列校验 422
2. POST /plan/{id}/scene/{scene_id}/split —— 实拍块切分（镜一分为二 + 段拆两段）、
   US-3.5 边界规则（非实拍源 / 含字幕 / 含口播 / 切点落在镜外均拒；短半段允许）
"""
from __future__ import annotations

import time

import pytest

from app.schemas import (
    AdaptedSection,
    ComposeSettings,
    PackagingItem,
    Plan,
    Scene,
    ShotPlan,
)
from app.services.plans.store import plan_store

_TEST_PLAN_IDS: list[str] = []


def _sec(section_id: str, role: str, order: int, duration: float = 4.0, shots: int = 1) -> AdaptedSection:
    return AdaptedSection(
        section_id=section_id,
        role=role,  # type: ignore[arg-type]
        theme=f"主题{order}",
        content_description=f"描述-{section_id}",
        shots=[
            ShotPlan(order=i, subject=f"主体{i}", visual=f"画面{i}", duration_seconds=duration / shots)
            for i in range(shots)
        ],
        order=order,
        duration_seconds=duration,
    )


def _scene(
    scene_id: str,
    section_id: str | None,
    role: str,
    start: float,
    duration: float,
    source: str = "user_material",
    shot_order: int = 0,
    voiceover_url: str | None = None,
) -> Scene:
    return Scene(
        scene_id=scene_id,
        section=role,  # type: ignore[arg-type]
        parent_section_id=section_id,
        shot_order=shot_order,
        source=source,  # type: ignore[arg-type]
        source_ref="m-1",
        start=start,
        duration=duration,
        narration=f"口播-{scene_id}",
        voiceover_url=voiceover_url,
    )


def _make_v2_plan(plan_id: str) -> Plan:
    """v2 数据：两段同 role=development（role 分组会错位，parent_section_id 才是对的）。"""
    return Plan(
        plan_id=plan_id,
        sample_ids=["sample-marketing-01"],
        project_id=None,
        session_id=None,
        settings=ComposeSettings(voiceover_enabled=True, tts_voice="zh_female_qingxin"),
        adapted_sections=[
            _sec("sec-a", "opening", 0, duration=4.0),
            _sec("sec-b", "development", 1, duration=3.0),
            _sec("sec-c", "development", 2, duration=5.0),
        ],
        main_track=[
            _scene("sc-a-0", "sec-a", "opening", 0.0, 4.0),
            _scene("sc-b-0", "sec-b", "development", 4.0, 3.0),
            _scene("sc-c-0", "sec-c", "development", 7.0, 5.0),
        ],
        packaging_track=[],
        duration_seconds=12.0,
        variant="A",
    )


def _make_old_plan(plan_id: str) -> Plan:
    """老数据：scene 无 parent_section_id，靠 sc-N 正则对 sec.order 兜底分组。"""
    return Plan(
        plan_id=plan_id,
        sample_ids=["sample-marketing-01"],
        project_id=None,
        session_id=None,
        settings=ComposeSettings(voiceover_enabled=True, tts_voice="zh_female_qingxin"),
        adapted_sections=[
            _sec("sec-0", "opening", 0, duration=3.0),
            _sec("sec-1", "development", 1, duration=4.0),
        ],
        main_track=[
            _scene("sc-0", None, "opening", 0.0, 3.0),
            _scene("sc-1", None, "development", 3.0, 4.0),
        ],
        packaging_track=[],
        duration_seconds=7.0,
        variant="A",
    )


@pytest.fixture(autouse=True)
def cleanup_canvas_plans():
    yield
    for plan_id in _TEST_PLAN_IDS:
        plan_store._plans.pop(plan_id, None)
    _TEST_PLAN_IDS.clear()


def _put(plan: Plan) -> Plan:
    _TEST_PLAN_IDS.append(plan.plan_id)
    plan_store.put(plan)
    return plan


# ---- reorder ---------------------------------------------------------------


def test_reorder_swaps_sections_and_relays_timeline(client):
    plan = _put(_make_v2_plan(f"plan-canvas-reorder-1-{int(time.time() * 1000)}"))
    resp = client.post(
        f"/api/plan/{plan.plan_id}/sections/reorder",
        json={"section_ids": ["sec-b", "sec-a", "sec-c"]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert [s["section_id"] for s in body["adapted_sections"]] == ["sec-b", "sec-a", "sec-c"]
    assert [s["order"] for s in body["adapted_sections"]] == [0, 1, 2]
    # 主轨按新段序重铺：sec-b 的镜在最前
    assert [sc["scene_id"] for sc in body["main_track"]] == ["sc-b-0", "sc-a-0", "sc-c-0"]
    assert [sc["start"] for sc in body["main_track"]] == [0.0, 3.0, 7.0]
    assert body["duration_seconds"] == pytest.approx(12.0)


def test_reorder_same_role_sections_do_not_tangle(client):
    """v2 回归锚：sec-b / sec-c 同 role=development——role 分组会把两段搅在一起。"""
    plan = _put(_make_v2_plan(f"plan-canvas-reorder-2-{int(time.time() * 1000)}"))
    resp = client.post(
        f"/api/plan/{plan.plan_id}/sections/reorder",
        json={"section_ids": ["sec-a", "sec-c", "sec-b"]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [sc["scene_id"] for sc in body["main_track"]] == ["sc-a-0", "sc-c-0", "sc-b-0"]
    assert [sc["start"] for sc in body["main_track"]] == [0.0, 4.0, 9.0]


def test_reorder_old_plan_materializes_parent_ids(client):
    """老 plan 无 parent_section_id：重排时按 sc-N 正则物化，重排后镜跟段走。"""
    plan = _put(_make_old_plan(f"plan-canvas-reorder-3-{int(time.time() * 1000)}"))
    resp = client.post(
        f"/api/plan/{plan.plan_id}/sections/reorder",
        json={"section_ids": ["sec-1", "sec-0"]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [sc["scene_id"] for sc in body["main_track"]] == ["sc-1", "sc-0"]
    assert [sc["start"] for sc in body["main_track"]] == [0.0, 4.0]
    # 物化落盘：此后 order 再位移也不会错位
    assert all(sc["parent_section_id"] for sc in body["main_track"])
    assert body["main_track"][0]["parent_section_id"] == "sec-1"


def test_reorder_clears_subtitles_and_trims_packaging(client):
    plan = _make_v2_plan(f"plan-canvas-reorder-4-{int(time.time() * 1000)}")
    plan.packaging_track = [
        PackagingItem(item_id="pkg-sub", kind="subtitle", start=0.0, end=2.0, text="字幕"),
        PackagingItem(item_id="pkg-title", kind="title_bar", start=0.0, end=2.0, text="标题"),
        PackagingItem(item_id="pkg-over", kind="sticker", start=11.0, end=15.0, text=""),
        PackagingItem(item_id="pkg-drop", kind="sticker", start=12.5, end=14.0, text=""),
    ]
    _put(plan)
    resp = client.post(
        f"/api/plan/{plan.plan_id}/sections/reorder",
        json={"section_ids": ["sec-c", "sec-b", "sec-a"]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    pkg = {it["item_id"]: it for it in body["packaging_track"]}
    assert "pkg-sub" not in pkg  # 字幕一律清空（请回 step3 重生成）
    assert "pkg-title" in pkg  # 时间锚定项保留
    assert pkg["pkg-over"]["end"] == pytest.approx(12.0)  # 跨新总长的截断保留
    assert "pkg-drop" not in pkg  # 整段超出新总长的丢弃


def test_reorder_noop_keeps_plan(client):
    plan = _put(_make_v2_plan(f"plan-canvas-reorder-5-{int(time.time() * 1000)}"))
    resp = client.post(
        f"/api/plan/{plan.plan_id}/sections/reorder",
        json={"section_ids": ["sec-a", "sec-b", "sec-c"]},
    )
    assert resp.status_code == 200, resp.text
    assert [sc["scene_id"] for sc in resp.json()["main_track"]] == ["sc-a-0", "sc-b-0", "sc-c-0"]


def test_reorder_invalid_permutation_422(client):
    plan = _put(_make_v2_plan(f"plan-canvas-reorder-6-{int(time.time() * 1000)}"))
    for bad in (["sec-a", "sec-b"], ["sec-a", "sec-b", "sec-x"], ["sec-a", "sec-b", "sec-c", "sec-a"]):
        resp = client.post(
            f"/api/plan/{plan.plan_id}/sections/reorder",
            json={"section_ids": bad},
        )
        assert resp.status_code == 422, (bad, resp.text)


def test_reorder_unknown_plan_404(client):
    resp = client.post(
        "/api/plan/plan-canvas-nope/sections/reorder",
        json={"section_ids": ["x"]},
    )
    assert resp.status_code == 404


# ---- split -----------------------------------------------------------------


def _make_split_plan(plan_id: str) -> Plan:
    """单段单镜 10s 实拍块：可切。"""
    return Plan(
        plan_id=plan_id,
        sample_ids=["sample-marketing-01"],
        project_id=None,
        session_id=None,
        settings=ComposeSettings(voiceover_enabled=True, tts_voice="zh_female_qingxin"),
        adapted_sections=[
            _sec("sec-0", "opening", 0, duration=10.0),
            _sec("sec-1", "closing", 1, duration=3.0),
        ],
        main_track=[
            _scene("sc-0", "sec-0", "opening", 0.0, 10.0, shot_order=0),
            _scene("sc-1", "sec-1", "closing", 10.0, 3.0, shot_order=0),
        ],
        packaging_track=[],
        duration_seconds=13.0,
        variant="A",
    )


def test_split_single_scene_creates_two_sections(client):
    plan = _put(_make_split_plan(f"plan-canvas-split-1-{int(time.time() * 1000)}"))
    resp = client.post(
        f"/api/plan/{plan.plan_id}/scene/sc-0/split",
        json={"split_at": 6.0},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    # 段拆两段：id / order / 时长
    secs = {s["section_id"]: s for s in body["adapted_sections"]}
    assert [s["section_id"] for s in body["adapted_sections"]] == ["sec-0", "sec-0-b", "sec-1"]
    assert [s["order"] for s in body["adapted_sections"]] == [0, 1, 2]
    assert secs["sec-0"]["duration_seconds"] == pytest.approx(6.0)
    assert secs["sec-0-b"]["duration_seconds"] == pytest.approx(4.0)

    # 镜一分为二：前半留原 id，后半起新 id；in/out 点连续
    scenes = {sc["scene_id"]: sc for sc in body["main_track"]}
    assert [sc["scene_id"] for sc in body["main_track"]] == ["sc-0", "sc-0-b", "sc-1"]
    a, b = scenes["sc-0"], scenes["sc-0-b"]
    assert a["duration"] == pytest.approx(6.0)
    assert b["duration"] == pytest.approx(4.0)
    assert a["out_point"] == pytest.approx(6.0)
    assert b["in_point"] == pytest.approx(6.0)
    assert b["start"] == pytest.approx(6.0)
    assert b["narration"] is None
    assert b["parent_section_id"] == "sec-0-b"
    # 后续段不受影响（时间不位移）
    assert scenes["sc-1"]["start"] == pytest.approx(10.0)
    assert scenes["sc-1"]["parent_section_id"] == "sec-1"
    assert body["duration_seconds"] == pytest.approx(13.0)


def test_split_multi_scene_section_moves_tail_scenes(client):
    plan = _make_split_plan(f"plan-canvas-split-2-{int(time.time() * 1000)}")
    # sec-0 追加第二镜（4s）——切第一镜时第二镜应整镜挪进 B 段
    sec0 = next(s for s in plan.adapted_sections if s.section_id == "sec-0")
    sec0.shots.append(ShotPlan(order=1, subject="主体1", visual="画面1", duration_seconds=4.0))
    plan.main_track.insert(
        1,
        _scene("sc-0-sh-1", "sec-0", "opening", 10.0, 4.0, shot_order=1),
    )
    plan.main_track[2].start = 14.0  # sec-1 后移
    plan.duration_seconds = 17.0
    _put(plan)

    resp = client.post(
        f"/api/plan/{plan.plan_id}/scene/sc-0/split",
        json={"split_at": 6.0},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    secs = {s["section_id"]: s for s in body["adapted_sections"]}
    assert secs["sec-0-b"]["duration_seconds"] == pytest.approx(8.0)  # 4 + 4
    assert [sc["scene_id"] for sc in body["main_track"]] == ["sc-0", "sc-0-b", "sc-0-sh-1", "sc-1"]
    b_sec = secs["sec-0-b"]
    assert [sh["order"] for sh in b_sec["shots"]] == [0, 1]
    tail = next(sc for sc in body["main_track"] if sc["scene_id"] == "sc-0-sh-1")
    assert tail["parent_section_id"] == "sec-0-b"
    assert tail["shot_order"] == 1
    # A 段 shots 只剩切点前
    assert [sh["order"] for sh in secs["sec-0"]["shots"]] == [0]


def test_split_rejects_non_real_source(client):
    plan = _make_split_plan(f"plan-canvas-split-3-{int(time.time() * 1000)}")
    plan.main_track[0] = _scene("sc-0", "sec-0", "opening", 0.0, 10.0, source="aigc_image")
    _put(plan)
    resp = client.post(
        f"/api/plan/{plan.plan_id}/scene/sc-0/split",
        json={"split_at": 5.0},
    )
    assert resp.status_code == 422
    assert "实拍" in resp.json()["detail"]


def test_split_rejects_block_with_subtitle_axis(client):
    plan = _make_split_plan(f"plan-canvas-split-4-{int(time.time() * 1000)}")
    plan.packaging_track = [
        PackagingItem(item_id="pkg-sub", kind="subtitle", start=1.0, end=3.0, text="字幕"),
    ]
    _put(plan)
    resp = client.post(
        f"/api/plan/{plan.plan_id}/scene/sc-0/split",
        json={"split_at": 5.0},
    )
    assert resp.status_code == 422
    assert "字幕" in resp.json()["detail"]


def test_split_rejects_block_with_voiceover(client):
    plan = _make_split_plan(f"plan-canvas-split-5-{int(time.time() * 1000)}")
    plan.main_track[0] = plan.main_track[0].model_copy(
        update={"voiceover_url": "/voiceovers/x/sc-0.wav"}
    )
    _put(plan)
    resp = client.post(
        f"/api/plan/{plan.plan_id}/scene/sc-0/split",
        json={"split_at": 5.0},
    )
    assert resp.status_code == 422
    assert "口播" in resp.json()["detail"]


def test_split_allows_short_half_and_rejects_outside(client):
    plan = _put(_make_split_plan(f"plan-canvas-split-6-{int(time.time() * 1000)}"))
    short = client.post(f"/api/plan/{plan.plan_id}/scene/sc-0/split", json={"split_at": 1.2})
    assert short.status_code == 200, short.text
    outside = _put(_make_split_plan(f"plan-canvas-split-6b-{int(time.time() * 1000)}"))
    for split_at in (0.0, 10.0):
        resp = client.post(
            f"/api/plan/{outside.plan_id}/scene/sc-0/split",
            json={"split_at": split_at},
        )
        assert resp.status_code == 422, (split_at, resp.text)


def test_split_unknown_plan_or_scene_404(client):
    plan = _put(_make_split_plan(f"plan-canvas-split-7-{int(time.time() * 1000)}"))
    resp = client.post("/api/plan/plan-canvas-nope/scene/sc-0/split", json={"split_at": 5.0})
    assert resp.status_code == 404
    resp = client.post(f"/api/plan/{plan.plan_id}/scene/sc-nope/split", json={"split_at": 5.0})
    assert resp.status_code == 422  # canvas_ops 先抛 scene 不存在
