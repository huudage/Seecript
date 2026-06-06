"""Clarify finalize JSON 端点测试。"""
from __future__ import annotations


def test_finalize_returns_final_brief(client):
    resp = client.post(
        "/api/clarify/finalize",
        json={
            "initial_brief": "想做一支咖啡店探店视频",
            "transcript": [],
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["final_brief"], "final_brief 不能为空"
    assert body["round"] >= 1


def test_finalize_with_transcript(client):
    resp = client.post(
        "/api/clarify/finalize",
        json={
            "initial_brief": "想做一支咖啡店探店视频",
            "transcript": [
                {"question": "目标受众是?", "answer": "18-25 岁女性"},
                {"question": "想种草还是带货?", "answer": "种草"},
            ],
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["final_brief"]
    # transcript 长度 2 → round 推到第 3 轮
    assert body["round"] == 3
