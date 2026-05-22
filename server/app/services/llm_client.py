"""LLM client abstraction.

Design pattern: **Adapter + Factory**, in service of Dependency Inversion.

- Business code (routers/) depends only on the abstract `LLMClient`, never on a concrete provider.
- Adding a new provider (e.g. OpenAI, Tongyi, GPT-OSS) only requires writing a new subclass and
  registering it in `get_llm_client()` — no business-code change. (Open/Closed)
- The mock client lets the whole product run end-to-end without any API key (essential for
  frontend dev, CI, and offline demo).

Concurrency note:
- Each request gets its own `httpx.AsyncClient` via `async with` to keep things simple in v0.1.
- For higher throughput we should hold a single shared client; deferred until v0.2 when we measure.
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


# Constants — extracted to avoid magic numbers per project rule.
HTTP_OK = 200
DEFAULT_RETRIES = 0  # v0.1 keeps it simple; users see error and retry. Add backoff in v0.2.


class LLMError(RuntimeError):
    """Wraps any LLM-related failure with a stable code so the API layer can map to HTTP."""

    def __init__(self, message: str, code: str = "LLM_ERROR", upstream_status: Optional[int] = None) -> None:
        super().__init__(message)
        self.code = code
        self.upstream_status = upstream_status


# --------------------------------------------------------------------------
# Abstract interface
# --------------------------------------------------------------------------
class LLMClient(ABC):
    """All LLM providers must implement this thin interface.

    `complete` returns the assistant reply as plain text. Callers that need JSON
    should ask the model for JSON in the system prompt and `json.loads(...)` here.
    """

    name: str = "abstract"

    @abstractmethod
    async def complete(
        self,
        system: str,
        user: str,
        *,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> str:
        """Send a (system, user) pair and return the assistant text."""

    async def complete_json(
        self,
        system: str,
        user: str,
        *,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> Any:
        """Convenience wrapper: parse the reply as JSON.

        Many providers (DeepSeek included) honor a system instruction "respond with
        valid JSON only" much better than free-form prompts. We retry once if the
        first reply isn't parseable JSON, asking the model to return JSON only.

        Why we wrap the *second* failure into LLMError instead of letting the
        ValueError bubble:
          The router layer only catches LLMError → any ValueError here would fall
          through the global middleware and surface to the user as a generic
          "internal server error" 500. By converting it here, the router cleanly
          maps it to a 502 with a Chinese toast message — single point of
          containment (DIP), no need for every router to add a redundant guard.
        """
        text = await self.complete(system, user, temperature=temperature, max_tokens=max_tokens)
        try:
            return _extract_json(text)
        except ValueError as first_err:
            log.warning("LLM returned non-JSON, retrying once with stricter system prompt: %s", first_err)

        stricter_system = (
            system
            + "\n\n严格要求：必须返回合法 JSON。不要使用 markdown 代码块。不要在 JSON 前后添加任何文字。"
        )
        text2 = await self.complete(stricter_system, user, temperature=temperature, max_tokens=max_tokens)
        try:
            return _extract_json(text2)
        except ValueError as retry_err:
            # The retry also failed. Log a snippet (not the full payload, which
            # could be huge) and surface as LLMError so the router can return a
            # clean 502 + Chinese toast instead of a confusing 500.
            snippet = (text2 or "")[:300]
            log.error("LLM JSON parse failed after retry. snippet=%r", snippet)
            raise LLMError(
                f"模型连续两次未返回合法 JSON，请稍后重试或更换更短的输入。snippet={snippet!r}",
                code="LLM_BAD_JSON",
            ) from retry_err


def _extract_json(text: str) -> Any:
    """Parse JSON from a possibly-noisy LLM reply (handles ```json fences and prefixes)."""
    s = text.strip()
    # Strip markdown code fences if present.
    if s.startswith("```"):
        # Drop the opening fence (handles ```json or ``` followed by newline).
        first_newline = s.find("\n")
        if first_newline != -1:
            s = s[first_newline + 1 :]
        # Drop closing fence.
        if s.endswith("```"):
            s = s[: -3]
        s = s.strip()
    # If the model added a preamble before the first {, slice from the first { to the last }.
    first_brace = s.find("{")
    first_bracket = s.find("[")
    starts = [i for i in (first_brace, first_bracket) if i != -1]
    if starts:
        start = min(starts)
        end = max(s.rfind("}"), s.rfind("]"))
        if end > start:
            s = s[start : end + 1]
    return json.loads(s)


# --------------------------------------------------------------------------
# Mock implementation (no network, returns canned but module-shaped data)
# --------------------------------------------------------------------------
class MockLLMClient(LLMClient):
    """Returns deterministic example payloads. Used when LLM_PROVIDER=mock or no API key is set.

    The mock fingerprints each prompt by checking for *unique JSON output-schema field names*
    declared in the system prompt. We deliberately avoid plain-text keywords like "人设" or
    "Hook" because multiple prompts mention each other's domain in their hint sections.
    Each schema field name below appears in one and only one prompt:
        - "personas"               -> persona module
        - "transferable_template"  -> skeleton module
        - "broad_traffic"          -> seo module
        - "low_value_count"        -> comments module
        - "rationale"              -> qa module (round + question + options + done)
        - "hook_narration"         -> script module (scenes + cta_narration + full_text)

    Order matters when one prompt references another's field name in a hint;
    keep the most specific fingerprint first.
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
        # Tiny artificial latency so the frontend can show a real loading state.
        await _sleep_briefly()
        if "transferable_template" in system:
            return _MOCK_SKELETON_JSON
        if "broad_traffic" in system:
            return _MOCK_SEO_JSON
        if "low_value_count" in system:
            return _MOCK_COMMENTS_JSON
        if "personas" in system:
            return _MOCK_PERSONA_JSON
        if "hook_narration" in system:
            return _MOCK_SCRIPT_JSON
        if "rationale" in system:
            return _MOCK_QA_JSON
        return '{"detail": "mock fallback"}'


async def _sleep_briefly() -> None:
    import asyncio

    await asyncio.sleep(0.4)


# --------------------------------------------------------------------------
# DeepSeek implementation (OpenAI-compatible /chat/completions)
# --------------------------------------------------------------------------
class DeepSeekLLMClient(LLMClient):
    """Calls DeepSeek's OpenAI-compatible Chat Completions endpoint.

    Reference: https://api-docs.deepseek.com/  (Chat Completions)
    """

    name = "deepseek"

    def __init__(self, settings: Settings) -> None:
        if not settings.deepseek_api_key:
            raise LLMError(
                "DEEPSEEK_API_KEY is empty but LLM_PROVIDER=deepseek. "
                "Set the key in server/.env or switch LLM_PROVIDER=mock.",
                code="LLM_NO_KEY",
            )
        self._api_key = settings.deepseek_api_key
        self._base_url = settings.deepseek_base_url.rstrip("/")
        self._model = settings.deepseek_model
        self._timeout = settings.llm_timeout_seconds
        self._default_temperature = settings.llm_temperature
        self._default_max_tokens = settings.llm_max_tokens

    async def complete(
        self,
        system: str,
        user: str,
        *,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> str:
        url = f"{self._base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        payload: Dict[str, Any] = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": self._default_temperature if temperature is None else temperature,
            "max_tokens": self._default_max_tokens if max_tokens is None else max_tokens,
            "stream": False,
        }

        started = time.perf_counter()
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(url, headers=headers, json=payload)
        except httpx.TimeoutException as e:
            raise LLMError(f"DeepSeek timeout after {self._timeout}s", code="LLM_TIMEOUT") from e
        except httpx.HTTPError as e:
            raise LLMError(f"DeepSeek network error: {e}", code="LLM_NETWORK") from e

        elapsed_ms = int((time.perf_counter() - started) * 1000)

        if resp.status_code != HTTP_OK:
            # Surface upstream message but never echo the API key.
            snippet = resp.text[:300]
            raise LLMError(
                f"DeepSeek HTTP {resp.status_code}: {snippet}",
                code=f"LLM_HTTP_{resp.status_code}",
                upstream_status=resp.status_code,
            )

        try:
            data = resp.json()
            choices: List[Dict[str, Any]] = data.get("choices") or []
            if not choices:
                raise ValueError("empty choices")
            content = choices[0].get("message", {}).get("content")
            if not isinstance(content, str) or not content.strip():
                raise ValueError("empty content")
        except (ValueError, KeyError, TypeError) as e:
            raise LLMError(f"DeepSeek malformed response: {e}", code="LLM_BAD_RESPONSE") from e

        usage = data.get("usage") or {}
        log.info(
            "deepseek ok | model=%s | %dms | prompt_tok=%s | completion_tok=%s",
            self._model,
            elapsed_ms,
            usage.get("prompt_tokens"),
            usage.get("completion_tokens"),
        )
        return content


# --------------------------------------------------------------------------
# Factory
# --------------------------------------------------------------------------
_PROVIDERS = {
    "mock": MockLLMClient,
    "deepseek": DeepSeekLLMClient,
}


def get_llm_client(settings: Optional[Settings] = None) -> LLMClient:
    """Return the LLM client based on configured provider. Falls back to mock if key missing."""
    s = settings or get_settings()
    provider = s.llm_provider
    if provider == "deepseek" and not s.deepseek_api_key:
        log.warning("LLM_PROVIDER=deepseek but DEEPSEEK_API_KEY is empty -> using mock")
        return MockLLMClient()
    cls = _PROVIDERS.get(provider, MockLLMClient)
    if cls is DeepSeekLLMClient:
        return DeepSeekLLMClient(s)
    return cls()


# --------------------------------------------------------------------------
# Mock fixtures (kept here so MockLLMClient ships self-contained)
# --------------------------------------------------------------------------
_MOCK_PERSONA_JSON = """
{
  "personas": [
    {
      "name": "打工人月薪 8k 的精致冰箱整理术",
      "differentiation": "把『收纳』×『冰箱』×『打工人预算』三层叠加，差异化竞争位空白。",
      "rationale": "细分赛道供给少，且与生鲜/收纳品牌契合度高，预算限制反而成为内容钩子。",
      "reference_accounts": ["@小麦的整理日记", "@省心生活"],
      "onboarding_advice": "前 10 条聚焦『冰箱开箱 + 周末囤货预算』，每周固定一更；建立『8k 工资生活提案』系列。",
      "monetization_outlook": "生鲜电商 / 收纳品牌植入潜力高，挂车转化好。",
      "score": 5
    },
    {
      "name": "3 块钱也好喝的打工人咖啡馆",
      "differentiation": "极致性价比 + DIY 配方，区别于器材党与门店党。",
      "rationale": "用户兴趣命中，且赛道里『平价 + 配方』缺位。",
      "reference_accounts": ["@只喝咖啡不睡觉", "@小厨房的精致"],
      "onboarding_advice": "建立『9.9 元配方挑战』系列，每周一新；前 15 条全部 1 分钟内强 Hook。",
      "monetization_outlook": "挂车带货 / 餐饮品牌种草中等，长期复利型账号。",
      "score": 4
    },
    {
      "name": "租房改造低预算图鉴",
      "differentiation": "真实租房 + 低预算前后对比，区别于装修博主。",
      "rationale": "兴趣可延展为整体居家改造，但赛道竞争较激烈，作为长尾备选更稳妥。",
      "reference_accounts": ["@小空间研究所", "@30 平也精致"],
      "onboarding_advice": "『百元改造系列』每 2 周一次，配合『翻车合集』反差内容。",
      "monetization_outlook": "家居 / 二手平台合作中等，但用户粘性强。",
      "score": 3
    }
  ]
}
"""

_MOCK_SKELETON_JSON = """
{
  "hook": {
    "strategy": "反常识陈述",
    "text": "90% 的人冰箱都用错了——你以为塞满才划算，其实越满越浪费。",
    "explanation": "用『多数人都错』这种反常识陈述快速制造好奇与停留，3 秒内点出『钱』这个高敏感词。"
  },
  "body": [
    {"timestamp": "0:05-0:30", "title": "暴露问题", "description": "拍杂乱冰箱，配真实生活独白。", "emotion_arc": "好奇"},
    {"timestamp": "0:30-1:00", "title": "提出 3 步法", "description": "从『分区 → 打标 → 周清』三步逐一展开。", "emotion_arc": "共鸣"},
    {"timestamp": "1:00-1:30", "title": "实拍演示对比", "description": "整理前后对比镜头，节奏快、强反差。", "emotion_arc": "反转"},
    {"timestamp": "1:30-2:00", "title": "用户自测题", "description": "提出『你家冰箱属于哪种』让用户对号入座。", "emotion_arc": "认同"}
  ],
  "cta": {
    "strategy": "评论区留言",
    "text": "你家冰箱属于哪一种？把首字母打在评论区，我下期挨个点评。",
    "explanation": "低门槛 + 个性化承诺，激发用户在评论区留言的同时为下期视频引流。"
  },
  "transferable_template": "Hook：[反常识陈述：多数人都错的某事]\\n\\nBody：[暴露问题] → [3 步方法] → [实拍对比] → [用户自测]\\n\\nCTA：[评论区低门槛话题 + 下期点评承诺]"
}
"""

_MOCK_SEO_JSON = """
{
  "titles": [
    {"type": "反常识型", "text": "越贵越好？39 元成分表把专柜打哭了", "char_count": 22, "notes": "强情绪"},
    {"type": "数字型", "text": "3 张成分表，看穿 5 个护肤智商税", "char_count": 17, "notes": "高信息密度"},
    {"type": "身份型", "text": "打工人最该屯的 10 件平价好物（成分版）", "char_count": 20, "notes": "受众明确"},
    {"type": "痛点型", "text": "月薪 8k 还买大牌？我替你算了笔账", "char_count": 17, "notes": "触发共鸣"},
    {"type": "悬念型", "text": "我把闺蜜的大牌全拆了——结果惊呆她", "char_count": 18, "notes": "故事感"}
  ],
  "description": "月薪 8k 也想精致护肤，但每次买大牌都心疼？我家闺蜜来摊牌了——3 张成分表对比一下，专柜热销和 39 元国货大牌的差别，可能比你想的少得多。",
  "tags": {
    "broad_traffic": ["#护肤", "#美妆", "#打工人"],
    "long_tail": ["#平价护肤推荐", "#护肤成分党", "#国货护肤", "#月薪八千护肤", "#护肤智商税"],
    "challenge_topics": ["#平价好物挑战", "#成分对比"]
  }
}
"""

_MOCK_COMMENTS_JSON = """
{
  "high_value": [
    {
      "author": "@小麦",
      "text": "博主3看2不看的原则我特别想知道更细节的，是不是有具体清单？",
      "classification": "干货提问",
      "replies": [
        {"tone": "专业解读", "text": "『3 看』=成分表前 5 / 浓度标注 / 防腐体系；『2 不看』=网红推荐 / 包装颜值。下期我做成图文清单，先存起来再看。"},
        {"tone": "幽默调侃", "text": "这是想直接抢我下期选题哈哈，留言点赞最多的提问下次第一条解答。"},
        {"tone": "共情安抚", "text": "太理解了，我以前也是面对成分表懵圈。我整理过一份清单，关注我，明天置顶发出来。"}
      ]
    },
    {
      "author": "@美妆喵",
      "text": "成分党表示，9.9 元的成分国货真的能打？我有点不信",
      "classification": "争议探讨",
      "replies": [
        {"tone": "专业解读", "text": "同样是『烟酰胺 + 神经酰胺』，9.9 元和 199 元的差异主要在浓度稳定性和肤感修饰，下期我做盲测。"},
        {"tone": "幽默调侃", "text": "不信也正常，我自己第一次也是震惊脸，不过证据全在视频里。"},
        {"tone": "共情安抚", "text": "完全理解你的怀疑。下期我做长期使用对比，让你有个判断依据。"}
      ]
    }
  ],
  "medium_value": [
    {"author": "@大白", "text": "博主能不能下期讲一下油皮怎么选", "classification": "下期选题", "replies": []},
    {"author": "@路过", "text": "我用过你说的国货 真的便宜大碗 但是味道有点冲", "classification": "中价值", "replies": []}
  ],
  "low_value_count": 4
}
"""

_MOCK_QA_JSON = """
{
  "round": 1,
  "question": "原视频用『反常识陈述』钩住观众，结合你的人设，第一句你想用哪种角度？",
  "rationale": "Hook 的角度决定 3 秒留存率，从最贴近你受众的反差入手最稳。",
  "options": [
    {"label": "数字反差：『同样护肤，专柜 599 vs 国货 39，结果意外得让你想骂人。』"},
    {"label": "身份反差：『打工人月薪 8k，凭什么用得起大牌？我闺蜜的答案让我傻眼。』"},
    {"label": "认知反差：『你以为成分越贵越好？我把 3 张成分表摊开，结果反过来了。』"}
  ],
  "done": false
}
"""

_MOCK_SCRIPT_JSON = """
{
  "hook_narration": "你是不是也以为越贵的护肤品成分越好？等下你看完这 3 张成分表，可能会想立刻把购物车清空——专柜 599 元的爆款和 39 元国货放在一起，差距小到我都怀疑自己买错了几年。",
  "scenes": [
    {
      "timestamp": "0:05-0:30",
      "title": "暴露问题 · 成分表三连摊",
      "narration": "我从我家梳妆台和闺蜜家的化妆台各拿了一瓶『打工人最常买』的精华，加上一瓶 39 元国货，三瓶并排摆在桌上，把成分表全部贴在白纸上——你看这一栏『烟酰胺 + 神经酰胺 + 透明质酸』，三瓶都有，浓度只差 1-2 个百分点。",
      "visual": "镜头从三瓶产品平移到三张成分表，红笔圈出相同成分。"
    },
    {
      "timestamp": "0:30-1:00",
      "title": "提出 3 步法 · 看清成分党黑话",
      "narration": "教你 3 步看穿『智商税』：第一步看成分表前 5，决定主要功效；第二步找浓度数字，没标的基本是噱头；第三步看防腐体系，便宜但不刺激的产品，这一步通常做得很扎实。",
      "visual": "三个数字 1/2/3 跳出，每个对应桌上一个成分表特写。"
    },
    {
      "timestamp": "1:00-1:30",
      "title": "实拍演示 · 上脸效果对比",
      "narration": "我让闺蜜左脸用 599 的，右脸用 39 的，2 周后我们再给你看真实素颜对比——剧透一下，肉眼几乎分不出，但她的钱包知道。",
      "visual": "实拍闺蜜左右脸对比，下方字幕弹出『2 周实测』。"
    },
    {
      "timestamp": "1:30-2:00",
      "title": "用户自测 · 你买过哪一种？",
      "narration": "你最近被什么平价好物惊艳过？或者你正在被哪个大牌智商税气到？把名字打在评论区，我会挑赞最高的 3 个，下期视频做长期使用对比。",
      "visual": "屏幕弹出『#平价好物挑战』话题贴片。"
    }
  ],
  "cta_narration": "如果你不想再被『成分党黑话』骗钱，关注我，下期《打工人值得屯的 10 件》正在做长期实测；觉得这个视频有用，点亮收藏，明天还能找到。",
  "full_text": "【Hook · 0:00-0:03】你是不是也以为越贵的护肤品成分越好？等下你看完这 3 张成分表，可能会想立刻把购物车清空——专柜 599 元的爆款和 39 元国货放在一起，差距小到我都怀疑自己买错了几年。\\n\\n【暴露问题 · 成分表三连摊 · 0:05-0:30】我从我家梳妆台和闺蜜家的化妆台各拿了一瓶『打工人最常买』的精华，加上一瓶 39 元国货，三瓶并排摆在桌上，把成分表全部贴在白纸上——你看这一栏『烟酰胺 + 神经酰胺 + 透明质酸』，三瓶都有，浓度只差 1-2 个百分点。\\n\\n【提出 3 步法 · 看清成分党黑话 · 0:30-1:00】教你 3 步看穿『智商税』：第一步看成分表前 5，决定主要功效；第二步找浓度数字，没标的基本是噱头；第三步看防腐体系，便宜但不刺激的产品，这一步通常做得很扎实。\\n\\n【实拍演示 · 上脸效果对比 · 1:00-1:30】我让闺蜜左脸用 599 的，右脸用 39 的，2 周后我们再给你看真实素颜对比——剧透一下，肉眼几乎分不出，但她的钱包知道。\\n\\n【用户自测 · 你买过哪一种？ · 1:30-2:00】你最近被什么平价好物惊艳过？或者你正在被哪个大牌智商税气到？把名字打在评论区，我会挑赞最高的 3 个，下期视频做长期使用对比。\\n\\n【CTA · 收尾】如果你不想再被『成分党黑话』骗钱，关注我，下期《打工人值得屯的 10 件》正在做长期实测；觉得这个视频有用，点亮收藏，明天还能找到。"
}
"""
