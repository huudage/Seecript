"""Module · Project（项目工作流容器）。

一个 project = 一次完整的『样例 → 改编 → 补全 → 渲染』流程容器。
project_id 是后端唯一隔离键：素材 / 资产库 / plans / gaps 全部按它分组。

Endpoints（全部 prefix=/api）：
- POST   /project                 创建：name + sample_id → 新 Project
- GET    /project                 列出全部项目（按 updated_at 倒序）
- GET    /project/{project_id}    单条详情
- PATCH  /project/{project_id}    部分字段更新（name/brief/video_goal/settings/...）
- DELETE /project/{project_id}    删除（级联清 var/projects、var/uploads、var/assets）
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException

from ..schemas import (
    Project,
    ProjectCreateRequest,
    ProjectListResponse,
    ProjectUpdateRequest,
)
from ..services.projects import project_store
from ..services.projects.store import ProjectNotFoundError, ProjectStoreError
from ._deps import require_project

log = logging.getLogger("seecript.project")
router = APIRouter()


def _sample_exists(sample_id: str) -> bool:
    """校验 sample_id 是不是已注册的内置样例 / 用户上传样例。

    - 内置：library._LIBRARY 名单
    - 用户上传：var/uploads/decompose/<sample_id>/video.mp4 在盘上
      （manifest 可能未生成，先允许建项目，后续在 Decompose 页跑拆解）

    晚 import 避免和 library / decompose 路由互引入。
    """
    from .library import _LIBRARY
    if any(it.id == sample_id for it in _LIBRARY):
        return True
    from .decompose import _user_uploads_root
    return (_user_uploads_root() / sample_id / "video.mp4").is_file()


@router.post("/project", response_model=Project)
async def create_project(body: ProjectCreateRequest) -> Project:
    """新建项目。校验 sample_id 真实存在，避免后续 plan/build 才发现样例缺失。"""
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="name 不能为空")
    if not _sample_exists(body.sample_id):
        raise HTTPException(status_code=404, detail=f"sample not found: {body.sample_id}")
    project = project_store.create(name=name, sample_id=body.sample_id)
    return project


@router.get("/project", response_model=ProjectListResponse)
async def list_projects() -> ProjectListResponse:
    """全量列出项目。首页项目网格用，按 updated_at 倒序。"""
    return ProjectListResponse(items=project_store.list())


@router.get("/project/{project_id}", response_model=Project)
async def get_project(project: Project = Depends(require_project)) -> Project:
    return project


@router.patch("/project/{project_id}", response_model=Project)
async def update_project(
    body: ProjectUpdateRequest,
    project: Project = Depends(require_project),
) -> Project:
    patch = body.model_dump(exclude_unset=True)
    try:
        return project_store.update(project.project_id, **patch)
    except ProjectNotFoundError:
        raise HTTPException(status_code=404, detail=f"project not found: {project.project_id}")
    except ProjectStoreError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/project/{project_id}")
async def delete_project(project: Project = Depends(require_project)) -> dict:
    """删除项目：级联清 var/projects/<id>/、var/uploads/<id>/、var/assets/<id>/。"""
    try:
        project_store.delete(project.project_id)
    except Exception as exc:  # noqa: BLE001
        log.exception("[project] delete %s failed: %s", project.project_id, exc)
        raise HTTPException(status_code=500, detail=f"delete failed: {exc}") from exc
    return {"deleted": True, "project_id": project.project_id}
