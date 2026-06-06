"""Stage-16 5 种结构模式回归：每个 pattern 都能跑通 adapt_structure 改编。

mock LLM 走 _build_mock_adapted_sections_json，pattern 通过 user payload 注入；
本测验证 mock 给出的段落满足各 pattern 的硬约束（首尾角色类、峰值 ≤1、段数范围）。
"""
from __future__ import annotations

import pytest

from app.schemas import (
    PackagingProfile,
    RhythmCurve,
    SampleManifest,
    Section,
    Shot,
    VideoUnderstanding,
    allowed_roles_for,
    role_is_closing,
    role_is_opening,
    role_is_peak,
)
from app.services.agent.plan_agent import adapt_structure


def _build_manifest(pattern: str, n_sections: int = 5) -> SampleManifest:
    """构造一个最小 manifest，understanding.structural_pattern 决定下游用哪套角色。"""
    allowed = allowed_roles_for(pattern)
    # 直接借用 allowed_roles 的前 n_sections 个；首末固定到开场/收尾类
    roles: list[str] = []
    if pattern == "dramatic":
        roles = ["opening"] + ["development"] * (n_sections - 2) + ["closing"]
    elif pattern == "stepwise":
        roles = ["intro"] + [f"step_{i+1}" for i in range(n_sections - 2)] + ["recap"]
    elif pattern == "listicle":
        roles = ["hook"] + [f"item_{i+1}" for i in range(n_sections - 2)] + ["closer"]
    elif pattern == "atmospheric":
        roles = ["establish"] + ["flow"] * (n_sections - 2) + ["resolve"]
    elif pattern == "info_dense":
        roles = ["title_card"] + ["info_block"] * (n_sections - 2) + ["payoff"]

    sections = [
        Section(role=r, theme=f"{r} 主题", start=float(i * 5), end=float((i + 1) * 5),
                summary=f"{r} 段", shot_indices=[i])
        for i, r in enumerate(roles)
    ]
    shots = [
        Shot(index=i, start=float(i * 5), end=float((i + 1) * 5),
             duration=5.0, thumbnail_url=None, transcript=None, tags=[])
        for i in range(n_sections)
    ]
    return SampleManifest(
        sample_id=f"s-{pattern}",
        title=f"{pattern} 样例",
        video_type="marketing",
        duration_seconds=float(n_sections * 5),
        video_url=f"/samples/s-{pattern}/video.mp4",
        has_voice=True,
        shots=shots,
        rhythm=RhythmCurve(times=[0.0, 5.0], cut_density=[1.0, 0.8],
                          bgm_energy=[0.2, 0.5], tempo_bpm=120.0),
        sections=sections,
        packaging=PackagingProfile(
            subtitle_style="大字加描边", has_title_bar=True,
            transition_types=["cut"], cover_style=None, sticker_density=0.2,
        ),
        understanding=VideoUnderstanding(
            archetype=f"{pattern} archetype",
            narrative_summary=f"{pattern} test sample",
            tone="energetic",
            structural_pattern=pattern,
            tempo="medium",
            estimated_segments=n_sections,
        ),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("pattern", ["dramatic", "stepwise", "listicle", "atmospheric", "info_dense"])
async def test_adapt_structure_respects_pattern(pattern):
    """对每个 pattern：adapt_structure 输出首=开场类、末=收尾类、峰值类 ≤1、段数在允许范围。"""
    manifest = _build_manifest(pattern, n_sections=5)
    sections = await adapt_structure(
        [manifest],
        brief=f"{pattern} 改编测试",
        video_goal=f"测试 {pattern} 模式",
    )
    assert sections, f"{pattern} 模式没产出任何段"

    n = len(sections)
    if pattern == "listicle":
        assert 2 <= n <= 8, f"{pattern} 段数 {n} 超 [2,8]"
    else:
        assert 3 <= n <= 7, f"{pattern} 段数 {n} 超 [3,7]"

    assert role_is_opening(sections[0].role, pattern), (
        f"{pattern} 首段 role={sections[0].role} 不属于开场类"
    )
    assert role_is_closing(sections[-1].role, pattern), (
        f"{pattern} 末段 role={sections[-1].role} 不属于收尾类"
    )

    peak_count = sum(1 for s in sections if role_is_peak(s.role, pattern))
    assert peak_count <= 1, f"{pattern} 峰值类段数 {peak_count} 超 1"

    # 中间段不应出现开场/收尾类
    for i in range(1, n - 1):
        r = sections[i].role
        assert not role_is_opening(r, pattern), f"{pattern} 中间段出现开场类 role={r}"
        assert not role_is_closing(r, pattern), f"{pattern} 中间段出现收尾类 role={r}"
