"""ConversationStore：⌘K 命令面板的项目级对话历史持久化。

设计：
- 项目级 scoped：跨 plan 切换、重新拆解后历史仍连续（用户心智匹配「视频工作坊」）
- 单文件 var/projects/<project_id>/conversations.json，按 created_at 顺序 append
- 自动 trim 到最近 200 条（保留早期 1 条 intro 占位 + 最近 199 条；超出 silently drop）
- 内存缓存 + RLock 单进程；多 worker 上 gunicorn 必须先换 SQLite/Postgres
- mutate → 先改内存、再原子写盘（与 ProjectStore 同范式）

NOTE：__legacy 兜底——plan.project_id 为空时落到 `__legacy/conversations.json`，
       前端不会主动请求该桶，仅作 trace 兜底。
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

from ...config import get_settings
from ...schemas import ConversationMessage

log = logging.getLogger("seecript.conversations")


MAX_MESSAGES = 200
LEGACY_OWNER = "__legacy"


def _var_root() -> Path:
    return get_settings().log_dir.parent / "var"


def _conversations_path(project_id: str) -> Path:
    """每个 project 一个文件：var/projects/<project_id>/conversations.json"""
    safe = (project_id or LEGACY_OWNER).strip() or LEGACY_OWNER
    return _var_root() / "projects" / safe / "conversations.json"


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


class ConversationStore:
    """线程安全的 per-project 对话历史。

    - in-memory `_by_project` 是热路径
    - conversations.json 是冷备份；按需 lazy load
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._by_project: dict[str, list[ConversationMessage]] = {}
        # 启动不全量扫盘（项目可能很多），按 project_id 第一次访问 lazy load。

    # ---------- 内部 ----------
    def _ensure_loaded(self, project_id: str) -> list[ConversationMessage]:
        if project_id in self._by_project:
            return self._by_project[project_id]
        path = _conversations_path(project_id)
        msgs: list[ConversationMessage] = []
        if path.exists():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                items = raw.get("messages", []) if isinstance(raw, dict) else []
                for item in items:
                    try:
                        msgs.append(ConversationMessage.model_validate(item))
                    except Exception as exc:  # noqa: BLE001
                        log.warning("[conversations] skip broken message in %s: %s", path, exc)
            except Exception as exc:  # noqa: BLE001
                log.warning("[conversations] failed to load %s: %s", path, exc)
        self._by_project[project_id] = msgs
        return msgs

    def _persist(self, project_id: str) -> None:
        msgs = self._by_project.get(project_id, [])
        path = _conversations_path(project_id)
        _atomic_write_json(path, {
            "project_id": project_id,
            "saved_at": time.time(),
            "messages": [m.model_dump() for m in msgs],
        })

    @staticmethod
    def _trim(msgs: list[ConversationMessage]) -> tuple[list[ConversationMessage], bool]:
        """超过 MAX_MESSAGES 时丢老消息；返回 (新列表, 是否截过)。"""
        if len(msgs) <= MAX_MESSAGES:
            return msgs, False
        return msgs[-MAX_MESSAGES:], True

    # ---------- public ----------
    def list(self, project_id: str) -> tuple[list[ConversationMessage], bool]:
        """读取一个项目的全部对话；返回 (messages, truncated)。

        truncated=True 表示文件里曾有 trim 操作（即此前消息超过 200，老消息已丢）。
        """
        with self._lock:
            msgs = list(self._ensure_loaded(project_id))
            truncated = len(msgs) >= MAX_MESSAGES
            return msgs, truncated

    def append(self, project_id: str, message: ConversationMessage) -> ConversationMessage:
        """追加一条消息；超 200 自动 trim 老消息后落盘。"""
        with self._lock:
            msgs = self._ensure_loaded(project_id)
            # 排重：相同 message_id 视为客户端重试，覆盖时间戳
            for i, existing in enumerate(msgs):
                if existing.message_id == message.message_id:
                    msgs[i] = message
                    self._persist(project_id)
                    return message
            msgs.append(message)
            trimmed, _ = self._trim(msgs)
            if trimmed is not msgs:
                self._by_project[project_id] = trimmed
            self._persist(project_id)
            return message

    def clear(self, project_id: str) -> None:
        with self._lock:
            self._by_project[project_id] = []
            self._persist(project_id)

    # ---------- 工厂便捷 ----------
    @staticmethod
    def make_message(
        *,
        role: str,
        kind: str,
        text: str = "",
        plan_id: Optional[str] = None,
        step: Optional[str] = None,
        meta: Optional[dict] = None,
        message_id: Optional[str] = None,
    ) -> ConversationMessage:
        return ConversationMessage(
            message_id=message_id or uuid.uuid4().hex[:16],
            role=role,  # type: ignore[arg-type]
            kind=kind,  # type: ignore[arg-type]
            text=text,
            created_at=time.time(),
            plan_id=plan_id,
            step=step,  # type: ignore[arg-type]
            meta=meta or {},
        )


conversation_store = ConversationStore()
