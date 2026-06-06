"""POST /api/packaging/recommend —— 用户偏好集成测试。

覆盖：
1. preset='minimalist' → agent 收到的 allowed_styles ⊂ {hard_cut, dissolve}（mock LLM 路径）
2. custom 模式 allowed_styles=['zoom'] → 输出 transitions 全部 style=='zoom'
3. clamp：超长 duration 被 max_transition_duration 钳到上限
4. 持久化：POST 后 plan.settings.packaging_prefs 已更新
5. expand_preset 函数本身：传入 preset='dialogue' 后字段被覆盖

mock 模式下 LLM 走 mock client，会返回 schema-shaped 但内容不可控的 JSON；
本测重点是钳制层是否对任意输入都能产生合法输出，不依赖具体 LLM 字段。
"""
from __future__ import annotations

import time

import pytest

from app.schemas import (
    AdaptedSection,
    ComposeSettings,
    PackagingPreferences,
    Plan,
    Scene,
)
from app.services.agent.packaging_agent import (
    _coerce_transition,
    expand_preset,
    recommend_packaging,
)
from app.services.plans.store import plan_store


_TEST_PLAN_IDS: list[str] = []


def _make_plan(plan_id: str, *, prefs: PackagingPreferences | None = None) -> Plan:
    settings_kwargs: dict = {}
    if prefs is not None:
        settings_kwargs["packaging_prefs"] = prefs
    return Plan(
        plan_id=plan_id,
        sample_ids=["sample-marketing-01"],
        project_id=None,
        session_id=None,
        video_goal="新品发布——突出差异化卖点",
        brief="差异化新品",
        settings=ComposeSettings(**settings_kwargs),
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
    for plan_id in _TEST_PLAN_IDS:
        plan_store._plans.pop(plan_id, None)
    _TEST_PLAN_IDS.clear()


def test_expand_preset_minimalist_overrides_fields():
    """preset='minimalist' → allowed_styles 被收紧到 [hard_cut, dissolve]，cover_source=video_goal。"""
    raw = PackagingPreferences(preset="minimalist")
    out = expand_preset(raw)
    assert set(out.allowed_transition_styles) == {"hard_cut", "dissolve"}
    assert out.max_transition_duration == 0.4
    assert out.cover_text_source == "video_goal"
    assert out.subtitle_background == "none"


def test_expand_preset_dialogue_enables_bilingual():
    raw = PackagingPreferences(preset="dialogue")
    out = expand_preset(raw)
    assert out.subtitle_bilingual is True
    assert out.subtitle_font_size == "large"


def test_expand_preset_custom_passthrough():
    """preset='custom' → 字段原样不动。"""
    raw = PackagingPreferences(
        preset="custom",
        allowed_transition_styles=["zoom"],
        max_transition_duration=1.5,
    )
    out = expand_preset(raw)
    assert out.allowed_transition_styles == ["zoom"]
    assert out.max_transition_duration == 1.5


def test_coerce_transition_replaces_style_outside_whitelist():
    """LLM 输出 style='whip' 但白名单只有 [zoom] → coerce 替换为 zoom，不丢条目。"""
    prefs = PackagingPreferences(
        preset="custom",
        allowed_transition_styles=["zoom"],
        max_transition_duration=0.8,
    )
    raw = {
        "style": "whip",
        "at_seconds": 3.0,
        "duration": 0.5,
        "from_section": "opening",
        "to_section": "development",
        "reason": "test",
    }
    out = _coerce_transition(raw, 0, prefs)
    assert out is not None
    assert out.style == "zoom"


def test_coerce_transition_clamps_duration_to_max():
    """LLM 输出 duration=2.5 → 钳到 max_transition_duration（0.6）。"""
    prefs = PackagingPreferences(
        preset="custom",
        allowed_transition_styles=["dissolve"],
        max_transition_duration=0.6,
    )
    raw = {
        "style": "dissolve",
        "at_seconds": 3.0,
        "duration": 2.5,
        "from_section": "opening",
        "to_section": "development",
        "reason": "test",
    }
    out = _coerce_transition(raw, 0, prefs)
    assert out is not None
    assert out.duration == 0.6


def test_coerce_transition_rejects_invalid_role():
    """空 from_section / 异常长 from_section → 丢弃整条。

    Stage-16 起 role 是自由字符串（要支持 step_N/item_N），不再校验白名单；
    仅在 role 为空或长度超 30 时拒绝。
    """
    prefs = PackagingPreferences()
    raw_empty = {
        "style": "dissolve",
        "at_seconds": 3.0,
        "duration": 0.4,
        "from_section": "",
        "to_section": "development",
        "reason": "test",
    }
    assert _coerce_transition(raw_empty, 0, prefs) is None
    raw_too_long = {
        "style": "dissolve",
        "at_seconds": 3.0,
        "duration": 0.4,
        "from_section": "x" * 50,
        "to_section": "development",
        "reason": "test",
    }
    assert _coerce_transition(raw_too_long, 0, prefs) is None


@pytest.mark.asyncio
async def test_recommend_packaging_respects_minimalist_allowed_styles():
    """完整跑一遍 recommend_packaging（mock LLM）→ 输出 transition style 全部 ∈ {hard_cut, dissolve}。"""
    prefs = PackagingPreferences(preset="minimalist")
    plan = _make_plan(f"plan-min-{int(time.time() * 1000)}", prefs=prefs)
    _TEST_PLAN_IDS.append(plan.plan_id)
    plan_store.put(plan)

    rec = await recommend_packaging(plan, apply=True, preferences=prefs)

    primary = rec.versions[0]
    assert len(primary.transitions) > 0, "至少应有一条 transition（兜底也算）"
    for tr in primary.transitions:
        assert tr.style in ("hard_cut", "dissolve"), (
            f"minimalist 预设下出现非法 style={tr.style}（应只有 hard_cut/dissolve）"
        )
        assert tr.duration <= 0.4, f"minimalist max_duration=0.4 应钳到 {tr.duration}"


@pytest.mark.asyncio
async def test_recommend_packaging_custom_single_style_funnels():
    """custom 模式只允许 zoom → 所有输出转场 style 都是 zoom（规则/LLM 都被收敛到唯一选项）。"""
    prefs = PackagingPreferences(
        preset="custom",
        allowed_transition_styles=["zoom"],
        max_transition_duration=0.5,
    )
    plan = _make_plan(f"plan-zoom-{int(time.time() * 1000)}", prefs=prefs)
    _TEST_PLAN_IDS.append(plan.plan_id)
    plan_store.put(plan)

    rec = await recommend_packaging(plan, apply=False, preferences=prefs)

    primary = rec.versions[0]
    assert len(primary.transitions) > 0
    for tr in primary.transitions:
        assert tr.style == "zoom"
        assert tr.duration <= 0.5


@pytest.mark.asyncio
async def test_recommend_packaging_video_goal_drives_cover_title():
    """cover_text_source=video_goal → 封面 title 取自 plan.video_goal 前 12 字。"""
    prefs = PackagingPreferences(
        preset="custom",
        cover_text_source="video_goal",
    )
    plan = _make_plan(f"plan-vg-{int(time.time() * 1000)}", prefs=prefs)
    _TEST_PLAN_IDS.append(plan.plan_id)
    plan_store.put(plan)

    rec = await recommend_packaging(plan, apply=False, preferences=prefs)

    primary = rec.versions[0]
    assert primary.cover is not None
    expected_prefix = plan.video_goal[:12]  # type: ignore[index]
    assert primary.cover.title == expected_prefix


def test_router_persists_preferences_to_plan_settings(client):
    """POST /api/packaging/recommend → plan.settings.packaging_prefs 被请求体覆盖并写盘。

    V2 起 /recommend 返回的是 PackagingRecommendationV2（5 维度候选），
    但偏好持久化路径不变。
    """
    plan = _make_plan(f"plan-persist-{int(time.time() * 1000)}")
    _TEST_PLAN_IDS.append(plan.plan_id)
    plan_store.put(plan)

    body = {
        "plan_id": plan.plan_id,
        "apply": True,
        "preferences": {
            "preset": "custom",
            "allowed_transition_styles": ["hard_cut", "wipe"],
            "max_transition_duration": 0.5,
            "subtitle_font_size": "large",
            "subtitle_position": "top",
            "subtitle_background": "gradient",
            "subtitle_bilingual": False,
            "cover_text_source": "custom",
            "cover_custom_text": "测试封面",
            "cover_duration": 1.8,
            "cover_with_subtitle": False,
            "llm_temperature": 0.5,
        },
    }
    resp = client.post("/api/packaging/recommend", json=body)
    assert resp.status_code == 200, resp.text
    # V2 响应：5 维度键齐
    data = resp.json()
    for k in ("subtitle_styles", "title_bars", "stickers", "transition_bundles", "covers"):
        assert k in data

    # plan.settings.packaging_prefs 应被请求体覆盖
    fresh = plan_store.get(plan.plan_id)
    assert fresh is not None
    pp = fresh.settings.packaging_prefs
    assert pp.preset == "custom"
    assert pp.allowed_transition_styles == ["hard_cut", "wipe"]
    assert pp.subtitle_font_size == "large"
    assert pp.subtitle_position == "top"
    assert pp.cover_custom_text == "测试封面"
    assert pp.llm_temperature == 0.5


def test_router_unknown_plan_returns_404(client):
    resp = client.post(
        "/api/packaging/recommend",
        json={"plan_id": "plan-does-not-exist", "apply": True},
    )
    assert resp.status_code == 404


def test_subtitle_items_seeded_with_prefs_on_plan_build(client):
    """plan/build 落盘的 subtitle PackagingItem.style 已携带 prefs 的字幕字段，给 ffmpeg burn 用。"""
    # 上传素材
    from io import BytesIO

    fake_video = b"\x00\x00\x00\x18ftypisom" + b"\x00" * 2048
    files = [("files", ("a.mp4", BytesIO(fake_video), "video/mp4"))]
    r = client.post("/api/material/upload", files=files, data={"project_id": "proj-pp-seed"})
    assert r.status_code == 200
    upload = r.json()

    # build plan 时带 packaging_prefs（preset=dialogue → 大字号底部）
    body = {
        "sample_ids": ["sample-marketing-01"],
        "project_id": "proj-pp-seed",
        "session_id": upload["session_id"],
        "brief": "种子测试",
        "video_goal": "测试包装",
        "settings": {
            "target_duration_seconds": 30,
            "voiceover_enabled": True,
            "packaging_prefs": {
                "preset": "dialogue",
                "allowed_transition_styles": ["hard_cut", "dissolve"],
                "max_transition_duration": 0.5,
                "subtitle_font_size": "medium",  # 会被 dialogue 预设覆盖到 large
                "subtitle_position": "top",
                "subtitle_background": "none",
                "subtitle_bilingual": False,
                "cover_text_source": "auto",
                "cover_duration": 1.2,
                "cover_with_subtitle": True,
                "llm_temperature": 0.7,
            },
        },
        "selected_materials": [m["material_id"] for m in upload["materials"]],
        "fills": [],
        "variant": "A",
    }
    r = client.post("/api/plan/build", json=body)
    assert r.status_code == 200, r.text
    plan = r.json()
    _TEST_PLAN_IDS.append(plan["plan_id"])

    # 找到 subtitle item
    # 注:auto-narration 已移除(用户在第 2 步会显式触发 copy/AIGC 才有 narration),
    # 所以 plan/build 直出时 main_track scene.narration 为空 → 没有 subtitle 烧入。
    # 这里转而验证 packaging_prefs 已落盘到 plan.settings,后续 LLM/burn 阶段会读取。
    subs = [it for it in plan["packaging_track"] if it["kind"] == "subtitle"]
    if subs:
        sub = subs[0]
        # dialogue 预设展开后 subtitle_font_size 应被覆盖为 large、bilingual=true
        assert sub["style"]["font_size"] == "large"
        assert sub["style"]["bilingual"] is True
    # plan.settings.packaging_prefs 必须落盘——下游 burn / 包装阶段读它
    prefs = plan["settings"].get("packaging_prefs") or {}
    assert prefs.get("preset") == "dialogue", prefs
