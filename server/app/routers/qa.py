"""Module 5 — 引导式问答 (Guided Q&A).

为什么把"轮次到了 done=true"放在 router 这一层而不是 prompt 里：
  Prompt 是"软约束"，LLM 偶尔会忘——尤其在复杂上下文下它会自我延伸到第 4、5 轮。
  Router 这一层是 deterministic 拦截 —— 用户最多回答 MAX_QA_ROUNDS 题，超过就强制
  done=true，不再调用 LLM、节省 token、保证 UX 准时收敛。
"""
from __future__ import annotations

import json
import logging
import time

from fastapi import APIRouter, HTTPException, Request

from ..config import get_settings
from ..schemas import MAX_QA_ROUNDS, QAOption, QARequest, QAResponse
from ..services.llm_client import LLMError, get_llm_client
from ..services.prompts import QA_SYSTEM_PROMPT


router = APIRouter()
log = logging.getLogger("seecript.qa")


# 3 轮主题（与 prompt 中的描述对齐；router 把它注入到 user message，避免 prompt 漂移）。
_ROUND_TOPICS = {
    1: "Hook 改写角度（开场 3 秒怎么钩住）",
    2: "Body 中最值得做差异化的那一段（hook 之后、CTA 之前）的切入方式",
    3: "CTA 互动方式（让粉丝怎么留言、关注、行动）",
}


@router.post("/next", response_model=QAResponse)
async def qa_next(req: QARequest, request: Request) -> QAResponse:
    """Return the next guided question, or done=true after MAX_QA_ROUNDS."""
    trace_id = getattr(request.state, "trace_id", "-")
    started = time.perf_counter()

    answered = len(req.answers)

    # Hard convergence: prompt-level guidance is unreliable; router is deterministic.
    if answered >= MAX_QA_ROUNDS:
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        log.info("[%s] qa done after %d rounds (no LLM call)", trace_id, answered)
        return QAResponse(
            round=MAX_QA_ROUNDS,
            done=True,
            question=None,
            rationale=None,
            options=[],
            model_used="router",
            elapsed_ms=elapsed_ms,
        )

    next_round = answered + 1
    user_msg = _build_user_message(req, next_round)

    settings = get_settings()
    client = get_llm_client(settings)
    try:
        data = await client.complete_json(
            QA_SYSTEM_PROMPT,
            user_msg,
            max_tokens=settings.llm_qa_max_tokens,
        )
    except LLMError as e:
        log.error("[%s] qa LLM error: %s", trace_id, e)
        raise HTTPException(status_code=502, detail=f"LLM 调用失败：{e}") from e

    if not isinstance(data, dict):
        raise HTTPException(status_code=502, detail="LLM 返回格式异常")

    try:
        raw_options = data.get("options") or []
        options = [QAOption(**o) for o in raw_options]
    except Exception as e:
        log.warning("[%s] qa options schema mismatch: %s | raw=%s", trace_id, e, data)
        raise HTTPException(status_code=502, detail=f"LLM 返回字段不符合 schema：{e}") from e

    # Defensive cap: schema allows up to 4 options; trim if LLM over-produces.
    if len(options) < 2:
        raise HTTPException(status_code=502, detail=f"LLM 返回选项数过少（{len(options)}），无法构成单选题")
    options = options[:4]

    elapsed_ms = int((time.perf_counter() - started) * 1000)
    log.info(
        "[%s] qa ok | provider=%s | %dms | round=%d | options=%d",
        trace_id,
        client.name,
        elapsed_ms,
        next_round,
        len(options),
    )
    return QAResponse(
        round=next_round,
        done=False,
        question=data.get("question") or "（LLM 未返回问题文本）",
        rationale=data.get("rationale"),
        options=options,
        model_used=client.name,
        elapsed_ms=elapsed_ms,
    )


def _build_user_message(req: QARequest, next_round: int) -> str:
    """Compose the user-side message that pins the LLM down to the right round + context."""
    skeleton_json = json.dumps(req.skeleton, ensure_ascii=False, indent=2)

    persona_block = f"\n【当前人设】\n{req.persona_hint}" if req.persona_hint else ""
    transcript_block = f"\n【原视频台词（供参考）】\n{req.transcript}" if req.transcript else ""
    # brief 是用户在第 3 步开始前主动填的『创作要求』（时长/节奏/风格 + 自由补充）。
    # 把它作为强约束注入到每一轮 user message 里，AI 出选项时必须把这套约束落地。
    brief_block = (
        f"\n【用户自填的创作要求（必须遵守）】\n{req.brief}" if req.brief else ""
    )

    if req.answers:
        history_lines = []
        for a in req.answers:
            history_lines.append(f"  - 第 {a.round} 题：{a.question}\n    用户选了：{a.choice}")
        history_block = "\n【已回答的轮次】\n" + "\n".join(history_lines)
    else:
        history_block = "\n【已回答的轮次】\n  （无，本次是第 1 题）"

    topic_hint = _ROUND_TOPICS[next_round]

    return (
        f"【对标视频骨架】\n{skeleton_json}"
        f"{persona_block}"
        f"{brief_block}"
        f"{transcript_block}"
        f"{history_block}\n"
        f"\n【当前应出第 {next_round} 题】\n"
        f"  本轮主题：{topic_hint}\n"
        f"  请严格按系统提示词的 JSON 格式返回，round 字段必须等于 {next_round}。"
    )
