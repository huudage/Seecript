"""LLM client abstraction.

Adapter + Factory，业务码依赖抽象 `LLMClient`，不直接接 provider。

Providers：
- `MockLLMClient`      离线 fixture；产品全链路 mock 模式必经
- `DoubaoArkLLMClient` 火山方舟 OpenAI 兼容 /chat/completions（默认 Doubao-Seed-2.0-lite）
- `DeepSeekLLMClient`  保留旧 provider；DeepSeek 也是 OpenAI 兼容

接口：
- `complete`             纯文本回复
- `complete_json`        封装：reply → json.loads（带一次 retry）
- `complete_with_tools`  Module 7 自然语言改片专用：返回 tool_calls 列表
- `complete_multimodal`  多模态：文字 + 图像（缩略图列表）→ 文字回复
                         doubao-seed-2.0-lite 已替代独立 VLM client，画面理解全走这里。
"""
from __future__ import annotations

import base64
import json
import logging
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, AsyncIterator, Dict, List, Optional, Sequence

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

    async def stream_complete(
        self,
        system: str,
        user: str,
        *,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> AsyncIterator[str]:
        """逐 token 流式 yield 文本。
        默认实现:fallback 到 complete() 整段一次性 yield,保证所有 provider 至少能用。
        真流式由子类 override(MockLLMClient 切片模拟,_OpenAICompatLLMClient 走 SSE)。
        """
        text = await self.complete(
            system, user, temperature=temperature, max_tokens=max_tokens,
        )
        if text:
            yield text

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

    async def complete_multimodal(
        self,
        system: str,
        user_text: str,
        images: Sequence[str | Path],
        *,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> str:
        """多模态：文字 + 图像列表 → 文字回复。

        - `images` 元素可以是本地路径（自动转 data URL）或 http(s)/data URL（直接透传）。
        - 默认实现回落到纯文本 `complete`，把"看不到图"的事实写进 system prompt——
          mock provider 走这条；DoubaoArk override 真正塞 image_url。
        """
        fallback_system = system + "\n\n（注：当前 provider 不支持图像输入，仅按文字描述推断。）"
        fallback_user = user_text + f"\n\n[图像数量：{len(images)}，描述略]"
        return await self.complete(
            fallback_system, fallback_user,
            temperature=temperature, max_tokens=max_tokens,
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


_SERVER_ROOT = Path(__file__).resolve().parents[2]  # server/


def _resolve_local_image(s: str) -> Optional[Path]:
    """把后端虚拟 URL（/samples/xxx, /uploads/xxx, /assets/xxx）映射回 server/ 目录下的真实磁盘路径。

    其它绝对/相对路径直接尝试；找不到则返回 None 让调用方走占位。
    """
    if s.startswith("/samples/"):
        p = _SERVER_ROOT / "samples" / s[len("/samples/"):].lstrip("/")
        return p if p.is_file() else None
    if s.startswith("/uploads/"):
        p = _SERVER_ROOT / "var" / "uploads" / s[len("/uploads/"):].lstrip("/")
        return p if p.is_file() else None
    if s.startswith("/assets/"):
        p = _SERVER_ROOT / "var" / "assets" / s[len("/assets/"):].lstrip("/")
        return p if p.is_file() else None
    p = Path(s)
    return p if p.is_file() else None


def _image_ref_to_url(ref: str | Path) -> str:
    """把图像引用归一化成可放进 OpenAI multimodal `image_url.url` 的字符串。

    规则：
    - http(s):// 或 data: 开头视为已经就绪，直接透传
    - 后端虚拟 URL（/samples/xxx / /uploads/xxx）先映射回 server/ 下的物理路径
    - 物理路径存在 → data:image/<ext>;base64,<...>
    - 解析失败 → 16×16 占位 PNG，让模型仍能拿到结构合法的 user content
    """
    s = str(ref)
    if s.startswith(("http://", "https://", "data:")):
        return s
    real = _resolve_local_image(s)
    if real is None:
        # 占位：16×16 灰色 PNG 的 base64。Ark 等服务端要求最小 14×14，
        # 用更小的图（如 1×1）会被拒 InvalidParameter；这里给 16×16 保险。
        placeholder = (
            "iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAIAAACQkWg2AAAAI0lEQVR4nGM8c"
            "eIEAymAiSTVDKMaiANMRKqDg1ENxACSQwkAXUkCeDMzQHkAAAAASUVORK5CYII="
        )
        return f"data:image/png;base64,{placeholder}"
    ext = real.suffix.lower().lstrip(".") or "jpeg"
    if ext == "jpg":
        ext = "jpeg"
    b64 = base64.b64encode(real.read_bytes()).decode("ascii")
    return f"data:image/{ext};base64,{b64}"


# --------------------------------------------------------------------------
# Mock
# --------------------------------------------------------------------------
class MockLLMClient(LLMClient):
    """离线 fixture。按 system prompt 中独有的输出 schema 字段名指纹路由。

    指纹键值（每个出现且仅出现在唯一 prompt 中）：
    - "sections"            → 拆解 Agent 段落结构（按 video_type 路由到 marketing/editing/motion_graph 三组 kind）
    - "gap_fill_narration"  → 缺口补全 · 文案
    - "frame_tags"          → 多模态帧打标 fallback（seed-2.0-lite 走 LLMClient.complete_multimodal）
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
        # 意图澄清助手:走 _build_mock_clarify_text,根据 IS_FINAL 决定是否带 QUESTION
        if "短视频脚本意图澄清助手" in system:
            return _build_mock_clarify_text(user)
        # aigc_prompt_agent 转写：指纹 t2v_prompt（在 adapted_sections 之前免冲突，因 system 都长）
        if "t2v_prompt" in system:
            return _build_mock_t2v_prompt_json(user)
        # aigc_prompt_agent 参考图策展：image_spec 指纹（在 t2v_prompt 之后，因为 image-spec 不含 t2v_prompt）
        if "参考图策展人 Agent" in system:
            return _build_mock_image_spec_json(user)
        # copy_outline_agent 文案大纲：指纹 copy_outline（在 adapted_sections 之前免冲突）
        if "copy_outline" in system:
            return _build_mock_copy_outline_json(user)
        # plan_agent 结构改编：指纹 adapted_sections + content_description（在 shot_roles 之前避免误匹配）
        if "adapted_sections" in system and "content_description" in system:
            return _build_mock_adapted_sections_json(user)
        # 新 decompose 拆分两阶段：理解 → shot-first 切段。各自有独有指纹。
        if "archetype" in system and "narrative_summary" in system:
            return _MOCK_UNDERSTANDING_JSON
        if "shot_roles" in system and "role" in system:
            return _build_mock_shot_roles_json(user)
        if "gap_fill_narration" in system:
            return _MOCK_GAP_FILL_JSON
        if "frame_tags" in system:
            return _MOCK_FRAME_TAGS_JSON
        # 包装 V2 指纹：5 维度候选（subtitle_styles + transition_bundles）
        if "subtitle_styles" in system and "transition_bundles" in system:
            return _build_mock_packaging_v2_json(user)
        if "transitions" in system and "palette" in system:
            return _MOCK_PACKAGING_JSON
        return '{"detail": "mock fallback"}'

    async def stream_complete(
        self,
        system: str,
        user: str,
        *,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> AsyncIterator[str]:
        """Mock 流式:把 complete() 的整段输出按 ~24 字切片 yield,模拟打字机。
        意图澄清场景能让前端看到 ===DRAFT=== 段流出来,跟真链路体感一致。
        """
        import asyncio
        text = await self.complete(
            system, user, temperature=temperature, max_tokens=max_tokens,
        )
        chunk = 24
        for i in range(0, len(text), chunk):
            await asyncio.sleep(0.02)
            yield text[i : i + chunk]

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

    async def complete_multimodal(
        self,
        system: str,
        user_text: str,
        images: Sequence[str | Path],
        *,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> str:
        """Mock 多模态：忽略实际图像内容，按 system 指纹路由到 fixture。

        - "frame_tags" 在 system → 返回 _MOCK_FRAME_TAGS_JSON
        - "archetype" + "narrative_summary" → 返回 _MOCK_UNDERSTANDING_JSON（视频画像阶段）
        - "shot_roles" + "role" → **动态**生成 shot-first roles（按 user 文本里的镜头条数）
        - 其它走父类 fallback（拼"看不到图"提示再 complete）
        """
        import asyncio
        await asyncio.sleep(0.4)
        if "frame_tags" in system:
            return _MOCK_FRAME_TAGS_JSON
        if "t2v_prompt" in system:
            return _build_mock_t2v_prompt_json(user_text)
        if "参考图策展人 Agent" in system:
            return _build_mock_image_spec_json(user_text)
        if "copy_outline" in system:
            return _build_mock_copy_outline_json(user_text)
        if "adapted_sections" in system and "content_description" in system:
            return _build_mock_adapted_sections_json(user_text)
        if "archetype" in system and "narrative_summary" in system:
            return _MOCK_UNDERSTANDING_JSON
        if "shot_roles" in system and "role" in system:
            return _build_mock_shot_roles_json(user_text)
        return await super().complete_multimodal(
            system, user_text, images,
            temperature=temperature, max_tokens=max_tokens,
        )


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

    async def stream_complete(
        self,
        system: str,
        user: str,
        *,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> AsyncIterator[str]:
        """OpenAI 兼容 /chat/completions stream=True SSE:逐 token yield delta.content。
        服务端规范:每行 `data: {...}` 内含 choices[0].delta.content。`data: [DONE]` 收流。
        网络/超时/解析失败统一抛 LLMError,与 _chat 错误码风格一致。
        """
        url = f"{self._base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }
        payload: Dict[str, Any] = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": self._default_temperature if temperature is None else temperature,
            "max_tokens": self._default_max_tokens if max_tokens is None else max_tokens,
            "stream": True,
        }
        started = time.perf_counter()
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                async with client.stream("POST", url, headers=headers, json=payload) as resp:
                    if resp.status_code != HTTP_OK:
                        snippet = (await resp.aread()).decode("utf-8", errors="replace")[:300]
                        raise LLMError(
                            f"{self.name} HTTP {resp.status_code}: {snippet}",
                            code=f"LLM_HTTP_{resp.status_code}",
                            upstream_status=resp.status_code,
                        )
                    async for line in resp.aiter_lines():
                        if not line:
                            continue
                        if not line.startswith("data:"):
                            continue
                        body = line[5:].strip()
                        if body == "[DONE]":
                            break
                        try:
                            chunk = json.loads(body)
                        except ValueError:
                            continue
                        choices = chunk.get("choices") or []
                        if not choices:
                            continue
                        delta = (choices[0].get("delta") or {}).get("content")
                        if isinstance(delta, str) and delta:
                            yield delta
        except httpx.TimeoutException as e:
            raise LLMError(f"{self.name} stream timeout after {self._timeout}s", code="LLM_TIMEOUT") from e
        except httpx.HTTPError as e:
            raise LLMError(f"{self.name} stream network error: {e}", code="LLM_NETWORK") from e
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        log.info("%s stream ok | model=%s | %dms", self.name, self._model, elapsed_ms)

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

    async def complete_multimodal(
        self,
        system: str,
        user_text: str,
        images: Sequence[str | Path],
        *,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> str:
        """OpenAI 兼容多模态：user message content 是 text + image_url 数组。

        所有 doubao-seed-2.0-lite 多模态画面理解任务（关键帧打标 / 段落分段 / 缺口判定）都走这个入口。
        """
        if not images:
            return await self.complete(
                system, user_text, temperature=temperature, max_tokens=max_tokens,
            )
        content: list[dict] = [{"type": "text", "text": user_text}]
        for ref in images:
            content.append({
                "type": "image_url",
                "image_url": {"url": _image_ref_to_url(ref)},
            })
        data = await self._chat(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": content},
            ],
            temperature=self._default_temperature if temperature is None else temperature,
            max_tokens=self._default_max_tokens if max_tokens is None else max_tokens,
        )
        choices: List[Dict[str, Any]] = data.get("choices") or []
        if not choices:
            raise LLMError(f"{self.name} empty choices", code="LLM_BAD_RESPONSE")
        msg_content = choices[0].get("message", {}).get("content")
        if not isinstance(msg_content, str) or not msg_content.strip():
            raise LLMError(f"{self.name} empty multimodal content", code="LLM_BAD_RESPONSE")
        return msg_content


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
    """生产工厂：缺 key 直接 raise LLMError，不再静默降级到 MockLLMClient。

    保留 LLM_PROVIDER=mock 仅用于单元测试（pytest fixture 显式设置），
    生产 .env 必须配真 provider + 对应 key。
    """
    s = settings or get_settings()
    provider = s.llm_provider
    if provider == "doubao_ark":
        if not s.ark_api_key:
            raise LLMError(
                "LLM_PROVIDER=doubao_ark 但 ARK_API_KEY 为空——生产环境不允许静默降级到 mock。"
                "请在 server/.env 配置真 key。",
                code="LLM_NO_KEY",
            )
        return DoubaoArkLLMClient(s)
    if provider == "deepseek":
        if not s.deepseek_api_key:
            raise LLMError(
                "LLM_PROVIDER=deepseek 但 DEEPSEEK_API_KEY 为空——生产环境不允许静默降级到 mock。"
                "请在 server/.env 配置真 key。",
                code="LLM_NO_KEY",
            )
        return DeepSeekLLMClient(s)
    if provider == "mock":
        # 仅供单元测试 monkeypatch 显式启用；线上 .env 永远不应是 mock
        return MockLLMClient()
    raise LLMError(
        f"未知 LLM_PROVIDER={provider!r}；生产支持 doubao_ark / deepseek，单测可用 mock。",
        code="LLM_BAD_PROVIDER",
    )


# --------------------------------------------------------------------------
# Mock fixtures
# --------------------------------------------------------------------------
_MOCK_UNDERSTANDING_JSON = """
{
  "archetype": "通用短视频原型",
  "narrative_summary": "[mock] 视频以氛围镜头开场，中段铺陈主体内容并在中后段拉到情绪高潮，结尾给出引导或落版。",
  "suggested_segments": 4,
  "tone": "中性平稳"
}
"""

def _build_mock_shot_roles_json(user_text: str) -> str:
    """动态生成 shot_roles mock：从 user payload 解析镜头数与 structural_pattern，按位置分配 role。

    pattern 检测：扫 user_text 里的 'structural_pattern: <name>' 或类似字串。默认 dramatic。
    分配策略（按 pattern 分支）：
    - dramatic    : 首=opening, 末=closing, 60% 位置=climax（n≥4）, 其余=development
    - stepwise    : 首=intro, 末=recap, 中间 step_1, step_2, ...
    - listicle    : 首=hook, 末=closer, 中间 item_1, item_2, ...
    - atmospheric : 首=establish, 末=resolve, 60%=peak（n≥4）, 其余=flow
    - info_dense  : 首=title_card, 末=payoff, 中间 info_block
    """
    import re
    import json
    shot_lines = re.findall(r"^(\d+):\s*[\d.]+", user_text, re.MULTILINE)
    n = len(shot_lines)
    if n == 0:
        n = 2

    pat = "dramatic"
    m = re.search(r"structural[_\s]?pattern[：:\s]*([a-z_]+)", user_text, re.IGNORECASE)
    if m and m.group(1).lower() in ("dramatic", "stepwise", "listicle", "atmospheric", "info_dense"):
        pat = m.group(1).lower()

    roles: list[dict] = []
    has_peak = pat in ("dramatic", "atmospheric")
    peak_idx = int(n * 0.6) if has_peak and n >= 4 else None
    if peak_idx is not None and (peak_idx <= 0 or peak_idx >= n - 1):
        peak_idx = None

    main_counter = 0
    for i in range(n):
        if i == 0:
            role, theme = {
                "dramatic":    ("opening",    "开场铺垫"),
                "stepwise":    ("intro",      "引入"),
                "listicle":    ("hook",       "钩子"),
                "atmospheric": ("establish",  "起势"),
                "info_dense":  ("title_card", "标题卡"),
            }[pat]
        elif i == n - 1:
            role, theme = {
                "dramatic":    ("closing", "余韵收尾"),
                "stepwise":    ("recap",   "总结"),
                "listicle":    ("closer",  "收尾"),
                "atmospheric": ("resolve", "余韵"),
                "info_dense":  ("payoff",  "落版"),
            }[pat]
        elif i == peak_idx:
            role, theme = ("climax", "情绪高潮") if pat == "dramatic" else ("peak", "顶点")
        else:
            main_counter += 1
            if pat == "dramatic":
                role, theme = "development", f"主体铺陈{main_counter}"
            elif pat == "stepwise":
                role, theme = f"step_{main_counter}", f"步骤 {main_counter}"
            elif pat == "listicle":
                role, theme = f"item_{main_counter}", f"第 {main_counter} 项"
            elif pat == "atmospheric":
                role, theme = "flow", f"流转{main_counter}"
            else:  # info_dense
                role, theme = "info_block", f"信息块{main_counter}"
        roles.append({"shot_index": i, "role": role, "theme": theme})

    return json.dumps({"shot_roles": roles, "tempo": "medium"}, ensure_ascii=False)


def _build_mock_adapted_sections_json(user_text: str) -> str:
    """动态生成 adapted_sections mock：从 user payload 解析 '原样例共 N 段' 拿到 N，
    解析 structural_pattern，按位置分配 role + adaptation_note + tempo，每段一句占位 content_description。
    """
    import re
    import json
    m = re.search(r"原样例共\s*(\d+)\s*段", user_text)
    n_src = int(m.group(1)) if m else 4

    pat = "dramatic"
    mp = re.search(r"本次结构模式[：:\s]*([a-z_]+)", user_text)
    if not mp:
        mp = re.search(r"structural[_\s]?pattern[：:\s]*([a-z_]+)", user_text, re.IGNORECASE)
    if mp and mp.group(1).lower() in ("dramatic", "stepwise", "listicle", "atmospheric", "info_dense"):
        pat = mp.group(1).lower()

    seg_min, seg_max = (2, 8) if pat == "listicle" else (3, 7)
    n = max(seg_min, min(seg_max, n_src))

    m_dur = re.search(r"目标总时长[：:]\s*(\d+(?:\.\d+)?)\s*s", user_text)
    target_total = float(m_dur.group(1)) if m_dur else 30.0

    has_peak = pat in ("dramatic", "atmospheric")
    peak_idx = int(n * 0.6) if has_peak and n >= 4 else None
    if peak_idx is not None and (peak_idx <= 0 or peak_idx >= n - 1):
        peak_idx = None

    roles_seq: list[tuple[str, str]] = []
    main_counter = 0
    for i in range(n):
        if i == 0:
            roles_seq.append({
                "dramatic":    ("opening",    "开场钩子"),
                "stepwise":    ("intro",      "引入"),
                "listicle":    ("hook",       "钩子"),
                "atmospheric": ("establish",  "起势"),
                "info_dense":  ("title_card", "标题卡"),
            }[pat])
        elif i == n - 1:
            roles_seq.append({
                "dramatic":    ("closing", "行动引导"),
                "stepwise":    ("recap",   "总结"),
                "listicle":    ("closer",  "收尾"),
                "atmospheric": ("resolve", "余韵"),
                "info_dense":  ("payoff",  "落版"),
            }[pat])
        elif i == peak_idx:
            roles_seq.append(("climax", "卖点高潮") if pat == "dramatic" else ("peak", "顶点"))
        else:
            main_counter += 1
            if pat == "dramatic":
                roles_seq.append(("development", f"主体铺陈{main_counter}"))
            elif pat == "stepwise":
                roles_seq.append((f"step_{main_counter}", f"步骤 {main_counter}"))
            elif pat == "listicle":
                roles_seq.append((f"item_{main_counter}", f"第 {main_counter} 项"))
            elif pat == "atmospheric":
                roles_seq.append(("flow", f"流转{main_counter}"))
            else:
                roles_seq.append(("info_block", f"信息块{main_counter}"))

    role_weight: dict[str, float] = {}
    for role, _ in roles_seq:
        if role in ("opening", "intro", "hook", "establish", "title_card",
                    "closing", "recap", "closer", "resolve", "payoff"):
            role_weight[role] = 4.0
        elif role in ("climax", "peak"):
            role_weight[role] = 7.0
        else:
            role_weight[role] = 6.0

    weight_sum = sum(role_weight[r] for r, _ in roles_seq) or 1.0
    scale = target_total / weight_sum

    secs: list[dict] = []
    for i, (role, theme) in enumerate(roles_seq):
        dur = round(max(2.0, min(30.0, role_weight[role] * scale)), 1)
        secs.append({
            "role": role,
            "theme": theme,
            "content_description": (
                f"[mock] {theme}：紧扣用户主题给一句口播，搭配一组主体画面，"
                f"承接上下段叙事节奏。"
            ),
            "adaptation_note": "[mock] 沿用样例骨架，按目标时长重排节奏",
            "tempo": "fast" if role in ("climax", "peak", "hook", "title_card") else "medium",
            "source_section_indices": [min(i, max(0, n_src - 1))],
            "duration_seconds": dur,
        })
    return json.dumps({"adapted_sections": secs}, ensure_ascii=False)


def _build_mock_clarify_text(user_text: str) -> str:
    """意图澄清 mock:按 user payload 里的 ROUND 与 IS_FINAL 字段动态生成 ===DRAFT===/===QUESTION===。

    - 非最终轮:首段思考流(给前端 thinking 区) + DRAFT + 一个具体追问。
    - 最终轮(IS_FINAL=true):只输出思考流 + DRAFT,QUESTION 段写 NULL。
    所有路径加 [mock] 前缀,方便联调时辨识。
    """
    import re
    m_round = re.search(r"ROUND:\s*(\d+)\s*/\s*(\d+)", user_text)
    round_no = int(m_round.group(1)) if m_round else 1
    is_final = "IS_FINAL: true" in user_text or "IS_FINAL:true" in user_text

    m_brief = re.search(r"INITIAL_BRIEF:\s*(.+)", user_text)
    brief = m_brief.group(1).strip()[:60] if m_brief else "用户主题"

    thinking = (
        f"[mock] 第 {round_no} 轮思考:用户给的核心信息聚焦在「{brief}」附近。"
        f"目标受众、行动号召这两个维度还略模糊,先把现有信息按主流短视频脚本骨架重写,"
        f"并在最不确定的位置发问。\n"
    )
    draft = (
        f"主题:{brief}\n"
        f"卖点:[mock] 强反差画面 + 一句金句,前 3 秒锁住注意力\n"
        f"受众:[mock] 18-30 岁年轻用户,通勤刷屏场景\n"
        f"目的:[mock] 种草引导关注或点击购物车\n"
        f"平台:[mock] 抖音 / 小红书竖屏 9:16\n"
        f"语气:[mock] 真诚不油腻、轻松带点节奏\n"
        f"CTA:[mock] 「点关注 + 主页拿同款」"
    )
    if is_final:
        question = "NULL"
    else:
        questions = [
            "[mock] 这条视频是想直接带货下单、还是先做认知种草?用一句话告诉我。",
            "[mock] 主要给哪类用户看?(年龄段或场景任选一个即可)",
            "[mock] 视频结尾你最希望观众做的一件事是什么?",
        ]
        question = questions[(round_no - 1) % len(questions)]

    return (
        f"{thinking}===DRAFT===\n{draft}\n===QUESTION===\n{question}"
    )


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
    {"frame_id": "f-001", "tags": ["室内", "近景", "口播", "纯色背景"], "subtitle_style": "大字加描边",
     "recommended_section": "opening", "highlight_score": 0.82, "highlight_reason": "正面近景情绪强"},
    {"frame_id": "f-002", "tags": ["产品特写", "环形光", "白色桌面"], "subtitle_style": "大字加描边",
     "recommended_section": "development", "highlight_score": 0.65, "highlight_reason": "产品特写清晰但缺动作"},
    {"frame_id": "f-003", "tags": ["对比镜头", "实拍", "户外"], "subtitle_style": "无字幕",
     "recommended_section": "closing", "highlight_score": 0.5, "highlight_reason": "户外光线中性"}
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


def _build_mock_t2v_prompt_json(user_text: str) -> str:
    """aigc_prompt_agent 的 mock：从 user payload 抽『段落主题』『内容说明』『视频整体主题』
    拼出一句要素齐备的 Seedance 中文 prompt。所有路径都加 [mock] 前缀，方便排查。

    输出含 `thinking` 2-3 条，前端 agent 化面板用来展示『思考过程』。
    """
    import json
    import re

    def _find(label: str) -> str:
        m = re.search(rf"{label}[：:]\s*(.+)", user_text)
        return m.group(1).strip() if m else ""

    theme = _find("段落主题") or "段落主题"
    content = _find("段落内容说明") or _find("原始槽位需求") or "本段画面"
    brief = _find("视频整体主题") or ""
    # 控制长度（system prompt 要求 60-120 字）
    content_short = content[:50] + ("…" if len(content) > 50 else "")
    brief_clip = (f"，整体方向围绕『{brief[:20]}』" if brief else "")
    prompt = (
        f"[mock] {theme}：{content_short}。中景跟随镜头，自然光与冷暖对比，"
        f"电影感色调，节奏与情绪贴合主题{brief_clip}。"
    )
    thinking = [
        f"[mock] 主体：从段落主题『{theme[:12]}』提取核心",
        "[mock] 镜头：中景跟随 + 自然光，匹配段落叙事节奏",
        "[mock] 风格：电影感色调 + 冷暖对比，强化情绪",
    ]
    return json.dumps({"prompt": prompt, "thinking": thinking}, ensure_ascii=False)


def _build_mock_image_spec_json(user_text: str) -> str:
    """aigc_prompt_agent.generate_image_specs 的 mock：根据段落上下文产 2 张参考图建议 + 思考链。

    主题短 → 1 张；主题长 → 2 张。所有路径都加 [mock] 前缀。
    """
    import json
    import re

    def _find(label: str) -> str:
        m = re.search(rf"{label}[：:]\s*(.+)", user_text)
        return m.group(1).strip() if m else ""

    theme = _find("段落主题") or "段落画面"
    content = _find("段落内容说明") or _find("原始槽位需求") or "本段画面"
    ratio = _find("画幅默认") or "16:9"
    content_short = content[:40] + ("…" if len(content) > 40 else "")

    specs = [
        {
            "slot_id": "img-1",
            "caption": f"[mock] {theme[:20]} · 主场景",
            "prompt": (
                f"[mock] {theme}：{content_short}。中景固定机位，自然光带轻微逆光，"
                f"电影感暖金调，氛围庄重。"
            ),
            "ratio": ratio,
        },
        {
            "slot_id": "img-2",
            "caption": f"[mock] {theme[:20]} · 特写补镜",
            "prompt": (
                f"[mock] {theme} 主体细节特写，浅景深，35mm 胶片质感，"
                f"低饱和冷调，氛围克制。"
            ),
            "ratio": ratio,
        },
    ]
    thinking = [
        f"[mock] 识别核心主体：『{theme[:14]}』",
        "[mock] 单段配 2 张：主场景 + 特写互补",
        "[mock] 主场景中景定调，特写补主体细节",
    ]
    return json.dumps({"specs": specs, "thinking": thinking}, ensure_ascii=False)


def _build_mock_copy_outline_json(user_text: str) -> str:
    """copy_outline_agent 的 mock：从 user payload 抽『段落主题』『内容说明』『关键词』
    拼出一份字卡画面 spec + 思考链。所有路径都加 [mock] 前缀。
    """
    import re
    m_theme = re.search(r"段落主题：(.+)", user_text)
    theme = m_theme.group(1).strip() if m_theme else "段落"
    m_keywords = re.search(r"全局关键词：(.+)", user_text)
    keywords_raw = m_keywords.group(1).strip() if m_keywords else ""
    keywords = [k.strip("[]『』 ") for k in re.split(r"[,，、]", keywords_raw) if k.strip("[]『』 ")][:2]
    m_dur = re.search(r"段落时长：约\s*([0-9.]+)", user_text)
    duration = float(m_dur.group(1)) if m_dur else 4.0

    main_text = f"[mock] {theme[:14]}"
    sub_text = (keywords[0] if keywords else "看这一幕").strip()[:30]

    outline = {
        "main_text": main_text[:24],
        "sub_text": sub_text,
        "core_message": f"[mock] {theme[:18]} 的关键时刻",
        "emotional_hook": "wow",
        "must_include_keywords": keywords,
        "recommended_spec": {
            "font_family": "tech_mono",
            "layout": "split_top_bottom" if sub_text else "center",
            "bg_mode": "solid",
            "bg_color": "#0B1220",
            "text_color": "#FACC15",
            "accent_color": "#38BDF8",
            "animation": "zoom_pop",
            "emoji_decor": ["✨"],
            "duration_seconds": max(1.5, min(15.0, duration)),
        },
        "tone_lean": "[mock] 与全局调性一致，节奏微紧",
    }
    thinking = [
        f"[mock] 提取核心信息：『{theme[:14]}』",
        "[mock] 选择情绪钩子：惊艳（突出反差）",
        "[mock] 字体 tech_mono + 暗底亮黄主标 + 电光蓝副标",
        "[mock] 动画 zoom_pop（开场冲击）",
    ]
    if keywords:
        thinking.append(f"[mock] 强制关键词：{'、'.join(keywords)}")
    return json.dumps({"outline": outline, "thinking": thinking}, ensure_ascii=False)


# packaging_agent 走 complete_json 拿转场 + 封面。系统中独有的指纹是 "from_section" / "palette"。
# 时间留 0 占位，server 端 _section_pairs 会按真实 plan 段落切换点对齐。
_MOCK_PACKAGING_JSON = """
{
  "transitions": [
    {"at_seconds": 0.0, "from_section": "opening", "to_section": "development",
     "style": "whip", "duration": 0.4,
     "reason": "[mock] opening→development 用甩切制造冲击"},
    {"at_seconds": 0.0, "from_section": "development", "to_section": "closing",
     "style": "zoom", "duration": 0.5,
     "reason": "[mock] development→closing 用推拉收尾"}
  ],
  "cover": {
    "title": "[mock] 这条视频凭什么爆",
    "subtitle": "3 秒抓住你",
    "palette": ["#FFE600", "#1F2937", "#FFFFFF"],
    "layout": "center",
    "style_note": "[mock] 黑底黄字大标题居中"
  }
}
"""


def _build_mock_packaging_v2_json(user: str) -> str:
    """packaging_agent V2 的 mock：从 user prompt 抽前两/最后一个 scene_id，
    产出 5 维度候选 JSON（subtitle_styles / title_bars / stickers /
    transition_bundles / covers）。candidate_id 与 schema 约束一致。
    """
    import json
    import re

    scene_ids = re.findall(r"scene_id=([\w\-]+)", user)
    if not scene_ids:
        scene_ids = ["sc-1", "sc-2", "sc-3"]
    first_id = scene_ids[0]
    mid_id = scene_ids[1] if len(scene_ids) > 1 else scene_ids[0]
    last_id = scene_ids[-1]
    # 从 user prompt 抽段落主题作 title
    brief_m = re.search(r"创作者主题：(.+)", user)
    brief = brief_m.group(1).strip()[:12] if brief_m else "爆款标题"

    payload = {
        "subtitle_styles": [
            {
                "candidate_id": "sub-01",
                "label": "[mock] 底部中字｜阴影底",
                "font_size": "medium",
                "position": "bottom",
                "background": "shadow",
                "bilingual": False,
                "rationale": "[mock] 可读性高、占用画面少",
            },
            {
                "candidate_id": "sub-02",
                "label": "[mock] 底部大字｜渐变底",
                "font_size": "large",
                "position": "bottom",
                "background": "gradient",
                "bilingual": False,
                "rationale": "[mock] 信息密度高时拉满字号",
            },
            {
                "candidate_id": "sub-03",
                "label": "[mock] 中字无底",
                "font_size": "medium",
                "position": "bottom",
                "background": "none",
                "bilingual": False,
                "rationale": "[mock] 极简风、纯净画面",
            },
        ],
        "title_bars": [
            {
                "candidate_id": "tb-01",
                "text": f"[mock] {brief}",
                "target_scene_id": first_id,
                "start": 0.2,
                "end": 1.6,
                "font_size": "large",
                "color": "#FFFFFF",
                "background_color": "#14181F",
                "position": "top",
                "rationale": "[mock] 开场点题",
            },
            {
                "candidate_id": "tb-02",
                "text": "[mock] 重点来了",
                "target_scene_id": mid_id,
                "start": 0.0,
                "end": 1.2,
                "font_size": "medium",
                "color": "#0F172A",
                "background_color": "#FACC15",
                "position": "top",
                "rationale": "[mock] 中段强调",
            },
        ],
        "stickers": [
            {
                "candidate_id": "st-01",
                "text": "[mock]点这",
                "target_scene_id": last_id,
                "start": 0.0,
                "end": 0.9,
                "color": "#000000",
                "background_color": "#FFE600",
                "position": "bottom-center",
                "rationale": "[mock] 收尾 CTA",
            },
            {
                "candidate_id": "st-02",
                "text": "[mock]关注",
                "target_scene_id": last_id,
                "start": 0.0,
                "end": 0.8,
                "color": "#FFFFFF",
                "background_color": "#DB2777",
                "position": "top-right",
                "rationale": "[mock] 备选关注",
            },
        ],
        "transition_bundles": [
            {
                "candidate_id": "tr-01",
                "at_seconds": 0.0,
                "from_section": "opening",
                "to_section": "development",
                "options": [
                    {"style": "whip", "duration": 0.4, "reason": "[mock] 甩切冲击"},
                    {"style": "zoom", "duration": 0.4, "reason": "[mock] 推拉过渡"},
                ],
                "rationale": "[mock] 开场→发展 转场组",
            },
            {
                "candidate_id": "tr-02",
                "at_seconds": 0.0,
                "from_section": "development",
                "to_section": "closing",
                "options": [
                    {"style": "zoom", "duration": 0.5, "reason": "[mock] 推拉收尾"},
                    {"style": "fade", "duration": 0.4, "reason": "[mock] 渐隐收尾"},
                ],
                "rationale": "[mock] 发展→结尾 转场组",
            },
        ],
        "covers": [
            {
                "candidate_id": "cv-01",
                "title": f"[mock]{brief}",
                "subtitle": "3 秒抓住你",
                "palette": ["#FFE600", "#1F2937", "#FFFFFF"],
                "layout": "center",
                "style_note": "[mock] 黑底黄字居中",
                "rationale": "[mock] 强冲击对比色",
            },
            {
                "candidate_id": "cv-02",
                "title": f"[mock]{brief}",
                "subtitle": "看完就懂",
                "palette": ["#0EA5E9", "#0F172A", "#F8FAFC"],
                "layout": "left",
                "style_note": "[mock] 蓝白冷调",
                "rationale": "[mock] 信息流稳重风",
            },
        ],
    }
    return json.dumps(payload, ensure_ascii=False)
