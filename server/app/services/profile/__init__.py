"""个性知识库（Profile）—— Hermes 风格规则蒸馏与注入。

模块组成：
- paths.py   : var/profiles/{user_id}/ 路径辅助
- schemas.py : TraceA / TraceB / ProjectKB / ProfileSettings Pydantic 模型
- store.py   : JSONL append（trace）+ 原子 JSON 读写（settings / project kb）
- distill.py : LLM 蒸馏 worker，post-render 异步触发
- inject.py  : plan/build & gap/fill 注入合并

P1 范围：paths/schemas/store；distill & inject 在 P2/P3 落地。
单用户模式下 user_id 一律为 "default"，schema 预留多用户字段。
"""
from __future__ import annotations

DEFAULT_USER_ID = "default"

from .paths import (
    profile_dir,
    settings_path,
    trace_a_path,
    trace_b_path,
    project_kb_dir,
    project_kb_path,
)
from .schemas import (
    ProfileSettings,
    TraceA,
    TraceB,
    PlanSnapshot,
    StructureDiff,
    NarrationDiff,
    SourceChange,
    RoleChange,
    ProjectKB,
    KBRule,
)
from .store import (
    load_settings,
    save_settings,
    append_trace_a,
    append_trace_b,
    read_traces_a,
    read_traces_b,
    save_project_kb,
    load_project_kb,
    list_project_kbs,
)
from .snapshot import to_snapshot, structure_diff
from .distill import distill_project_kb
from .inject import collect_active_rules, format_rules_for_prompt, count_applied_rules

__all__ = [
    "DEFAULT_USER_ID",
    "ProfileSettings",
    "TraceA",
    "TraceB",
    "PlanSnapshot",
    "StructureDiff",
    "NarrationDiff",
    "SourceChange",
    "RoleChange",
    "ProjectKB",
    "KBRule",
    "profile_dir",
    "settings_path",
    "trace_a_path",
    "trace_b_path",
    "project_kb_dir",
    "project_kb_path",
    "load_settings",
    "save_settings",
    "append_trace_a",
    "append_trace_b",
    "read_traces_a",
    "read_traces_b",
    "save_project_kb",
    "load_project_kb",
    "list_project_kbs",
    "to_snapshot",
    "structure_diff",
    "distill_project_kb",
    "collect_active_rules",
    "format_rules_for_prompt",
    "count_applied_rules",
]
