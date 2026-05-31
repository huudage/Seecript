"""多样例（1-2 个）拆解整合改编测试。

覆盖：
1. `adapt_structure([manifest_A, manifest_B], ...)` 的 user payload 同时含 `(样例A)` 和 `(样例B)` 字面 + `原样例共 {N_A+N_B} 段`
2. 单样例时回退到旧行为：不打 (样例X) tag
3. PlanBuildRequest pydantic 校验：sample_ids=[] / 3 项 / 超长 → ValidationError
4. 集成：POST /api/plan/build 带 sample_ids=[s1, s2] → 返回 plan.sample_ids 与请求一致
5. 跨样例 source_shot_indices：若 LLM 输出的 src_idx 跨两份 manifest，对应 section.source_shot_indices 应为空
"""
from __future__ import annotations

from io import BytesIO

import pytest
from pydantic import ValidationError

from app.schemas import (
    PackagingProfile,
    PlanBuildRequest,
    RhythmCurve,
    SampleManifest,
    Section,
    Shot,
)
from app.services.agent.plan_agent import _materialize, adapt_structure


def _mk_manifest(sample_id: str, n_sections: int = 4) -> SampleManifest:
    """构造 n 段 marketing 风格的 mock manifest。"""
    assert n_sections >= 3
    roles: list[str] = ["opening"]
    for i in range(1, n_sections - 1):
        roles.append("climax" if i == n_sections // 2 else "development")
    roles.append("closing")
    sections = [
        Section(
            role=role,  # type: ignore[arg-type]
            theme=f"{role}-{i}",
            start=float(i * 5),
            end=float((i + 1) * 5),
            summary=f"{sample_id} 第 {i} 段",
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
            thumbnail_url=f"/thumb/{sample_id}-{i}.jpg",
            transcript=None,
            tags=[],
        )
        for i in range(n_sections * 2)
    ]
    return SampleManifest(
        sample_id=sample_id,
        title=f"{sample_id} title",
        video_type="marketing",
        duration_seconds=float(n_sections * 5),
        video_url=f"/samples/{sample_id}/video.mp4",
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
        understanding=None,
        utterances=[],
    )


# -------------- adapt_structure prompt 拼装 --------------

@pytest.mark.asyncio
async def test_multi_sample_prompt_contains_both_tags_and_combined_count(monkeypatch):
    """两份 manifest 进 adapt_structure：user payload 必须同时含 (样例A) 和 (样例B)，且 `原样例共 N 段` N = 合并段数。"""
    captured: dict[str, str] = {}

    async def fake_complete(self, system, user, *, temperature=None, max_tokens=None):
        captured["user"] = user
        captured["system"] = system
        # 返回一个合法 adapted_sections，让 adapt_structure 不走 fallback
        return '{"adapted_sections": [' \
            '{"role":"opening","theme":"开场","content_description":"开场吸睛","source_section_indices":[0],"duration_seconds":4.0},' \
            '{"role":"development","theme":"发展","content_description":"中段铺垫","source_section_indices":[2],"duration_seconds":18.0},' \
            '{"role":"closing","theme":"收尾","content_description":"结尾 CTA","source_section_indices":[6],"duration_seconds":4.0}' \
            ']}'

    from app.services import llm_client as _llm_client_mod
    monkeypatch.setattr(_llm_client_mod.MockLLMClient, "complete", fake_complete)

    m_a = _mk_manifest("sample-AAA", n_sections=4)
    m_b = _mk_manifest("sample-BBB", n_sections=3)
    n_total = len(m_a.sections) + len(m_b.sections)  # 4 + 3 = 7

    adapted = await adapt_structure(
        [m_a, m_b],
        brief="跨样例改编",
        video_goal="测试整合 prompt",
    )
    assert adapted, "应有返回结构"

    user = captured["user"]
    assert "(样例A)" in user, f"user prompt 缺少 (样例A) tag:\n{user}"
    assert "(样例B)" in user, f"user prompt 缺少 (样例B) tag:\n{user}"
    assert f"原样例共 {n_total} 段" in user, f"段数标签错误:\n{user}"


@pytest.mark.asyncio
async def test_single_sample_prompt_omits_tags(monkeypatch):
    """单样例（仍是 list 但只有 1 份）行为应与旧版一致：不打 (样例X) tag。"""
    captured: dict[str, str] = {}

    async def fake_complete(self, system, user, *, temperature=None, max_tokens=None):
        captured["user"] = user
        return '{"adapted_sections": [' \
            '{"role":"opening","theme":"开场","content_description":"开场吸睛","source_section_indices":[0],"duration_seconds":4.0},' \
            '{"role":"development","theme":"发展","content_description":"中段","source_section_indices":[1],"duration_seconds":18.0},' \
            '{"role":"closing","theme":"收尾","content_description":"结尾","source_section_indices":[2],"duration_seconds":4.0}' \
            ']}'

    from app.services import llm_client as _llm_client_mod
    monkeypatch.setattr(_llm_client_mod.MockLLMClient, "complete", fake_complete)

    m_a = _mk_manifest("sample-ONLY", n_sections=4)
    await adapt_structure([m_a], brief="单样例", video_goal="对照组")
    user = captured["user"]
    assert "(样例A)" not in user
    assert "(样例B)" not in user
    assert "原样例共 4 段" in user


# -------------- _materialize 跨样例 shot 处理 --------------

def test_materialize_cross_sample_section_emits_empty_shots():
    """若一段的 source_section_indices 同时取自样例 A 和 B → source_shot_indices 应为 []."""
    m_a = _mk_manifest("sample-A", n_sections=3)  # 3 段：global_idx 0/1/2
    m_b = _mk_manifest("sample-B", n_sections=3)  # 3 段：global_idx 3/4/5

    combined_sections: list[tuple[int, Section, int]] = []
    for mi, m in enumerate([m_a, m_b]):
        for sec in m.sections:
            combined_sections.append((len(combined_sections), sec, mi))

    # 1) 纯样例 A 的段（src_idx=[0]）→ shot 应非空，对应 m_a.sections[0].shot_indices
    items_pure_a = [
        {"role": "opening", "theme": "t", "content_description": "x",
         "source_section_indices": [0], "duration_seconds": 4.0},
        {"role": "development", "theme": "t", "content_description": "x",
         "source_section_indices": [1], "duration_seconds": 22.0},
        {"role": "closing", "theme": "t", "content_description": "x",
         "source_section_indices": [2], "duration_seconds": 4.0},
    ]
    adapted_pure = _materialize(items_pure_a, combined_sections)
    assert adapted_pure[0].source_shot_indices, "纯单一样例的段应保留 shot_indices"

    # 2) 跨样例段（src_idx=[0,3]，A 的 0 + B 的 0）→ shot_indices 应为空
    items_mixed = [
        {"role": "opening", "theme": "t", "content_description": "x",
         "source_section_indices": [0], "duration_seconds": 4.0},
        {"role": "development", "theme": "t", "content_description": "x",
         "source_section_indices": [0, 3], "duration_seconds": 22.0},
        {"role": "closing", "theme": "t", "content_description": "x",
         "source_section_indices": [5], "duration_seconds": 4.0},
    ]
    adapted_mixed = _materialize(items_mixed, combined_sections)
    # 中间段跨样例 → shot_indices 应为空
    assert adapted_mixed[1].source_shot_indices == [], (
        f"跨样例 src_idx=[0,3] 的段 shot_indices 应为空，"
        f"实际 {adapted_mixed[1].source_shot_indices}"
    )


# -------------- PlanBuildRequest pydantic 校验 --------------

def test_plan_build_request_rejects_empty_sample_ids():
    with pytest.raises(ValidationError):
        PlanBuildRequest(
            sample_ids=[],
            project_id="p",
            session_id="s",
            selected_materials=[],
            fills=[],
            variant="A",
        )


def test_plan_build_request_rejects_three_sample_ids():
    with pytest.raises(ValidationError):
        PlanBuildRequest(
            sample_ids=["s1", "s2", "s3"],
            project_id="p",
            session_id="s",
            selected_materials=[],
            fills=[],
            variant="A",
        )


def test_plan_build_request_accepts_one_or_two_sample_ids():
    """长度 1 和 2 都合法。"""
    r1 = PlanBuildRequest(
        sample_ids=["s1"],
        project_id="p", session_id="s",
        selected_materials=[], fills=[], variant="A",
    )
    assert r1.sample_ids == ["s1"]
    r2 = PlanBuildRequest(
        sample_ids=["s1", "s2"],
        project_id="p", session_id="s",
        selected_materials=[], fills=[], variant="A",
    )
    assert r2.sample_ids == ["s1", "s2"]


# -------------- HTTP 集成：plan/build 接受 sample_ids --------------

def test_http_plan_build_with_two_samples_returns_combined_plan(client):
    """POST /api/plan/build 带两个真实样例 → 返回的 plan.sample_ids 与请求一致。"""
    fake_video = b"\x00\x00\x00\x18ftypisom" + b"\x00" * 1024
    project_id = "proj-multi-sample-test"
    r = client.post(
        "/api/material/upload",
        files=[("files", ("a.mp4", BytesIO(fake_video), "video/mp4"))],
        data={"project_id": project_id},
    )
    assert r.status_code == 200, r.text
    sid = r.json()["session_id"]

    r = client.post("/api/plan/build", json={
        "sample_ids": ["sample-marketing-01", "sample-vlog-01"],
        "project_id": project_id,
        "session_id": sid,
        "brief": "跨样例集成测试",
        "video_goal": "30 秒讲清",
        "selected_materials": [],
        "fills": [],
        "variant": "A",
    })
    assert r.status_code == 200, r.text
    plan = r.json()
    assert plan["sample_ids"] == ["sample-marketing-01", "sample-vlog-01"]
    # 3-7 段约束仍要满足
    assert 3 <= len(plan["adapted_sections"]) <= 7
