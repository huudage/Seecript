"""profile 子模块的路径常量与辅助。

布局：
    var/profiles/{user_id}/
    ├── settings.json
    ├── traces/
    │   ├── A_structure_diff.jsonl
    │   └── B_nl_edit.jsonl
    └── projects/
        └── {project_id}.json

settings.log_dir 在生产 / 测试都指向 server 下子目录，profiles 跟 plans/projects 平级。
"""
from __future__ import annotations

from pathlib import Path

from ...config import get_settings


def _var_root() -> Path:
    return get_settings().log_dir.parent / "var"


def profile_dir(user_id: str) -> Path:
    p = _var_root() / "profiles" / user_id
    p.mkdir(parents=True, exist_ok=True)
    return p


def settings_path(user_id: str) -> Path:
    return profile_dir(user_id) / "settings.json"


def traces_dir(user_id: str) -> Path:
    p = profile_dir(user_id) / "traces"
    p.mkdir(parents=True, exist_ok=True)
    return p


def trace_a_path(user_id: str) -> Path:
    return traces_dir(user_id) / "A_structure_diff.jsonl"


def trace_b_path(user_id: str) -> Path:
    return traces_dir(user_id) / "B_nl_edit.jsonl"


def project_kb_dir(user_id: str) -> Path:
    p = profile_dir(user_id) / "projects"
    p.mkdir(parents=True, exist_ok=True)
    return p


def project_kb_path(user_id: str, project_id: str) -> Path:
    return project_kb_dir(user_id) / f"{project_id}.json"
