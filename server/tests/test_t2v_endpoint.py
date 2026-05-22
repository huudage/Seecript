"""End-to-end tests for /api/t2v/* (mock provider)."""
from __future__ import annotations

import time


def test_submit_returns_task_id(client):
    """Happy path: valid prompt → 200 + task_id."""
    body = {
        "prompt": "一杯冰美式咖啡，木桌、暖光台灯、清晨光线，水雾缓慢冒出。",
        "size": "720x1280",
        "quality": "speed",
        "with_audio": False,
    }
    r = client.post("/api/t2v/submit", json=body)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["task_id"].startswith("mock-")
    assert data["status"] == "pending"
    assert data["provider"] == "mock"
    assert data["elapsed_ms"] >= 0


def test_submit_rejects_empty_prompt(client):
    body = {"prompt": "   ", "size": "720x1280"}
    r = client.post("/api/t2v/submit", json=body)
    assert r.status_code in (400, 422)  # 422 if Pydantic catches first, 400 from router


def test_submit_rejects_oversize_prompt(client):
    """Pydantic schema caps at 500 chars — anything bigger should 422."""
    body = {"prompt": "啊" * 600, "size": "720x1280"}
    r = client.post("/api/t2v/submit", json=body)
    assert r.status_code == 422


def test_submit_shot_preview_mode_returns_200(client):
    """shot_preview_mode merges server-side; still mock round-trip."""
    body = {
        "prompt": "竖屏桌面，产品特写，柔和侧光。",
        "size": "720x1280",
        "quality": "speed",
        "with_audio": False,
        "shot_preview_mode": True,
    }
    r = client.post("/api/t2v/submit", json=body)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["task_id"].startswith("mock-")


def test_submit_accepts_duration_seconds(client):
    body = {
        "prompt": "一杯冰美式咖啡，木桌、暖光台灯、清晨光线，水雾缓慢冒出。",
        "size": "720x1280",
        "quality": "speed",
        "with_audio": False,
        "duration_seconds": 10,
        "shot_preview_mode": False,
    }
    r = client.post("/api/t2v/submit", json=body)
    assert r.status_code == 200, r.text


def test_submit_rejects_bad_size(client):
    """size must be from the merged CogVideoX-3 / CogVideoX-2 enum (schemas.T2VSize)."""
    body = {"prompt": "test prompt", "size": "9999x9999"}
    r = client.post("/api/t2v/submit", json=body)
    assert r.status_code == 422


def test_query_unknown_task_returns_404(client):
    r = client.get("/api/t2v/query/mock-does-not-exist-1234")
    assert r.status_code == 404


def test_full_lifecycle_pending_then_succeeded(client):
    """Submit → poll once (pending) → wait → poll again (succeeded).

    This is the smoke test the deploy script should run to verify a green
    deployment. It exercises the entire two-call dance without any LLM key.
    """
    body = {
        "prompt": "测试视频生成完整生命周期",
        "size": "720x1280",
        "quality": "speed",
        "with_audio": False,
    }
    r = client.post("/api/t2v/submit", json=body)
    assert r.status_code == 200
    task_id = r.json()["task_id"]

    # First poll: should be pending (mock duration set to 0.1s in conftest,
    # but we read immediately so we should still catch PROCESSING — the mock
    # client checks elapsed time on each query call).
    r1 = client.get(f"/api/t2v/query/{task_id}")
    assert r1.status_code == 200
    # Could be either pending or succeeded depending on timing; both are valid.
    assert r1.json()["status"] in ("pending", "succeeded")

    # Wait past mock duration, then poll again → must be succeeded.
    time.sleep(0.25)
    r2 = client.get(f"/api/t2v/query/{task_id}")
    assert r2.status_code == 200
    final = r2.json()
    assert final["status"] == "succeeded"
    assert final["video_url"] is not None
    assert final["task_id"] == task_id
    assert final["provider"] == "mock"


def test_health_includes_t2v_provider(client):
    """Make sure /api/health surfaces T2V provider so ops can spot misconfigs."""
    r = client.get("/api/health")
    assert r.status_code == 200
    data = r.json()
    assert data["t2v_provider"] == "mock"
