"""Voice store：URL 路径生成 + 落盘往返 + 清理。"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from app.config import get_settings
from app.services.tts import store as voice_store


_TEST_PLAN = "plan-voice-store-test"


@pytest.fixture(autouse=True)
def clean_voice_dir():
    root = get_settings().log_dir.parent / "var" / "voiceovers" / _TEST_PLAN
    if root.exists():
        shutil.rmtree(root, ignore_errors=True)
    yield
    if root.exists():
        shutil.rmtree(root, ignore_errors=True)


def test_voice_url_path_shape():
    url = voice_store.voice_url(_TEST_PLAN, "sc-0")
    assert url == f"/voiceovers/{_TEST_PLAN}/sc-0.wav"


def test_save_then_resolve_local_path():
    fake_wav = b"RIFF\x00\x00\x00\x00WAVEfmt "
    url = voice_store.save_wav(_TEST_PLAN, "sc-0", fake_wav)
    assert url.startswith("/voiceovers/")

    local = voice_store.url_to_local_path(url)
    assert local is not None and local.exists()
    assert local.read_bytes() == fake_wav


def test_delete_removes_file():
    fake_wav = b"RIFF\x00\x00\x00\x00WAVEfmt "
    voice_store.save_wav(_TEST_PLAN, "sc-1", fake_wav)
    assert voice_store.delete(_TEST_PLAN, "sc-1") is True
    assert voice_store.url_to_local_path(f"/voiceovers/{_TEST_PLAN}/sc-1.wav") is None
    # 二次 delete 文件不存在不报错
    assert voice_store.delete(_TEST_PLAN, "sc-1") is False


def test_url_to_local_path_rejects_non_voiceover_url():
    assert voice_store.url_to_local_path("/uploads/foo/bar.wav") is None
    assert voice_store.url_to_local_path("") is None
    assert voice_store.url_to_local_path("https://example.com/x.wav") is None
