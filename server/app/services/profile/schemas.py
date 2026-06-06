"""profile 模块的数据 schema。

Trace A 是结构 diff（render commit 触发）；Trace B 是 NL 编辑事件（scene PATCH / gap fill 触发）。
ProjectKB 是单项目蒸馏后的知识包，project_id 维度落盘，render commit 覆盖式更新。

DesignDecisions：
- PlanSnapshot 只保留蒸馏与回放需要的字段（adapted_sections + main_track 关键列），
  不存全量 Plan 是为了 trace 文件不爆且 schema 演化不串。
- TraceB 不区分"被保留 / 被推翻"——按用户决策 D 沉淀，蒸馏 prompt 全采。
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class SceneSnapshot(BaseModel):
    """Plan.main_track[i] 的蒸馏快照，省去 voiceover / aigc_urls / transition。"""

    scene_id: str
    section: str
    source: str
    duration: float
    narration: Optional[str] = None


class AdaptedSectionSnapshot(BaseModel):
    """Plan.adapted_sections[i] 的蒸馏快照——只关心 role/theme/时长比，不要全文。"""

    order: int
    role: str
    theme: str
    start: float = 0.0
    end: float = 0.0


class PlanSnapshot(BaseModel):
    """v0 / v1 通用：仅保留蒸馏需要的列。"""

    plan_id: str
    sample_ids: list[str] = Field(default_factory=list)
    video_type: Optional[str] = None
    brief: Optional[str] = None
    duration_seconds: float = 0.0
    adapted_sections: list[AdaptedSectionSnapshot] = Field(default_factory=list)
    main_track: list[SceneSnapshot] = Field(default_factory=list)


class RoleChange(BaseModel):
    """段落角色的拆分/合并/替换。例如 {from: "climax", to: "climax+climax"} 表示一段高潮被拆成两段。"""

    section_order: int
    before: str
    after: str


class SourceChange(BaseModel):
    """同一段落里 scene 的 source 类型变化（climax 段把 aigc_t2v 全换成 copy）。"""

    section_role: str
    before: str
    after: str
    scene_id: Optional[str] = None


class NarrationDiff(BaseModel):
    scene_id: str
    before: Optional[str] = None
    after: Optional[str] = None


class StructureDiff(BaseModel):
    section_count_delta: int = 0
    role_changes: list[RoleChange] = Field(default_factory=list)
    source_changes: list[SourceChange] = Field(default_factory=list)
    narration_diffs: list[NarrationDiff] = Field(default_factory=list)


class TraceA(BaseModel):
    """结构迁移 diff —— render commit 时落盘。"""

    ts: int
    project_id: str
    plan_id: str
    user_id: str = "default"
    sample_ids: list[str] = Field(default_factory=list)
    v0: PlanSnapshot
    v1: PlanSnapshot
    diff: StructureDiff = Field(default_factory=StructureDiff)


class TraceB(BaseModel):
    """NL 编辑事件 —— scene PATCH / gap fill / ⌘K compose_edit 时落盘。

    decision D：不区分 survived / dropped；蒸馏时全采。
    context 取值：
    - "scene_edit"   — 段落字段直改（SceneEditPanel）
    - "gap_fill"     — gap fill 时带的 prompt_hint
    - "compose_edit" — ⌘K 对话编辑小助手的 apply（多 diff 落地，after.ops 列出实际 ops）
    - "compose_edit_dismissed" — ⌘K dry-run 后用户撤回的 diff（信号：这个方向用户不喜欢）
    """

    ts: int
    project_id: str
    plan_id: str
    user_id: str = "default"
    context: str
    scene_id: Optional[str] = None
    gap_id: Optional[str] = None
    section_role: Optional[str] = None
    user_input: str
    before: dict = Field(default_factory=dict)
    after: dict = Field(default_factory=dict)


class KBRule(BaseModel):
    id: str
    scope: str  # "structure" | "source" | "narration" | "pacing" | "packaging"
    text: str
    evidence_trace_ids: list[str] = Field(default_factory=list)


class ProjectKB(BaseModel):
    """单项目蒸馏后的知识包。render commit 时覆盖式重写——以最新一次蒸馏为准。"""

    project_id: str
    project_title: str = ""
    video_type: Optional[str] = None
    render_committed_at: int
    summary: str = ""
    rules: list[KBRule] = Field(default_factory=list)


class ProfileSettings(BaseModel):
    """用户级开关。

    realtime_distill_enabled: render commit 后是否立即蒸馏（false 时仅写 trace）
    enabled_extra_project_ids: 注入时除了 top-10 最近完成项目外，用户额外手动启用的老项目
    """

    realtime_distill_enabled: bool = True
    enabled_extra_project_ids: list[str] = Field(default_factory=list)
