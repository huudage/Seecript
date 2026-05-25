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
    """Mock 通过 system prompt 中的 schema 关键字路由到不同的 fixture（见 MockLLMClient 文档）。"""

    @pytest.mark.asyncio
    async def test_decompose_sections_fixture(self):
        c = MockLLMClient()
        text = await c.complete(
            'output JSON with "sections" array, each item has "kind" (hook|body|cta)', "u"
        )
        data = json.loads(text)
        assert "sections" in data and len(data["sections"]) == 3
        assert {s["kind"] for s in data["sections"]} == {"hook", "body", "cta"}

    @pytest.mark.asyncio
    async def test_gap_fill_fixture(self):
        c = MockLLMClient()
        text = await c.complete(
            'output JSON with "gap_fill_narration" string for the missing slot', "u"
        )
        data = json.loads(text)
        assert "gap_fill_narration" in data
        assert isinstance(data["gap_fill_narration"], str)

    @pytest.mark.asyncio
    async def test_frame_tags_fixture(self):
        c = MockLLMClient()
        text = await c.complete('output JSON with "frame_tags" list per frame', "u")
        data = json.loads(text)
        assert "frame_tags" in data and len(data["frame_tags"]) >= 1

    @pytest.mark.asyncio
    async def test_complete_json_parses(self):
        c = MockLLMClient()
        data = await c.complete_json('output JSON with "sections" hook body cta', "u")
        assert isinstance(data, dict)

    @pytest.mark.asyncio
    async def test_complete_with_tools_returns_tool_calls(self):
        c = MockLLMClient()
        tools = [{
            "type": "function",
            "function": {
                "name": "edit_scene_narration",
                "description": "改写指定 scene 的口播文字",
                "parameters": {"type": "object", "properties": {"scene_id": {"type": "string"}}},
            },
        }]
        result = await c.complete_with_tools(
            "你是一个视频编辑助手", "把这一段口播改成更口语化的版本", tools
        )
        assert "tool_calls" in result
        assert len(result["tool_calls"]) >= 1
        assert result["tool_calls"][0]["name"] == "edit_scene_narration"


class TestFactory:
    def test_returns_mock_when_provider_mock(self, monkeypatch):
        monkeypatch.setenv("LLM_PROVIDER", "mock")
        from app.config import get_settings

        get_settings.cache_clear()
        c = get_llm_client()
        assert c.name == "mock"

    def test_falls_back_to_mock_when_doubao_ark_missing_key(self, monkeypatch):
        monkeypatch.setenv("LLM_PROVIDER", "doubao_ark")
        monkeypatch.setenv("ARK_API_KEY", "")
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
