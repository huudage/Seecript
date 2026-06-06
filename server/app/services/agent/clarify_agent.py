"""Clarify Agent —— 视频工坊 step 1 意图澄清的多轮追问引擎。

为什么需要:用户在 BriefInput 里给的"主题/卖点/目的"通常是一句过于笼统的话
(『想做一个卖耳机的视频』),后续 plan_agent 拿到的 user payload 信息密度太低,
生成的 adapted_sections 容易偏题或互相重复。

工作方式:无状态多轮——前端每轮把 INITIAL_BRIEF + 历史 Q/A transcript 一起送进来,
本 agent 让 LLM 输出 `===DRAFT===` 段(最新整段 brief 重写稿) + `===QUESTION===` 段
(本轮唯一追问;最终轮强制 NULL)。流式 yield 给路由层做 SSE 推送。

3 轮硬上限在路由层 cap(`/clarify/round`),本 agent 接收到 `is_final=True` 时
会强制丢弃 LLM 越权输出的 question(即便 LLM 没遵守也兜底)。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import AsyncIterator, Optional

from pydantic import BaseModel

from ..llm_client import LLMError, get_llm_client

log = logging.getLogger("seecript.agent.clarify")


# System prompt 同时是 MockLLMClient 路由指纹:必须含 "短视频脚本意图澄清助手"。
# 不要轻易改这串字符——改了 mock 路由会失配,本地 dev mode 全链路会崩。
_CLARIFY_SYSTEM = (
    "你是短视频脚本意图澄清助手。任务是在最多 3 轮对话内,把用户最初零散的"
    "「意图」打磨成一份信息密度高、可直接用于生成分镜脚本的最终 brief。\n\n"
    "每一轮你要做两件事:\n"
    "1) 用自然语言简要复述你目前对用户意图的理解,识别最不确定的一个维度"
    "(目标受众、核心卖点/亮点、视频目的——卖货/种草/教程/娱乐、平台风格、口吻语气、行动号召)。\n"
    "2) 在所有维度中,挑【信息缺口最大】的那一个,提【一个】具体、可一句话回答的问题。"
    "问题不要套话,不要让用户做发散选择。\n\n"
    "输出严格用 ===PART=== 分隔的两段纯文本:\n"
    "===DRAFT===\n"
    "<对当前 brief 的最新重写稿,不超过 500 字,可直接灌入下游生成器>\n"
    "===QUESTION===\n"
    "<本轮唯一的追问;若你判断已经足够清晰,或处于最终轮,输出英文 NULL>\n\n"
    "最终轮规则: 当系统标注 IS_FINAL=true 时,你只能输出 ===DRAFT=== 段,"
    "QUESTION 段必须是 NULL。final draft 必须包含: 主题/卖点/受众/目的/平台/语气/CTA(若已知)。\n\n"
    "重要约束:\n"
    "- DRAFT 段开头之前的内容(思考流程)是允许的,会作为「思考流」展示给用户。\n"
    "- DRAFT 段必须以 `===DRAFT===` 行开始,且只有一处。\n"
    "- QUESTION 段必须以 `===QUESTION===` 行开始,且整段是一行短句或 NULL。"
)

_DRAFT_MARK = "===DRAFT==="
_QUESTION_MARK = "===QUESTION==="


class ClarifyTurn(BaseModel):
    """一轮 Q/A 历史。前端把 transcript 完整回传,本 agent 无状态。"""

    question: str
    answer: str


@dataclass
class TokenDelta:
    """流式输出的「思考流」片段——===DRAFT=== 标记之前的纯文本。"""

    text: str


@dataclass
class DraftDone:
    """检测到 `===QUESTION===` 标记后,DRAFT 段已确定。"""

    draft: str


@dataclass
class RoundDone:
    """整段 LLM 输出已完成。is_final 时 question 永远 None。
    final_brief 仅在最终轮非空(即可直接写回 BriefInput)。"""

    question: Optional[str]
    final_brief: Optional[str]
    is_final: bool


ClarifyEvent = TokenDelta | DraftDone | RoundDone


def _build_user_payload(
    *,
    initial_brief: str,
    transcript: list[ClarifyTurn],
    round_no: int,
    is_final: bool,
) -> str:
    lines: list[str] = []
    lines.append(f"INITIAL_BRIEF:\n{initial_brief.strip() or '(empty)'}\n")
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


async def run_clarify_round(
    *,
    initial_brief: str,
    transcript: list[ClarifyTurn],
    round_no: int,
    is_final: bool,
) -> AsyncIterator[ClarifyEvent]:
    """跑一轮意图澄清。

    yield 顺序:
    1. 任意条 TokenDelta(若干,直到检测到 `===DRAFT===` 标记)
    2. DraftDone(draft=...)  —— 当检测到 `===QUESTION===` 标记或流结束时
    3. RoundDone(question, final_brief, is_final) —— 最终一条

    is_final=True 时,RoundDone.question 强制 None,final_brief = DRAFT 段。
    """
    user_payload = _build_user_payload(
        initial_brief=initial_brief,
        transcript=transcript,
        round_no=round_no,
        is_final=is_final,
    )
    client = get_llm_client()

    # 流式缓冲:
    # - `phase` 控制 token 落到哪个累积区:'thinking' | 'draft' | 'question'
    # - `pending` 是滑动窗口,保留 N 个字符直到能判断标记是否在边界
    buf_thinking: list[str] = []
    buf_draft: list[str] = []
    buf_question: list[str] = []
    pending = ""
    phase = "thinking"
    draft_emitted = False

    # 标记最长长度:max(len(DRAFT_MARK), len(QUESTION_MARK)) - 1,
    # 至少留 13 字符未 flush 才能判断标记是否在中间被切。
    keep = max(len(_DRAFT_MARK), len(_QUESTION_MARK))

    async def _flush_thinking_tail() -> AsyncIterator[ClarifyEvent]:
        nonlocal pending
        if pending:
            buf_thinking.append(pending)
            yield TokenDelta(text=pending)
            pending = ""

    try:
        async for delta in client.stream_complete(
            _CLARIFY_SYSTEM,
            user_payload,
            temperature=0.7,
            max_tokens=800,
        ):
            pending += delta

            # 在每个 phase 下扫描 pending,把可确定的部分 flush 出去,
            # 剩下的尾巴留作下一片(防止标记被切断)。
            while True:
                if phase == "thinking":
                    idx = pending.find(_DRAFT_MARK)
                    if idx >= 0:
                        # 标记前的全部 token → thinking
                        head = pending[:idx]
                        if head:
                            buf_thinking.append(head)
                            yield TokenDelta(text=head)
                        # 跳过标记自身(包括标记后的可能换行)
                        rest = pending[idx + len(_DRAFT_MARK):]
                        if rest.startswith("\n"):
                            rest = rest[1:]
                        pending = rest
                        phase = "draft"
                        continue
                    # 没找到标记:flush 安全前缀,保留 keep 长尾巴
                    if len(pending) > keep:
                        safe = pending[:-keep]
                        buf_thinking.append(safe)
                        yield TokenDelta(text=safe)
                        pending = pending[-keep:]
                    break
                elif phase == "draft":
                    idx = pending.find(_QUESTION_MARK)
                    if idx >= 0:
                        head = pending[:idx]
                        buf_draft.append(head)
                        draft_text = "".join(buf_draft).strip()
                        if not draft_emitted:
                            yield DraftDone(draft=draft_text)
                            draft_emitted = True
                        rest = pending[idx + len(_QUESTION_MARK):]
                        if rest.startswith("\n"):
                            rest = rest[1:]
                        pending = rest
                        phase = "question"
                        continue
                    if len(pending) > keep:
                        safe = pending[:-keep]
                        buf_draft.append(safe)
                        pending = pending[-keep:]
                    break
                else:  # phase == "question"
                    # question 段不再寻找标记;一直累积到流结束
                    buf_question.append(pending)
                    pending = ""
                    break
    except LLMError as exc:
        log.exception("[clarify] LLM stream failed round=%d is_final=%s", round_no, is_final)
        raise

    # 流结束:把 pending 尾巴 flush 到当前 phase
    if pending:
        if phase == "thinking":
            buf_thinking.append(pending)
            yield TokenDelta(text=pending)
        elif phase == "draft":
            buf_draft.append(pending)
        else:
            buf_question.append(pending)

    # 计算最终 draft 与 question
    draft_text = "".join(buf_draft).strip()
    question_text = "".join(buf_question).strip()

    # 兜底:如果 LLM 没遵守标记,把整段 thinking 当 draft——
    # 这种情况只能尽力把内容写回 BriefInput,聊胜于无。
    if not draft_text and not draft_emitted:
        draft_text = "".join(buf_thinking).strip()

    if not draft_emitted and draft_text:
        yield DraftDone(draft=draft_text)

    # is_final 强制丢弃 question
    if is_final or question_text.upper() == "NULL" or not question_text:
        question_out: Optional[str] = None
    else:
        # 取第一行非空,避免 LLM 多说一堆
        first_line = next(
            (ln.strip() for ln in question_text.splitlines() if ln.strip() and ln.strip().upper() != "NULL"),
            None,
        )
        question_out = first_line

    final_brief = draft_text if is_final else None
    yield RoundDone(
        question=question_out,
        final_brief=final_brief,
        is_final=is_final,
    )
