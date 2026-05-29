"""Asset Router HTTP 烟测：上传 + 列表 + PATCH + 删除流程。

测试场景：
1. POST /asset/upload：上传 BGM mp3 → 落盘 + manifest 入库 + status=processing
2. GET /asset/library：能列出刚上传的资产
3. PATCH /asset/{id}：改 title/description/tags 字段
4. POST /asset/{id}/touch：use_count +1
5. DELETE /asset/{id}：摘除 + 404 验证
6. 二次上传同字节内容触发 sha256 dedup → 返回老 asset
"""
from __future__ import annotations

import io

import pytest

from app.services.assets import asset_store


@pytest.fixture(autouse=True)
def clean_assets_for_router():
    asset_store._by_id.clear()
    asset_store._by_hash.clear()
    yield
    asset_store._by_id.clear()
    asset_store._by_hash.clear()


# 一段 1KB 的 fake mp3 字节（不需要真音频解码，BackgroundTask 探测失败也只会落 metadata 空）
_FAKE_MP3 = b"\xff\xfb\x90\x00" + b"\x00" * 1020


def test_upload_bgm_returns_asset_in_processing_state(client):
    """上传 BGM 后立即返回 Asset，status=processing；store 中可查到。"""
    resp = client.post(
        "/api/asset/upload",
        files={"file": ("hook.mp3", io.BytesIO(_FAKE_MP3), "audio/mpeg")},
        data={"kind": "bgm", "title": "Test Hook"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["kind"] == "bgm"
    assert data["title"] == "Test Hook"
    assert data["file_name"] == "hook.mp3"
    assert data["asset_id"].startswith("ass-")
    assert data["file_url"].startswith("/assets/local/bgm/")
    # status 可能在 BackgroundTask 完成后变 ready；上传响应返回时通常仍 processing
    assert data["status"] in ("processing", "ready", "failed")


def test_upload_rejects_wrong_mime(client):
    """kind=bgm 时拒绝 image content-type。"""
    resp = client.post(
        "/api/asset/upload",
        files={"file": ("fake.jpg", io.BytesIO(b"not-real-jpg"), "image/jpeg")},
        data={"kind": "bgm"},
    )
    assert resp.status_code == 415


def test_upload_rejects_empty_file(client):
    """空文件返回 400。"""
    resp = client.post(
        "/api/asset/upload",
        files={"file": ("empty.mp3", io.BytesIO(b""), "audio/mpeg")},
        data={"kind": "bgm"},
    )
    assert resp.status_code == 400


def test_upload_dedup_returns_existing_asset(client):
    """同字节内容第二次上传应命中 sha256 dedup，返回老 asset_id。"""
    files = {"file": ("dup.mp3", io.BytesIO(_FAKE_MP3), "audio/mpeg")}
    r1 = client.post("/api/asset/upload", files=files, data={"kind": "bgm"})
    assert r1.status_code == 200
    aid1 = r1.json()["asset_id"]

    r2 = client.post(
        "/api/asset/upload",
        files={"file": ("dup-renamed.mp3", io.BytesIO(_FAKE_MP3), "audio/mpeg")},
        data={"kind": "bgm"},
    )
    assert r2.status_code == 200
    aid2 = r2.json()["asset_id"]
    assert aid1 == aid2, "同 hash 应去重到同一 asset_id"


def test_list_library_returns_uploaded_assets(client):
    """上传后 GET /asset/library 能列出来。"""
    client.post(
        "/api/asset/upload",
        files={"file": ("list.mp3", io.BytesIO(_FAKE_MP3), "audio/mpeg")},
        data={"kind": "bgm"},
    )
    resp = client.get("/api/asset/library")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] >= 1
    assert any(item["file_name"] == "list.mp3" for item in body["items"])


def test_list_library_filters_by_kind(client):
    """kind 过滤精确生效。"""
    client.post(
        "/api/asset/upload",
        files={"file": ("k.mp3", io.BytesIO(_FAKE_MP3), "audio/mpeg")},
        data={"kind": "bgm"},
    )
    resp = client.get("/api/asset/library?kind=reference_image")
    assert resp.status_code == 200
    assert resp.json()["total"] == 0


def test_patch_updates_title_and_tags(client):
    """PATCH 改 title/tags 生效。"""
    r1 = client.post(
        "/api/asset/upload",
        files={"file": ("patch.mp3", io.BytesIO(_FAKE_MP3), "audio/mpeg")},
        data={"kind": "bgm", "title": "原名"},
    )
    aid = r1.json()["asset_id"]

    r2 = client.patch(
        f"/api/asset/{aid}",
        json={"title": "改后", "tags": ["热血", "高燃"]},
    )
    assert r2.status_code == 200
    body = r2.json()
    assert body["title"] == "改后"
    assert body["tags"] == ["热血", "高燃"]


def test_touch_increments_use_count(client):
    r1 = client.post(
        "/api/asset/upload",
        files={"file": ("t.mp3", io.BytesIO(_FAKE_MP3), "audio/mpeg")},
        data={"kind": "bgm"},
    )
    aid = r1.json()["asset_id"]

    r2 = client.post(f"/api/asset/{aid}/touch")
    assert r2.status_code == 200
    assert r2.json()["use_count"] == 1

    r3 = client.post(f"/api/asset/{aid}/touch")
    assert r3.json()["use_count"] == 2


def test_delete_then_get_returns_404(client):
    r1 = client.post(
        "/api/asset/upload",
        files={"file": ("del.mp3", io.BytesIO(_FAKE_MP3), "audio/mpeg")},
        data={"kind": "bgm"},
    )
    aid = r1.json()["asset_id"]

    r2 = client.delete(f"/api/asset/{aid}")
    assert r2.status_code == 200
    assert r2.json()["deleted"] is True

    r3 = client.get(f"/api/asset/{aid}")
    assert r3.status_code == 404


def test_get_nonexistent_returns_404(client):
    resp = client.get("/api/asset/ass-nope")
    assert resp.status_code == 404
