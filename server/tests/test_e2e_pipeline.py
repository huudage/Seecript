"""端到端联调测试：覆盖 7 个模块的完整数据流（mock 模式）。

走一遍：
  library → decompose → material/upload → plan/build → gap/detect → gap/fill →
  render/submit + stream → edit/apply

所有上游都走 mock provider，不消耗任何 API 配额，纯校验路由 + 数据流契约。
"""
from __future__ import annotations

import json
import time
from io import BytesIO

import pytest


def _build_plan(client) -> dict:
    """共用 helper：上传 → 构建 → 检测 → 选择性 fill；返回最新 plan dict。"""
    fake_video = b"\x00\x00\x00\x18ftypisom" + b"\x00" * 2048
    project_id = "proj-e2e-test"
    files = [
        ("files", ("clip-a.mp4", BytesIO(fake_video), "video/mp4")),
        ("files", ("clip-b.mp4", BytesIO(fake_video), "video/mp4")),
    ]
    r = client.post(
        "/api/material/upload",
        files=files,
        data={"project_id": project_id},
    )
    assert r.status_code == 200, r.text
    upload = r.json()
    session_id = upload["session_id"]
    material_ids = [m["material_id"] for m in upload["materials"]]

    r = client.post(
        "/api/plan/build",
        json={
            "sample_ids": ["sample-marketing-01"],
            "project_id": project_id,
            "session_id": session_id,
            "brief": "新品发布——突出差异化卖点",
            "video_goal": "30 秒内说清产品差异化卖点，面向初次接触的用户，节奏紧凑",
            "settings": {
                "target_duration_seconds": 45,
                "target_platform": "douyin",
                "tone": "tight_hype",
                "cta": "点击主页预约",
                "keywords": ["差异化", "新品"],
            },
            "selected_materials": material_ids,
            "fills": [],
            "variant": "A",
        },
    )
    assert r.status_code == 200, r.text
    plan = r.json()
    assert plan["plan_id"].startswith("plan-")
    # video_goal 必须回写到 plan；adapted_sections 必须非空且首末 role 合规
    assert plan["video_goal"] and "差异化" in plan["video_goal"]
    # settings 必须原样回写（render/edit 阶段复用）
    assert plan["settings"]["target_duration_seconds"] == 45
    assert plan["settings"]["cta"] == "点击主页预约"
    assert plan["settings"]["keywords"] == ["差异化", "新品"]
    adapted = plan["adapted_sections"]
    assert isinstance(adapted, list) and len(adapted) >= 3
    assert adapted[0]["role"] == "opening" and adapted[-1]["role"] == "closing"
    for sec in adapted:
        assert sec["section_id"] and sec["content_description"], sec
        # 每段都要有 duration_seconds（驱动 Scene.duration + AIGC 链式分段）
        assert 2.0 <= sec["duration_seconds"] <= 30.0, sec
    # 各段时长之和应贴近目标总时长（45s，允许 ±25%）
    total = sum(s["duration_seconds"] for s in adapted)
    assert abs(total - 45) / 45 <= 0.25, f"adapted 总时长 {total} 偏离目标 45s 过大"
    return plan


def test_library_and_manifest(client):
    """模块 1：素材库列表 + 单样例 manifest。三类型 video_type 都走通。"""
    r = client.get("/api/library")
    assert r.status_code == 200
    items = r.json()
    assert len(items) >= 3
    # 库里至少各有一个 marketing / editing / motion_graph
    seen_types = {it["video_type"] for it in items}
    assert seen_types == {"marketing", "editing", "motion_graph"}

    # 段落 role 必须落在 SectionRole 4 元枚举里，且首尾恰为 opening/closing
    allowed_roles = {"opening", "development", "climax", "closing"}
    for item in items:
        r = client.get(f"/api/sample/{item['id']}/manifest")
        assert r.status_code == 200, f"manifest {item['id']}: {r.text}"
        manifest = r.json()
        assert manifest["sample_id"] == item["id"]
        assert manifest["video_type"] == item["video_type"]
        assert len(manifest["shots"]) > 0
        roles = [s["role"] for s in manifest["sections"]]
        assert set(roles).issubset(allowed_roles), (
            f"{item['id']} sections {roles} 含非法 role"
        )
        assert roles, f"{item['id']} 段落为空"
        assert roles[0] == "opening", f"{item['id']} 首段应为 opening，实际 {roles[0]}"
        assert roles[-1] == "closing", f"{item['id']} 末段应为 closing，实际 {roles[-1]}"
        # climax 最多 1 段
        assert sum(1 for r_ in roles if r_ == "climax") <= 1
        # motion_graph 是纯 BGM（无口播）；其它视频 has_voice 取决于真实音轨，不在此处强约束
        if item["video_type"] == "motion_graph":
            assert manifest["has_voice"] is False
        else:
            assert isinstance(manifest["has_voice"], bool)


def test_material_upload_and_plan_build(client):
    """模块 3+5：上传素材 → 构建 Plan → 缺口识别 → 缺口补全。"""
    plan = _build_plan(client)
    assert len(plan["main_track"]) > 0
    assert plan["session_id"]

    r = client.post("/api/gap/detect", json={"plan_id": plan["plan_id"]})
    assert r.status_code == 200
    gaps = r.json()
    assert isinstance(gaps, list)

    # 每个 gap.section_id 都必须指回 plan.adapted_sections 里的某段
    adapted_ids = {s["section_id"] for s in plan["adapted_sections"]}
    for g in gaps:
        assert g["section_id"] in adapted_ids, (
            f"gap {g['gap_id']} section_id={g['section_id']} 不在 {adapted_ids}"
        )

    target_gap = next((g for g in gaps if g["status"] != "ok"), gaps[0] if gaps else None)
    if target_gap is not None:
        r = client.post(
            "/api/gap/fill",
            json={
                "gap_id": target_gap["gap_id"],
                "action": "copy",
                "params": {"prompt_hint": target_gap["requirement"]},
            },
        )
        assert r.status_code == 200, r.text
        fill = r.json()
        assert fill["gap_id"] == target_gap["gap_id"]
        assert fill["action"] == "copy"


def test_render_submit_and_stream(client):
    """模块 5+6：渲染流水线 SSE 全程跑通。"""
    plan = _build_plan(client)
    plan_id = plan["plan_id"]

    r = client.post("/api/render/submit", json={"plan_id": plan_id, "variant": "A"})
    assert r.status_code == 200, r.text
    job_id = r.json()["job_id"]

    # 注：TestClient 把 BackgroundTasks 同步跑在 response 之后；
    # 真到 stream 时 job 通常已结束，subscribe 只回放 last_event。
    # 这里只断言「最终终态正确」，progress 流的实时性由前端集成验证。
    deadline = time.time() + 60
    saw_done = False
    last_event_kind = None
    done_payload: dict | None = None

    with client.stream("GET", f"/api/render/stream?job_id={job_id}") as resp:
        assert resp.status_code == 200
        event_name: str | None = None
        for raw in resp.iter_lines():
            if not raw:
                continue
            line = raw if isinstance(raw, str) else raw.decode("utf-8", errors="ignore")
            if line.startswith("event:"):
                event_name = line.split(":", 1)[1].strip()
                last_event_kind = event_name
            elif line.startswith("data:") and event_name:
                data = json.loads(line.split(":", 1)[1].strip())
                if event_name == "done":
                    saw_done = True
                    done_payload = data.get("payload") or data
                    break
                elif event_name == "error":
                    pytest.fail(f"render errored: {data}")
                elif event_name == "progress":
                    assert "step" in data and "percent" in data
            if time.time() > deadline:
                pytest.fail("render SSE timeout")

    assert saw_done, f"never saw done (last event = {last_event_kind})"
    assert done_payload and "video_url" in done_payload
    assert done_payload["plan_id"] == plan_id


def test_edit_apply_creates_new_plan(client):
    """模块 7：自然语言编辑 → 新 plan_id + plan_store 持久化。"""
    plan = _build_plan(client)
    plan_id = plan["plan_id"]

    r = client.post(
        "/api/edit/apply",
        json={
            "plan_id": plan_id,
            "track": "voice",
            "instruction": "把开场 narration 改得更口语化",
            "marks": [],
        },
    )
    assert r.status_code == 200, r.text
    new_plan = r.json()
    assert new_plan["plan_id"] != plan_id
    assert new_plan["plan_id"].startswith("plan-")
    assert len(new_plan["main_track"]) == len(plan["main_track"])

    # 新 plan 必须能再次被检索（plan_store 持久化）
    r = client.post("/api/render/submit", json={"plan_id": new_plan["plan_id"], "variant": "A"})
    assert r.status_code == 200, r.text


def test_edit_apply_rejects_unknown_plan(client):
    r = client.post(
        "/api/edit/apply",
        json={"plan_id": "plan-nonexistent", "track": "voice", "instruction": "x", "marks": []},
    )
    assert r.status_code == 404


def test_render_rejects_unknown_plan(client):
    r = client.post("/api/render/submit", json={"plan_id": "plan-nonexistent", "variant": "A"})
    assert r.status_code == 404
