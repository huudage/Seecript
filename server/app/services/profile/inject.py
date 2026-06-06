"""把所有"启用中"的项目知识包合并成扁平规则池——给 plan_agent / gap_agent 注入。

启用判定（与 routers/knowledge._compute_top10_project_ids 一致）：
- top-10 最近完成（status="rendered"）的项目自动启用
- + ProfileSettings.enabled_extra_project_ids（用户手动加的老项目）

返回的 KBRule 列表保留 `id` 与 `scope`，按 scope 分组方便 prompt 拼接。
"""
from __future__ import annotations

import logging
from collections import defaultdict
from typing import Optional

from .schemas import KBRule
from .store import list_project_kbs, load_settings

log = logging.getLogger("seecript.profile.inject")


def collect_active_rules(user_id: str = "default") -> dict[str, list[KBRule]]:
    """返回 {scope: [KBRule]}。scope ∈ {structure, source, narration, pacing}。"""
    try:
        from ..projects import project_store
    except Exception:  # noqa: BLE001
        log.warning("[profile.inject] project_store 导入失败，KB 注入退化为空")
        return {}

    settings = load_settings(user_id)
    extra = set(settings.enabled_extra_project_ids)

    items = project_store.list()  # updated_at desc
    rendered = [p.project_id for p in items if p.status == "rendered"]
    top10 = set(rendered[:10])
    active = top10 | extra
    if not active:
        return {}

    kbs = list_project_kbs(user_id)
    grouped: dict[str, list[KBRule]] = defaultdict(list)
    for kb in kbs:
        if kb.project_id not in active:
            continue
        for r in kb.rules:
            if r.scope in {"structure", "source", "narration", "pacing"}:
                grouped[r.scope].append(r)
    return dict(grouped)


def format_rules_for_prompt(
    grouped: dict[str, list[KBRule]],
    *,
    scopes: Optional[list[str]] = None,
    max_per_scope: int = 6,
) -> str:
    """把分组 KBRule 拼成中文 prompt 段。空时返回 ""。

    scopes=None 表示全部 4 类；指定时只输出列表里的 scope。
    """
    if not grouped:
        return ""
    use_scopes = scopes or ["structure", "source", "narration", "pacing"]
    labels = {
        "structure": "段落结构偏好",
        "source": "镜头来源选择偏好",
        "narration": "口播/文案风格偏好",
        "pacing": "节奏与时长偏好",
    }
    blocks: list[str] = []
    for scope in use_scopes:
        rules = grouped.get(scope) or []
        if not rules:
            continue
        lines = [f"【{labels[scope]}】"]
        for r in rules[:max_per_scope]:
            lines.append(f"- {r.text}")
        blocks.append("\n".join(lines))
    if not blocks:
        return ""
    return (
        "用户个性偏好（基于过往项目蒸馏，请在不违反硬约束的前提下尽量遵循）：\n"
        + "\n\n".join(blocks)
    )


def count_applied_rules(grouped: dict[str, list[KBRule]]) -> int:
    """所有 scope 的规则总数（去掉空 scope）。"""
    return sum(len(v) for v in grouped.values())
