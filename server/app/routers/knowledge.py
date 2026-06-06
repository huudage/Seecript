"""Module · Knowledge（个性知识库）—— Hermes 风格规则蒸馏的 read/write API。

Endpoints（prefix=/api）：
- GET   /profile                        : 总览（settings + 默认库说明 + project KB 列表 + 最近 10 完成项目）
- PATCH /profile/settings               : 改用户级开关（realtime_distill / enabled_extra_project_ids）
- PATCH /profile/projects/{id}/enabled  : 单独把某项目加入/移出 enabled_extra_project_ids
- GET   /profile/projects/{id}          : 取单个项目的完整 KB（含 rules 全文）

「默认库」的内容由 server/app/services/agent/decompose_agent.py 的 prompt 充当，不蒸馏、永远开启，
所以这里只回它的一句说明文案 + prompt 摘要，前端展示用。
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..services.profile import (
    DEFAULT_USER_ID,
    ProfileSettings,
    ProjectKB,
    list_project_kbs,
    load_project_kb,
    load_settings,
    save_settings,
)
from ..services.projects import project_store

log = logging.getLogger("seecript.knowledge")
router = APIRouter()


# 默认库说明 —— 当前用 decompose_agent 的 system prompt 当默认库（不蒸馏、不可编辑）。
# 后续可以替换为真知识包；这里给前端展示用的元信息。
DEFAULT_KB_DESCRIPTION = (
    "默认库 = 内置「样例拆解」prompt：覆盖结构识别（5 种叙事模式）、节奏曲线建模、"
    "段落角色判定与口播风格归纳。永远开启、不可编辑。"
)


class ProjectKBSummary(BaseModel):
    """前端列表用的项目知识包摘要——不带 rules 全文。"""

    project_id: str
    project_title: str
    video_type: Optional[str] = None
    render_committed_at: int
    summary: str
    rules_count: int
    enabled: bool = Field(..., description="是否被注入（top-10 最近完成 OR 用户在 enabled_extra_project_ids 里加了它）")
    is_top10: bool = Field(..., description="是否落在 top-10 最近完成的窗口内")
    is_extra_enabled: bool = Field(..., description="是否被用户手动加到 enabled_extra_project_ids")


class ProfileOverview(BaseModel):
    settings: ProfileSettings
    default_kb_description: str = DEFAULT_KB_DESCRIPTION
    projects: list[ProjectKBSummary] = Field(default_factory=list)


class ProfileSettingsPatch(BaseModel):
    """PATCH /profile/settings —— 任一字段 None 表示不动。"""

    realtime_distill_enabled: Optional[bool] = None
    enabled_extra_project_ids: Optional[list[str]] = Field(
        default=None,
        max_length=64,
        description="完整覆盖（不是 diff），前端按需传完整列表",
    )


class ProjectEnabledPatch(BaseModel):
    enabled: bool


def _compute_top10_project_ids(user_id: str = DEFAULT_USER_ID) -> set[str]:
    """选最近 10 个**已渲染**项目作为默认注入窗口。

    判定「完成」= status == "rendered"。不到 10 个就全要。
    """
    items = project_store.list()  # 已按 updated_at desc
    completed = [p for p in items if p.status == "rendered"]
    return {p.project_id for p in completed[:10]}


def _summarize_kb(kb: ProjectKB, *, is_top10: bool, is_extra_enabled: bool) -> ProjectKBSummary:
    return ProjectKBSummary(
        project_id=kb.project_id,
        project_title=kb.project_title or "(未命名项目)",
        video_type=kb.video_type,
        render_committed_at=kb.render_committed_at,
        summary=kb.summary,
        rules_count=len(kb.rules),
        enabled=is_top10 or is_extra_enabled,
        is_top10=is_top10,
        is_extra_enabled=is_extra_enabled,
    )


@router.get("/profile", response_model=ProfileOverview)
async def get_profile() -> ProfileOverview:
    user_id = DEFAULT_USER_ID
    settings = load_settings(user_id)
    kbs = list_project_kbs(user_id)
    top10 = _compute_top10_project_ids(user_id)
    extra = set(settings.enabled_extra_project_ids)
    summaries = [
        _summarize_kb(
            kb,
            is_top10=kb.project_id in top10,
            is_extra_enabled=kb.project_id in extra,
        )
        for kb in kbs
    ]
    return ProfileOverview(settings=settings, projects=summaries)


@router.patch("/profile/settings", response_model=ProfileSettings)
async def patch_settings(body: ProfileSettingsPatch) -> ProfileSettings:
    user_id = DEFAULT_USER_ID
    settings = load_settings(user_id)
    patch = body.model_dump(exclude_unset=True)
    if "realtime_distill_enabled" in patch:
        settings.realtime_distill_enabled = bool(patch["realtime_distill_enabled"])
    if "enabled_extra_project_ids" in patch:
        ids = patch["enabled_extra_project_ids"] or []
        # 去重 + 去空
        settings.enabled_extra_project_ids = list({pid for pid in ids if isinstance(pid, str) and pid.strip()})
    save_settings(user_id, settings)
    log.info("[knowledge] settings patched: realtime=%s extra=%d",
             settings.realtime_distill_enabled, len(settings.enabled_extra_project_ids))
    return settings


@router.patch("/profile/projects/{project_id}/enabled", response_model=ProfileSettings)
async def patch_project_enabled(project_id: str, body: ProjectEnabledPatch) -> ProfileSettings:
    """单项目 enabled 开关——只动 enabled_extra_project_ids，不动 top-10 窗口。

    top-10 是计算结果，不持久化；用户对 top-10 内的项目再点"启用"也允许（幂等）。
    """
    user_id = DEFAULT_USER_ID
    settings = load_settings(user_id)
    ids = set(settings.enabled_extra_project_ids)
    if body.enabled:
        ids.add(project_id)
    else:
        ids.discard(project_id)
    settings.enabled_extra_project_ids = sorted(ids)
    save_settings(user_id, settings)
    log.info("[knowledge] project enabled=%s project=%s extra_total=%d",
             body.enabled, project_id, len(settings.enabled_extra_project_ids))
    return settings


@router.get("/profile/projects/{project_id}", response_model=ProjectKB)
async def get_project_kb(project_id: str) -> ProjectKB:
    user_id = DEFAULT_USER_ID
    kb = load_project_kb(user_id, project_id)
    if kb is None:
        raise HTTPException(status_code=404, detail=f"project KB not found: {project_id}")
    return kb
