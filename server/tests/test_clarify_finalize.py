"""Clarify finalize JSON 端点测试（v2 · 不再调 LLM，直接拼字段）。"""
from __future__ import annotations


def test_finalize_with_outline_returns_stitched_brief(client):
    """传入完整五件套 outline → final_brief 按【主题/内容/受众/目的/语气】顺序拼接。"""
    resp = client.post(
        "/api/clarify/finalize",
        json={
            "outline": {
                "topic": "咖啡店探店",
                "content": "强反差画面 + 一句金句",
                "audience": "18-30 岁通勤族",
                "goal": "种草",
                "tone": "轻松真诚",
            },
            "initial_brief": "想做一支咖啡店探店视频",
            "transcript": [],
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    fb = body["final_brief"]
    assert "【主题】咖啡店探店" in fb
    assert "【内容】强反差画面" in fb
    assert "【受众】18-30 岁通勤族" in fb
    assert "【目的】种草" in fb
    assert "【语气】轻松真诚" in fb
    # outline 原样回传
    assert body["outline"]["topic"] == "咖啡店探店"
    # round 推算 = transcript 长度 + 1
    assert body["round"] == 1


def test_finalize_partial_outline_only_writes_present_fields(client):
    """只填了 topic + goal → final_brief 只含两段，不留空头标签。"""
    resp = client.post(
        "/api/clarify/finalize",
        json={
            "outline": {
                "topic": "咖啡店探店",
                "content": None,
                "audience": None,
                "goal": "种草",
                "tone": None,
            },
            "initial_brief": "",
            "transcript": [],
        },
    )
    assert resp.status_code == 200
    fb = resp.json()["final_brief"]
    assert "【主题】" in fb
    assert "【目的】" in fb
    assert "【内容】" not in fb
    assert "【受众】" not in fb
    assert "【语气】" not in fb


def test_finalize_empty_outline_falls_back_to_initial_brief(client):
    """outline 全空 + initial_brief 有值 → 把 initial_brief 当 final_brief。"""
    resp = client.post(
        "/api/clarify/finalize",
        json={
            "outline": {},
            "initial_brief": "我想做一支咖啡视频",
            "transcript": [],
        },
    )
    assert resp.status_code == 200
    assert resp.json()["final_brief"] == "我想做一支咖啡视频"


def test_finalize_empty_everything_returns_400(client):
    """outline 全空且 initial_brief 也空 → 拒绝。"""
    resp = client.post(
        "/api/clarify/finalize",
        json={"outline": {}, "initial_brief": "", "transcript": []},
    )
    assert resp.status_code == 400


def test_finalize_round_clamps_to_3(client):
    """transcript 长度 2 + 后续 → round 不会超过 3。"""
    resp = client.post(
        "/api/clarify/finalize",
        json={
            "outline": {"topic": "x"},
            "initial_brief": "x",
            "transcript": [
                {"question": "受众？", "answer": "年轻人"},
                {"question": "目的？", "answer": "种草"},
                {"question": "语气？", "answer": "活泼"},
            ],
        },
    )
    assert resp.status_code == 200
    assert resp.json()["round"] == 3
