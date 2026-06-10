"""compute_material_fit / annotate_plan_fit_scores 单元测试（stage-59）。

确保：
1. 段位推荐命中权重生效（recommended_section == section.role 加 0.4）
2. 标签命中权重（材料 tag 与 section.theme/content_description 关键词重合）
3. 时长贴合：素材时长贴近 scene.duration 时拿满分
4. 高光评分：material.highlight_score 折算
5. annotate_plan_fit_scores 只动 user_material scene，其它 source 清空 fit_score
6. material 找不到时清 fit_score（避免历史脏数据）
"""
from __future__ import annotations

from app.schemas import AdaptedSection, Material, Scene
from app.services.materials.fit import annotate_plan_fit_scores, compute_material_fit


def _section(role: str = "development", theme: str = "新品奶茶介绍",
             content: str = "展示新品奶茶的色泽和包装", section_id: str = "sec-0") -> AdaptedSection:
    return AdaptedSection(
        section_id=section_id, role=role, theme=theme,
        content_description=content, source_shot_indices=[0],
        order=0, duration_seconds=5.0,
    )


def _material(
    *,
    material_id: str = "mat-1",
    recommended_section: str | None = "development",
    tags: list[str] | None = None,
    highlight: float = 0.6,
    duration: float | None = 5.0,
) -> Material:
    return Material(
        material_id=material_id,
        session_id="proj-test",
        project_id="proj-test",
        filename=f"{material_id}.mp4",
        media_type="video",
        duration_seconds=duration,
        url=f"/uploads/{material_id}.mp4",
        thumbnail_url=None,
        tags=tags or ["奶茶", "新品", "特写"],
        highlight_score=highlight,
        recommended_section=recommended_section,  # type: ignore[arg-type]
    )


def test_recommended_section_hit_dominates_score():
    """段位推荐命中应显著提升评分（差不多 +0.4 区间）。"""
    sec = _section()
    hit = _material(recommended_section="development")
    miss = _material(recommended_section="opening")
    s_hit, _ = compute_material_fit(material=hit, section=sec, scene_duration=5.0)
    s_miss, _ = compute_material_fit(material=miss, section=sec, scene_duration=5.0)
    assert s_hit - s_miss >= 0.35, f"段位命中差应 >= 0.35，实际 {s_hit:.2f} vs {s_miss:.2f}"


def test_tag_overlap_lifts_score():
    sec = _section(theme="咖啡品鉴", content="慢动作展示拿铁拉花")
    relevant = _material(tags=["咖啡", "拿铁", "拉花"])
    irrelevant = _material(tags=["运动", "户外"])
    s_rel, reason_rel = compute_material_fit(material=relevant, section=sec, scene_duration=5.0)
    s_irr, _ = compute_material_fit(material=irrelevant, section=sec, scene_duration=5.0)
    assert s_rel > s_irr, "tag 命中评分必须 > 不命中"
    assert "主体匹配" in reason_rel


def test_duration_match_max_when_aligned():
    sec = _section()
    perfect = _material(duration=5.0)
    way_off = _material(duration=20.0)
    s_perf, _ = compute_material_fit(material=perfect, section=sec, scene_duration=5.0)
    s_off, _ = compute_material_fit(material=way_off, section=sec, scene_duration=5.0)
    assert s_perf > s_off


def test_highlight_score_contributes():
    sec = _section()
    high = _material(highlight=0.95)
    low = _material(highlight=0.10)
    s_hi, _ = compute_material_fit(material=high, section=sec, scene_duration=5.0)
    s_lo, _ = compute_material_fit(material=low, section=sec, scene_duration=5.0)
    # 高光权重 0.10，差应在 0.05~0.10 之间
    assert s_hi > s_lo
    assert s_hi - s_lo <= 0.12


def test_score_clamped_to_unit_interval():
    sec = _section()
    perfect = _material(recommended_section="development", tags=["奶茶", "新品"], highlight=1.0, duration=5.0)
    s, _ = compute_material_fit(material=perfect, section=sec, scene_duration=5.0)
    assert 0.0 <= s <= 1.0
    assert s > 0.85, f"全维度命中应该接近满分，实际 {s:.2f}"


def test_annotate_skips_non_user_material_scenes():
    sec = _section(section_id="sec-0", role="opening")
    sec2 = _section(section_id="sec-1", role="development")
    mat = _material(material_id="mat-x", recommended_section="opening")
    mats = {"mat-x": mat}
    main = [
        Scene(scene_id="sc-0", section="opening", parent_section_id="sec-0",
              source="user_material", source_ref="mat-x",
              start=0.0, duration=3.0, narration="开场"),
        Scene(scene_id="sc-1", section="development", parent_section_id="sec-1",
              source="aigc_image", source_ref="img-1",
              start=3.0, duration=4.0, narration="主体"),
        Scene(scene_id="sc-2", section="development", parent_section_id="sec-1",
              source="text_card", source_ref="card-1",
              start=7.0, duration=2.0, narration=None),
    ]
    written = annotate_plan_fit_scores(
        main_track=main, adapted_sections=[sec, sec2], materials_by_id=mats,
    )
    assert written == 1
    assert main[0].fit_score is not None
    assert main[1].fit_score is None
    assert main[2].fit_score is None


def test_annotate_clears_fit_when_material_missing():
    """如果 source_ref 找不到（例如素材被删但 plan 没刷新），清空 fit_score 而不是保留旧值。"""
    sec = _section(section_id="sec-0", role="development")
    main = [
        Scene(scene_id="sc-0", section="development", parent_section_id="sec-0",
              source="user_material", source_ref="mat-deleted",
              start=0.0, duration=3.0, narration="x", fit_score=0.9, fit_reason="残留旧分"),
    ]
    annotate_plan_fit_scores(
        main_track=main, adapted_sections=[sec], materials_by_id={},
    )
    assert main[0].fit_score is None
    assert main[0].fit_reason is None
