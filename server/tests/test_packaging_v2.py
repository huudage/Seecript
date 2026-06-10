"""POST /api/packaging/recommend + /apply —— V2 5 维度多候选集成测试。

覆盖：
1. /recommend 返回 V2 响应（subtitle_styles/title_bars/stickers/transition_bundles/covers）
2. recommend_packaging_v2 不会 mutate plan.packaging_track
3. apply_selection_to_plan 把 selection 写到 plan.packaging_track + Scene.transition_in
4. transition selection 选不到 bundle / 选 invalid style → 跳过该 bundle，不报错
5. /recommend 走 mock LLM 时 5 维度 candidate_id 都齐
"""
from __future__ import annotations

import time

import pytest

from app.schemas import (
    AdaptedSection,
    ComposeSettings,
    PackagingPreferences,
    PackagingSelection,
    Plan,
    Scene,
)
from app.services.agent.packaging_agent import (
    apply_selection_to_plan,
    recommend_packaging_v2,
)
from app.services.plans.store import plan_store


_TEST_PLAN_IDS: list[str] = []


def _make_plan(plan_id: str, *, prefs: PackagingPreferences | None = None) -> Plan:
    kw: dict = {"voiceover_enabled": True, "subtitle_enabled": True}
    if prefs is not None:
        kw["packaging_prefs"] = prefs
    return Plan(
        plan_id=plan_id,
        sample_ids=["sample-marketing-01"],
        project_id=None,
        session_id=None,
        video_goal="新品发布——突出差异化卖点",
        brief="差异化新品",
        settings=ComposeSettings(**kw),
        adapted_sections=[
            AdaptedSection(
                section_id="adp-opening", role="opening", theme="开场",
                content_description="hook", source_shot_indices=[0],
                order=0, duration_seconds=3.0,
            ),
            AdaptedSection(
                section_id="adp-dev", role="development", theme="主体",
                content_description="body", source_shot_indices=[1],
                order=1, duration_seconds=5.0,
            ),
            AdaptedSection(
                section_id="adp-closing", role="closing", theme="收尾",
                content_description="cta", source_shot_indices=[2],
                order=2, duration_seconds=4.0,
            ),
        ],
        main_track=[
            Scene(scene_id="sc-0", section="opening", source="user_material",
                  source_ref="m-1", start=0.0, duration=3.0, narration="开场词"),
            Scene(scene_id="sc-1", section="development", source="user_material",
                  source_ref="m-2", start=3.0, duration=5.0, narration="主体词"),
            Scene(scene_id="sc-2", section="closing", source="user_material",
                  source_ref="m-3", start=8.0, duration=4.0, narration="收尾词"),
        ],
        packaging_track=[],
        duration_seconds=12.0,
        variant="A",
    )


@pytest.fixture(autouse=True)
def cleanup_plans():
    yield
    for pid in _TEST_PLAN_IDS:
        plan_store._plans.pop(pid, None)
    _TEST_PLAN_IDS.clear()


@pytest.mark.asyncio
async def test_recommend_v2_returns_all_five_dimensions():
    """mock LLM 路径下 5 维度都至少给 1 个候选。"""
    plan = _make_plan(f"plan-v2-rec-{int(time.time() * 1000)}")
    _TEST_PLAN_IDS.append(plan.plan_id)
    plan_store.put(plan)

    rec = await recommend_packaging_v2(plan, preferences=PackagingPreferences())

    assert rec.plan_id == plan.plan_id
    assert len(rec.subtitle_styles) >= 1
    assert len(rec.title_bars) >= 1
    assert len(rec.stickers) >= 1
    assert len(rec.transition_bundles) >= 1
    assert len(rec.covers) >= 1
    # candidate_id 唯一
    sub_ids = {c.candidate_id for c in rec.subtitle_styles}
    assert len(sub_ids) == len(rec.subtitle_styles)


@pytest.mark.asyncio
async def test_recommend_v2_does_not_mutate_plan():
    """V2 /recommend 必须不写 plan.packaging_track。"""
    plan = _make_plan(f"plan-v2-pure-{int(time.time() * 1000)}")
    _TEST_PLAN_IDS.append(plan.plan_id)
    plan_store.put(plan)

    before_pkg = list(plan.packaging_track)
    await recommend_packaging_v2(plan, preferences=PackagingPreferences())
    fresh = plan_store.get(plan.plan_id)
    assert fresh is not None
    assert [it.item_id for it in fresh.packaging_track] == [it.item_id for it in before_pkg]


@pytest.mark.asyncio
async def test_apply_selection_writes_packaging_track_and_transition_in():
    """选 1 subtitle_style + 1 title_bar + 1 sticker + 1 transition + 1 cover → 全部落地。"""
    plan = _make_plan(f"plan-v2-apply-{int(time.time() * 1000)}")
    _TEST_PLAN_IDS.append(plan.plan_id)
    plan_store.put(plan)

    rec = await recommend_packaging_v2(plan, preferences=PackagingPreferences())
    assert rec.transition_bundles
    bundle = rec.transition_bundles[0]
    picked_style = bundle.options[0].style

    sel = PackagingSelection(
        plan_id=plan.plan_id,
        subtitle_style_id=rec.subtitle_styles[0].candidate_id,
        title_bar_ids=[rec.title_bars[0].candidate_id],
        sticker_ids=[rec.stickers[0].candidate_id],
        transition_selections={bundle.candidate_id: picked_style},
        cover_id=rec.covers[0].candidate_id,
        recommendation=rec,
    )
    out = apply_selection_to_plan(plan, sel)
    # subtitle 每段 narration 一条
    subs = [it for it in out.packaging_track if it.kind == "subtitle"]
    assert len(subs) == len([sc for sc in plan.main_track if sc.narration])
    # title_bar + sticker + cover 各 1 条
    assert sum(1 for it in out.packaging_track if it.kind == "title_bar") == 1
    assert sum(1 for it in out.packaging_track if it.kind == "sticker") == 1
    assert sum(1 for it in out.packaging_track if it.kind == "cover") == 1
    # transition 落到 Scene.transition_in
    transitions_set = [sc for sc in out.main_track if sc.transition_in is not None]
    assert len(transitions_set) >= 1
    assert transitions_set[0].transition_in.style == picked_style


@pytest.mark.asyncio
async def test_apply_selection_skips_invalid_transition_style():
    """transition_selections 写了一个 bundle.options 里没有的 style → 跳过、不报错。"""
    plan = _make_plan(f"plan-v2-skip-{int(time.time() * 1000)}")
    _TEST_PLAN_IDS.append(plan.plan_id)
    plan_store.put(plan)

    rec = await recommend_packaging_v2(plan, preferences=PackagingPreferences())
    bundle = rec.transition_bundles[0]

    sel = PackagingSelection(
        plan_id=plan.plan_id,
        subtitle_style_id=None,
        title_bar_ids=[],
        sticker_ids=[],
        transition_selections={bundle.candidate_id: "invalid_style_xx"},  # 不在 options 里
        cover_id=None,
        recommendation=rec,
    )
    out = apply_selection_to_plan(plan, sel)
    # 没选到合法 style → 该 bundle 跳过，无 transition_in 落地
    assert all(sc.transition_in is None for sc in out.main_track)


@pytest.mark.asyncio
async def test_apply_selection_clears_old_transitions():
    """apply 前 main_track 上手工塞了 transition_in，apply 后应被清空（再按 selection 重写）。"""
    from app.schemas import SceneTransition

    plan = _make_plan(f"plan-v2-clr-{int(time.time() * 1000)}")
    _TEST_PLAN_IDS.append(plan.plan_id)
    plan.main_track[1].transition_in = SceneTransition(style="whip", duration=0.3)
    plan_store.put(plan)

    rec = await recommend_packaging_v2(plan, preferences=PackagingPreferences())
    sel = PackagingSelection(
        plan_id=plan.plan_id,
        subtitle_style_id=None,
        title_bar_ids=[],
        sticker_ids=[],
        transition_selections={},  # 空：不应留下任何旧 transition
        cover_id=None,
        recommendation=rec,
    )
    out = apply_selection_to_plan(plan, sel)
    assert all(sc.transition_in is None for sc in out.main_track)


def test_router_recommend_v2_response_shape(client):
    """HTTP /api/packaging/recommend 直接打——response 应是 V2 5 维度结构。"""
    plan = _make_plan(f"plan-v2-http-{int(time.time() * 1000)}")
    _TEST_PLAN_IDS.append(plan.plan_id)
    plan_store.put(plan)

    resp = client.post(
        "/api/packaging/recommend",
        json={"plan_id": plan.plan_id, "apply": False},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["plan_id"] == plan.plan_id
    for key in ("subtitle_styles", "title_bars", "stickers", "transition_bundles", "covers"):
        assert key in data
        assert isinstance(data[key], list)
        assert len(data[key]) >= 1, f"{key} 应至少有 1 个候选"


def test_router_apply_persists(client):
    """HTTP /api/packaging/apply 把选择写到 plan.packaging_track。"""
    plan = _make_plan(f"plan-v2-app-{int(time.time() * 1000)}")
    _TEST_PLAN_IDS.append(plan.plan_id)
    plan_store.put(plan)

    rec_resp = client.post(
        "/api/packaging/recommend",
        json={"plan_id": plan.plan_id, "apply": False},
    )
    rec = rec_resp.json()
    bundle = rec["transition_bundles"][0]
    style = bundle["options"][0]["style"]

    sel_body = {
        "plan_id": plan.plan_id,
        "subtitle_style_id": rec["subtitle_styles"][0]["candidate_id"],
        "title_bar_ids": [rec["title_bars"][0]["candidate_id"]],
        "sticker_ids": [rec["stickers"][0]["candidate_id"]],
        "transition_selections": {bundle["candidate_id"]: style},
        "cover_id": rec["covers"][0]["candidate_id"],
        "recommendation": rec,
    }
    apply_resp = client.post("/api/packaging/apply", json=sel_body)
    assert apply_resp.status_code == 200, apply_resp.text
    new_plan = apply_resp.json()
    assert any(it["kind"] == "title_bar" for it in new_plan["packaging_track"])
    assert any(it["kind"] == "cover" for it in new_plan["packaging_track"])


def test_router_apply_plan_mismatch(client):
    """recommendation.plan_id 与请求 plan_id 不一致 → 400。"""
    plan = _make_plan(f"plan-v2-bad-{int(time.time() * 1000)}")
    _TEST_PLAN_IDS.append(plan.plan_id)
    plan_store.put(plan)

    sel_body = {
        "plan_id": plan.plan_id,
        "subtitle_style_id": None,
        "title_bar_ids": [],
        "sticker_ids": [],
        "transition_selections": {},
        "cover_id": None,
        "recommendation": {
            "plan_id": "plan-DOES-NOT-MATCH",
            "subtitle_styles": [],
            "title_bars": [],
            "stickers": [],
            "transition_bundles": [],
            "covers": [],
            "notes": [],
        },
    }
    resp = client.post("/api/packaging/apply", json=sel_body)
    assert resp.status_code == 400


# ---------- stage-58: 转场不再默认硬切 + 同义词归一化 ----------

def test_normalize_transition_synonyms():
    """LLM 输出 'fade' / 'crossfade' / '硬切' 等同义词应被归一化到白名单 token。"""
    from app.services.agent.packaging_agent import _normalize_transition_style

    assert _normalize_transition_style("fade") == "dissolve"
    assert _normalize_transition_style("Cross-Fade") == "dissolve"
    assert _normalize_transition_style("crossfade") == "dissolve"
    assert _normalize_transition_style("硬切") == "hard_cut"
    assert _normalize_transition_style("镜头甩动") == "whip"
    assert _normalize_transition_style("zoom_in") == "zoom"
    # 已是白名单 token 不变
    assert _normalize_transition_style("dissolve") == "dissolve"
    # 未知保留 lower 形态（让白名单判定决定丢弃）
    assert _normalize_transition_style("UnknownXYZ") == "unknownxyz"


def test_prefer_soft_primary_skips_hard_cut():
    """_prefer_soft_primary 应优先 dissolve/slide/zoom，不让 hard_cut 当默认。"""
    from app.services.agent.packaging_agent import _prefer_soft_primary

    full = PackagingPreferences()  # default allowed = [hard_cut, dissolve, slide, ...]
    assert _prefer_soft_primary(full) == "dissolve"

    # 白名单去掉 dissolve → 退到 slide
    no_diss = full.model_copy(update={"allowed_transition_styles": ["hard_cut", "slide", "wipe"]})
    assert _prefer_soft_primary(no_diss) == "slide"

    # 白名单只剩 hard_cut → 这才用 hard_cut
    only_hard = full.model_copy(update={"allowed_transition_styles": ["hard_cut"]})
    assert _prefer_soft_primary(only_hard) == "hard_cut"


@pytest.mark.asyncio
async def test_rule_based_v2_does_not_default_to_hard_cut():
    """规则兜底（mock LLM 也走类似路径）下 transition_bundles 主选不应是 hard_cut——
    stage-58 之前 _rule_based_v2_candidates 用 allowed[0]=hard_cut 做兜底主选，
    导致 step3 进入后整片硬切。"""
    plan = _make_plan(f"plan-v2-no-hardcut-{int(time.time() * 1000)}")
    _TEST_PLAN_IDS.append(plan.plan_id)
    plan_store.put(plan)

    rec = await recommend_packaging_v2(plan, preferences=PackagingPreferences())
    assert rec.transition_bundles, "至少应该有 1 个转场 bundle"

    # 主选 (options[0]) 不应是 hard_cut，规则表里 opening→development=dissolve、
    # development→closing=zoom，都明确不是 hard_cut。
    primaries = [b.options[0].style for b in rec.transition_bundles]
    assert all(s != "hard_cut" for s in primaries), (
        f"规则兜底不应给硬切作主选；实际 primaries={primaries}"
    )
