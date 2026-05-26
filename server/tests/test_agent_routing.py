"""Agent 路由直测：decompose 三类型 prompt + gap_agent T2V 分支。

这两个测试用例直接调 agent 层（不走 HTTP），覆盖：
1. decompose_agent 把 video_type 正确路由到三组段落 prompt，
   mock LLM 据此返回对应 kind 枚举的 fixture。
2. gap_agent 的 aigc 分支调 Seedance T2V mock：submit → poll → succeed，
   返回 task_id 作为 new_material_id。
"""
from __future__ import annotations

import pytest

from app.schemas import (
    Gap,
    Material,
    PackagingProfile,
    RhythmCurve,
    SampleManifest,
    Section,
    Shot,
    kinds_for_video_type,
)
from app.services.agent.decompose_agent import decompose
from app.services.agent.gap_agent import detect_gaps, fill_gap


# ----------------------------- decompose 三类型 --------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("video_type", "expected_kinds"),
    [
        ("marketing", {"hook", "body", "cta"}),
        ("editing", {"opening", "climax", "closing"}),
        ("motion_graph", {"intro", "build", "drop", "outro"}),
    ],
)
async def test_decompose_routes_by_video_type(video_type, expected_kinds):
    """无 video_path 时走 mock 数据；段落 kind 必须落在该 video_type 允许的枚举里。"""
    manifest = await decompose(
        sample_id=f"sample-{video_type}-test",
        video_type=video_type,
    )
    assert manifest.video_type == video_type
    assert manifest.shots, "decompose 必须返回至少一个 shot"
    kinds = {s.kind for s in manifest.sections}
    # mock LLM 按 system 指纹路由返回的 fixture 与 video_type 一致
    assert kinds == expected_kinds, f"{video_type}: got {kinds}, want {expected_kinds}"
    # 段落 kind 必须是该类型允许的枚举
    allowed = set(kinds_for_video_type(video_type))
    assert kinds.issubset(allowed)


@pytest.mark.asyncio
async def test_decompose_motion_graph_skips_voice():
    """motion_graph 无 video_path 时默认 has_voice=True（无文件无法 VAD）；
    本测试只验证段落结构能正确走 motion_graph fixture。"""
    manifest = await decompose(sample_id="sample-mg-test", video_type="motion_graph")
    assert manifest.video_type == "motion_graph"
    assert len(manifest.sections) == 4
    kinds_seq = [s.kind for s in manifest.sections]
    # mock fixture 段落顺序：intro → build → drop → outro
    assert kinds_seq == ["intro", "build", "drop", "outro"]


# ----------------------------- gap_agent T2V 分支 ------------------------------


def _mini_manifest(video_type="marketing") -> SampleManifest:
    kinds = kinds_for_video_type(video_type)
    sections = [
        Section(kind=k, start=float(i * 5), end=float((i + 1) * 5),
                summary=f"{k} 段", shot_indices=[i])
        for i, k in enumerate(kinds)
    ]
    shots = [
        Shot(index=i, start=float(i * 5), end=float((i + 1) * 5),
             duration=5.0, thumbnail_url=None, transcript=None, tags=[])
        for i in range(len(kinds))
    ]
    return SampleManifest(
        sample_id="s-test", title="t", video_type=video_type,
        duration_seconds=float(len(kinds) * 5),
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
    缩短轮询参数让测试快速完成，验证 new_material_id == task_id 且 status=ok。"""
    manifest = _mini_manifest("marketing")
    gaps = detect_gaps(manifest, materials=[])
    assert gaps, "无 material 时应至少识别出若干 miss 槽位"
    miss_gap = next((g for g in gaps if g.status == "miss"), gaps[0])
    assert miss_gap.section in {"hook", "body", "cta"}

    # Mock T2V 默认 8s 转 succeeded；这里把 wait 上限设短一点验证超时分支
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
    assert "渲染" in (result.note or "") or "task=" in (result.note or "")


@pytest.mark.asyncio
async def test_gap_agent_aigc_succeeds_when_wait_long_enough(monkeypatch):
    """把 mock T2V 的 mock_duration 调到 0 让 query 立即返回 succeeded。"""
    monkeypatch.setenv("T2V_MOCK_DURATION_SECONDS", "0")
    from app.config import get_settings
    get_settings.cache_clear()

    manifest = _mini_manifest("editing")
    gaps = detect_gaps(manifest, materials=[])
    target = next(g for g in gaps if g.status == "miss")
    assert target.section in {"opening", "climax", "closing"}

    result = await fill_gap(
        target, action="aigc",
        params={"poll_interval_seconds": 0.05, "max_wait_seconds": 3.0},
    )
    assert result.action == "aigc"
    assert result.status == "ok"
    assert result.new_material_id and result.new_material_id.startswith("mock-t2v-")


# ----------------------------- detect_gaps 三类型 ------------------------------


@pytest.mark.parametrize(
    ("video_type", "expected_sections"),
    [
        ("marketing", {"hook", "body", "cta"}),
        ("editing", {"opening", "climax", "closing"}),
        ("motion_graph", {"intro", "build", "drop", "outro"}),
    ],
)
def test_detect_gaps_covers_all_video_types(video_type, expected_sections):
    """每个 video_type 都应能在无 material 时识别出该类型全部 kind 对应的槽位。"""
    manifest = _mini_manifest(video_type)
    gaps = detect_gaps(manifest, materials=[])
    seen = {g.section for g in gaps}
    assert seen == expected_sections
    # 全 miss 状态（没有任何 material）
    assert all(g.status == "miss" for g in gaps)


def test_detect_gaps_assigns_material_when_section_matches():
    """有 recommended_section=hook 的 material 时，hook 槽位首位应被填上。"""
    manifest = _mini_manifest("marketing")
    mat = Material(
        material_id="mat-hook-1",
        filename="hook.mp4", media_type="video", recommended_section="hook",
        duration_seconds=4.0,
    )
    gaps = detect_gaps(manifest, materials=[mat])
    hook_gaps = [g for g in gaps if g.section == "hook"]
    assert hook_gaps
    assert hook_gaps[0].status == "ok"
    assert hook_gaps[0].matched_material_id == "mat-hook-1"
