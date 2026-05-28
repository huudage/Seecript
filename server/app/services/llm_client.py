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
from typing import Any, Dict, List, Optional, Sequence

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
    """把后端虚拟 URL（/samples/xxx, /uploads/xxx）映射回 server/ 目录下的真实磁盘路径。

    其它绝对/相对路径直接尝试；找不到则返回 None 让调用方走占位。
    """
    if s.startswith("/samples/"):
        p = _SERVER_ROOT / "samples" / s[len("/samples/"):].lstrip("/")
        return p if p.is_file() else None
    if s.startswith("/uploads/"):
        p = _SERVER_ROOT / "var" / "uploads" / s[len("/uploads/"):].lstrip("/")
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
        if "transitions" in system and "palette" in system:
            return _MOCK_PACKAGING_JSON
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
_MOCK_UNDERSTANDING_JSON = """
{
  "archetype": "通用短视频原型",
  "narrative_summary": "[mock] 视频以氛围镜头开场，中段铺陈主体内容并在中后段拉到情绪高潮，结尾给出引导或落版。",
  "suggested_segments": 4,
  "tone": "中性平稳"
}
"""

def _build_mock_shot_roles_json(user_text: str) -> str:
    """动态生成 shot_roles mock：从 user payload 解析镜头数，按位置分配 role。

    解析规则：user_text 中形如 "0: 0.0-3.0s" 的行代表一个镜头。
    分配策略：第一个=opening，最后一个=closing，≥4 个时 60% 位置=climax，其余=development。
    """
    import re
    import json
    shot_lines = re.findall(r"^(\d+):\s*[\d.]+", user_text, re.MULTILINE)
    n = len(shot_lines)
    if n == 0:
        n = max(1, len([img for img in [] if img]))
        n = max(n, 2)

    roles: list[dict] = []
    climax_idx = int(n * 0.6) if n >= 4 else None
    if climax_idx is not None and (climax_idx <= 0 or climax_idx >= n - 1):
        climax_idx = None

    for i in range(n):
        if i == 0:
            role, theme = "opening", "开场铺垫"
        elif i == n - 1:
            role, theme = "closing", "余韵收尾"
        elif i == climax_idx:
            role, theme = "climax", "情绪高潮"
        else:
            role, theme = "development", f"主体铺陈{i}"
        roles.append({"shot_index": i, "role": role, "theme": theme})

    return json.dumps({"shot_roles": roles}, ensure_ascii=False)


def _build_mock_adapted_sections_json(user_text: str) -> str:
    """动态生成 adapted_sections mock：从 user payload 解析 '原样例共 N 段' 拿到 N，
    按位置分配 role，每段写一句占位 content_description。

    分配策略：
    - i=0       → opening
    - i=n-1     → closing
    - i=int(n*0.6) → climax（仅 n≥4 时；且不与首末重叠）
    - 其余      → development
    source_section_indices 落 [min(i, n-1)]，让 plan_agent 的 _materialize 能找到镜头。
    """
    import re
    import json
    m = re.search(r"原样例共\s*(\d+)\s*段", user_text)
    n_src = int(m.group(1)) if m else 4
    n = max(3, min(7, n_src))

    climax_idx = int(n * 0.6) if n >= 4 else None
    if climax_idx is not None and (climax_idx <= 0 or climax_idx >= n - 1):
        climax_idx = None

    secs: list[dict] = []
    for i in range(n):
        if i == 0:
            role, theme = "opening", "开场钩子"
        elif i == n - 1:
            role, theme = "closing", "行动引导"
        elif i == climax_idx:
            role, theme = "climax", "卖点高潮"
        else:
            role, theme = "development", f"主体铺陈{i}"
        secs.append({
            "role": role,
            "theme": theme,
            "content_description": (
                f"[mock] {theme}：紧扣用户主题给一句口播，搭配一组主体画面，"
                f"承接上下段叙事节奏。"
            ),
            "source_section_indices": [min(i, max(0, n_src - 1))],
        })
    return json.dumps({"adapted_sections": secs}, ensure_ascii=False)


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
