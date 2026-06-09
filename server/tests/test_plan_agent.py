"""plan_agent 直测：结构改编硬约束 + mock 路由指纹。

测试场景：
1. mock LLM 走 `adapted_sections` 指纹 → adapt_structure 必须返回符合硬约束的段落
   （3-7 段、首 opening、末 closing、≤1 climax、中间皆 development、每段 content_description 非空）
2. section_id 稳定为 sec-0..N；纯新增段（source_section_indices=[]）借相邻段的 shot
3. 空 manifest 走 fallback；fallback 不会爆炸，返回 1:1 拷贝
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
)
from app.services.agent.plan_agent import adapt_structure


_ALLOWED_ROLES = {"opening", "development", "climax", "closing"}


def _mini_manifest(n_sections: int = 4) -> SampleManifest:
    """构造 n 段 manifest（首 opening、末 closing、中间 development 或 climax）。"""
    assert n_sections >= 3
    roles: list[str] = ["opening"]
    for i in range(1, n_sections - 1):
        roles.append("climax" if i == n_sections // 2 else "development")
    roles.append("closing")

    sections = [
        Section(
            role=role,  # type: ignore[arg-type]
            theme=f"样例-{role}-{i}",
            start=float(i * 5),
            end=float((i + 1) * 5),
            summary=f"样例第 {i} 段总结",
            shot_indices=[i * 2, i * 2 + 1],
        )
        for i, role in enumerate(roles)
    ]
    shots = [
        Shot(
            index=i,
            start=float(i * 2.5),
            end=float((i + 1) * 2.5),
            duration=2.5,
            thumbnail_url=f"/thumb/{i}.jpg",
            transcript=None,
            tags=[],
        )
        for i in range(n_sections * 2)
    ]
    return SampleManifest(
        sample_id="s-plan-agent-test",
        title="plan-agent 单测样例",
        video_type="marketing",
        duration_seconds=float(n_sections * 5),
        video_url="/samples/s-plan-agent-test/video.mp4",
        has_voice=True,
        shots=shots,
        rhythm=RhythmCurve(
            times=[0.0, 5.0], cut_density=[1.0, 0.6],
            bgm_energy=[0.1, 0.4], tempo_bpm=120.0,
        ),
        sections=sections,
        packaging=PackagingProfile(
            subtitle_style="大字加描边", has_title_bar=True,
            transition_types=["cut"], cover_style=None, sticker_density=0.2,
        ),
        understanding=VideoUnderstanding(
            archetype="测试用艺术展",
            narrative_summary="一段用于单测的占位画像。",
            suggested_segments=n_sections,
            tone="冷静克制",
        ),
        utterances=[],
    )


@pytest.mark.asyncio
async def test_adapt_structure_satisfies_hard_constraints():
    """mock 路由命中 `adapted_sections` 指纹后，返回的结构必须满足全部硬约束。"""
    manifest = _mini_manifest(4)
    adapted = await adapt_structure(
        [manifest],
        brief="新视频要讲的是城市夜跑装备的轻量化升级",
        video_goal="30 秒内说清产品差异化卖点，面向初次接触的用户",
    )
    assert 3 <= len(adapted) <= 7, f"段数越界：{len(adapted)}"

    roles = [s.role for s in adapted]
    for r in roles:
        assert r in _ALLOWED_ROLES, f"非法 role={r}"

    assert roles[0] == "opening", f"首段必须 opening，实际 {roles[0]}"
    assert roles[-1] == "closing", f"末段必须 closing，实际 {roles[-1]}"
    assert sum(1 for r in roles if r == "climax") <= 1, "climax 至多 1 段"
    # 中间段不允许 opening/closing
    for r in roles[1:-1]:
        assert r not in ("opening", "closing"), f"中间段不应出现 {r}"


@pytest.mark.asyncio
async def test_adapt_structure_emits_content_description_and_stable_ids():
    """每段 content_description 非空；section_id 严格 sec-0..N；order 与下标对齐。"""
    manifest = _mini_manifest(4)
    adapted = await adapt_structure(
        [manifest], brief="主题测试", video_goal="目的测试",
    )
    for i, sec in enumerate(adapted):
        assert sec.section_id == f"sec-{i}", f"section_id 不稳定：{sec.section_id}"
        assert sec.order == i, f"order 应等于下标，实际 {sec.order}"
        assert sec.content_description.strip(), f"sec-{i} content_description 为空"
        assert sec.theme.strip(), f"sec-{i} theme 为空"


@pytest.mark.asyncio
async def test_adapt_structure_borrows_shots_for_pure_new_sections():
    """source_shot_indices 一定非空——纯新增段也会借上一段的 shot 当占位缩略图。"""
    manifest = _mini_manifest(4)
    adapted = await adapt_structure(
        [manifest], brief="b", video_goal="g",
    )
    for sec in adapted:
        assert sec.source_shot_indices, (
            f"sec-{sec.order} source_shot_indices 为空，缩略图反查会断"
        )


@pytest.mark.asyncio
async def test_adapt_structure_fallback_when_manifest_empty():
    """manifest.sections 为空时不能爆炸——直接 fallback 返回空列表。"""
    manifest = _mini_manifest(3)
    # 强行清空 sections 模拟极端场景
    manifest = manifest.model_copy(update={"sections": []})
    manifest_list = [manifest]
    adapted = await adapt_structure(manifest_list, brief="b", video_goal="g")
    assert adapted == [], "空 sections 应走 fallback 返回空"


# ---------------- 时长归一化 + ComposeSettings 注入 ----------------


@pytest.mark.asyncio
@pytest.mark.parametrize("target_total", [15.0, 30.0, 60.0, 90.0])
async def test_adapt_structure_durations_track_target_total(target_total):
    """每段 duration_seconds 必须落在 schema 允许的 [2, 30] 区间；
    总和必须贴近 settings.target_duration_seconds（±25% 兜底，含 mock + clamp 噪声）。"""
    from app.schemas import ComposeSettings
    manifest = _mini_manifest(4)
    adapted = await adapt_structure(
        [manifest],
        brief="测试主题",
        video_goal="测试目的",
        settings=ComposeSettings(target_duration_seconds=target_total),
    )
    assert adapted, "adapt_structure 应该返回至少一段"
    durations = [sec.duration_seconds for sec in adapted]
    for d in durations:
        assert 2.0 <= d <= 30.0, f"段时长越界：{d}"
    total = sum(durations)
    # 允许 25% 偏差：clamp + 残差均摊后仍可能有少量误差，但不应跑飞
    assert abs(total - target_total) / target_total <= 0.25, (
        f"总时长偏离过大：want≈{target_total} got={total:.1f}"
    )


@pytest.mark.asyncio
async def test_adapt_structure_respects_settings_defaults():
    """不传 settings 时按 ComposeSettings 默认值（target_total=30s）跑通。"""
    manifest = _mini_manifest(4)
    adapted = await adapt_structure([manifest], brief="b", video_goal="g")
    assert adapted, "默认 settings 也应该返回结构"
    total = sum(s.duration_seconds for s in adapted)
    # 默认 30s，允许 25% 偏差
    assert 22.0 <= total <= 38.0, f"默认 30s 偏差过大：{total:.1f}"


def test_fallback_adaptation_scales_to_target_total():
    """LLM 失败兜底也要按 target_total 缩放每段时长，而不是死写 4/6/7/4。"""
    from app.services.agent.plan_agent import _fallback_adaptation
    manifest = _mini_manifest(4)
    adapted = _fallback_adaptation(manifest.sections, target_total=60.0)
    assert len(adapted) == 4
    total = sum(s.duration_seconds for s in adapted)
    # role 默认权重 4+6+7+4=21 → 缩放后接近 60；clamp 后允许 ±30%
    assert 42.0 <= total <= 78.0, f"fallback 缩放后偏离过大：{total:.1f}"
    for sec in adapted:
        assert 2.0 <= sec.duration_seconds <= 30.0


# ---------------- stage-34：subject_anchors 提取 + 注入 ----------------


def test_extract_subject_anchors_parses_clarify_suffix():
    """从 ClarifyPanel 写入的「（涉及 X、Y、Z）」后缀里反解 anchors。"""
    from app.services.agent.plan_agent import extract_subject_anchors

    brief = "新品纸巾种草（涉及纸巾、抹布、清洁喷雾）"
    anchors = extract_subject_anchors(brief)
    assert anchors == ["纸巾", "抹布", "清洁喷雾"], f"实际：{anchors}"


def test_extract_subject_anchors_parses_head_format():
    """content 空时 ClarifyPanel 用「核心可拍物体：X、Y」格式，也要解出。"""
    from app.services.agent.plan_agent import extract_subject_anchors

    brief = "国家文物展\n核心可拍物体：青铜鼎、玉器、陶俑"
    anchors = extract_subject_anchors(brief)
    assert "青铜鼎" in anchors and "玉器" in anchors and "陶俑" in anchors


def test_extract_subject_anchors_dedupes_and_caps():
    """多次「（涉及）」累积、去重、上限 8。"""
    from app.services.agent.plan_agent import extract_subject_anchors

    brief = "主题 A（涉及纸巾、抹布）；主题 B（涉及纸巾、拖把、清洁喷雾、洗碗布、玻璃水、马桶刷、抽纸、湿巾、洗洁精）"
    anchors = extract_subject_anchors(brief)
    # 去重 + 上限 8
    assert len(anchors) <= 8
    assert anchors[0] == "纸巾", f"应保序去重：{anchors}"
    assert len(set(anchors)) == len(anchors), f"未去重：{anchors}"


def test_extract_subject_anchors_returns_empty_when_no_marker():
    from app.services.agent.plan_agent import extract_subject_anchors

    assert extract_subject_anchors(None) == []
    assert extract_subject_anchors("") == []
    assert extract_subject_anchors("普通文案没有标记") == []


@pytest.mark.asyncio
async def test_adapt_structure_injects_missing_subject_anchors():
    """LLM 漏掉了某个 anchor 时，_enforce_subject_anchors 必须机械补回去。"""
    from app.services.agent.plan_agent import adapt_structure

    manifest = _mini_manifest(4)
    brief = "新品纸巾种草，主推沙发耐用日常清洁（涉及纸巾、抹布、清洁喷雾）"
    adapted = await adapt_structure(
        [manifest],
        brief=brief,
        video_goal="30 秒讲清家清好物三件套",
    )
    # 把整支视频的 subject + visual + targets.name + content_description 拼起来
    blob_parts: list[str] = []
    for sec in adapted:
        blob_parts.append(sec.content_description or "")
        for sh in (sec.shots or []):
            blob_parts.append(sh.subject or "")
            blob_parts.append(sh.visual or "")
            for t in (sh.targets or []):
                blob_parts.append(t.name or "")
    blob = "\n".join(blob_parts)
    for anchor in ("纸巾", "抹布", "清洁喷雾"):
        assert anchor in blob, f"anchor『{anchor}』未被注入：\n{blob[:400]}"


def test_enforce_subject_anchors_directly_with_synthetic_sections():
    """单测 _enforce_subject_anchors：构造一份没有 anchor 的 sections，验证注入后命中。"""
    from app.schemas import AdaptedSection, ShotPlan
    from app.services.agent.plan_agent import _enforce_subject_anchors

    sections = [
        AdaptedSection(
            section_id="sec-0", role="opening", theme="开场",
            content_description="主播口播开场",
            shots=[ShotPlan(order=0, subject="主播", visual="主播正脸特写", narration="嗨", duration_seconds=3.0)],
            order=0, duration_seconds=3.0,
        ),
        AdaptedSection(
            section_id="sec-1", role="development", theme="展示",
            content_description="产品展示",
            shots=[ShotPlan(order=0, subject="桌面", visual="桌面摆放", narration="", duration_seconds=4.0)],
            order=1, duration_seconds=4.0,
        ),
        AdaptedSection(
            section_id="sec-2", role="closing", theme="收尾",
            content_description="行动引导",
            shots=[ShotPlan(order=0, subject="主播", visual="主播总结", narration="拜拜", duration_seconds=3.0)],
            order=2, duration_seconds=3.0,
        ),
    ]
    out = _enforce_subject_anchors(sections, ["纸巾", "抹布"])
    blob = "\n".join(
        (sec.content_description or "") + "\n" +
        "\n".join((sh.subject or "") + "|" + (sh.visual or "") + "|" + "、".join(t.name for t in (sh.targets or []))
                  for sh in (sec.shots or []))
        for sec in out
    )
    assert "纸巾" in blob, blob
    assert "抹布" in blob, blob
    # 首段（opening）和末段（closing）不应被改——只动中间主体段
    assert out[0].shots[0].subject == "主播"
    assert out[-1].shots[0].subject == "主播"

