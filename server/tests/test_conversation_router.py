"""ConversationStore + /api/conversation 路由测试（任务 #407）。

覆盖：
1. project 不存在 → 404
2. POST append → GET 拿回；message_id 一致；trim 到 200
3. DELETE → 历史清空
4. 项目级 scoped：project A / B 互不影响
5. 落盘文件存在且 JSON 合法
"""
from __future__ import annotations

import json
import shutil
import time

import pytest

from app.config import get_settings
from app.schemas import ConversationMessage
from app.services.projects.conversation_store import (
    MAX_MESSAGES,
    conversation_store,
)
from app.services.projects.store import project_store


_TEST_PROJECT_IDS: list[str] = []


@pytest.fixture(autouse=True)
def cleanup_conv():
    yield
    var_root = get_settings().log_dir.parent / "var" / "projects"
    for pid in _TEST_PROJECT_IDS:
        project_store._by_id.pop(pid, None)
        conversation_store._by_project.pop(pid, None)
        path = var_root / pid
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)
    _TEST_PROJECT_IDS.clear()


def _make_project(client) -> str:
    r = client.post("/api/project", json={
        "name": f"单测·CONV-{int(time.time() * 1000)}",
        "sample_ids": ["sample-marketing-01"],
    })
    assert r.status_code == 200, r.text
    pid = r.json()["project_id"]
    _TEST_PROJECT_IDS.append(pid)
    return pid


def test_list_unknown_project_returns_404(client):
    r = client.get("/api/conversation/zzz-not-exists")
    assert r.status_code == 404


def test_empty_history_returns_empty_list(client):
    pid = _make_project(client)
    r = client.get(f"/api/conversation/{pid}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["project_id"] == pid
    assert body["messages"] == []
    assert body["truncated"] is False


def test_append_and_list_round_trip(client):
    pid = _make_project(client)
    r = client.post(f"/api/conversation/{pid}/append", json={
        "role": "user",
        "kind": "user_instruction",
        "text": "把开场段缩短到 4 秒",
        "plan_id": "plan-xyz",
        "step": "step2",
        "meta": {"apply": False},
    })
    assert r.status_code == 200, r.text
    msg = r.json()
    assert msg["role"] == "user"
    assert msg["text"] == "把开场段缩短到 4 秒"
    assert msg["message_id"]  # 服务端应补 uuid

    r2 = client.get(f"/api/conversation/{pid}")
    assert r2.status_code == 200
    body = r2.json()
    assert len(body["messages"]) == 1
    assert body["messages"][0]["message_id"] == msg["message_id"]


def test_persists_to_disk_as_json(client):
    pid = _make_project(client)
    client.post(f"/api/conversation/{pid}/append", json={
        "role": "agent", "kind": "agent_reply", "text": "好",
    })
    var_root = get_settings().log_dir.parent / "var" / "projects"
    path = var_root / pid / "conversations.json"
    assert path.exists(), f"未落盘: {path}"
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["project_id"] == pid
    assert isinstance(raw["messages"], list)
    assert raw["messages"][0]["role"] == "agent"


def test_clear_resets_history(client):
    pid = _make_project(client)
    client.post(f"/api/conversation/{pid}/append", json={
        "role": "user", "text": "x",
    })
    r = client.delete(f"/api/conversation/{pid}")
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True}
    body = client.get(f"/api/conversation/{pid}").json()
    assert body["messages"] == []


def test_project_scope_isolation(client):
    pid1 = _make_project(client)
    pid2 = _make_project(client)
    client.post(f"/api/conversation/{pid1}/append", json={
        "role": "user", "text": "项目 1 的话",
    })
    msgs1 = client.get(f"/api/conversation/{pid1}").json()["messages"]
    msgs2 = client.get(f"/api/conversation/{pid2}").json()["messages"]
    assert len(msgs1) == 1
    assert len(msgs2) == 0


def test_trim_to_max_messages():
    """直接打 ConversationStore：超 MAX_MESSAGES 后老消息被丢。"""
    pid = "test-trim-pid"
    _TEST_PROJECT_IDS.append(pid)
    # 先模拟一个 project 存在（store 不会校验 project，只 store 自己的历史）
    # 写 250 条，预期只剩最近 200
    for i in range(MAX_MESSAGES + 50):
        msg = conversation_store.make_message(
            role="user", kind="user_instruction",
            text=f"msg-{i:03d}", message_id=f"id-{i:03d}",
        )
        conversation_store.append(pid, msg)
    msgs, truncated = conversation_store.list(pid)
    assert len(msgs) == MAX_MESSAGES
    assert msgs[0].text == f"msg-{50:03d}"  # 最早保留的应是第 50 条
    assert msgs[-1].text == f"msg-{MAX_MESSAGES + 49:03d}"
    assert truncated is True


def test_message_id_dedup_overwrites():
    """相同 message_id 视为客户端重试 → 覆盖而非追加。"""
    pid = "test-dedup-pid"
    _TEST_PROJECT_IDS.append(pid)
    m1 = ConversationMessage(
        message_id="dup-1", role="user", kind="user_instruction",
        text="第一版", created_at=time.time(),
    )
    m2 = ConversationMessage(
        message_id="dup-1", role="user", kind="user_instruction",
        text="第二版（重试）", created_at=time.time() + 1,
    )
    conversation_store.append(pid, m1)
    conversation_store.append(pid, m2)
    msgs, _ = conversation_store.list(pid)
    assert len(msgs) == 1
    assert msgs[0].text == "第二版（重试）"
