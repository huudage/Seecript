"""aigc_prompt_agent 直测：mock 路由 + 硬约束 + fallback。

校验：
1. mock LLM 走 `t2v_prompt` 指纹 → generate_aigc_prompt 返回非空 prompt，
   不出现 role 元数据词（opening/development/climax/closing）
2. 上下文（section.theme / content_description）被合并进 user payload，
   mock helper 能解析出『段落主题』并拼回 prompt 文本
3. plan=None / section=None 时不爆炸，走 fallback 路径并仍返回非空 prompt
"""
from __future__ import annotations

import asyncio

from app.schemas import AdaptedSection, ComposeSettings, Gap, Plan
from app.services.agent.aigc_prompt_agent import generate_aigc_prompt


def _mini_section() -> AdaptedSection:
    return AdaptedSection(
        section_id="sec-0",
        role="opening",
        theme="悬念开场",
        content_description="埃及黄金面具特写 + 金字塔轮廓高光，吊起好奇心，避免观众划走。",
        source_section_indices=[0],
        source_shot_indices=[0],
        order=0,
        duration_seconds=4.0,
    )


def _mini_plan(section: AdaptedSection) -> Plan:
    return Plan(
        plan_id="plan-test-aigc-prompt",
        sample_ids=["sample-marketing-01"],
        session_id="sess-test",
        brief="埃及历史文物展览推广",
        video_goal="吸引用户来参观",
        adapted_sections=[section],
        variant="A",
        duration_seconds=4.0,
        main_track=[],
        packaging_track=[],
        settings=ComposeSettings(),
    )


def _mini_gap(section_id: str = "sec-0") -> Gap:
    return Gap(
        gap_id="gap-test",
        section="opening",
        section_id=section_id,
        slot_index=0,
        requirement="开场要求：埃及黄金面具特写 + 金字塔轮廓",
        status="miss",
        impact="high",
    )


def test_generate_aigc_prompt_mock_round_trip():
    """mock 路径下：返回完备 prompt + thinking，无 role 元数据词，含主题关键字。"""
    sec = _mini_section()
    plan = _mini_plan(sec)
    gap = _mini_gap()
    prompt, thinking = asyncio.run(generate_aigc_prompt(gap, plan, sec))
    assert prompt, "prompt 不能为空"
    assert isinstance(thinking, list), "thinking 必须是 list"
    # mock 模板里有『悬念开场』作为段落主题回写
    assert "悬念开场" in prompt
    # 不应该出现 role 元数据词
    for bad in ("opening", "development", "climax", "closing"):
        assert bad not in prompt, f"prompt 含元数据词 {bad}: {prompt}"
    assert len(prompt) > 20


def test_generate_aigc_prompt_no_section_no_plan():
    """section/plan 都 None 时也能返回非空 prompt（走 mock 或 fallback 兜底）。"""
    gap = _mini_gap()
    prompt, _thinking = asyncio.run(generate_aigc_prompt(gap, None, None))
    assert prompt
    for bad in ("opening", "development", "climax", "closing"):
        assert bad not in prompt


def test_generate_aigc_prompt_with_user_hint():
    """创作者 hint 不应导致 LLM 调用失败；mock 路径下仍返回有效 prompt。"""
    sec = _mini_section()
    plan = _mini_plan(sec)
    gap = _mini_gap()
    prompt, _thinking = asyncio.run(
        generate_aigc_prompt(gap, plan, sec, user_hint="一定要有日落金光")
    )
    assert prompt
    assert len(prompt) > 20
