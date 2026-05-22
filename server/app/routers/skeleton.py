"""Module 1 — 爆款逆向拆解 (text-only entry; ASR uploaded transcript flows in here)."""
from __future__ import annotations

import logging
import time

from fastapi import APIRouter, HTTPException, Request

from ..schemas import (
    CTASection,
    HookSection,
    NarrativeBeat,
    SkeletonRequest,
    SkeletonResponse,
)
from ..config import get_settings
from ..services.llm_client import LLMError, get_llm_client
from ..services.prompts import SKELETON_SYSTEM_PROMPT


router = APIRouter()
log = logging.getLogger("seecript.skeleton")


@router.post("/extract", response_model=SkeletonResponse)
async def extract_skeleton(req: SkeletonRequest, request: Request) -> SkeletonResponse:
    """Extract Hook / Body / CTA / transferable template from a transcript."""
    trace_id = getattr(request.state, "trace_id", "-")
    started = time.perf_counter()

    persona_block = f"\n【当前人设上下文】{req.persona_hint}" if req.persona_hint else ""
    user_msg = f"【视频台词】\n{req.transcript}{persona_block}"

    settings = get_settings()
    client = get_llm_client(settings)
    try:
        data = await client.complete_json(
            SKELETON_SYSTEM_PROMPT,
            user_msg,
            max_tokens=settings.llm_skeleton_max_tokens,
        )
    except LLMError as e:
        log.error("[%s] skeleton LLM error: %s", trace_id, e)
        raise HTTPException(status_code=502, detail=f"LLM 调用失败：{e}") from e

    if not isinstance(data, dict):
        raise HTTPException(status_code=502, detail="LLM 返回格式异常")

    try:
        hook = HookSection(**data["hook"])
        body = [NarrativeBeat(**b) for b in data["body"]]
        cta = CTASection(**data["cta"])
        template = str(data["transferable_template"])
    except Exception as e:
        log.warning("[%s] skeleton schema mismatch: %s | raw=%s", trace_id, e, data)
        raise HTTPException(status_code=502, detail=f"LLM 返回字段不符合 schema：{e}") from e

    elapsed_ms = int((time.perf_counter() - started) * 1000)
    log.info("[%s] skeleton ok | provider=%s | %dms | beats=%d", trace_id, client.name, elapsed_ms, len(body))
    return SkeletonResponse(
        hook=hook,
        body=body,
        cta=cta,
        transferable_template=template,
        model_used=client.name,
        elapsed_ms=elapsed_ms,
    )
