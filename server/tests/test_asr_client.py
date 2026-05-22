"""Tests for ASR client abstraction layer."""
from __future__ import annotations

import pytest

from app.services.asr_client import (
    ASRError,
    DoubaoBigmodelASRClient,
    MockASRClient,
    _DOUBAO_ERROR_HINTS,
    get_asr_client,
)


class TestMockASRClient:
    @pytest.mark.asyncio
    async def test_transcribe_bytes_returns_text(self):
        c = MockASRClient()
        text = await c.transcribe_bytes(b"\x00\x01\x02fake-mp3-bytes")
        assert isinstance(text, str)
        assert len(text) > 0

    @pytest.mark.asyncio
    async def test_transcribe_bytes_accepts_format_kwarg(self):
        c = MockASRClient()
        text = await c.transcribe_bytes(b"\x00\x01\x02", audio_format="m4a")
        assert isinstance(text, str)


class TestFactory:
    def test_returns_mock_when_provider_mock(self, monkeypatch):
        monkeypatch.setenv("ASR_PROVIDER", "mock")
        from app.config import get_settings

        get_settings.cache_clear()
        c = get_asr_client()
        assert c.name == "mock"

    def test_falls_back_to_mock_when_doubao_missing_key(self, monkeypatch):
        monkeypatch.setenv("ASR_PROVIDER", "doubao")
        monkeypatch.setenv("DOUBAO_API_KEY", "")
        from app.config import get_settings

        get_settings.cache_clear()
        c = get_asr_client()
        assert c.name == "mock"

    def test_uses_doubao_when_key_present(self, monkeypatch):
        monkeypatch.setenv("ASR_PROVIDER", "doubao")
        monkeypatch.setenv("DOUBAO_API_KEY", "fake-key-for-test")
        from app.config import get_settings

        get_settings.cache_clear()
        c = get_asr_client()
        assert c.name == "doubao"


class TestDoubaoErrorHints:
    """Verify we surface user-friendly Chinese messages for known Volcengine error codes."""

    def test_silent_audio_hint(self):
        assert "静音" in _DOUBAO_ERROR_HINTS[20000003]

    def test_invalid_params_hint(self):
        assert "参数" in _DOUBAO_ERROR_HINTS[45000001]

    def test_format_error_hint(self):
        assert "格式" in _DOUBAO_ERROR_HINTS[45000151]


class TestDoubaoClient:
    """Construction-time validation only; full HTTP path is covered by integration tests
    that monkey-patch httpx (out of scope for v0.2 first pass)."""

    def test_construct_without_key_raises(self, monkeypatch):
        monkeypatch.setenv("ASR_PROVIDER", "doubao")
        monkeypatch.setenv("DOUBAO_API_KEY", "")
        from app.config import Settings, get_settings

        get_settings.cache_clear()
        s = Settings()
        with pytest.raises(ASRError) as exc:
            DoubaoBigmodelASRClient(s)
        assert exc.value.code == "ASR_NO_KEY"


def test_asr_error_carries_code():
    e = ASRError("oops", code="ASR_NO_KEY")
    assert e.code == "ASR_NO_KEY"
