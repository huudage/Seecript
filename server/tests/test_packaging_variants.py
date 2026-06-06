"""Stage-16 PackagingRecommendation 多版本 schema 测试。

验证：
- 新格式 PackagingRecommendation(versions=[...]) 直接构造成功
- 老数据（顶层 transitions/cover）能被 model_validator 自动包装为单 aggressive variant
- recommend_packaging mock 路径能产出 2 个 variant（aggressive + elegant）
"""
from __future__ import annotations

import time

import pytest

from app.schemas import (
    AdaptedSection,
    ComposeSettings,
    CoverDesign,
    PackagingPreferences,
    PackagingRecommendation,
    PackagingVariant,
    Plan,
    Scene,
    TransitionSuggestion,
)
from app.services.agent.packaging_agent import recommend_packaging
from app.services.plans.store import plan_store


def _make_plan(plan_id: str) -> Plan:
    return Plan(
        plan_id=plan_id,
        sample_ids=["sample-marketing-01"],
        project_id=None,
        session_id=None,
        video_goal="测试包装",
        brief="测试主题",
        settings=ComposeSettings(),
        adapted_sections=[
            AdaptedSection(section_id="adp-0", role="opening", theme="开场",
                          content_description="hook", source_shot_indices=[0],
                          order=0, duration_seconds=3.0),
            AdaptedSection(section_id="adp-1", role="development", theme="主体",
                          content_description="body", source_shot_indices=[1],
                          order=1, duration_seconds=5.0),
            AdaptedSection(section_id="adp-2", role="closing", theme="收尾",
                          content_description="cta", source_shot_indices=[2],
                          order=2, duration_seconds=3.0),
        ],
        main_track=[
            Scene(scene_id="sc-0", section="opening", source="user_material",
                  source_ref="m-1", start=0.0, duration=3.0, narration=""),
            Scene(scene_id="sc-1", section="development", source="user_material",
                  source_ref="m-2", start=3.0, duration=5.0, narration=""),
            Scene(scene_id="sc-2", section="closing", source="user_material",
                  source_ref="m-3", start=8.0, duration=3.0, narration=""),
        ],
        packaging_track=[],
        duration_seconds=11.0,
        variant="A",
    )


def test_new_format_dual_variant_constructs():
    rec = PackagingRecommendation(
        plan_id="p-1",
        versions=[
            PackagingVariant(
                version_id="aggressive",
                version_label="强冲击版",
                transitions=[],
                cover=None,
            ),
            PackagingVariant(
                version_id="elegant",
                version_label="高级感版",
                transitions=[],
                cover=None,
            ),
        ],
        notes=[],
    )
    assert len(rec.versions) == 2
    assert rec.versions[0].version_id == "aggressive"
    assert rec.versions[1].version_id == "elegant"


def test_legacy_top_level_wraps_into_single_variant():
    """旧数据 {plan_id, transitions, cover, notes} → validator 自动包成 versions=[aggressive]."""
    legacy = {
        "plan_id": "p-legacy",
        "transitions": [
            {
                "item_id": "pkg-tr-00",
                "at_seconds": 3.0,
                "from_section": "opening",
                "to_section": "development",
                "style": "hard_cut",
                "duration": 0.4,
                "reason": "test",
            }
        ],
        "cover": {
            "title": "封面",
            "subtitle": None,
            "palette": ["#FFE600", "#1F2937"],
            "layout": "center",
            "style_note": "test",
        },
        "notes": [],
    }
    rec = PackagingRecommendation.model_validate(legacy)
    assert len(rec.versions) == 1
    assert rec.versions[0].version_id == "aggressive"
    assert len(rec.versions[0].transitions) == 1
    assert rec.versions[0].cover is not None
    assert rec.versions[0].cover.title == "封面"


_TEST_PLAN_IDS: list[str] = []


@pytest.fixture(autouse=True)
def cleanup_plans():
    yield
    for pid in _TEST_PLAN_IDS:
        plan_store._plans.pop(pid, None)
    _TEST_PLAN_IDS.clear()


@pytest.mark.asyncio
async def test_recommend_packaging_emits_dual_variants():
    """recommend_packaging 一定产出 2 个 variant：aggressive + elegant。"""
    plan = _make_plan(f"plan-pv-{int(time.time() * 1000)}")
    _TEST_PLAN_IDS.append(plan.plan_id)
    plan_store.put(plan)

    rec = await recommend_packaging(plan, apply=False, preferences=PackagingPreferences())
    assert len(rec.versions) == 2
    ids = [v.version_id for v in rec.versions]
    assert "aggressive" in ids and "elegant" in ids

    aggressive = next(v for v in rec.versions if v.version_id == "aggressive")
    elegant = next(v for v in rec.versions if v.version_id == "elegant")
    # elegant 的 item_id 应该带 'eleg' 标记
    if elegant.transitions:
        assert all("eleg" in t.item_id for t in elegant.transitions)
    if aggressive.cover and elegant.cover:
        # elegant cover layout 强制 center
        assert elegant.cover.layout == "center"
