"""Clarify 第 3 轮硬上限测试 — transcript=2 + force_finalize=False 仍强制 is_final=True。"""
from __future__ import annotations

import base64
import json


def _encode(payload: dict) -> str:
    raw = json.dumps(payload).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _parse_sse(text: str) -> list[dict]:
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


def test_third_round_forces_finalize_even_when_force_false(client):
    """transcript 长度 2 → round_no=3 → 服务端 cap 强制 is_final=True。"""
    p = _encode({
        "initial_brief": "想做一支咖啡店探店视频",
        "transcript": [
            {"question": "受众是?", "answer": "18-25 岁女性"},
            {"question": "目的是?", "answer": "种草"},
        ],
        "force_finalize": False,
    })
    resp = client.get(f"/api/clarify/round?p={p}")
    assert resp.status_code == 200
    events = _parse_sse(resp.text)
    done = next(e for e in events if e["event"] == "done")["data"]
    assert done["round"] == 3
    assert done["is_final"] is True
    assert done["question"] is None
    assert done["final_brief"], "第 3 轮 final_brief 必须有内容"
