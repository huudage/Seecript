"""Module · Step（步骤状态机 / 「下一步」提交）。

线性工作流 library → decompose → compose → render。每点一次「下一步」=
POST .../step/<step>/commit，把当前步产物快照落盘 + 推进 Project.step_states。

Endpoints（prefix=/api）：
- POST /project/{project_id}/step/{step}/commit   提交当前步快照 → 返回更新后的 Project
- GET  /project/{project_id}/step/{step}          读单步最近快照（前端进页面回填用）
- GET  /project/{project_id}/steps                列出全部已提交快照

「保留下游」语义：commit 把下游已 saved 的步骤打成 dirty（产物保留，只提示过期），
不删盘上的 plan/gap/render；下次用户在下游重新 commit 才覆盖。
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Path

from ..schemas import Project, ReferenceVersion, StepName, StepSnapshot
from ..services.projects import project_store, step_store
from ..services.projects.store import ProjectNotFoundError
from ._deps import require_project

log = logging.getLogger("seecript.step")
router = APIRouter()


def _apply_side_effects(project_id: str, snapshot: StepSnapshot) -> None:
    """按 step 把快照里的关键产物 id 回写到 Project 顶层字段。

    这些字段是『最近一次』的快捷指针；真源仍是 step.json 的 payload。
    回写失败只 warn 不阻断 commit（step_states 已是权威状态）。

    library payload 协议（stage-15）：
    - 新：{"references": [{"sample_id": str, "slot_id": str}, ...]}（首选）
    - 老兼容：{"sample_ids": [str, ...]}——按当时 active slot 反查填进 reference_versions
    """
    payload = snapshot.payload or {}
    try:
        if snapshot.step == "library":
            refs = _parse_library_references(payload)
            if refs:
                project_store.update(project_id, reference_versions=[r.model_dump() for r in refs])
        elif snapshot.step == "compose":
            plan_id = payload.get("plan_id")
            if plan_id:
                project_store.mark_planned(project_id, plan_id)
        elif snapshot.step == "render":
            job_id = payload.get("job_id")
            if job_id:
                project_store.mark_rendered(project_id, job_id)
        # decompose：无顶层字段回写（manifest 在样例共享区）
    except Exception as exc:  # noqa: BLE001
        log.warning("[step] side-effect for %s/%s failed: %s", project_id, snapshot.step, exc)


def _parse_library_references(payload: dict) -> list[ReferenceVersion]:
    """library step 兼容老 `sample_ids` payload。

    优先级：payload.references > payload.sample_ids（按当前 active slot 反查）。
    数量限制 1-2；解析失败返回空列表，由调用方决定是否回写。
    """
    raw_refs = payload.get("references")
    if isinstance(raw_refs, list) and raw_refs:
        out: list[ReferenceVersion] = []
        for item in raw_refs[:2]:
            if not isinstance(item, dict):
                continue
            sid = item.get("sample_id")
            slot = item.get("slot_id")
            if isinstance(sid, str) and isinstance(slot, str) and sid and slot:
                out.append(ReferenceVersion(sample_id=sid, slot_id=slot))
        return out
    # 老 payload：sample_ids → 按 active slot 反查
    legacy_ids = payload.get("sample_ids")
    if isinstance(legacy_ids, list) and 1 <= len(legacy_ids) <= 2:
        from ..services.library import manifest_store
        out2: list[ReferenceVersion] = []
        for sid in legacy_ids:
            if not isinstance(sid, str):
                continue
            active = manifest_store.get_active_slot(sid)
            if active:
                out2.append(ReferenceVersion(sample_id=sid, slot_id=active))
        return out2
    return []


@router.post("/project/{project_id}/step/{step}/commit", response_model=Project)
async def commit_step(
    snapshot: StepSnapshot,
    step: StepName = Path(...),
    project: Project = Depends(require_project),
) -> Project:
    """提交当前步快照。body 里的 step 必须与 path 的 step 一致，避免错位写盘。"""
    if snapshot.step != step:
        raise HTTPException(
            status_code=400,
            detail=f"snapshot.step={snapshot.step} 与 path step={step} 不一致",
        )
    # 先跑顶层字段回写（mark_planned 等），再 step_store.save 推进状态机
    _apply_side_effects(project.project_id, snapshot)
    try:
        return step_store.save(project.project_id, snapshot)
    except ProjectNotFoundError:
        raise HTTPException(status_code=404, detail=f"project not found: {project.project_id}")


@router.get("/project/{project_id}/step/{step}", response_model=StepSnapshot | None)
async def get_step(
    step: StepName = Path(...),
    project: Project = Depends(require_project),
) -> StepSnapshot | None:
    """读单步最近快照；从未提交过 → null（前端据此 reset 本地 store）。"""
    return step_store.get(project.project_id, step)


@router.get("/project/{project_id}/steps", response_model=list[StepSnapshot])
async def list_steps(project: Project = Depends(require_project)) -> list[StepSnapshot]:
    return step_store.list(project.project_id)
