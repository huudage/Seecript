"""HTTP 烟测：POST /material/clone-from-asset —— 把「我的素材」资产库里的
reference_image / reference_video 克隆为内容素材库的 Material。

覆盖：
- reference_image asset → 复制文件 + 缩略图复用文件本体 + Material 入库
- reference_video asset → 复制文件 + 触发 PySceneDetect dispatch（mock 下 skip）
- bgm asset → 拒绝（skipped）
- 不存在 asset_id → skipped
- 跨项目（asset.owner != target project）→ skipped
"""
from __future__ import annotations

import shutil
import time
from pathlib import Path

import pytest

from app.config import get_settings
from app.schemas import Asset
from app.services.assets import asset_store
from app.services.materials import material_store


_TEST_PROJECT = "proj-clone-from-asset-test"


def _seed_asset(
    kind: str,
    *,
    asset_id: str,
    file_name: str,
    body: bytes,
    duration_seconds: float | None = None,
    make_thumb: bool = False,
) -> Asset:
    settings = get_settings()
    kind_dir = settings.log_dir.parent / "var" / "assets" / _TEST_PROJECT / kind
    kind_dir.mkdir(parents=True, exist_ok=True)
    ext = "jpg" if kind == "reference_image" else ("mp4" if kind == "reference_video" else "mp3")
    file_path = kind_dir / f"{asset_id}.{ext}"
    file_path.write_bytes(body)

    metadata: dict = {}
    if duration_seconds is not None:
        metadata["duration_seconds"] = duration_seconds
    if make_thumb and kind == "reference_video":
        thumb_path = kind_dir / f"{asset_id}.thumb.jpg"
        thumb_path.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 100)  # fake JPEG header
        metadata["thumbnail_url"] = f"/assets/{_TEST_PROJECT}/{kind}/{thumb_path.name}"

    asset = Asset(
        asset_id=asset_id,
        owner=_TEST_PROJECT,
        kind=kind,  # type: ignore[arg-type]
        file_name=file_name,
        file_url=f"/assets/{_TEST_PROJECT}/{kind}/{file_path.name}",
        file_size=len(body),
        content_hash="testhash" + asset_id,
        mime="image/jpeg" if kind == "reference_image" else "video/mp4" if kind == "reference_video" else "audio/mpeg",
        title=file_name,
        description="",
        tags=["test-tag"],
        metadata=metadata,
        status="ready",
        error=None,
        created_at=time.time(),
        last_used_at=None,
        use_count=0,
    )
    asset_store.upsert(asset)
    return asset


@pytest.fixture(autouse=True)
def _clean():
    asset_store._states.pop(_TEST_PROJECT, None)
    asset_store._owner_by_asset = {
        aid: owner for aid, owner in asset_store._owner_by_asset.items() if owner != _TEST_PROJECT
    }
    material_store._states.pop(_TEST_PROJECT, None) if hasattr(material_store, "_states") else None

    settings = get_settings()
    for sub in (
        settings.log_dir.parent / "var" / "assets" / _TEST_PROJECT,
        settings.log_dir.parent / "var" / "uploads" / _TEST_PROJECT,
        settings.log_dir.parent / "var" / "materials" / _TEST_PROJECT,
        settings.log_dir.parent / "var" / "projects" / _TEST_PROJECT,
    ):
        if sub.exists():
            shutil.rmtree(sub, ignore_errors=True)

    yield

    asset_store._states.pop(_TEST_PROJECT, None)
    asset_store._owner_by_asset = {
        aid: owner for aid, owner in asset_store._owner_by_asset.items() if owner != _TEST_PROJECT
    }
    if hasattr(material_store, "_states"):
        material_store._states.pop(_TEST_PROJECT, None)


def test_clone_image_asset_creates_material(client):
    _seed_asset(
        "reference_image",
        asset_id="ass-img1",
        file_name="cover.jpg",
        body=b"\xff\xd8\xff\xe0" + b"\x00" * 200,
    )
    resp = client.post(
        "/api/material/clone-from-asset",
        json={"project_id": _TEST_PROJECT, "source_asset_ids": ["ass-img1"]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["project_id"] == _TEST_PROJECT
    assert len(body["materials"]) == 1
    assert body["skipped"] == []
    m = body["materials"][0]
    assert m["media_type"] == "image"
    assert m["filename"] == "cover.jpg"
    assert m["file_url"].startswith(f"/uploads/{_TEST_PROJECT}/")
    # image：缩略图复用文件本体
    assert m["thumbnail_url"] == m["file_url"]
    # 物理文件确实落盘
    settings = get_settings()
    dst = settings.log_dir.parent / "var" / m["file_url"].lstrip("/")
    assert dst.exists()


def test_clone_video_asset_copies_file_and_thumbnail(client):
    _seed_asset(
        "reference_video",
        asset_id="ass-vid1",
        file_name="sample.mp4",
        body=b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 500,
        duration_seconds=6.5,
        make_thumb=True,
    )
    resp = client.post(
        "/api/material/clone-from-asset",
        json={"project_id": _TEST_PROJECT, "source_asset_ids": ["ass-vid1"]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["skipped"] == []
    assert len(body["materials"]) == 1
    m = body["materials"][0]
    assert m["media_type"] == "video"
    assert m["duration_seconds"] == pytest.approx(6.5)
    # 视频独立缩略图
    assert m["thumbnail_url"] and m["thumbnail_url"] != m["file_url"]
    assert "_thumb.jpg" in m["thumbnail_url"]
    assert m["preprocess_status"] in ("pending", "ready", "running", "failed", "skipped")
    assert m["origin"] == "system_clone"


def test_clone_skips_bgm_asset(client):
    _seed_asset(
        "bgm",
        asset_id="ass-bgm1",
        file_name="hook.mp3",
        body=b"\xff\xfb\x90\x00" + b"\x00" * 100,
    )
    resp = client.post(
        "/api/material/clone-from-asset",
        json={"project_id": _TEST_PROJECT, "source_asset_ids": ["ass-bgm1"]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["materials"] == []
    assert body["skipped"] == ["ass-bgm1"]


def test_clone_missing_asset_id_goes_to_skipped(client):
    resp = client.post(
        "/api/material/clone-from-asset",
        json={"project_id": _TEST_PROJECT, "source_asset_ids": ["ass-nonexistent"]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["materials"] == []
    assert body["skipped"] == ["ass-nonexistent"]


def test_clone_other_project_asset_is_rejected(client):
    """Asset.owner != target project_id → 拒绝（asset 属另一项目）。"""
    other_project = "proj-other-owner"
    settings = get_settings()
    other_dir = settings.log_dir.parent / "var" / "assets" / other_project / "reference_image"
    other_dir.mkdir(parents=True, exist_ok=True)
    other_file = other_dir / "ass-cross.jpg"
    other_file.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 100)

    cross_asset = Asset(
        asset_id="ass-cross",
        owner=other_project,
        kind="reference_image",
        file_name="cross.jpg",
        file_url=f"/assets/{other_project}/reference_image/{other_file.name}",
        file_size=104,
        content_hash="crosshash",
        mime="image/jpeg",
        title="cross",
        description="",
        tags=[],
        metadata={},
        status="ready",
        error=None,
        created_at=time.time(),
        last_used_at=None,
        use_count=0,
    )
    asset_store.upsert(cross_asset)
    try:
        resp = client.post(
            "/api/material/clone-from-asset",
            json={"project_id": _TEST_PROJECT, "source_asset_ids": ["ass-cross"]},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["materials"] == []
        assert body["skipped"] == ["ass-cross"]
    finally:
        asset_store._states.pop(other_project, None)
        asset_store._owner_by_asset.pop("ass-cross", None)
        shutil.rmtree(settings.log_dir.parent / "var" / "assets" / other_project, ignore_errors=True)


def test_clone_empty_source_ids_rejected(client):
    resp = client.post(
        "/api/material/clone-from-asset",
        json={"project_id": _TEST_PROJECT, "source_asset_ids": []},
    )
    assert resp.status_code == 422  # pydantic min_length=1


def test_clone_system_project_id_rejected(client):
    _seed_asset(
        "reference_image",
        asset_id="ass-sys-illegal",
        file_name="x.jpg",
        body=b"\xff\xd8\xff\xe0" + b"\x00" * 50,
    )
    resp = client.post(
        "/api/material/clone-from-asset",
        json={"project_id": "__system__", "source_asset_ids": ["ass-sys-illegal"]},
    )
    assert resp.status_code == 400
