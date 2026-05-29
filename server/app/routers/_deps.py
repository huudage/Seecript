"""FastAPI 路由共享依赖。

放小巧的 cross-cutting 依赖，避免在 routers 之间循环 import。
"""
from __future__ import annotations

from fastapi import HTTPException, Path

from ..schemas import Project
from ..services.projects import project_store


def require_project(project_id: str = Path(..., min_length=1)) -> Project:
    """从 path param 中拉 project；不存在直接 404。"""
    proj = project_store.get(project_id)
    if proj is None:
        raise HTTPException(status_code=404, detail=f"project not found: {project_id}")
    return proj
