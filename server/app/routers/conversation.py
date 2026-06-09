"""⌘K 命令面板对话历史路由（项目级 scoped）。

- GET    /conversation/<project_id>          → 项目全部历史（最近 200 条）
- POST   /conversation/<project_id>/append   → 前端主动追加（intro / 用户撤回标记等）
- DELETE /conversation/<project_id>          → 清空历史

⌘K 编辑成功 / 撤回的 user/agent 消息由 routers/edit.py 内部直接调 conversation_store.append()，
前端不需要为每条消息做一次 POST。
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException

from ..schemas import (
    ConversationAppendRequest,
    ConversationListResponse,
    ConversationMessage,
)
from ..services.projects.conversation_store import conversation_store
from ..services.projects.store import project_store

log = logging.getLogger("seecript.routers.conversation")
router = APIRouter(prefix="/api", tags=["conversation"])


def _ensure_project_exists(project_id: str) -> None:
    if project_store.get(project_id) is None:
        raise HTTPException(status_code=404, detail=f"project not found: {project_id}")


@router.get("/conversation/{project_id}", response_model=ConversationListResponse)
async def list_conversation(project_id: str) -> ConversationListResponse:
    """读取项目级 ⌘K 对话历史。

    project 不存在 → 404。空历史 → messages=[]。
    """
    _ensure_project_exists(project_id)
    messages, truncated = conversation_store.list(project_id)
    return ConversationListResponse(
        project_id=project_id, messages=messages, truncated=truncated,
    )


@router.post("/conversation/{project_id}/append", response_model=ConversationMessage)
async def append_conversation(
    project_id: str, body: ConversationAppendRequest,
) -> ConversationMessage:
    """前端追加一条消息——一般是 intro 或本地侧错误日志。

    ⌘K 编辑流的 user/agent 消息由后端 edit 路由内部 append，不必走这条。
    """
    _ensure_project_exists(project_id)
    msg = conversation_store.make_message(
        role=body.role, kind=body.kind, text=body.text,
        plan_id=body.plan_id, step=body.step,
        meta=body.meta, message_id=body.message_id,
    )
    return conversation_store.append(project_id, msg)


@router.delete("/conversation/{project_id}")
async def clear_conversation(project_id: str) -> dict[str, bool]:
    """清空项目级历史（⌘K 面板「清空对话」按钮）。"""
    _ensure_project_exists(project_id)
    conversation_store.clear(project_id)
    log.info("[conversation] cleared project=%s", project_id)
    return {"ok": True}
