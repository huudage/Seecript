"""Gap copy fill 自动链 TTS —— voiceover_enabled 开关分支。

行为契约：
- plan.settings.voiceover_enabled=True + copy 成功 + narration 非空
  → 自动调用 mock TTS → FillResult.voiceover_url 非空 + scene.voiceover_url 回写 + 落盘
- plan.settings.voiceover_enabled=False
  → 不调 TTS，voiceover_url 仍为 None
- gap.section_id 找不到对应 plan/section
  → 静默跳过，不影响 copy 返回成功
"""
from __future__ import annotations

import shutil
import time

import pytest

from app.config import get_settings
from app.routers.gap import _maybe_auto_tts
from app.schemas import (
    AdaptedSection,
    ComposeSettings,
    FillResult,
    Plan,
    Scene,
)
from app.services.plans.store import plan_store


_TEST_PLAN_IDS: list[str] = []


def _make_plan(plan_id: str, *, voiceover_enabled: bool = True, section_id: str = "adp-dev-1") -> Plan:
    return Plan(
        plan_id=plan_id,
        sample_ids=["sample-marketing-01"],
        project_id=None,
        session_id=None,
        settings=ComposeSettings(voiceover_enabled=voiceover_enabled),
        adapted_sections=[
            AdaptedSection(
                section_id=section_id, role="development", theme="对比段",
                content_description="对比", source_shot_indices=[1],
                order=2, duration_seconds=4.0,
            ),
        ],
        main_track=[
            Scene(
                scene_id="sc-2", section="development", source="text_card",
                source_ref=f"text-card-{section_id}", start=0.0, duration=4.0,
                narration="",  # 等 copy 后填回
            ),
        ],
        packaging_track=[],
        duration_seconds=4.0,
        variant="A",
    )


@pytest.fixture(autouse=True)
def cleanup_plans():
    yield
    voice_root = get_settings().log_dir.parent / "var" / "voiceovers"
    for plan_id in _TEST_PLAN_IDS:
        plan_store._plans.pop(plan_id, None)
        target = voice_root / plan_id
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
    _TEST_PLAN_IDS.clear()


def _copy_result(section_id: str, narration: str = "AI 补全的口播文案") -> FillResult:
    return FillResult(
        gap_id=f"gap-dev-0-{section_id}",
        action="copy",
        narration=narration,
        status="ok",
        section_id=section_id,
    )


def test_auto_tts_chains_when_voiceover_enabled():
    section_id = f"adp-on-{int(time.time() * 1000)}"
    plan = _make_plan(f"plan-autotts-on-{int(time.time() * 1000)}", section_id=section_id)
    _TEST_PLAN_IDS.append(plan.plan_id)
    plan_store.put(plan)

    result = _copy_result(section_id)
    chained = _maybe_auto_tts(result)

    assert chained.voiceover_url == f"/voiceovers/{plan.plan_id}/sc-2.wav"
    refreshed = plan_store.get(plan.plan_id)
    assert refreshed.main_track[0].voiceover_url == chained.voiceover_url
    # 空 narration 的 scene 应被回填
    assert refreshed.main_track[0].narration == "AI 补全的口播文案"


def test_auto_tts_skipped_when_voiceover_disabled():
    section_id = f"adp-off-{int(time.time() * 1000)}"
    plan = _make_plan(
        f"plan-autotts-off-{int(time.time() * 1000)}",
        voiceover_enabled=False, section_id=section_id,
    )
    _TEST_PLAN_IDS.append(plan.plan_id)
    plan_store.put(plan)

    result = _copy_result(section_id)
    chained = _maybe_auto_tts(result)

    assert chained.voiceover_url is None
    refreshed = plan_store.get(plan.plan_id)
    assert refreshed.main_track[0].voiceover_url is None


def test_auto_tts_no_op_when_section_id_unknown():
    plan = _make_plan(f"plan-autotts-noid-{int(time.time() * 1000)}")
    _TEST_PLAN_IDS.append(plan.plan_id)
    plan_store.put(plan)

    result = _copy_result("adp-totally-unknown-12345")
    chained = _maybe_auto_tts(result)

    assert chained.voiceover_url is None


def test_auto_tts_no_op_for_non_copy_action():
    section_id = f"adp-aigc-{int(time.time() * 1000)}"
    plan = _make_plan(f"plan-autotts-aigc-{int(time.time() * 1000)}", section_id=section_id)
    _TEST_PLAN_IDS.append(plan.plan_id)
    plan_store.put(plan)

    result = FillResult(
        gap_id="gap-x", action="aigc",
        new_material_id="task-1",
        status="ok", section_id=section_id,
    )
    chained = _maybe_auto_tts(result)
    assert chained.voiceover_url is None
