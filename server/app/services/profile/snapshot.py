"""Plan ↔ PlanSnapshot 的转换 + 结构 diff 计算。

snapshot 是给 trace 落盘 / 蒸馏 prompt 用的 Plan 精简版（避免存全量 Plan 把 jsonl 文件撑爆）。

structure_diff 走两份 snapshot：
- section_count_delta：段落数变化
- role_changes：相同 order 上 role 字符串前后差异
- source_changes：按 section_role 聚合 scene.source 类型集合，求集合差
- narration_diffs：相同 scene_id 上 narration 文本差异

diff 不试图做对齐——order 错位 / scene_id 重命名都会落成 noisy diff，蒸馏 prompt 会 dedupe。
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from .schemas import (
    AdaptedSectionSnapshot,
    NarrationDiff,
    PlanSnapshot,
    RoleChange,
    SceneSnapshot,
    SourceChange,
    StructureDiff,
)

if TYPE_CHECKING:
    from ...schemas import Plan


def to_snapshot(plan: "Plan") -> PlanSnapshot:
    """把完整 Plan 压成 PlanSnapshot（只留蒸馏 + diff 需要的列）。"""
    sample_ids = list(plan.sample_ids) if hasattr(plan, "sample_ids") else []
    settings = getattr(plan, "settings", None)
    video_type = None
    if settings is not None:
        # settings 上没有 video_type 字段，留 None；后续若加上可在这里读
        video_type = getattr(settings, "video_type", None)
    return PlanSnapshot(
        plan_id=plan.plan_id,
        sample_ids=sample_ids,
        video_type=video_type,
        brief=plan.brief,
        duration_seconds=float(plan.duration_seconds or 0.0),
        adapted_sections=[
            AdaptedSectionSnapshot(
                order=sec.order,
                role=sec.role,
                theme=sec.theme or "",
                start=0.0,  # AdaptedSection 没有 start/end；duration_seconds 累计在 main_track
                end=float(sec.duration_seconds or 0.0),
            )
            for sec in plan.adapted_sections
        ],
        main_track=[
            SceneSnapshot(
                scene_id=sc.scene_id,
                section=sc.section,
                source=sc.source,
                duration=float(sc.duration or 0.0),
                narration=sc.narration or None,
            )
            for sc in plan.main_track
        ],
    )


def structure_diff(v0: PlanSnapshot, v1: PlanSnapshot) -> StructureDiff:
    """计算两份 snapshot 的结构差异。"""
    role_changes: list[RoleChange] = []
    # adapted_sections 按 order 配对
    v0_secs = {s.order: s for s in v0.adapted_sections}
    v1_secs = {s.order: s for s in v1.adapted_sections}
    for order in sorted(set(v0_secs) | set(v1_secs)):
        a = v0_secs.get(order)
        b = v1_secs.get(order)
        if a is None and b is not None:
            role_changes.append(RoleChange(section_order=order, before="", after=b.role))
        elif a is not None and b is None:
            role_changes.append(RoleChange(section_order=order, before=a.role, after=""))
        elif a and b and a.role != b.role:
            role_changes.append(RoleChange(section_order=order, before=a.role, after=b.role))

    # source 集合差：按 scene.section 聚合
    def _by_section(snap: PlanSnapshot) -> dict[str, set[str]]:
        out: dict[str, set[str]] = {}
        for sc in snap.main_track:
            out.setdefault(sc.section, set()).add(sc.source)
        return out

    v0_src = _by_section(v0)
    v1_src = _by_section(v1)
    source_changes: list[SourceChange] = []
    for section in sorted(set(v0_src) | set(v1_src)):
        a = v0_src.get(section, set())
        b = v1_src.get(section, set())
        if a != b:
            source_changes.append(SourceChange(
                section_role=section,
                before=",".join(sorted(a)) or "(none)",
                after=",".join(sorted(b)) or "(none)",
            ))

    # narration diff：按 scene_id 配对
    v0_nar = {sc.scene_id: (sc.narration or "") for sc in v0.main_track}
    v1_nar = {sc.scene_id: (sc.narration or "") for sc in v1.main_track}
    narration_diffs: list[NarrationDiff] = []
    for sid in sorted(set(v0_nar) | set(v1_nar)):
        a = v0_nar.get(sid, "")
        b = v1_nar.get(sid, "")
        if a.strip() != b.strip():
            narration_diffs.append(NarrationDiff(scene_id=sid, before=a or None, after=b or None))

    return StructureDiff(
        section_count_delta=len(v1.adapted_sections) - len(v0.adapted_sections),
        role_changes=role_changes,
        source_changes=source_changes,
        narration_diffs=narration_diffs,
    )
