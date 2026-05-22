"""Tests for T2V (text-to-video) client abstraction layer."""
from __future__ import annotations

import asyncio

import pytest

from app.services.t2v_client import (
    MockT2VClient,
    T2VError,
    ZhipuT2VClient,
    get_t2v_client,
)


# ---------------------------------------------------------------------------
# Mock client behaviour
# ---------------------------------------------------------------------------
class TestMockT2VClient:
    @pytest.mark.asyncio
    async def test_submit_returns_task_id(self):
        c = MockT2VClient(mock_duration_seconds=0.05)
        result = await c.submit(
            "a cat playing piano",
            size="720x1280",
            quality="speed",
            with_audio=False,
            user_id="seecript-test-user",
        )
        assert result.task_id.startswith("mock-")
        assert result.request_id.startswith("req-")
        assert "mock" in result.model

    @pytest.mark.asyncio
    async def test_query_pending_then_succeeded(self):
        """The mock client must transition PROCESSING→SUCCESS after duration elapses.

        This is the contract the frontend polling loop is built on; if mock
        flips immediately to SUCCESS we lose coverage on the polling path.
        """
        c = MockT2VClient(mock_duration_seconds=0.2)
        submitted = await c.submit(
            "test prompt",
            size="720x1280",
            quality="speed",
            with_audio=False,
            user_id="seecript-test-user",
        )
        # Immediately after submit → still pending
        first = await c.query(submitted.task_id)
        assert first.status == "pending"
        assert first.video_url is None

        # Wait past the mock duration, query again → succeeded
        await asyncio.sleep(0.3)
        second = await c.query(submitted.task_id)
        assert second.status == "succeeded"
        assert second.video_url is not None
        assert second.video_url.startswith("http")

    @pytest.mark.asyncio
    async def test_query_unknown_task_raises(self):
        c = MockT2VClient(mock_duration_seconds=0.1)
        with pytest.raises(T2VError) as exc:
            await c.query("non-existent-task-id")
        assert exc.value.code == "T2V_TASK_NOT_FOUND"


# ---------------------------------------------------------------------------
# Factory + provider selection
# ---------------------------------------------------------------------------
class TestFactory:
    def test_returns_mock_when_provider_mock(self, monkeypatch):
        monkeypatch.setenv("T2V_PROVIDER", "mock")
        from app.config import get_settings

        get_settings.cache_clear()
        c = get_t2v_client()
        assert c.name == "mock"

    def test_falls_back_to_mock_when_zhipu_missing_key(self, monkeypatch):
        """Critical safety net: if user typo-ed T2V_PROVIDER=zhipu but didn't
        set the key, we fall back to mock with a warning rather than crashing
        the whole app at boot."""
        monkeypatch.setenv("T2V_PROVIDER", "zhipu")
        monkeypatch.setenv("ZHIPU_API_KEY", "")
        from app.config import get_settings

        get_settings.cache_clear()
        c = get_t2v_client()
        assert c.name == "mock"

    def test_uses_zhipu_when_key_present(self, monkeypatch):
        monkeypatch.setenv("T2V_PROVIDER", "zhipu")
        monkeypatch.setenv("ZHIPU_API_KEY", "fake-key-for-test-only")
        from app.config import get_settings

        get_settings.cache_clear()
        c = get_t2v_client()
        assert c.name == "zhipu"

    def test_mock_singleton_preserves_task_store(self, monkeypatch):
        """The factory must return the SAME mock instance across calls so the
        in-memory task dict survives between submit() and the next query()."""
        monkeypatch.setenv("T2V_PROVIDER", "mock")
        from app.config import get_settings

        get_settings.cache_clear()
        c1 = get_t2v_client()
        c2 = get_t2v_client()
        assert c1 is c2


# ---------------------------------------------------------------------------
# Zhipu construction
# ---------------------------------------------------------------------------
class TestZhipuClient:
    def test_construct_without_key_raises(self, monkeypatch):
        monkeypatch.setenv("T2V_PROVIDER", "zhipu")
        monkeypatch.setenv("ZHIPU_API_KEY", "")
        from app.config import Settings, get_settings

        get_settings.cache_clear()
        s = Settings()
        with pytest.raises(T2VError) as exc:
            ZhipuT2VClient(s)
        assert exc.value.code == "T2V_NO_KEY"

    def test_construct_with_key_succeeds(self, monkeypatch):
        monkeypatch.setenv("T2V_PROVIDER", "zhipu")
        monkeypatch.setenv("ZHIPU_API_KEY", "fake-key")
        from app.config import Settings, get_settings

        get_settings.cache_clear()
        s = Settings()
        c = ZhipuT2VClient(s)
        assert c.name == "zhipu"


def test_t2v_error_carries_code_and_status():
    e = T2VError("oops", code="T2V_TIMEOUT", upstream_status=504)
    assert e.code == "T2V_TIMEOUT"
    assert e.upstream_status == 504
