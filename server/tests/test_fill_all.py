"""一键 AI 生成全缺口（POST /api/gap/fill-all）端到端测试。

覆盖：
1. plan 不存在 → 404
2. 没跑过 detect → 200 但 fills=[]，stopped_reason 提示
3. 全 ok 缺口 → 200 但 fills=[]，stopped_reason 提示无需生成
4. 顺序成功跑完所有 status≠ok 的 gap，每个 fill 都是 aigc + ok
5. 中途某段失败时立即停止，返回 failed_gap_id 与已完成 fills

mock 说明：
- T2V mock 默认 8s 延迟会让批量任务超时；测试里 monkeypatch fill_gap 直接桩掉，
  逐条返回可控 FillResult，避免依赖 Seedance + ffmpeg 真实链式
- 同时 monkeypatch 尾帧抽取（虽然桩了 fill_gap 后用不上，但保险）
"""
from __future__ import annotations

from io import BytesIO

import pytest

from app.schemas import FillResult
from app.services.materials import gap_store


@pytest.fixture
def session_with_plan(client) -> tuple[str, str]:
    fake = b"\x00\x00\x00\x18ftypisom" + b"\x00" * 1024
    project_id = "proj-fill-all-test"
    files = [("files", ("a.mp4", BytesIO(fake), "video/mp4"))]
    r = client.post("/api/material/upload", files=files, data={"project_id": project_id})
    sid = r.json()["session_id"]
    r = client.post(
        "/api/plan/build",
        json={
            "sample_ids": ["sample-marketing-01"],
            "project_id": project_id,
            "session_id": sid,
            "brief": "fill-all 测试",
            "video_goal": "30 秒说清差异化",
            "selected_materials": [],
            "fills": [],
            "variant": "A",
        },
    )
    return sid, r.json()["plan_id"]


def test_fill_all_404_for_unknown_plan(client):
    r = client.post("/api/gap/fill-all", json={"plan_id": "plan-nope"})
    assert r.status_code == 404


def test_fill_all_no_detect_returns_empty(client, session_with_plan):
    """plan 已建但没跑 /gap/detect → fills=[] + stopped_reason 提示。"""
    _, plan_id = session_with_plan
    r = client.post("/api/gap/fill-all", json={"plan_id": plan_id})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["plan_id"] == plan_id
    assert body["fills"] == []
    assert body["stopped_reason"] and "detect" in body["stopped_reason"]


def test_fill_all_all_ok_returns_empty(client, session_with_plan, monkeypatch):
    """全部 gap 都 ok 时跳过生成。"""
    sid, plan_id = session_with_plan
    # detect 一次写入 gap_store，然后强行把所有 gap status 改 ok
    r = client.post("/api/gap/detect", json={"plan_id": plan_id, "session_id": sid})
    gaps = r.json()
    assert gaps
    for g in gap_store.list_by_plan(plan_id):
        g.status = "ok"

    r = client.post("/api/gap/fill-all", json={"plan_id": plan_id})
    body = r.json()
    assert body["fills"] == []
    assert body["stopped_reason"] and "无需" in body["stopped_reason"]


def test_fill_all_sequential_success(client, session_with_plan, monkeypatch):
    """顺序成功：每个 status≠ok 的 gap 都被 fill_gap aigc 调一次。"""
    sid, plan_id = session_with_plan
    r = client.post("/api/gap/detect", json={"plan_id": plan_id, "session_id": sid})
    gaps = r.json()
    pending = [g for g in gaps if g["status"] != "ok"]
    assert pending, "至少要有一个非 ok gap 才能测批量"

    calls: list[str] = []

    async def fake_fill(gap, action, params):
        calls.append(gap.gap_id)
        assert action == "aigc"
        # duration 必须从 AdaptedSection 注入
        assert "duration_seconds" in params
        return FillResult(
            gap_id=gap.gap_id, action="aigc",
            new_material_id=f"task-{gap.gap_id}",
            video_urls=[f"/aigc/{gap.gap_id}.mp4"],
            cover_url=f"/aigc/{gap.gap_id}.jpg",
            chunks_count=1,
            chunk_task_ids=[f"task-{gap.gap_id}"],
            status="ok",
            note="mock 成功",
        )

    monkeypatch.setattr("app.routers.gap.fill_gap", fake_fill)

    r = client.post("/api/gap/fill-all", json={"plan_id": plan_id})
    body = r.json()
    assert body["failed_gap_id"] is None
    assert body["stopped_reason"] is None
    assert len(body["fills"]) == len(pending)
    # 调用顺序与 pending 顺序一致
    assert calls == [g["gap_id"] for g in pending]
    for f in body["fills"]:
        assert f["action"] == "aigc"
        assert f["status"] == "ok"
        assert f["video_urls"]


def test_fill_all_stops_on_first_failure(client, session_with_plan, monkeypatch):
    """中途失败立即停：只回填第一段成功 + 第二段失败，第三段不再调。"""
    sid, plan_id = session_with_plan
    r = client.post("/api/gap/detect", json={"plan_id": plan_id, "session_id": sid})
    pending = [g for g in r.json() if g["status"] != "ok"]
    assert len(pending) >= 2, "需要 ≥2 个非 ok gap 才能验证 stop-on-failure"

    seen: list[str] = []

    async def fake_fill(gap, action, params):
        seen.append(gap.gap_id)
        if len(seen) == 1:
            return FillResult(
                gap_id=gap.gap_id, action="aigc",
                new_material_id="task-1",
                video_urls=["/aigc/ok.mp4"],
                chunks_count=1, chunk_task_ids=["task-1"],
                status="ok", note="mock 成功",
            )
        return FillResult(
            gap_id=gap.gap_id, action="aigc",
            status="warn", chunks_count=0,
            note="Seedance 配额耗尽",
        )

    monkeypatch.setattr("app.routers.gap.fill_gap", fake_fill)

    r = client.post("/api/gap/fill-all", json={"plan_id": plan_id})
    body = r.json()
    # 只调用了前两段：第一段 ok，第二段 warn → 立即停
    assert len(seen) == 2, f"应在第 2 段失败后停，实际调了 {len(seen)} 次"
    assert body["failed_gap_id"] == pending[1]["gap_id"]
    assert body["stopped_reason"] and "失败" in body["stopped_reason"]
    assert len(body["fills"]) == 2
    assert body["fills"][0]["status"] == "ok"
    assert body["fills"][1]["status"] == "warn"
