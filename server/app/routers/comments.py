"""Module 4 — 评论分拣助手."""
from __future__ import annotations

import logging
import time

from fastapi import APIRouter, HTTPException, Request

from ..config import get_settings
from ..schemas import ClassifiedComment, CommentsRequest, CommentsResponse
from ..services.llm_client import LLMError, get_llm_client
from ..services.prompts import COMMENTS_SYSTEM_PROMPT


router = APIRouter()
log = logging.getLogger("seecript.comments")


@router.post("/classify", response_model=CommentsResponse)
async def classify_comments(req: CommentsRequest, request: Request) -> CommentsResponse:
    """Classify pasted comments into high/medium/low value tiers and draft replies."""
    trace_id = getattr(request.state, "trace_id", "-")
    started = time.perf_counter()

    persona_block = f"\n【当前人设上下文】{req.persona_hint}" if req.persona_hint else ""
    user_msg = f"【原始评论】\n{req.raw_text}{persona_block}"

    settings = get_settings()
    client = get_llm_client(settings)
    try:
        data = await client.complete_json(
            COMMENTS_SYSTEM_PROMPT,
            user_msg,
            max_tokens=settings.llm_comments_max_tokens,
        )
    except LLMError as e:
        log.error("[%s] comments LLM error: %s", trace_id, e)
        raise HTTPException(status_code=502, detail=f"LLM 调用失败：{e}") from e

    if not isinstance(data, dict):
        raise HTTPException(status_code=502, detail="LLM 返回格式异常")

    try:
        high = [ClassifiedComment(**c) for c in data.get("high_value", [])]
        med = [ClassifiedComment(**c) for c in data.get("medium_value", [])]
        low_count = int(data.get("low_value_count", 0))
    except Exception as e:
        log.warning("[%s] comments schema mismatch: %s | raw=%s", trace_id, e, data)
        raise HTTPException(status_code=502, detail=f"LLM 返回字段不符合 schema：{e}") from e

    elapsed_ms = int((time.perf_counter() - started) * 1000)
    log.info(
        "[%s] comments ok | provider=%s | %dms | high=%d med=%d low=%d",
        trace_id,
        client.name,
        elapsed_ms,
        len(high),
        len(med),
        low_count,
    )
    return CommentsResponse(
        high_value=high,
        medium_value=med,
        low_value_count=low_count,
        model_used=client.name,
        elapsed_ms=elapsed_ms,
    )
