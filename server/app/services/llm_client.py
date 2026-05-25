"""LLM client abstraction.

Adapter + Factory，业务码依赖抽象 `LLMClient`，不直接接 provider。

Providers：
- `MockLLMClient`      离线 fixture；产品全链路 mock 模式必经
- `DoubaoArkLLMClient` 火山方舟 OpenAI 兼容 /chat/completions（默认 Doubao-Seed-2.0-lite）
- `DeepSeekLLMClient`  保留旧 provider；DeepSeek 也是 OpenAI 兼容

接口：
- `complete`            纯文本回复
- `complete_json`       封装：reply → json.loads（带一次 retry）
- `complete_with_tools` Module 7 自然语言改片专用：返回 tool_calls 列表（function-call schema）
"""
from __future__ import annotations

import json
import logging
import time
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

import httpx

from ..config import Settings, get_settings


log = logging.getLogger("seecript.llm")


HTTP_OK = 200


class LLMError(RuntimeError):
    def __init__(self, message: str, code: str = "LLM_ERROR", upstream_status: Optional[int] = None) -> None:
        super().__init__(message)
        self.code = code
        self.upstream_status = upstream_status


# --------------------------------------------------------------------------
# Abstract interface
# --------------------------------------------------------------------------
class LLMClient(ABC):
    name: str = "abstract"

    @abstractmethod
    async def complete(
        self,
        system: str,
        user: str,
        *,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> str: ...

    async def complete_json(
        self,
        system: str,
        user: str,
        *,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> Any:
        text = await self.complete(system, user, temperature=temperature, max_tokens=max_tokens)
        try:
            return _extract_json(text)
        except ValueError as first_err:
            log.warning("LLM returned non-JSON, retrying with stricter prompt: %s", first_err)

        stricter = system + "\n\n严格要求：必须返回合法 JSON。不要使用 markdown 代码块。不要在 JSON 前后添加任何文字。"
        text2 = await self.complete(stricter, user, temperature=temperature, max_tokens=max_tokens)
        try:
            return _extract_json(text2)
        except ValueError as retry_err:
            snippet = (text2 or "")[:300]
            log.error("LLM JSON parse failed after retry. snippet=%r", snippet)
            raise LLMError(
                f"模型连续两次未返回合法 JSON，请稍后重试。snippet={snippet!r}",
                code="LLM_BAD_JSON",
            ) from retry_err

    async def complete_with_tools(
        self,
        system: str,
        user: str,
        tools: list[dict],
        *,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> dict:
        """Module 7 自然语言改片专用。

        返回 dict：{"tool_calls": [{"name": str, "arguments": dict}, ...], "content": Optional[str]}
        缺省 base 实现：让子类 override；mock 走自己的实现。
        """
        raise NotImplementedError(
            f"{self.__class__.__name__} does not support tool calling; "
            "use Doubao Ark or Mock provider for module 7."
        )


def _extract_json(text: str) -> Any:
    s = text.strip()
    if s.startswith("```"):
        first_newline = s.find("\n")
        if first_newline != -1:
            s = s[first_newline + 1:]
        if s.endswith("```"):
            s = s[:-3]
        s = s.strip()
    first_brace = s.find("{")
    first_bracket = s.find("[")
    starts = [i for i in (first_brace, first_bracket) if i != -1]
    if starts:
        start = min(starts)
        end = max(s.rfind("}"), s.rfind("]"))
        if end > start:
            s = s[start: end + 1]
    return json.loads(s)


# --------------------------------------------------------------------------
# Mock
# --------------------------------------------------------------------------
class MockLLMClient(LLMClient):
    """离线 fixture。按 system prompt 中独有的输出 schema 字段名指纹路由。

    指纹键值（每个出现且仅出现在唯一 prompt 中）：
    - "sections"            → 拆解 Agent 段落结构（hook/body/cta）
    - "gap_fill_narration"  → 缺口补全 · 文案
    - "frame_tags"          → VLM 帧打标 fallback（VLMClient 也可走 LLMClient.complete）
    - "edit_tool_calls"     → Module 7 工具调用回放
    其它走 detail fallback。
    """

    name = "mock"

    async def complete(
        self,
        system: str,
        user: str,
        *,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> str:
        import asyncio
        await asyncio.sleep(0.3)
        if "edit_tool_calls" in system:
            return _MOCK_EDIT_TOOLS_JSON
        if "sections" in system and "hook" in system and "cta" in system:
            return _MOCK_DECOMPOSE_SECTIONS_JSON
        if "gap_fill_narration" in system:
            return _MOCK_GAP_FILL_JSON
        if "frame_tags" in system:
            return _MOCK_FRAME_TAGS_JSON
        return '{"detail": "mock fallback"}'

    async def complete_with_tools(
        self,
        system: str,
        user: str,
        tools: list[dict],
        *,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> dict:
        import asyncio
        await asyncio.sleep(0.3)
        # 返回一个示例 tool_calls：根据 user 指令的关键字猜动作；找不到就用第一个 tool。
        tool_name = tools[0]["function"]["name"] if tools else "edit_scene_duration"
        if "时长" in user or "更短" in user or "更长" in user:
            tool_name = "edit_scene_duration"
        elif "口语" in user or "字幕" in user or "口播" in user:
            tool_name = "edit_scene_narration"
        elif "替换" in user or "换成" in user:
            tool_name = "replace_scene_material"
        return {
            "tool_calls": [
                {
                    "name": tool_name,
                    "arguments": {"target": "[mock] scene-id-from-marks", "note": user[:80]},
                }
            ],
            "content": None,
        }


# --------------------------------------------------------------------------
# OpenAI-compatible base (Doubao Ark + DeepSeek 共用)
# --------------------------------------------------------------------------
class _OpenAICompatLLMClient(LLMClient):
    """共享逻辑：所有 OpenAI 兼容 /chat/completions 端点。子类提供 base_url / model / key。"""

    name = "openai_compat"

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
        timeout: int,
        default_temperature: float,
        default_max_tokens: int,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._timeout = timeout
        self._default_temperature = default_temperature
        self._default_max_tokens = default_max_tokens

    async def _chat(self, messages: list[dict], **extra) -> dict:
        url = f"{self._base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        payload: Dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "temperature": extra.pop("temperature", self._default_temperature),
            "max_tokens": extra.pop("max_tokens", self._default_max_tokens),
            "stream": False,
        }
        payload.update(extra)

        started = time.perf_counter()
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(url, headers=headers, json=payload)
        except httpx.TimeoutException as e:
            raise LLMError(f"{self.name} timeout after {self._timeout}s", code="LLM_TIMEOUT") from e
        except httpx.HTTPError as e:
            raise LLMError(f"{self.name} network error: {e}", code="LLM_NETWORK") from e
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        if resp.status_code != HTTP_OK:
            snippet = resp.text[:300]
            raise LLMError(
                f"{self.name} HTTP {resp.status_code}: {snippet}",
                code=f"LLM_HTTP_{resp.status_code}",
                upstream_status=resp.status_code,
            )
        try:
            data = resp.json()
        except ValueError as e:
            raise LLMError(f"{self.name} malformed JSON response", code="LLM_BAD_RESPONSE") from e
        usage = data.get("usage") or {}
        log.info("%s ok | model=%s | %dms | prompt_tok=%s | completion_tok=%s",
                 self.name, self._model, elapsed_ms,
                 usage.get("prompt_tokens"), usage.get("completion_tokens"))
        return data

    async def complete(
        self,
        system: str,
        user: str,
        *,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> str:
        data = await self._chat(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            temperature=self._default_temperature if temperature is None else temperature,
            max_tokens=self._default_max_tokens if max_tokens is None else max_tokens,
        )
        choices: List[Dict[str, Any]] = data.get("choices") or []
        if not choices:
            raise LLMError(f"{self.name} empty choices", code="LLM_BAD_RESPONSE")
        content = choices[0].get("message", {}).get("content")
        if not isinstance(content, str) or not content.strip():
            raise LLMError(f"{self.name} empty content", code="LLM_BAD_RESPONSE")
        return content

    async def complete_with_tools(
        self,
        system: str,
        user: str,
        tools: list[dict],
        *,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> dict:
        data = await self._chat(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            temperature=self._default_temperature if temperature is None else temperature,
            max_tokens=self._default_max_tokens if max_tokens is None else max_tokens,
            tools=tools,
            tool_choice="auto",
        )
        choices: List[Dict[str, Any]] = data.get("choices") or []
        if not choices:
            raise LLMError(f"{self.name} empty choices", code="LLM_BAD_RESPONSE")
        msg = choices[0].get("message", {})
        raw_calls = msg.get("tool_calls") or []
        parsed = []
        for c in raw_calls:
            fn = c.get("function", {})
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            parsed.append({"name": fn.get("name", ""), "arguments": args})
        return {"tool_calls": parsed, "content": msg.get("content")}


# --------------------------------------------------------------------------
# Doubao Ark (火山方舟) — 默认 LLM provider
# --------------------------------------------------------------------------
class DoubaoArkLLMClient(_OpenAICompatLLMClient):
    """火山方舟 ark.cn-beijing.volces.com/api/v3 ——与 OpenAI Chat API 完全兼容。

    `ark_llm_model` 实际是 endpoint_id（如 `ep-20260508213828-7ntjl`），方舟侧把它
    路由到具体的 Doubao-Seed-2.0-lite / 1.5-pro 模型实例。"""

    name = "doubao_ark"

    def __init__(self, settings: Settings) -> None:
        if not settings.ark_api_key:
            raise LLMError(
                "ARK_API_KEY is empty but LLM_PROVIDER=doubao_ark. "
                "Set the key in server/.env or switch LLM_PROVIDER=mock.",
                code="LLM_NO_KEY",
            )
        super().__init__(
            api_key=settings.ark_api_key,
            base_url=settings.ark_base_url,
            model=settings.ark_llm_model,
            timeout=settings.llm_timeout_seconds,
            default_temperature=settings.llm_temperature,
            default_max_tokens=settings.llm_max_tokens,
        )


# --------------------------------------------------------------------------
# DeepSeek (向后兼容，旧 LLM_PROVIDER=deepseek 仍可用)
# --------------------------------------------------------------------------
class DeepSeekLLMClient(_OpenAICompatLLMClient):
    name = "deepseek"

    def __init__(self, settings: Settings) -> None:
        if not settings.deepseek_api_key:
            raise LLMError(
                "DEEPSEEK_API_KEY is empty but LLM_PROVIDER=deepseek. "
                "Set the key in server/.env or switch LLM_PROVIDER=mock.",
                code="LLM_NO_KEY",
            )
        super().__init__(
            api_key=settings.deepseek_api_key,
            base_url=settings.deepseek_base_url,
            model=settings.deepseek_model,
            timeout=settings.llm_timeout_seconds,
            default_temperature=settings.llm_temperature,
            default_max_tokens=settings.llm_max_tokens,
        )


# --------------------------------------------------------------------------
# Factory
# --------------------------------------------------------------------------
def get_llm_client(settings: Optional[Settings] = None) -> LLMClient:
    s = settings or get_settings()
    provider = s.llm_provider
    if provider == "doubao_ark":
        if not s.ark_api_key:
            log.warning("LLM_PROVIDER=doubao_ark but ARK_API_KEY is empty -> using mock")
            return MockLLMClient()
        return DoubaoArkLLMClient(s)
    if provider == "deepseek":
        if not s.deepseek_api_key:
            log.warning("LLM_PROVIDER=deepseek but DEEPSEEK_API_KEY is empty -> using mock")
            return MockLLMClient()
        return DeepSeekLLMClient(s)
    return MockLLMClient()


# --------------------------------------------------------------------------
# Mock fixtures
# --------------------------------------------------------------------------
_MOCK_DECOMPOSE_SECTIONS_JSON = """
{
  "sections": [
    {"kind": "hook", "start": 0.0, "end": 4.5,
     "summary": "痛点提问 + 大字幕开场——首镜对准产品配文『你是不是也踩过这个坑？』",
     "shot_indices": [0, 1]},
    {"kind": "body", "start": 4.5, "end": 25.5,
     "summary": "三段对比：原方案问题 → 新方案演示 → 效果对比，节奏 BPM≈120 切镜",
     "shot_indices": [2, 3, 4, 5, 6, 7, 8, 9]},
    {"kind": "cta", "start": 25.5, "end": 30.5,
     "summary": "大字幕收尾 + 评论引导『你试过哪种？』",
     "shot_indices": [10, 11]}
  ]
}
"""

_MOCK_GAP_FILL_JSON = """
{
  "gap_fill_narration": "现在镜头切到产品特写——你看这个细节，跟刚才完全不一样了。",
  "alternatives": [
    "再来一个角度，差距更明显。",
    "对比下来，差别就在这里。"
  ],
  "notes": "[mock] 已按口语化 + 强反差 + 6 秒以内三个约束生成"
}
"""

_MOCK_FRAME_TAGS_JSON = """
{
  "frame_tags": [
    {"frame_id": "f-001", "tags": ["室内", "近景", "口播", "纯色背景"], "subtitle_style": "大字加描边"},
    {"frame_id": "f-002", "tags": ["产品特写", "环形光", "白色桌面"], "subtitle_style": "大字加描边"},
    {"frame_id": "f-003", "tags": ["对比镜头", "实拍", "户外"], "subtitle_style": "无字幕"}
  ]
}
"""

_MOCK_EDIT_TOOLS_JSON = """
{
  "tool_calls": [
    {"name": "edit_scene_narration",
     "arguments": {"scene_id": "sc-2", "narration": "[mock] 改写后的口语化口播"}}
  ]
}
"""
