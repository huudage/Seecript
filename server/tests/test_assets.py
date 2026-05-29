"""Asset Library 单测：store CRUD + reference 解析 + BGM 集成。

测试场景：
1. AssetStore 基础 CRUD（upsert/get/list/delete）
2. sha256 去重：同内容二次上传返回老 asset
3. resolve_reference_image_urls：图/视频抽帧 round-robin + max_total 截断
4. plan.py _build_bgm_config：asset_id → BGMConfig 解析 + status 校验
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from app.schemas import Asset, AssetKind, AssetStatus
from app.services.assets import asset_store, resolve_reference_image_urls
from app.services.assets.store import sha256_of_bytes


TEST_OWNER = "proj-test-assets"


@pytest.fixture
def clean_store():
    """每个测试前清空 asset_store 内存索引 + 磁盘目录，避免跨测试污染。

    v2 起 asset_store 是多 owner 注册表；upsert 触发的 _OwnerState 会 _load() 磁盘
    manifest.json，所以光清内存不够，还得删 var/assets/<TEST_OWNER>/。
    """
    import shutil
    from app.services.assets.store import _assets_base
    test_dir = _assets_base() / TEST_OWNER

    def _wipe():
        asset_store._owner_by_asset.clear()
        asset_store._states.clear()
        if test_dir.exists():
            shutil.rmtree(test_dir)

    _wipe()
    yield
    _wipe()


def test_asset_store_upsert_and_get(clean_store):
    """upsert 后能 get 回来；字段完整。"""
    asset = Asset(
        asset_id="ass-test001",
        owner=TEST_OWNER,
        kind="bgm",
        file_name="test.mp3",
        file_url="/assets/local/bgm/ass-test001.mp3",
        file_size=1024,
        content_hash="abc123",
        mime="audio/mpeg",
        title="测试 BGM",
        description="单测用",
        tags=["测试", "mock"],
        metadata={"duration_seconds": 30.0},
        status="ready",
        error=None,
        created_at=time.time(),
        last_used_at=None,
        use_count=0,
    )
    asset_store.upsert(asset)
    retrieved = asset_store.get("ass-test001")
    assert retrieved is not None
    assert retrieved.asset_id == "ass-test001"
    assert retrieved.kind == "bgm"
    assert retrieved.title == "测试 BGM"
    assert retrieved.status == "ready"


def test_asset_store_find_by_hash_dedup(clean_store):
    """同 content_hash 二次 upsert 应返回老 asset（去重）。"""
    h = sha256_of_bytes(b"mock-content")
    a1 = Asset(
        asset_id="ass-dup1",
        owner=TEST_OWNER,
        kind="bgm",
        file_name="dup.mp3",
        file_url="/assets/local/bgm/ass-dup1.mp3",
        file_size=100,
        content_hash=h,
        mime="audio/mpeg",
        title="原始",
        created_at=time.time(),
    )
    asset_store.upsert(a1)
    found = asset_store.find_by_hash(TEST_OWNER, h)
    assert found is not None
    assert found.asset_id == "ass-dup1"


def test_asset_store_list_filter_by_kind(clean_store):
    """list(kind=...) 只返回该类型资产。"""
    asset_store.upsert(Asset(
        asset_id="ass-bgm1", owner=TEST_OWNER, kind="bgm",
        file_name="b.mp3", file_url="/b.mp3", file_size=1, content_hash="h1",
        mime="audio/mpeg", created_at=time.time(),
    ))
    asset_store.upsert(Asset(
        asset_id="ass-img1", owner=TEST_OWNER, kind="reference_image",
        file_name="i.jpg", file_url="/i.jpg", file_size=1, content_hash="h2",
        mime="image/jpeg", created_at=time.time(),
    ))
    bgms = asset_store.list(TEST_OWNER, kind="bgm")
    assert len(bgms) == 1
    assert bgms[0].asset_id == "ass-bgm1"
    imgs = asset_store.list(TEST_OWNER, kind="reference_image")
    assert len(imgs) == 1
    assert imgs[0].asset_id == "ass-img1"


def test_asset_store_delete_removes_from_index(clean_store):
    """delete 后 get 返回 None；hash 索引也清除。"""
    h = "hash-del"
    asset_store.upsert(Asset(
        asset_id="ass-del", owner=TEST_OWNER, kind="bgm",
        file_name="del.mp3", file_url="/del.mp3", file_size=1, content_hash=h,
        mime="audio/mpeg", created_at=time.time(),
    ))
    assert asset_store.get("ass-del") is not None
    assert asset_store.find_by_hash(TEST_OWNER, h) is not None
    # delete 会尝试删文件，但测试中文件不存在不影响索引清除
    ok = asset_store.delete("ass-del")
    assert ok is True
    assert asset_store.get("ass-del") is None
    assert asset_store.find_by_hash(TEST_OWNER, h) is None


def test_asset_store_touch_increments_use_count(clean_store):
    """touch 后 use_count +1，last_used_at 更新。"""
    asset_store.upsert(Asset(
        asset_id="ass-touch", owner=TEST_OWNER, kind="bgm",
        file_name="t.mp3", file_url="/t.mp3", file_size=1, content_hash="ht",
        mime="audio/mpeg", created_at=time.time(), use_count=0, last_used_at=None,
    ))
    before = asset_store.get("ass-touch")
    assert before.use_count == 0
    assert before.last_used_at is None
    asset_store.touch("ass-touch")
    after = asset_store.get("ass-touch")
    assert after.use_count == 1
    assert after.last_used_at is not None


def test_resolve_reference_image_urls_empty():
    """空 asset_ids 返回空列表。"""
    urls = resolve_reference_image_urls([])
    assert urls == []


def test_resolve_reference_image_urls_image_only(clean_store):
    """reference_image 类型直接用 file_url。"""
    asset_store.upsert(Asset(
        asset_id="ass-img1", owner=TEST_OWNER, kind="reference_image",
        file_name="ref.jpg", file_url="/assets/local/reference_image/ass-img1.jpg",
        file_size=1, content_hash="hi1", mime="image/jpeg",
        status="ready", created_at=time.time(),
    ))
    urls = resolve_reference_image_urls(["ass-img1"])
    assert urls == ["/assets/local/reference_image/ass-img1.jpg"]


def test_resolve_reference_image_urls_video_frames(clean_store):
    """reference_video 用 metadata.frame_urls；缺则回落 thumbnail_url。"""
    asset_store.upsert(Asset(
        asset_id="ass-vid1", owner=TEST_OWNER, kind="reference_video",
        file_name="ref.mp4", file_url="/assets/local/reference_video/ass-vid1.mp4",
        file_size=1, content_hash="hv1", mime="video/mp4",
        status="ready", created_at=time.time(),
        metadata={
            "frame_urls": ["/assets/local/reference_video/ass-vid1.frames/frame-00.jpg",
                           "/assets/local/reference_video/ass-vid1.frames/frame-01.jpg"],
            "thumbnail_url": "/assets/local/reference_video/ass-vid1.thumb.jpg",
        },
    ))
    urls = resolve_reference_image_urls(["ass-vid1"])
    assert len(urls) == 2
    assert "frame-00.jpg" in urls[0]
    assert "frame-01.jpg" in urls[1]


def test_resolve_reference_image_urls_max_total_truncates(clean_store):
    """超出 max_total 时截断；图优先，视频帧降采样。"""
    asset_store.upsert(Asset(
        asset_id="ass-img1", owner=TEST_OWNER, kind="reference_image",
        file_name="i1.jpg", file_url="/i1.jpg", file_size=1, content_hash="hi1",
        mime="image/jpeg", status="ready", created_at=time.time(),
    ))
    asset_store.upsert(Asset(
        asset_id="ass-img2", owner=TEST_OWNER, kind="reference_image",
        file_name="i2.jpg", file_url="/i2.jpg", file_size=1, content_hash="hi2",
        mime="image/jpeg", status="ready", created_at=time.time(),
    ))
    asset_store.upsert(Asset(
        asset_id="ass-vid1", owner=TEST_OWNER, kind="reference_video",
        file_name="v1.mp4", file_url="/v1.mp4", file_size=1, content_hash="hv1",
        mime="video/mp4", status="ready", created_at=time.time(),
        metadata={"frame_urls": [f"/f{i}.jpg" for i in range(10)]},
    ))
    urls = resolve_reference_image_urls(["ass-img1", "ass-img2", "ass-vid1"], max_total=5)
    assert len(urls) == 5
    # 图优先：前 2 是 i1/i2，后 3 是视频帧降采样
    assert urls[0] == "/i1.jpg"
    assert urls[1] == "/i2.jpg"
    assert all("/f" in u for u in urls[2:])


def test_resolve_reference_image_urls_skips_bgm(clean_store):
    """BGM 类型不参与视觉参考，静默跳过。"""
    asset_store.upsert(Asset(
        asset_id="ass-bgm1", owner=TEST_OWNER, kind="bgm",
        file_name="b.mp3", file_url="/b.mp3", file_size=1, content_hash="hb",
        mime="audio/mpeg", status="ready", created_at=time.time(),
    ))
    urls = resolve_reference_image_urls(["ass-bgm1"])
    assert urls == []


def test_resolve_reference_image_urls_skips_not_ready(clean_store):
    """status != ready 的资产静默跳过。"""
    asset_store.upsert(Asset(
        asset_id="ass-proc", owner=TEST_OWNER, kind="reference_image",
        file_name="p.jpg", file_url="/p.jpg", file_size=1, content_hash="hp",
        mime="image/jpeg", status="processing", created_at=time.time(),
    ))
    urls = resolve_reference_image_urls(["ass-proc"])
    assert urls == []


def test_build_bgm_config_resolves_asset_id():
    """plan.py _build_bgm_config：asset_id → BGMConfig.track_url。"""
    from app.routers.plan import _build_bgm_config
    asset_store.upsert(Asset(
        asset_id="ass-bgm-plan", owner=TEST_OWNER, kind="bgm",
        file_name="plan.mp3", file_url="/assets/local/bgm/ass-bgm-plan.mp3",
        file_size=1, content_hash="hbp", mime="audio/mpeg",
        status="ready", created_at=time.time(),
    ))
    cfg = _build_bgm_config("ass-bgm-plan")
    assert cfg.bgm_asset_id == "ass-bgm-plan"
    assert cfg.track_url == "/assets/local/bgm/ass-bgm-plan.mp3"
    assert cfg.volume == 0.35
    assert cfg.duck_with_voice is True


def test_build_bgm_config_returns_empty_when_not_ready():
    """status=processing 时返回空 BGMConfig（避免渲染拿到不存在文件）。"""
    from app.routers.plan import _build_bgm_config
    asset_store.upsert(Asset(
        asset_id="ass-bgm-proc", owner=TEST_OWNER, kind="bgm",
        file_name="proc.mp3", file_url="/proc.mp3", file_size=1, content_hash="hbpr",
        mime="audio/mpeg", status="processing", created_at=time.time(),
    ))
    cfg = _build_bgm_config("ass-bgm-proc")
    assert cfg.bgm_asset_id is None
    assert cfg.track_url is None


def test_build_bgm_config_returns_empty_when_not_found():
    """asset_id 不存在时返回空 BGMConfig。"""
    from app.routers.plan import _build_bgm_config
    cfg = _build_bgm_config("ass-nonexist")
    assert cfg.bgm_asset_id is None
    assert cfg.track_url is None


def test_build_bgm_config_returns_empty_when_wrong_kind():
    """kind != bgm 时返回空 BGMConfig。"""
    from app.routers.plan import _build_bgm_config
    asset_store.upsert(Asset(
        asset_id="ass-img-wrong", owner=TEST_OWNER, kind="reference_image",
        file_name="w.jpg", file_url="/w.jpg", file_size=1, content_hash="hw",
        mime="image/jpeg", status="ready", created_at=time.time(),
    ))
    cfg = _build_bgm_config("ass-img-wrong")
    assert cfg.bgm_asset_id is None
    assert cfg.track_url is None
