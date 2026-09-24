"""Tests for LLM client abstraction layer."""
from __future__ import annotations

import json

import pytest

from app.config import Settings
from app.services.llm_client import (
    DoubaoArkLLMClient,
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
    async def test_decompose_shot_roles_fixture(self):
        c = MockLLMClient()
        # mock 解析 user 文本里的 "<idx>: <start>-<end>s" 行数 → 动态生成 shot_roles。
        user_payload = (
            "镜头列表：\n"
            "0: 0.0-3.0s | (无口播)\n"
            "1: 3.0-7.5s | (无口播)\n"
            "2: 7.5-12.0s | (无口播)\n"
            "3: 12.0-18.0s | (无口播)\n"
        )
        text = await c.complete(
            'output JSON with "shot_roles" array, each item has shot_index + role + theme '
            '(opening/development/climax/closing)', user_payload
        )
        data = json.loads(text)
        assert "shot_roles" in data
        assert len(data["shot_roles"]) == 4
        roles = [s["role"] for s in data["shot_roles"]]
        # 第一镜头必须 opening、最后必须 closing
        assert roles[0] == "opening"
        assert roles[-1] == "closing"
        # 中间镜头不允许 opening/closing
        for r in roles[1:-1]:
            assert r in ("development", "climax")
        # 至多 1 个 climax
        assert roles.count("climax") <= 1

    @pytest.mark.asyncio
    async def test_understanding_fixture(self):
        c = MockLLMClient()
        text = await c.complete(
            'output JSON with "archetype" and "narrative_summary" describing the video', "u"
        )
        data = json.loads(text)
        assert "archetype" in data
        assert "narrative_summary" in data
        assert "suggested_segments" in data

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
        data = await c.complete_json(
            'output JSON with "shot_roles" array using role + theme schema',
            "0: 0.0-2.0s | x\n1: 2.0-4.0s | y",
        )
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

    @pytest.mark.asyncio
    async def test_multimodal_frame_tags_routes_to_fixture(self):
        """多模态接口在 system 含 'frame_tags' 时返回打标 fixture。"""
        c = MockLLMClient()
        text = await c.complete_multimodal(
            "你是短视频画面打标助手……返回 frame_tags JSON",
            "请给这些关键帧打标",
            ["data:image/png;base64,iVBORw0KGgo="],
        )
        data = json.loads(text)
        assert "frame_tags" in data

    @pytest.mark.asyncio
    async def test_multimodal_understanding_routes_to_fixture(self):
        """system 含 archetype + narrative_summary 时多模态返回视频画像 fixture。"""
        c = MockLLMClient()
        text = await c.complete_multimodal(
            "你是视频内容分析师，请输出 JSON 含 archetype 和 narrative_summary",
            "关键帧列表",
            [""],
        )
        data = json.loads(text)
        assert "archetype" in data and "narrative_summary" in data

    @pytest.mark.asyncio
    async def test_multimodal_shot_roles_returns_role_fixture(self):
        """system 含 'shot_roles' + 'role' 时多模态返回 per-shot 角色 fixture。"""
        c = MockLLMClient()
        text = await c.complete_multimodal(
            "你是短视频结构分析师，请为每个镜头输出 shot_roles JSON，含 role + theme",
            "0: 0.0-2.0s | (无口播)\n1: 2.0-5.0s | (无口播)\n2: 5.0-8.0s | (无口播)",
            ["", "", ""],
        )
        data = json.loads(text)
        assert "shot_roles" in data
        assert len(data["shot_roles"]) == 3
        roles = [s["role"] for s in data["shot_roles"]]
        assert roles[0] == "opening"
        assert roles[-1] == "closing"
        for s in data["shot_roles"]:
            assert s["role"] in {"opening", "development", "climax", "closing"}


class TestFactory:
    def test_returns_mock_when_provider_mock(self, monkeypatch):
        monkeypatch.setenv("LLM_PROVIDER", "mock")
        from app.config import get_settings

        get_settings.cache_clear()
        c = get_llm_client()
        assert c.name == "mock"

    def test_doubao_ark_missing_key_raises(self, monkeypatch):
        """Stage-prod #1：缺 key 不再 silent fallback，硬失败给前端 500。"""
        monkeypatch.setenv("LLM_PROVIDER", "doubao_ark")
        monkeypatch.setenv("ARK_API_KEY", "")
        from app.config import get_settings

        get_settings.cache_clear()
        with pytest.raises(LLMError) as exc_info:
            get_llm_client()
        assert exc_info.value.code == "LLM_NO_KEY"

    def test_deepseek_missing_key_raises(self, monkeypatch):
        """Stage-prod #1：DeepSeek 缺 key 同样硬失败。"""
        monkeypatch.setenv("LLM_PROVIDER", "deepseek")
        monkeypatch.setenv("DEEPSEEK_API_KEY", "")
        from app.config import get_settings

        get_settings.cache_clear()
        with pytest.raises(LLMError) as exc_info:
            get_llm_client()
        assert exc_info.value.code == "LLM_NO_KEY"


class TestErrors:
    def test_llm_error_carries_code(self):
        e = LLMError("oops", code="LLM_TIMEOUT")
        assert e.code == "LLM_TIMEOUT"
        assert "oops" in str(e)


class _FakeResponse:
    def __init__(self, payload: dict, status_code: int = 200) -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self) -> dict:
        return self._payload


class _FakeAsyncClient:
    """记录 Doubao 客户端实际 POST 的 URL 和 body，不访问网络。"""

    captured: list[tuple[str, dict]] = []

    def __init__(self, *args, **kwargs) -> None:
        del args, kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def post(self, url, headers, json):
        del headers
        _FakeAsyncClient.captured.append((url, json))
        return _FakeResponse({
            "status": "completed",
            "output": [{
                "type": "message",
                "content": [{"type": "output_text", "text": '{"ok": true}'}],
            }],
            "usage": {"input_tokens": 3, "output_tokens": 4},
        })


class TestDoubaoResponses:
    def _client(self) -> DoubaoArkLLMClient:
        return DoubaoArkLLMClient(Settings(
            llm_provider="doubao_ark",
            ark_api_key="test-key",
            ark_llm_model="doubao-seed-2-1-lite-260915",
        ))

    @pytest.mark.asyncio
    async def test_complete_posts_responses_api(self, monkeypatch):
        _FakeAsyncClient.captured = []
        monkeypatch.setattr("app.services.llm_client.httpx.AsyncClient", _FakeAsyncClient)
        text = await self._client().complete("只输出 JSON", "ping")
        assert text == '{"ok": true}'
        url, body = _FakeAsyncClient.captured[-1]
        assert url.endswith("/responses")
        assert body["model"] == "doubao-seed-2-1-lite-260915"
        assert body["stream"] is False
        assert body["store"] is False
        assert body["thinking"] == {"type": "disabled"}
        assert body["instructions"] == "只输出 JSON"
        assert body["input"][0]["content"][0] == {"type": "input_text", "text": "ping"}
        assert "tools" not in body

    @pytest.mark.asyncio
    async def test_multimodal_uses_input_image(self, monkeypatch):
        _FakeAsyncClient.captured = []
        monkeypatch.setattr("app.services.llm_client.httpx.AsyncClient", _FakeAsyncClient)
        await self._client().complete_multimodal("看图", "描述", ["https://example.com/a.png"])
        _, body = _FakeAsyncClient.captured[-1]
        parts = body["input"][0]["content"]
        assert parts[0]["type"] == "input_text"
        assert parts[1] == {"type": "input_image", "image_url": "https://example.com/a.png"}

    @pytest.mark.asyncio
    async def test_tools_are_flattened_for_responses(self, monkeypatch):
        _FakeAsyncClient.captured = []
        monkeypatch.setattr("app.services.llm_client.httpx.AsyncClient", _FakeAsyncClient)
        tools = [{
            "type": "function",
            "function": {
                "name": "edit_scene_narration",
                "description": "改口播",
                "parameters": {"type": "object", "properties": {}},
            },
        }]
        await self._client().complete_with_tools("sys", "user", tools)
        _, body = _FakeAsyncClient.captured[-1]
        assert body["tools"][0]["name"] == "edit_scene_narration"
        assert "function" not in body["tools"][0]
