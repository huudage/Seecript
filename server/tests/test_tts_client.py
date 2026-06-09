"""TTS 客户端：mock + volc 路径切换 + WAV 字节合法性。

mock 模式：无 Key 自动回落，能产出可解析 WAV（RIFF header）。
volc 模式：API Key 齐全时 backend_name 切换到 'volc'；HTTP 失败应转 TTSError。
"""
from __future__ import annotations

import io
import wave

import pytest

from app.config import get_settings
from app.services.tts import backend_name, synthesize
from app.services.tts.client import TTSError, _mock_synthesize


def test_mock_backend_when_no_key(monkeypatch):
    monkeypatch.setenv("TTS_PROVIDER", "mock")
    monkeypatch.delenv("VOLC_TTS_APP_ID", raising=False)
    monkeypatch.delenv("VOLC_TTS_ACCESS_TOKEN", raising=False)
    get_settings.cache_clear()
    assert backend_name() == "mock"


def test_mock_synthesize_produces_valid_wav():
    wav_bytes = _mock_synthesize("你好世界", sample_rate=24000)
    assert wav_bytes.startswith(b"RIFF")
    with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
        assert wf.getframerate() == 24000
        assert wf.getnchannels() == 1
        assert wf.getnframes() > 0


def test_synthesize_empty_text_raises():
    with pytest.raises(TTSError) as excinfo:
        synthesize("   ", voice="zh_female_qingxin")
    assert excinfo.value.code == "EMPTY_TEXT"


def test_volc_backend_selected_when_both_keys_set(monkeypatch):
    monkeypatch.setenv("TTS_PROVIDER", "volc")
    monkeypatch.setenv("VOLC_TTS_APP_ID", "fake-app")
    monkeypatch.setenv("VOLC_TTS_ACCESS_TOKEN", "fake-token")
    get_settings.cache_clear()
    assert backend_name() == "volc"


def test_volc_missing_key_raises_at_synthesize(monkeypatch):
    """Stage-prod #1：缺 key 不再 silent fallback——synthesize 直接抛 TTS_NO_KEY 给前端 500。"""
    monkeypatch.setenv("TTS_PROVIDER", "volc")
    # 用 setenv("") 而非 delenv：pydantic-settings 会从 .env 加载真值，
    # delenv 只清 os.environ 不会覆盖 .env 里的默认值。
    monkeypatch.setenv("VOLC_TTS_APP_ID", "")
    monkeypatch.setenv("VOLC_TTS_ACCESS_TOKEN", "fake-token")
    get_settings.cache_clear()
    # backend_name 仍报 "volc"（保留原 provider 名），synthesize 调用时硬失败
    assert backend_name() == "volc"
    with pytest.raises(TTSError) as excinfo:
        synthesize("你好", voice="zh_female_qingxin")
    assert excinfo.value.code == "TTS_NO_KEY"
