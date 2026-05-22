"""Pytest fixtures shared across all tests."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# Ensure `server/` is on sys.path so `from app.xxx import ...` works regardless of CWD.
SERVER_DIR = Path(__file__).resolve().parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))


@pytest.fixture(autouse=True)
def force_mock_provider(monkeypatch):
    """All tests run against the mock LLM/ASR by default — no network, no API keys required.

    Why autouse:
    - Prevents accidental real-network calls leaking from misconfigured local .env.
    - Tests that need to test the DeepSeek path can override this fixture explicitly.
    """
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    monkeypatch.setenv("ASR_PROVIDER", "mock")
    monkeypatch.setenv("T2V_PROVIDER", "mock")
    # Fast-forward mock T2V so polling tests don't sleep 8s each.
    monkeypatch.setenv("T2V_MOCK_DURATION_SECONDS", "0.1")
    # Reset the lru_cache so the new env vars are picked up.
    from app.config import get_settings
    import app.services.t2v_client as t2v_module

    get_settings.cache_clear()
    # Also reset the T2V mock singleton — otherwise its in-memory task store
    # leaks PROCESSING tasks across tests, causing flaky behavior.
    t2v_module._mock_singleton = None
    yield
    get_settings.cache_clear()
    t2v_module._mock_singleton = None


@pytest.fixture
def client():
    """Synchronous TestClient — covers all test cases below."""
    from fastapi.testclient import TestClient

    from app.main import create_app

    app = create_app()
    return TestClient(app)
