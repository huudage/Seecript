"""Tests for LLM client abstraction layer."""
from __future__ import annotations

import json

import pytest

from app.services.llm_client import (
    LLMError,
    MockLLMClient,
    _extract_json,
    get_llm_client,
)


class TestExtractJson:
    def test_plain_json_object(self):
        assert _extract_json('{"a": 1}') == {"a": 1}

    def test_plain_json_array(self):
        assert _extract_json("[1, 2, 3]") == [1, 2, 3]

    def test_strips_markdown_fence(self):
        text = '```json\n{"key": "value"}\n```'
        assert _extract_json(text) == {"key": "value"}

    def test_strips_bare_fence(self):
        text = '```\n{"key": "value"}\n```'
        assert _extract_json(text) == {"key": "value"}

    def test_handles_preamble_before_json(self):
        text = 'Sure, here you go:\n{"k": 1}'
        assert _extract_json(text) == {"k": 1}

    def test_raises_on_garbage(self):
        with pytest.raises(json.JSONDecodeError):
            _extract_json("not json at all")


class TestMockLLMClient:
    """Mock fingerprints each prompt by output-schema field name (see MockLLMClient docstring)."""

    @pytest.mark.asyncio
    async def test_persona_fixture_returned(self):
        c = MockLLMClient()
        text = await c.complete('output JSON: {"personas": [...]}', "u")
        data = json.loads(text)
        assert "personas" in data and len(data["personas"]) >= 1

    @pytest.mark.asyncio
    async def test_seo_fixture_returned(self):
        c = MockLLMClient()
        text = await c.complete('schema requires "broad_traffic" array', "u")
        data = json.loads(text)
        assert "titles" in data and "tags" in data

    @pytest.mark.asyncio
    async def test_skeleton_fixture_returned(self):
        c = MockLLMClient()
        text = await c.complete('schema requires "transferable_template"', "u")
        data = json.loads(text)
        assert "hook" in data and "body" in data and "cta" in data

    @pytest.mark.asyncio
    async def test_comments_fixture_returned(self):
        c = MockLLMClient()
        text = await c.complete('schema requires "low_value_count" int', "u")
        data = json.loads(text)
        assert "high_value" in data and "medium_value" in data

    @pytest.mark.asyncio
    async def test_complete_json_parses(self):
        c = MockLLMClient()
        data = await c.complete_json('schema requires "personas"', "u")
        assert isinstance(data, dict)


class TestFactory:
    def test_returns_mock_when_provider_mock(self, monkeypatch):
        monkeypatch.setenv("LLM_PROVIDER", "mock")
        from app.config import get_settings

        get_settings.cache_clear()
        c = get_llm_client()
        assert c.name == "mock"

    def test_falls_back_to_mock_when_deepseek_missing_key(self, monkeypatch):
        monkeypatch.setenv("LLM_PROVIDER", "deepseek")
        monkeypatch.setenv("DEEPSEEK_API_KEY", "")
        from app.config import get_settings

        get_settings.cache_clear()
        c = get_llm_client()
        assert c.name == "mock"


class TestErrors:
    def test_llm_error_carries_code(self):
        e = LLMError("oops", code="LLM_TIMEOUT")
        assert e.code == "LLM_TIMEOUT"
        assert "oops" in str(e)
