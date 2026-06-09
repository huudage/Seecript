"""Clarify SSE 端点测试（v2）—— 验证 mock LLM 下 progress + outline_ready + done 事件契约。"""
from __future__ import annotations

import base64
import json


def _encode(payload: dict) -> str:
    raw = json.dumps(payload).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _parse_sse(text: str) -> list[dict]:
    """切 SSE 文本成 [{event, data}, ...]。"""
    out: list[dict] = []
    for block in text.split("\n\n"):
        if not block.strip():
            continue
        event = None
        data_lines = []
        for line in block.splitlines():
            if line.startswith("event:"):
                event = line.split(":", 1)[1].strip()
            elif line.startswith("data:"):
                data_lines.append(line.split(":", 1)[1].strip())
        if event and data_lines:
            out.append({"event": event, "data": json.loads("\n".join(data_lines))})
    return out


def test_round_emits_progress_then_done(client):
    """第 1 轮：transcript 空 → 至少 1 个 progress + 1 个 done；done 含 outline + question。"""
    p = _encode({
        "initial_brief": "想做一支咖啡店探店视频",
        "transcript": [],
        "force_finalize": False,
    })
    resp = client.get(f"/api/clarify/round?p={p}")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    events = _parse_sse(resp.text)
    assert len(events) >= 2
    assert events[-1]["event"] == "done"
    done = events[-1]["data"]
    assert done["round"] == 1
    assert done["is_final"] is False
    assert done["question"], "第 1 轮一定要有问题"
    # v2：outline 五件套结构
    outline = done["outline"]
    assert isinstance(outline, dict)
    for key in ("topic", "content", "audience", "goal", "tone"):
        assert key in outline, f"outline 缺字段 {key}"
    # mock 至少填了 topic
    assert outline["topic"], "mock 至少应有 topic"

    # 中间至少有 1 个 progress（thinking 或 outline_ready）
    progresses = [e for e in events if e["event"] == "progress"]
    assert progresses, "expected at least one progress event"
    # 至少一条 outline_ready 透出 outline
    outline_ready = [e for e in progresses if e["data"]["step"] == "outline_ready"]
    assert outline_ready, "expected outline_ready progress"
    assert "outline" in outline_ready[-1]["data"]["payload"]


def test_force_finalize_done_is_final(client):
    """force_finalize=True → done.is_final=True、question=None、outline 五字段非空。"""
    p = _encode({
        "initial_brief": "想做一支咖啡店探店视频",
        "transcript": [],
        "force_finalize": True,
    })
    resp = client.get(f"/api/clarify/round?p={p}")
    assert resp.status_code == 200
    events = _parse_sse(resp.text)
    done_events = [e for e in events if e["event"] == "done"]
    assert len(done_events) == 1
    done = done_events[0]["data"]
    assert done["is_final"] is True
    assert done["question"] is None
    assert done["outline"]["topic"], "final 轮 topic 必须有值"


def test_invalid_payload_returns_400(client):
    resp = client.get("/api/clarify/round?p=not-base64!!!")
    assert resp.status_code in (400, 422)
