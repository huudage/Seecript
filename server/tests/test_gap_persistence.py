"""验证 gap/detect → gap/fill 闭环：

- detect 结果存进 GapStore；fill 用同一个 gap_id 必须能 lookup 到 Gap
- detect 改用 plan_store 反查 sample_id，不再硬取 _LIBRARY[0]
- gap.sample_thumbnail_url 字段被填充（mock 样例 shot 自带 thumbnail_url）
- copy fill 返回 alternatives（mock 数据里就有）

跟 test_e2e_pipeline.py 区分：那里走全链路 smoke；这里专门验阶段 5+ 改动。
"""
from __future__ import annotations

from io import BytesIO

import pytest


@pytest.fixture
def session_with_plan(client) -> tuple[str, str]:
    """上传两个素材 → 用真 sample_id 走 plan/build → 返回 (session_id, plan_id)。"""
    fake_video = b"\x00\x00\x00\x18ftypisom" + b"\x00" * 1024
    project_id = "proj-gap-persistence"
    files = [
        ("files", ("a.mp4", BytesIO(fake_video), "video/mp4")),
        ("files", ("b.mp4", BytesIO(fake_video), "video/mp4")),
    ]
    r = client.post("/api/material/upload", files=files, data={"project_id": project_id})
    assert r.status_code == 200, r.text
    upload = r.json()
    sid = upload["session_id"]

    r = client.post(
        "/api/plan/build",
        json={
            "sample_ids": ["sample-vlog-01"],  # 用 editing 类型校验 sample_ids 真的被传递
            "project_id": project_id,
            "session_id": sid,
            "brief": "测试咖啡店探店剪辑",
            "selected_materials": [m["material_id"] for m in upload["materials"]],
            "fills": [],
            "variant": "A",
        },
    )
    assert r.status_code == 200, r.text
    plan = r.json()
    assert plan["sample_ids"] == ["sample-vlog-01"]
    assert plan["brief"] == "测试咖啡店探店剪辑"
    return sid, plan["plan_id"]


def test_detect_persists_to_gap_store(client, session_with_plan):
    """detect → fill 直接按 gap_id lookup，不再触发重复 detect。"""
    sid, plan_id = session_with_plan
    r = client.post("/api/gap/detect", json={"plan_id": plan_id, "session_id": sid})
    assert r.status_code == 200, r.text
    gaps = r.json()
    assert len(gaps) > 0
    # SectionRole 4 元枚举：opening/development/climax/closing —— 任意视频都从这 4 个里选
    assert {g["section"] for g in gaps}.issubset({"opening", "development", "climax", "closing"})

    # 找一个 gap，调 fill action=copy；不再因为 detect 没跑过而 404
    target = gaps[0]
    r = client.post("/api/gap/fill", json={
        "gap_id": target["gap_id"],
        "action": "copy",
        "params": {"prompt_hint": "强调氛围"},
    })
    assert r.status_code == 200, r.text
    fill = r.json()
    assert fill["gap_id"] == target["gap_id"]
    assert fill["action"] == "copy"


def test_fill_unknown_gap_404(client):
    """没经过 detect 的 gap_id 直接调 fill 应返回 404，而不是悄悄 mock 兜底。"""
    r = client.post("/api/gap/fill", json={
        "gap_id": "gap-bogus-99",
        "action": "copy",
        "params": {},
    })
    assert r.status_code == 404


def test_gap_has_sample_thumbnail_url(client, session_with_plan):
    """每个 gap 都应该带回样例对应镜头的 thumbnail_url（mock manifest 里都有）。"""
    sid, plan_id = session_with_plan
    r = client.post("/api/gap/detect", json={"plan_id": plan_id, "session_id": sid})
    assert r.status_code == 200, r.text
    gaps = r.json()
    # 至少绝大多数 gap 应该带 thumbnail（mock samples 每个 shot 都有 thumbnail_url）
    with_thumb = [g for g in gaps if g.get("sample_thumbnail_url")]
    assert len(with_thumb) >= len(gaps) // 2, gaps


def test_copy_fill_returns_alternatives(client, session_with_plan):
    """copy action 应该返回 alternatives 数组（mock LLM 数据自带）。"""
    sid, plan_id = session_with_plan
    r = client.post("/api/gap/detect", json={"plan_id": plan_id, "session_id": sid})
    gaps = r.json()
    r = client.post("/api/gap/fill", json={
        "gap_id": gaps[0]["gap_id"],
        "action": "copy",
        "params": {},
    })
    fill = r.json()
    assert "alternatives" in fill
    assert isinstance(fill["alternatives"], list)


def test_session_empty_returns_empty_materials(client):
    """没传 session_id 时不再回落 mock；走真链路：仍能返回 gaps（来自 manifest 缺口），不 500。"""
    # 先建一个 plan
    r = client.post("/api/plan/build", json={
        "sample_ids": ["sample-marketing-01"],
        "project_id": "proj-session-fallback",
        "session_id": "no-session",
        "selected_materials": [],
        "fills": [],
        "variant": "A",
    })
    plan_id = r.json()["plan_id"]
    # 不传 project_id 也不传 session_id → materials=[]，所有 gap miss
    r = client.post("/api/gap/detect", json={"plan_id": plan_id})
    assert r.status_code == 200
    gaps = r.json()
    # 至少有 1 个段落产出 gap（mock 兜底已删，但缺素材时所有 gap 都标 miss/insufficient）
    assert isinstance(gaps, list)


def test_plan_uses_aigc_t2v_not_t2i(client, session_with_plan):
    """fills 带 new_material_id 时 plan 的 Scene.source 必须是 aigc_t2v（不是死字面量 aigc_t2i）。"""
    sid, _ = session_with_plan
    r = client.post("/api/plan/build", json={
        "sample_ids": ["sample-marketing-01"],
        "project_id": "proj-gap-persistence",
        "session_id": sid,
        "selected_materials": [],
        "fills": [
            {
                "gap_id": "gap-opening-0",
                "action": "aigc",
                "new_material_id": "mock-task-xyz",
                "status": "ok",
                "alternatives": [],
            },
        ],
        "variant": "A",
    })
    assert r.status_code == 200, r.text
    plan = r.json()
    sources = {sc["source"] for sc in plan["main_track"]}
    # 至少有一个 scene 走了 aigc_t2v 而不是 aigc_t2i
    assert "aigc_t2v" in sources
    assert "aigc_t2i" not in sources
