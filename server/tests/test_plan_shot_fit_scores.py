"""stage-77 (2026-06-12) GET /plan/{plan_id}/scene/{scene_id}/material/{material_id}/shot-scores
回归：换源弹窗里给每个 MaterialShot 打适配度分。

用户原话：「在内容轨生成之后，基于不同分镜的内容要求，对每个真实素材切片
对每一个分镜的适配程度进行打分，在分镜编辑的切片选择界面展示分数」

覆盖：
1. 正常视频素材：返回每个 shot 一行，score_pct 单调随 _score_pair 走，quality 三档分级
2. 非本项目素材：404（防跨项目泄漏）
3. plan 不存在 / scene 不存在 → 404
4. 没绑 project 的老 plan → 400
5. 素材没切镜（video 但 shots=[]）→ scores=[]（不报错，前端不显示徽章）
6. 图片素材 → 合成虚拟 shot 给 1 行分
"""
from __future__ import annotations

import time

import pytest

from app.schemas import (
    AdaptedSection,
    ComposeSettings,
    Material,
    MaterialShot,
    Plan,
    Scene,
    ShotPlan,
)
from app.services.materials.store import material_store
from app.services.plans.store import plan_store


_TEST_PLAN_IDS: list[str] = []
_TEST_PROJECTS: list[str] = []


def _make_plan_with_scene_shot(*, plan_id: str, project_id: str | None) -> Plan:
    """构造最小可评分 plan：1 个 section + 1 个 ShotPlan + 1 个 Scene。"""
    return Plan(
        plan_id=plan_id,
        sample_ids=["sample-marketing-01"],
        project_id=project_id,
        session_id=None,
        settings=ComposeSettings(),
        adapted_sections=[
            AdaptedSection(
                section_id="sec-0",
                role="hook",
                theme="开场抓人",
                content_description="主播口播 + 产品快速露出",
                source_shot_indices=[0],
                order=0,
                duration_seconds=4.0,
                shots=[
                    ShotPlan(
                        order=0,
                        subject="奶茶杯特写",
                        visual="奶茶杯近景特写，逆光高光",
                        narration="第一口就上头",
                        duration_seconds=4.0,
                    ),
                ],
            ),
        ],
        main_track=[
            Scene(
                scene_id="sc-0-sh-0",
                section="hook",
                parent_section_id="sec-0",
                shot_order=0,
                shot_subject="奶茶杯特写",
                source="text_card",  # 占位（stage-76 占位）
                source_ref="placeholder-sec-0-sh-0",
                start=0.0,
                duration=4.0,
                narration="第一口就上头",
            ),
        ],
        packaging_track=[],
        duration_seconds=4.0,
        variant="A",
    )


def _make_video_material(
    *,
    project_id: str,
    material_id: str,
    shots: list[tuple[str, float, str]],  # (caption, action_density, recommended_role)
) -> Material:
    mat_shots: list[MaterialShot] = []
    cursor = 0.0
    for i, (caption, density, role) in enumerate(shots):
        dur = 3.5
        mat_shots.append(MaterialShot(
            index=i,
            start=cursor,
            end=cursor + dur,
            duration=dur,
            caption=caption,
            action_density=density,
            recommended_role=role or None,  # type: ignore[arg-type]
        ))
        cursor += dur
    return Material(
        material_id=material_id,
        filename=f"{material_id}.mp4",
        media_type="video",
        duration_seconds=cursor,
        tags=["奶茶", "近景"],
        recommended_section="hook",
        preprocess_status="ready",
        shots=mat_shots,
        file_url=f"/uploads/{project_id}/{material_id}.mp4",
    )


@pytest.fixture(autouse=True)
def _cleanup():
    yield
    for pid in _TEST_PLAN_IDS:
        plan_store._plans.pop(pid, None)
    _TEST_PLAN_IDS.clear()
    for prj in _TEST_PROJECTS:
        material_store._by_session.pop(prj, None)
    _TEST_PROJECTS.clear()


def test_shot_scores_returns_per_shot_scores(client):
    """video material 多 shot：每个 shot 都有分，奶茶相关高分排前，无关 shot 低分。"""
    proj = f"proj-shot-fit-{int(time.time() * 1000)}"
    _TEST_PROJECTS.append(proj)
    plan = _make_plan_with_scene_shot(
        plan_id=f"plan-fit-1-{int(time.time() * 1000)}", project_id=proj,
    )
    _TEST_PLAN_IDS.append(plan.plan_id)
    plan_store.put(plan)

    mat = _make_video_material(
        project_id=proj,
        material_id="m-fit-1",
        shots=[
            ("奶茶杯近景特写 逆光高光", 0.85, "hook"),     # 强相关 + role 命中 → 高分
            ("店员擦桌子 中景", 0.3, "development"),       # 弱相关
            ("门口大牌特写", 0.5, ""),                     # 中性
        ],
    )
    material_store.put(proj, [mat])

    resp = client.get(
        f"/api/plan/{plan.plan_id}/scene/sc-0-sh-0/material/m-fit-1/shot-scores",
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["plan_id"] == plan.plan_id
    assert body["scene_id"] == "sc-0-sh-0"
    assert body["material_id"] == "m-fit-1"
    assert body["section_role"] == "hook"
    assert body["scene_shot_subject"] == "奶茶杯特写"
    assert body["scene_duration"] == pytest.approx(4.0)

    scores = body["scores"]
    assert len(scores) == 3
    # shot 0 应该是最高分（奶茶+特写+hook role 三命中）
    by_idx = {s["shot_index"]: s for s in scores}
    assert by_idx[0]["score_pct"] > by_idx[1]["score_pct"]
    assert by_idx[0]["score_pct"] > by_idx[2]["score_pct"]
    # quality 字段必在三档之一
    for s in scores:
        assert s["quality"] in {"good", "weak", "missing"}
        assert 0 <= s["score_pct"] <= 100
        assert 0.0 <= s["score"] <= 1.0


def test_shot_scores_cross_project_material_rejected(client):
    """material 在别的项目 → 404，禁跨项目泄漏。"""
    proj_a = f"proj-fit-a-{int(time.time() * 1000)}"
    proj_b = f"proj-fit-b-{int(time.time() * 1000)}"
    _TEST_PROJECTS.extend([proj_a, proj_b])
    plan = _make_plan_with_scene_shot(
        plan_id=f"plan-fit-cross-{int(time.time() * 1000)}", project_id=proj_a,
    )
    _TEST_PLAN_IDS.append(plan.plan_id)
    plan_store.put(plan)
    material_store.put(proj_b, [_make_video_material(
        project_id=proj_b, material_id="m-other",
        shots=[("foo", 0.5, "hook")],
    )])
    resp = client.get(
        f"/api/plan/{plan.plan_id}/scene/sc-0-sh-0/material/m-other/shot-scores",
    )
    assert resp.status_code == 404


def test_shot_scores_unknown_plan_returns_404(client):
    resp = client.get(
        "/api/plan/no-such-plan/scene/sc-0-sh-0/material/m-any/shot-scores",
    )
    assert resp.status_code == 404


def test_shot_scores_unknown_scene_returns_404(client):
    proj = f"proj-fit-unk-{int(time.time() * 1000)}"
    _TEST_PROJECTS.append(proj)
    plan = _make_plan_with_scene_shot(
        plan_id=f"plan-fit-unkscene-{int(time.time() * 1000)}", project_id=proj,
    )
    _TEST_PLAN_IDS.append(plan.plan_id)
    plan_store.put(plan)
    resp = client.get(
        f"/api/plan/{plan.plan_id}/scene/no-such-scene/material/whatever/shot-scores",
    )
    assert resp.status_code == 404


def test_shot_scores_plan_without_project_returns_400(client):
    plan = _make_plan_with_scene_shot(
        plan_id=f"plan-fit-noproj-{int(time.time() * 1000)}", project_id=None,
    )
    _TEST_PLAN_IDS.append(plan.plan_id)
    plan_store.put(plan)
    resp = client.get(
        f"/api/plan/{plan.plan_id}/scene/sc-0-sh-0/material/m-any/shot-scores",
    )
    assert resp.status_code == 400


def test_shot_scores_video_without_shots_returns_empty_scores(client):
    """video 但没切镜（preprocess 未跑/失败）→ scores=[]；前端据此不显示徽章。"""
    proj = f"proj-fit-noshots-{int(time.time() * 1000)}"
    _TEST_PROJECTS.append(proj)
    plan = _make_plan_with_scene_shot(
        plan_id=f"plan-fit-noshots-{int(time.time() * 1000)}", project_id=proj,
    )
    _TEST_PLAN_IDS.append(plan.plan_id)
    plan_store.put(plan)
    bare = Material(
        material_id="m-bare",
        filename="bare.mp4",
        media_type="video",
        duration_seconds=10.0,
        tags=[],
        preprocess_status="skipped",
        shots=[],
        file_url=f"/uploads/{proj}/m-bare.mp4",
    )
    material_store.put(proj, [bare])
    resp = client.get(
        f"/api/plan/{plan.plan_id}/scene/sc-0-sh-0/material/m-bare/shot-scores",
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["scores"] == []


def test_shot_scores_image_material_returns_single_virtual_shot(client):
    """图片素材：合成虚拟 shot 给 1 行分（与 shot_matcher 同口径）。"""
    proj = f"proj-fit-img-{int(time.time() * 1000)}"
    _TEST_PROJECTS.append(proj)
    plan = _make_plan_with_scene_shot(
        plan_id=f"plan-fit-img-{int(time.time() * 1000)}", project_id=proj,
    )
    _TEST_PLAN_IDS.append(plan.plan_id)
    plan_store.put(plan)
    img = Material(
        material_id="m-img",
        filename="cover.jpg",
        media_type="image",
        duration_seconds=None,
        tags=["奶茶", "封面"],
        subjects=["奶茶杯"],
        highlight_reason="奶茶杯特写 高饱和",
        recommended_section="hook",
        preprocess_status="skipped",
        shots=[],
        file_url=f"/uploads/{proj}/m-img.jpg",
    )
    material_store.put(proj, [img])
    resp = client.get(
        f"/api/plan/{plan.plan_id}/scene/sc-0-sh-0/material/m-img/shot-scores",
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["scores"]) == 1
    assert body["scores"][0]["shot_index"] == 0
    # 奶茶相关 + role=hook 命中 → 应该 >= weak
    assert body["scores"][0]["quality"] in {"weak", "good"}
