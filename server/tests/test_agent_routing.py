"""Agent 路由直测：decompose role 输出 + gap_agent T2V 分支。

测试场景：
1. decompose_agent 在无 video_path 时走 mock 数据，必须返回符合 SectionRole 4 元枚举
   的段落（且首尾恰好 opening/closing，最多 1 个 climax）。
2. gap_agent 的 aigc 分支调 Seedance T2V mock：submit → poll → succeed，
   返回 task_id 作为 new_material_id。
"""
from __future__ import annotations

from typing import get_args

import pytest

from app.schemas import (
    AdaptedSection,
    Gap,
    Material,
    PackagingProfile,
    RhythmCurve,
    SampleManifest,
    Section,
    SectionRole,
    Shot,
)
from app.services.agent.decompose_agent import decompose
from app.services.agent.gap_agent import detect_gaps, fill_gap


_ALLOWED_ROLES: set[SectionRole] = set(get_args(SectionRole))  # type: ignore[arg-type]


def _adapt_from_manifest(manifest: SampleManifest) -> list[AdaptedSection]:
    """测试辅助：把 manifest.sections 1:1 包成 AdaptedSection，模拟"老 plan + legacy_wrap"。"""
    return [
        AdaptedSection(
            section_id=f"sec-{i}",
            role=sec.role,
            theme=sec.theme or "段落",
            content_description=f"测试段 {sec.role}",
            source_section_indices=[i],
            source_shot_indices=list(sec.shot_indices or []),
            order=i,
        )
        for i, sec in enumerate(manifest.sections)
    ]


# ----------------------------- decompose 三类型 --------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("video_type", ["marketing", "editing", "motion_graph"])
async def test_decompose_returns_role_based_sections(video_type):
    """无 video_path 时走 mock；段落必须满足 4 元 role 约束：
    - 至少 3 段
    - 首段=opening，末段=closing
    - climax 最多 1 段
    - 所有 role 必须落在 4 元枚举里
    """
    manifest = await decompose(
        sample_id=f"sample-{video_type}-test",
        video_type=video_type,
    )
    assert manifest.video_type == video_type
    assert manifest.shots, "decompose 必须返回至少一个 shot"

    sections = manifest.sections
    assert len(sections) >= 3, f"段落数过少：{len(sections)}"

    roles = [s.role for s in sections]
    for r in roles:
        assert r in _ALLOWED_ROLES, f"非法 role={r}"

    assert sections[0].role == "opening", f"首段 role 应为 opening，实际 {sections[0].role}"
    assert sections[-1].role == "closing", f"末段 role 应为 closing，实际 {sections[-1].role}"
    climax_count = sum(1 for r in roles if r == "climax")
    assert climax_count <= 1, f"climax 段最多 1 段，实际 {climax_count}"

    # theme 字段应非空（mock fixture 给了默认 theme）
    for sec in sections:
        assert isinstance(sec.theme, str)


# ----------------------------- gap_agent T2V 分支 ------------------------------


def _mini_manifest(video_type="marketing") -> SampleManifest:
    """构造一个 4 段 role 的最小 manifest：opening / development / climax / closing。"""
    structure: list[tuple[SectionRole, str]] = [
        ("opening", "钩子开场"),
        ("development", "主体铺陈"),
        ("climax", "情绪高潮"),
        ("closing", "行动引导"),
    ]
    sections = [
        Section(role=role, theme=theme, start=float(i * 5), end=float((i + 1) * 5),
                summary=f"{role} 段", shot_indices=[i])
        for i, (role, theme) in enumerate(structure)
    ]
    shots = [
        Shot(index=i, start=float(i * 5), end=float((i + 1) * 5),
             duration=5.0, thumbnail_url=None, transcript=None, tags=[])
        for i in range(len(structure))
    ]
    return SampleManifest(
        sample_id="s-test", title="t", video_type=video_type,
        duration_seconds=float(len(structure) * 5),
        video_url="/samples/s-test/video.mp4",
        has_voice=video_type != "motion_graph",
        shots=shots,
        rhythm=RhythmCurve(times=[0.0, 5.0], cut_density=[1.0, 0.6], bgm_energy=[0.1, 0.4], tempo_bpm=120.0),
        sections=sections,
        packaging=PackagingProfile(
            subtitle_style="大字加描边", has_title_bar=True,
            transition_types=["cut"], cover_style=None, sticker_density=0.2,
        ),
    )


@pytest.mark.asyncio
async def test_gap_agent_aigc_calls_seedance_mock():
    """aigc 分支：mock T2V 在 ~8s 后从 pending 跳到 succeeded；
    缩短轮询参数让测试快速完成，验证 new_material_id == task_id 且 status=warn（超时）。"""
    manifest = _mini_manifest("marketing")
    gaps = detect_gaps(_adapt_from_manifest(manifest), manifest, materials=[])
    assert gaps, "无 material 时应至少识别出若干 miss 槽位"
    miss_gap = next((g for g in gaps if g.status == "miss"), gaps[0])
    assert miss_gap.section in _ALLOWED_ROLES

    result = await fill_gap(
        miss_gap,
        action="aigc",
        params={
            "prompt": "测试用 prompt",
            "duration_seconds": 5,
            "poll_interval_seconds": 0.1,
            "max_wait_seconds": 0.5,
        },
    )
    # max_wait=0.5s < mock 默认 8s，必然超时；返回 task_id + warn
    assert result.action == "aigc"
    assert result.new_material_id and result.new_material_id.startswith("mock-t2v-")
    assert result.status == "warn"
    # chunk-aware note：要么提到 timeout，要么提到 Seedance 部分完成（0/1 段）
    note = result.note or ""
    assert "timeout" in note or "Seedance" in note, f"unexpected note: {note!r}"
    # 单段失败时 chunk_task_ids 至少回写一个 task_id，前端 refresh 能复用
    assert result.chunk_task_ids and result.chunk_task_ids[0].startswith("mock-t2v-")


@pytest.mark.asyncio
async def test_gap_agent_aigc_succeeds_when_wait_long_enough(monkeypatch):
    """把 mock T2V 的 mock_duration 调到 0 让 query 立即返回 succeeded。"""
    monkeypatch.setenv("T2V_MOCK_DURATION_SECONDS", "0")
    from app.config import get_settings
    get_settings.cache_clear()

    manifest = _mini_manifest("editing")
    gaps = detect_gaps(_adapt_from_manifest(manifest), manifest, materials=[])
    target = next(g for g in gaps if g.status == "miss")
    assert target.section in _ALLOWED_ROLES

    result = await fill_gap(
        target, action="aigc",
        params={"poll_interval_seconds": 0.05, "max_wait_seconds": 3.0},
    )
    assert result.action == "aigc"
    assert result.status == "ok"
    assert result.new_material_id and result.new_material_id.startswith("mock-t2v-")
    # 5s 请求 ≤ SEEDANCE_MAX 12s → 单段；video_urls 应回写 mock CDN url
    assert result.chunks_count == 1
    assert len(result.video_urls) == 1
    assert result.video_urls[0].startswith("/aigc/")
    assert result.cover_url and result.cover_url.startswith("/aigc/")


# ----------------------------- detect_gaps role 覆盖 ----------------------------


def test_detect_gaps_covers_all_roles_in_manifest():
    """无 material 时，每个 role 都应至少识别出 1 个槽，且都为 miss。"""
    manifest = _mini_manifest("marketing")
    gaps = detect_gaps(_adapt_from_manifest(manifest), manifest, materials=[])
    seen_roles = {g.section for g in gaps}
    expected = {"opening", "development", "climax", "closing"}
    assert seen_roles == expected, f"got {seen_roles}, want {expected}"
    assert all(g.status == "miss" for g in gaps)


def test_detect_gaps_assigns_material_when_role_matches():
    """有 recommended_section=opening 的 material 时，opening 槽位首位应被填上。"""
    manifest = _mini_manifest("marketing")
    mat = Material(
        material_id="mat-opening-1",
        filename="opening.mp4",
        media_type="video",
        recommended_section="opening",
        duration_seconds=4.0,
        highlight_score=0.8,
    )
    gaps = detect_gaps(_adapt_from_manifest(manifest), manifest, materials=[mat])
    opening_gaps = [g for g in gaps if g.section == "opening"]
    assert opening_gaps
    assert opening_gaps[0].status == "ok"
    assert opening_gaps[0].matched_material_id == "mat-opening-1"
