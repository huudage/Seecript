"""manifest_store 版本槽模型覆盖。

测试矩阵：
- 空目录 → version_count=0 / get_active_slot=None / load_active=None
- create_version 一次 → 1 槽 + active 指向它
- create_version 两次 → 2 槽 + active 切到新的；list_versions 按 mtime 升序
- 第三次 create_version 不传 replace_slot → SlotsFullError
- 第三次 create_version 传 replace_slot=旧槽 → 旧槽被删，新槽 active
- update_version 就地编辑 → 槽内容变，但 slot_id / 槽数不变
- activate 切换 active 指针；slot 不存在 → FileNotFoundError
- delete_version 删 active 槽 → 自动跳到剩下那个；删完没了 active 清空
- legacy manifest.json 自动迁成 v1（兼容 precompute_samples）
- sample 目录不存在 → locate/list/load 返 None，update/create 抛 FileNotFoundError
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.schemas import (
    PackagingProfile,
    RhythmCurve,
    SampleManifest,
    Section,
    Shot,
)
from app.services.library import manifest_store


def _mk_manifest(sample_id: str, marker: str = "v1") -> SampleManifest:
    """构造一个最小可校验 manifest；marker 写进 title 便于断言哪个版本。"""
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
def sample_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """造一个 user-* 样例物理目录（走 var/uploads/decompose 分支）。"""
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    from app.config import get_settings
    get_settings.cache_clear()

    sample_id = "user-store-test"
    d = tmp_path / "var" / "uploads" / "decompose" / sample_id
    d.mkdir(parents=True)
    (d / "video.mp4").write_bytes(b"")
    yield sample_id, d
    get_settings.cache_clear()


def test_initial_state_is_empty(sample_dir):
    sample_id, _ = sample_dir
    assert manifest_store.version_count(sample_id) == 0
    assert manifest_store.get_active_slot(sample_id) is None
    assert manifest_store.load_active(sample_id) is None
    assert manifest_store.has_active(sample_id) is False
    assert manifest_store.list_versions(sample_id) == []


def test_create_first_version(sample_dir):
    sample_id, d = sample_dir
    m1 = _mk_manifest(sample_id, marker="v1")

    slot = manifest_store.create_version(sample_id, m1)

    assert manifest_store.version_count(sample_id) == 1
    assert manifest_store.get_active_slot(sample_id) == slot
    assert manifest_store.has_active(sample_id) is True
    assert (d / f"manifest.v_{slot}.json").is_file()
    assert (d / "manifest.active").read_text(encoding="utf-8").strip() == slot

    loaded = manifest_store.load_active(sample_id)
    assert loaded is not None and "v1" in loaded.title


def test_create_two_versions(sample_dir):
    sample_id, d = sample_dir
    slot1 = manifest_store.create_version(sample_id, _mk_manifest(sample_id, marker="v1"))
    # mtime 粒度问题：确保第二个文件 mtime 严格更晚
    import time
    time.sleep(0.05)
    slot2 = manifest_store.create_version(sample_id, _mk_manifest(sample_id, marker="v2"))

    assert manifest_store.version_count(sample_id) == 2
    assert manifest_store.get_active_slot(sample_id) == slot2

    versions = manifest_store.list_versions(sample_id)
    assert len(versions) == 2
    # 按 mtime 升序：最旧在前
    assert versions[0].slot_id == slot1
    assert versions[1].slot_id == slot2
    assert versions[1].is_active is True
    assert versions[0].is_active is False


def test_third_create_without_replace_raises(sample_dir):
    sample_id, _ = sample_dir
    manifest_store.create_version(sample_id, _mk_manifest(sample_id, marker="v1"))
    manifest_store.create_version(sample_id, _mk_manifest(sample_id, marker="v2"))
    with pytest.raises(manifest_store.SlotsFullError):
        manifest_store.create_version(sample_id, _mk_manifest(sample_id, marker="v3"))


def test_third_create_with_replace_evicts_old(sample_dir):
    sample_id, d = sample_dir
    slot1 = manifest_store.create_version(sample_id, _mk_manifest(sample_id, marker="v1"))
    import time
    time.sleep(0.05)
    slot2 = manifest_store.create_version(sample_id, _mk_manifest(sample_id, marker="v2"))

    slot3 = manifest_store.create_version(
        sample_id, _mk_manifest(sample_id, marker="v3"), replace_slot=slot1,
    )

    assert manifest_store.version_count(sample_id) == 2
    assert not (d / f"manifest.v_{slot1}.json").is_file()
    assert (d / f"manifest.v_{slot2}.json").is_file()
    assert (d / f"manifest.v_{slot3}.json").is_file()
    assert slot3 != slot1, "slot_id 不应复用，避免脏读"
    assert manifest_store.get_active_slot(sample_id) == slot3


def test_replace_slot_without_full_raises(sample_dir):
    sample_id, _ = sample_dir
    slot1 = manifest_store.create_version(sample_id, _mk_manifest(sample_id, marker="v1"))
    with pytest.raises(ValueError, match="slot 还有空位"):
        manifest_store.create_version(
            sample_id, _mk_manifest(sample_id, marker="v2"), replace_slot=slot1,
        )


def test_replace_slot_nonexistent_raises(sample_dir):
    sample_id, _ = sample_dir
    manifest_store.create_version(sample_id, _mk_manifest(sample_id, marker="v1"))
    manifest_store.create_version(sample_id, _mk_manifest(sample_id, marker="v2"))
    with pytest.raises(ValueError, match="不存在"):
        manifest_store.create_version(
            sample_id, _mk_manifest(sample_id, marker="v3"), replace_slot="deadbeef",
        )


def test_update_version_in_place(sample_dir):
    sample_id, d = sample_dir
    slot = manifest_store.create_version(sample_id, _mk_manifest(sample_id, marker="orig"))
    edited = _mk_manifest(sample_id, marker="edited")

    manifest_store.update_version(sample_id, slot, edited)

    # slot id 不变、槽数不变
    assert manifest_store.version_count(sample_id) == 1
    assert manifest_store.get_active_slot(sample_id) == slot
    loaded = manifest_store.load_version(sample_id, slot)
    assert loaded is not None and "edited" in loaded.title


def test_update_nonexistent_slot_raises(sample_dir):
    sample_id, _ = sample_dir
    with pytest.raises(FileNotFoundError):
        manifest_store.update_version(sample_id, "deadbeef", _mk_manifest(sample_id))


def test_activate_switches_pointer(sample_dir):
    sample_id, _ = sample_dir
    slot1 = manifest_store.create_version(sample_id, _mk_manifest(sample_id, marker="v1"))
    import time
    time.sleep(0.05)
    slot2 = manifest_store.create_version(sample_id, _mk_manifest(sample_id, marker="v2"))
    assert manifest_store.get_active_slot(sample_id) == slot2

    manifest_store.activate(sample_id, slot1)
    assert manifest_store.get_active_slot(sample_id) == slot1
    loaded = manifest_store.load_active(sample_id)
    assert loaded is not None and "v1" in loaded.title


def test_activate_nonexistent_raises(sample_dir):
    sample_id, _ = sample_dir
    manifest_store.create_version(sample_id, _mk_manifest(sample_id))
    with pytest.raises(FileNotFoundError):
        manifest_store.activate(sample_id, "deadbeef")


def test_delete_active_slot_self_heals(sample_dir):
    sample_id, d = sample_dir
    slot1 = manifest_store.create_version(sample_id, _mk_manifest(sample_id, marker="v1"))
    import time
    time.sleep(0.05)
    slot2 = manifest_store.create_version(sample_id, _mk_manifest(sample_id, marker="v2"))
    # active = slot2

    assert manifest_store.delete_version(sample_id, slot2) is True
    # 自愈：active 跳到剩下那个
    assert manifest_store.version_count(sample_id) == 1
    assert manifest_store.get_active_slot(sample_id) == slot1
    loaded = manifest_store.load_active(sample_id)
    assert loaded is not None and "v1" in loaded.title


def test_delete_last_slot_clears_active(sample_dir):
    sample_id, d = sample_dir
    slot = manifest_store.create_version(sample_id, _mk_manifest(sample_id))
    assert manifest_store.delete_version(sample_id, slot) is True
    assert manifest_store.version_count(sample_id) == 0
    assert manifest_store.get_active_slot(sample_id) is None
    assert manifest_store.has_active(sample_id) is False
    assert not (d / "manifest.active").exists()


def test_delete_nonexistent_returns_false(sample_dir):
    sample_id, _ = sample_dir
    assert manifest_store.delete_version(sample_id, "deadbeef") is False


def test_active_self_heals_when_pointer_dangling(sample_dir):
    """手动把 manifest.active 指向不存在的 slot → 下次访问自愈到最新槽。"""
    sample_id, d = sample_dir
    slot = manifest_store.create_version(sample_id, _mk_manifest(sample_id))
    (d / "manifest.active").write_text("nonexistent", encoding="utf-8")

    cur = manifest_store.get_active_slot(sample_id)
    assert cur == slot
    assert (d / "manifest.active").read_text(encoding="utf-8").strip() == slot


def test_legacy_manifest_json_migrates_to_slot(sample_dir):
    """precompute_samples 时代的 manifest.json 第一次访问时自动迁成 v1 slot。"""
    sample_id, d = sample_dir
    legacy_payload = _mk_manifest(sample_id, marker="legacy").model_dump()
    (d / "manifest.json").write_text(
        json.dumps(legacy_payload, ensure_ascii=False), encoding="utf-8",
    )

    versions = manifest_store.list_versions(sample_id)

    assert len(versions) == 1
    assert versions[0].is_active is True
    assert not (d / "manifest.json").exists(), "legacy 文件应被重命名"
    assert (d / f"manifest.v_{versions[0].slot_id}.json").is_file()
    loaded = manifest_store.load_active(sample_id)
    assert loaded is not None and "legacy" in loaded.title


def test_load_published_is_alias_for_load_active(sample_dir):
    """plan_agent / gap.py 仍调 load_published，必须等价于 load_active。"""
    sample_id, _ = sample_dir
    manifest_store.create_version(sample_id, _mk_manifest(sample_id, marker="aliased"))
    pub = manifest_store.load_published(sample_id)
    act = manifest_store.load_active(sample_id)
    assert pub is not None and act is not None
    assert pub.title == act.title


def test_missing_sample_dir(monkeypatch, tmp_path):
    """sample 目录不存在 → 读类返 None / 空，写类抛 FileNotFoundError。"""
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    from app.config import get_settings
    get_settings.cache_clear()
    try:
        sample_id = "user-nonexistent"
        assert manifest_store.locate_sample_dir(sample_id) is None
        assert manifest_store.version_count(sample_id) == 0
        assert manifest_store.list_versions(sample_id) == []
        assert manifest_store.load_active(sample_id) is None
        assert manifest_store.get_active_slot(sample_id) is None
        with pytest.raises(FileNotFoundError):
            manifest_store.create_version(sample_id, _mk_manifest(sample_id))
        with pytest.raises(FileNotFoundError):
            manifest_store.update_version(sample_id, "deadbeef", _mk_manifest(sample_id))
        assert manifest_store.delete_version(sample_id, "deadbeef") is False
    finally:
        get_settings.cache_clear()
