"""Clarify Agent —— 视频工坊 step 1 意图澄清的多轮追问引擎（v2 · 五件套结构化）。

为什么这次重写：v1 用 `===DRAFT===` / `===QUESTION===` 文本标记切流，draft 是自由
文本，用户没法局部改、finalize 还得再问一次 LLM。v2 改成 JSON 五件套：
`topic / content / audience / goal / tone`，每轮把每个字段单独 emit，前端可以
独立编辑、用户点 OK 时由前端把五件套拼成 brief，后端 finalize 不再 LLM。

工作方式：无状态多轮——前端把 INITIAL_BRIEF + 历史 Q/A transcript 一起送进来，
本 agent 让 LLM 输出一段 JSON：
```json
{
  "outline": {
    "topic": "...", "content": "...", "audience": "...",
    "goal": "...", "tone": "..."
  },
  "question": "本轮唯一追问，已经够清楚就给 null",
  "thinking": "（可选）思考流，前端展示给用户看推理过程"
}
```
路由层根据 round_no/3 + force_finalize 决定 is_final，最终轮强制把 question 置空。

兼容性：保留 `===DRAFT===` 字面值在系统提示里——MockLLMClient 路由用「短视频脚本意图
澄清助手」做指纹，并按这串字符识别 mock 分支；这次改 prompt 必须保留指纹。
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any, AsyncIterator, Optional

from pydantic import BaseModel

from ...schemas import ClarifyOutline
from ..llm_client import LLMError, get_llm_client

log = logging.getLogger("seecript.agent.clarify")


# System prompt 同时是 MockLLMClient 路由指纹：必须含「短视频脚本意图澄清助手」。
# 换 prompt 时务必保留这串中文，否则 mock 分支识别不到，本地 dev 全链路崩。
_CLARIFY_SYSTEM = (
    "你是短视频脚本意图澄清助手。任务是把用户给的 INITIAL_BRIEF + 历史 TRANSCRIPT，"
    "整合成一份五件套结构化 brief：主题 / 内容卖点 / 受众 / 目的 / 语气。\n\n"
    "**核心规则：每一轮都必须把 outline 五个字段全部填满**——\n"
    "- 用户没明说的字段，**根据 INITIAL_BRIEF 做最合理的推测**填入，并在 question 里说明你假设了什么；\n"
    "- 仅当字段在已有信息里完全无依据、且推测会误导时才允许 null；topic 必须永远非空。\n"
    "- 用户在后续轮次给出补充/纠正后，要把对应字段更新为新值。\n"
    "- 如果用户输入里出现了 DETECTED_SUBJECTS 段（用户已上传素材里 VLM 识别到的物体/场景），\n"
    "  你**必须**把这些对象在 outline.content 里点名出现（用顿号串联），缺的就拼上；\n"
    "  比如 DETECTED_SUBJECTS 含「纸巾」，content 必须出现「纸巾」二字。\n\n"
    "question 的语义（v3 调整）：\n"
    "- 不再是「让用户作答」的硬追问，而是「让用户检查」的提示——告诉用户你做了哪些假设，"
    "或哪个字段你还没把握，引导用户决定要不要补充。\n"
    "- 若五件套已经非常贴合用户表达、无需任何假设，question 给 null。\n"
    "- IS_FINAL=true 时 question 必须是 null。\n\n"
    "输出严格 JSON 对象，不要 Markdown 围栏，不要任何额外文字：\n"
    "{\n"
    '  "thinking": "（可选）30 字内的思考流，告诉用户你怎么综合的",\n'
    '  "outline": {\n'
    '    "topic":    "<必填，一句话主题，最多 50 字>",\n'
    '    "content":  "<核心卖点/亮点；多条用顿号或换行；最多 200 字>",\n'
    '    "audience": "<目标受众画像；最多 80 字>",\n'
    '    "goal":     "<目的：卖货/种草/教程/娱乐/品牌 等>",\n'
    '    "tone":     "<语气风格：温柔/高能/沙雕/严肃 等>"\n'
    "  },\n"
    '  "question": "<向用户求证你做的假设；够清楚或最终轮请给 null；≤40 字>"\n'
    "}\n\n"
    "重要约束：\n"
    "- 输出**纯 JSON**，禁止三重反引号或任何前后缀。\n"
    "- 历史轮 TRANSCRIPT 已经包含用户补充信息，不要重复假设、不要忽略用户已澄清的字段。\n"
    "- 即使是历史 marker `===DRAFT===` 也别出现在你的输出里——纯 JSON 即可。"
)


class ClarifyTurn(BaseModel):
    """一轮 Q/A 历史。前端把 transcript 完整回传，本 agent 无状态。"""

    question: str
    answer: str


@dataclass
class ThinkingDelta:
    """『思考流』流式片段——LLM 在出 JSON 前的中间叙述（mock 模式没有）。"""

    text: str


@dataclass
class OutlineReady:
    """LLM 完整输出已解析成五件套结构。"""

    outline: ClarifyOutline
    thinking: str


@dataclass
class RoundDone:
    """整轮结束。is_final=True 时 question 永远 None。"""

    outline: ClarifyOutline
    question: Optional[str]
    is_final: bool


ClarifyEvent = ThinkingDelta | OutlineReady | RoundDone


def _build_user_payload(
    *,
    initial_brief: str,
    transcript: list[ClarifyTurn],
    round_no: int,
    is_final: bool,
    detected_subjects: list[str] | None = None,
) -> str:
    lines: list[str] = []
    lines.append(f"INITIAL_BRIEF:\n{initial_brief.strip() or '(empty)'}\n")
    if detected_subjects:
        # 用户已上传的素材里 VLM 已识别到的对象/主体清单。LLM 必须在 outline.content
        # 里点名出现这些对象（用顿号串联），保证「带货纸巾」上传纸巾照片就一定能在
        # 内容卖点里看到「纸巾」二字。
        subjects_str = "、".join(s.strip() for s in detected_subjects if s.strip())[:300]
        if subjects_str:
            lines.append("DETECTED_SUBJECTS（用户已上传素材里 VLM 识别出的物体/场景；")
            lines.append("  必须在 outline.content 里点名出现，缺的就拼上）:")
            lines.append(subjects_str)
            lines.append("")
    if transcript:
        lines.append("TRANSCRIPT:")
        for i, t in enumerate(transcript, 1):
            lines.append(f"Q{i}: {t.question.strip()}")
            lines.append(f"A{i}: {t.answer.strip()}")
        lines.append("")
    else:
        lines.append("TRANSCRIPT: (empty — this is round 1)")
        lines.append("")
    lines.append(f"ROUND: {round_no}/3")
    lines.append(f"IS_FINAL: {'true' if is_final else 'false'}")
    return "\n".join(lines)


_JSON_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.MULTILINE)


def _extract_json_object(text: str) -> Optional[dict[str, Any]]:
    """从 LLM 输出里抠出第一个 `{...}` JSON 对象。

    LLM 偶尔会带 Markdown 围栏或前后说明文字。先剥围栏，再用括号配对从第一个 `{`
    扫到对应的 `}`——比直接 json.loads 整段文本鲁棒。
    """
    if not text:
        return None
    cleaned = _JSON_FENCE_RE.sub("", text).strip()
    # 找第一个 `{`
    start = cleaned.find("{")
    if start < 0:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(cleaned)):
        ch = cleaned[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                snippet = cleaned[start: i + 1]
                try:
                    return json.loads(snippet)
                except json.JSONDecodeError:
                    return None
    return None


def _coerce_outline(raw: Any) -> ClarifyOutline:
    """把 LLM 出的 outline dict 强转成 ClarifyOutline；非 dict / 字段缺失都填 None。"""
    if not isinstance(raw, dict):
        return ClarifyOutline()

    def _str_or_none(v: Any, max_len: int) -> Optional[str]:
        if v is None:
            return None
        if isinstance(v, (list, tuple)):
            v = "、".join(str(x) for x in v if x)
        s = str(v).strip()
        if not s or s.lower() in {"null", "none", "n/a", "不知道", "未知"}:
            return None
        return s[:max_len]

    return ClarifyOutline(
        topic=_str_or_none(raw.get("topic"), 200),
        content=_str_or_none(raw.get("content"), 400),
        audience=_str_or_none(raw.get("audience"), 200),
        goal=_str_or_none(raw.get("goal"), 200),
        tone=_str_or_none(raw.get("tone"), 200),
    )


def _coerce_question(v: Any) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip()
    if not s or s.lower() in {"null", "none", "n/a"}:
        return None
    # 单行；去掉可能的 markdown 前缀和句末空白
    first_line = next((ln.strip() for ln in s.splitlines() if ln.strip()), None)
    if not first_line:
        return None
    return first_line[:200]


async def run_clarify_round(
    *,
    initial_brief: str,
    transcript: list[ClarifyTurn],
    round_no: int,
    is_final: bool,
    detected_subjects: list[str] | None = None,
) -> AsyncIterator[ClarifyEvent]:
    """跑一轮意图澄清。

    yield 顺序：
    1. 任意条 ThinkingDelta（思考流；mock 里没有）
    2. OutlineReady(outline, thinking) —— 解析 LLM JSON 完成
    3. RoundDone(outline, question, is_final) —— 最后一条

    is_final=True 时 RoundDone.question 强制 None。

    detected_subjects 是用户已上传素材里 VLM 已识别出的物体/主体清单（如 ["纸巾", "客厅"]），
    本 agent 把它喂进 LLM prompt，要求 outline.content 里必须点名出现这些对象，
    保证素材上传与意图 outline 双向同步（#420）。
    """
    user_payload = _build_user_payload(
        initial_brief=initial_brief,
        transcript=transcript,
        round_no=round_no,
        is_final=is_final,
        detected_subjects=detected_subjects,
    )
    client = get_llm_client()

    # 完整 token 累积；JSON 完整性只能整段解析（与 v1 不同，v1 是文本 marker）
    buf: list[str] = []
    # 简易思考流：在第一个 `{` 之前的 token 实时透出，让用户感觉有响应
    json_started = False
    pre_json: list[str] = []

    try:
        async for delta in client.stream_complete(
            _CLARIFY_SYSTEM,
            user_payload,
            temperature=0.6,
            max_tokens=900,
        ):
            buf.append(delta)
            if not json_started:
                pre_json.append(delta)
                joined = "".join(pre_json)
                idx = joined.find("{")
                if idx >= 0:
                    head = joined[:idx]
                    if head.strip():
                        yield ThinkingDelta(text=head)
                    json_started = True
                    pre_json = []
                else:
                    # 没看到 `{` 之前的纯文本就是思考流
                    if delta:
                        yield ThinkingDelta(text=delta)
    except LLMError:
        log.exception("[clarify] LLM stream failed round=%d is_final=%s", round_no, is_final)
        raise

    full = "".join(buf)
    parsed = _extract_json_object(full)
    if parsed is None:
        log.warning("[clarify] failed to parse JSON, raw=%r", full[:500])
        # 最低兜底：把整段当 topic 塞进去，让用户能看见原文
        outline = ClarifyOutline(topic=full.strip()[:200] or None)
        thinking = ""
        question_raw: Any = None
    else:
        outline = _coerce_outline(parsed.get("outline") or {})
        thinking = str(parsed.get("thinking") or "").strip()
        question_raw = parsed.get("question")

    # detected_subjects 兜底：LLM 经常会忘把这些对象点名进 content。在 yield 前
    # 机械补回去——把缺的对象用顿号串拼到 content 末尾，括号注「（涉及 X、Y、Z）」。
    # 与系统提示是双保险：用户上传纸巾 → content 一定能看到「纸巾」二字。
    if detected_subjects:
        outline = _enforce_subjects_in_content(outline, detected_subjects)

    yield OutlineReady(outline=outline, thinking=thinking)

    question_out: Optional[str] = None if is_final else _coerce_question(question_raw)
    yield RoundDone(outline=outline, question=question_out, is_final=is_final)


def _enforce_subjects_in_content(
    outline: ClarifyOutline, subjects: list[str]
) -> ClarifyOutline:
    """LLM 忘把 detected_subjects 写进 outline.content 时，机械补回去。

    判定：subject 文本未出现在 content 里（精确子串匹配）。
    补法：把所有缺的 subject 顿号串拼，括号注「（涉及 ...）」追加到 content 末尾。
    总长度仍受 200 字限制；超过就截。
    """
    cleaned = [s.strip() for s in subjects if s and s.strip()]
    if not cleaned:
        return outline
    content = (outline.content or "").strip()
    missing = [s for s in cleaned if s not in content]
    if not missing:
        return outline
    suffix = "（涉及" + "、".join(missing) + "）"
    if content:
        new_content = content + suffix
    else:
        new_content = "核心卖点：" + "、".join(cleaned)
    new_content = new_content[:200]
    return outline.model_copy(update={"content": new_content})


def stitch_outline_to_brief(outline: ClarifyOutline) -> str:
    """把五件套拼成可直接灌进 BriefInput 的中文段。

    顺序固定：主题 → 内容 → 受众 → 目的 → 语气；缺的字段直接跳过，不留空头。
    用户点「采纳」时前端调用，后端 finalize 也复用——保证两边一致。
    """
    parts: list[tuple[str, Optional[str]]] = [
        ("主题", outline.topic),
        ("内容", outline.content),
        ("受众", outline.audience),
        ("目的", outline.goal),
        ("语气", outline.tone),
    ]
    chunks: list[str] = []
    for label, value in parts:
        if value and value.strip():
            chunks.append(f"【{label}】{value.strip()}")
    return "\n".join(chunks)
