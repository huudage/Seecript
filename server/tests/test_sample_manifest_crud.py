"""Manifest CRUD HTTP 端点 — 版本槽模型。

覆盖：
- GET  /sample/{id}/manifest/status        → version_count + active_slot + versions[]
- GET  /sample/{id}/versions               → list
- GET  /sample/{id}/manifest?slot=         → 拉 active / 指定槽 / 404
- PUT  /sample/{id}/manifest?slot=         → 就地编辑（不开新版本）
- POST /sample/{id}/versions/{slot}/activate
- DELETE /sample/{id}/versions/{slot}      → 删 active 自愈
- GET  /library 卡片填 manifest_status / version_count / active_slot
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from app.schemas import PackagingProfile, RhythmCurve, SampleManifest, Section, Shot
from app.services.library import manifest_store


def _mk_manifest(sample_id: str, marker: str = "v1") -> SampleManifest:
    shots = [
        Shot(index=0, start=0.0, end=2.0, duration=2.0,
             thumbnail_url=None, transcript=None, tags=[]),
        Shot(index=1, start=2.0, end=5.0, duration=3.0,
             thumbnail_url=None, transcript=None, tags=[]),
    ]
    return SampleManifest(
        sample_id=sample_id,
        title=f"{sample_id} {marker}",
        video_type="marketing",
        duration_seconds=5.0,
        video_url=f"/samples/{sample_id}/video.mp4",
        has_voice=True,
        shots=shots,
        rhythm=RhythmCurve(times=[0.0, 5.0], cut_density=[1.0, 0.6],
                           bgm_energy=[0.1, 0.4], tempo_bpm=120.0),
        sections=[
            Section(role="opening", theme="开场", start=0.0, end=2.0,
                    summary="opening", shot_indices=[0]),
            Section(role="climax", theme="高潮", start=2.0, end=5.0,
                    summary="climax", shot_indices=[1]),
        ],
        packaging=PackagingProfile(
            subtitle_style="大字加描边", has_title_bar=True,
            transition_types=["cut"], cover_style=None, sticker_density=0.2,
        ),
    )


@pytest.fixture
def uploaded_sample(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """造一个落在 var/uploads/decompose/ 的 user-* 物理目录，返回 sample_id。"""
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    from app.config import get_settings
    get_settings.cache_clear()

    sample_id = "user-crud-test"
    d = tmp_path / "var" / "uploads" / "decompose" / sample_id
    d.mkdir(parents=True)
    (d / "video.mp4").write_bytes(b"")
    (d / "meta.json").write_text(
        '{"title":"crud test","video_type":"marketing","uploaded_at":1}',
        encoding="utf-8",
    )
    yield sample_id
    get_settings.cache_clear()


# -----------------------------------------------------------------------------
# Status / Versions list
# -----------------------------------------------------------------------------

def test_get_status_404_when_sample_missing(client):
    r = client.get("/api/sample/user-not-exist/manifest/status")
    assert r.status_code == 404


def test_get_status_empty(client, uploaded_sample):
    r = client.get(f"/api/sample/{uploaded_sample}/manifest/status")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["version_count"] == 0
    assert body["active_slot"] is None
    assert body["versions"] == []
    assert body["max_versions"] == manifest_store.MAX_VERSIONS


def test_list_versions_empty(client, uploaded_sample):
    r = client.get(f"/api/sample/{uploaded_sample}/versions")
    assert r.status_code == 200
    assert r.json() == []


def test_status_after_two_creates(client, uploaded_sample):
    slot1 = manifest_store.create_version(uploaded_sample, _mk_manifest(uploaded_sample, "v1"))
    time.sleep(0.05)
    slot2 = manifest_store.create_version(uploaded_sample, _mk_manifest(uploaded_sample, "v2"))

    r = client.get(f"/api/sample/{uploaded_sample}/manifest/status")
    body = r.json()
    assert body["version_count"] == 2
    assert body["active_slot"] == slot2
    labels = [(v["slot_id"], v["label"], v["is_active"]) for v in body["versions"]]
    assert labels[0] == (slot1, "v1", False)
    assert labels[1] == (slot2, "v2", True)


# -----------------------------------------------------------------------------
# GET manifest
# -----------------------------------------------------------------------------

def test_get_active_manifest(client, uploaded_sample):
    manifest_store.create_version(uploaded_sample, _mk_manifest(uploaded_sample, "alpha"))
    r = client.get(f"/api/sample/{uploaded_sample}/manifest")
    assert r.status_code == 200
    assert "alpha" in r.json()["title"]


def test_get_manifest_409_when_no_versions(client, uploaded_sample):
    r = client.get(f"/api/sample/{uploaded_sample}/manifest")
    assert r.status_code == 409


def test_get_manifest_by_slot(client, uploaded_sample):
    slot1 = manifest_store.create_version(uploaded_sample, _mk_manifest(uploaded_sample, "older"))
    time.sleep(0.05)
    manifest_store.create_version(uploaded_sample, _mk_manifest(uploaded_sample, "newer"))

    r = client.get(f"/api/sample/{uploaded_sample}/manifest?slot={slot1}")
    assert r.status_code == 200
    assert "older" in r.json()["title"]


def test_get_manifest_unknown_slot_404(client, uploaded_sample):
    manifest_store.create_version(uploaded_sample, _mk_manifest(uploaded_sample))
    r = client.get(f"/api/sample/{uploaded_sample}/manifest?slot=deadbeef")
    assert r.status_code == 404


# -----------------------------------------------------------------------------
# PUT (in-place edit)
# -----------------------------------------------------------------------------

def test_put_in_place_edits_active_slot(client, uploaded_sample):
    slot = manifest_store.create_version(uploaded_sample, _mk_manifest(uploaded_sample, "orig"))

    payload = _mk_manifest(uploaded_sample, marker="edited").model_dump()
    r = client.put(f"/api/sample/{uploaded_sample}/manifest", json=payload)
    assert r.status_code == 200, r.text

    body = r.json()
    assert body["version_count"] == 1, "就地编辑不应开新槽"
    assert body["active_slot"] == slot
    loaded = manifest_store.load_version(uploaded_sample, slot)
    assert loaded is not None and "edited" in loaded.title


def test_put_with_explicit_slot(client, uploaded_sample):
    slot1 = manifest_store.create_version(uploaded_sample, _mk_manifest(uploaded_sample, "v1"))
    time.sleep(0.05)
    slot2 = manifest_store.create_version(uploaded_sample, _mk_manifest(uploaded_sample, "v2"))

    payload = _mk_manifest(uploaded_sample, marker="patched-v1").model_dump()
    r = client.put(f"/api/sample/{uploaded_sample}/manifest?slot={slot1}", json=payload)
    assert r.status_code == 200

    assert "patched-v1" in manifest_store.load_version(uploaded_sample, slot1).title
    # active 不动（仍是 slot2）
    assert manifest_store.get_active_slot(uploaded_sample) == slot2


def test_put_409_when_no_versions(client, uploaded_sample):
    payload = _mk_manifest(uploaded_sample).model_dump()
    r = client.put(f"/api/sample/{uploaded_sample}/manifest", json=payload)
    assert r.status_code == 409


def test_put_rejects_sample_id_mismatch(client, uploaded_sample):
    manifest_store.create_version(uploaded_sample, _mk_manifest(uploaded_sample))
    payload = _mk_manifest("user-wrong-id", marker="m").model_dump()
    r = client.put(f"/api/sample/{uploaded_sample}/manifest", json=payload)
    assert r.status_code == 400


# -----------------------------------------------------------------------------
# Activate / Delete
# -----------------------------------------------------------------------------

def test_activate_switches_active(client, uploaded_sample):
    slot1 = manifest_store.create_version(uploaded_sample, _mk_manifest(uploaded_sample, "v1"))
    time.sleep(0.05)
    slot2 = manifest_store.create_version(uploaded_sample, _mk_manifest(uploaded_sample, "v2"))
    assert manifest_store.get_active_slot(uploaded_sample) == slot2

    r = client.post(f"/api/sample/{uploaded_sample}/versions/{slot1}/activate")
    assert r.status_code == 200
    body = r.json()
    assert body["active_slot"] == slot1


def test_activate_unknown_404(client, uploaded_sample):
    manifest_store.create_version(uploaded_sample, _mk_manifest(uploaded_sample))
    r = client.post(f"/api/sample/{uploaded_sample}/versions/deadbeef/activate")
    assert r.status_code == 404


def test_delete_version_self_heals_active(client, uploaded_sample):
    slot1 = manifest_store.create_version(uploaded_sample, _mk_manifest(uploaded_sample, "v1"))
    time.sleep(0.05)
    slot2 = manifest_store.create_version(uploaded_sample, _mk_manifest(uploaded_sample, "v2"))

    r = client.delete(f"/api/sample/{uploaded_sample}/versions/{slot2}")
    assert r.status_code == 200
    body = r.json()
    assert body["version_count"] == 1
    assert body["active_slot"] == slot1


def test_delete_last_version_clears_active(client, uploaded_sample):
    slot = manifest_store.create_version(uploaded_sample, _mk_manifest(uploaded_sample))
    r = client.delete(f"/api/sample/{uploaded_sample}/versions/{slot}")
    assert r.status_code == 200
    body = r.json()
    assert body["version_count"] == 0
    assert body["active_slot"] is None


def test_delete_unknown_slot_404(client, uploaded_sample):
    r = client.delete(f"/api/sample/{uploaded_sample}/versions/deadbeef")
    assert r.status_code == 404


# -----------------------------------------------------------------------------
# Library 卡片字段
# -----------------------------------------------------------------------------

def test_library_lists_version_status(client, uploaded_sample):
    """uploaded_sample 默认 manifest_status=none，create 一版后变 ready，active_slot 跟随。"""
    r = client.get("/api/library?source=user")
    item = next((it for it in r.json() if it["id"] == uploaded_sample), None)
    assert item is not None
    assert item["manifest_status"] == "none"
    assert item["version_count"] == 0
    assert item["active_slot"] is None

    slot = manifest_store.create_version(uploaded_sample, _mk_manifest(uploaded_sample, "v1"))
    item = next(it for it in client.get("/api/library?source=user").json()
                if it["id"] == uploaded_sample)
    assert item["manifest_status"] == "ready"
    assert item["version_count"] == 1
    assert item["active_slot"] == slot
