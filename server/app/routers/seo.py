"""Module 3 — 标题与标签车间."""
from __future__ import annotations

import logging
import time

from fastapi import APIRouter, HTTPException, Request

from ..config import get_settings
from ..schemas import SEORequest, SEOResponse, TagCluster, TitleCandidate
from ..services.llm_client import LLMError, get_llm_client
from ..services.prompts import SEO_SYSTEM_PROMPT


router = APIRouter()
log = logging.getLogger("seecript.seo")


@router.post("/titles", response_model=SEOResponse)
async def generate_titles(req: SEORequest, request: Request) -> SEOResponse:
    """Produce 5+ title candidates, a description, and tag clusters for a target platform."""
    trace_id = getattr(request.state, "trace_id", "-")
    started = time.perf_counter()

    persona_block = f"\n【当前人设上下文】{req.persona_hint}" if req.persona_hint else ""
    # Platform is locked to douyin (Literal), so we don't echo it into the
    # prompt — the system prompt is already tuned for douyin specifically.
    user_msg = f"【脚本/口播稿】\n{req.script}{persona_block}"

    settings = get_settings()
    client = get_llm_client(settings)
    try:
        data = await client.complete_json(
            SEO_SYSTEM_PROMPT,
            user_msg,
            max_tokens=settings.llm_seo_max_tokens,
        )
    except LLMError as e:
        log.error("[%s] seo LLM error: %s", trace_id, e)
        raise HTTPException(status_code=502, detail=f"LLM 调用失败：{e}") from e

    if not isinstance(data, dict):
        raise HTTPException(status_code=502, detail="LLM 返回格式异常")

    try:
        titles = [TitleCandidate(**t) for t in data["titles"]]
        description = str(data["description"])
        tags = TagCluster(**data["tags"])
    except Exception as e:
        log.warning("[%s] seo schema mismatch: %s | raw=%s", trace_id, e, data)
        raise HTTPException(status_code=502, detail=f"LLM 返回字段不符合 schema：{e}") from e

    elapsed_ms = int((time.perf_counter() - started) * 1000)
    log.info(
        "[%s] seo ok | provider=%s | %dms | titles=%d platform=%s",
        trace_id,
        client.name,
        elapsed_ms,
        len(titles),
        req.platform,
    )
    return SEOResponse(
        titles=titles,
        description=description,
        tags=tags,
        platform=req.platform,
        model_used=client.name,
        elapsed_ms=elapsed_ms,
    )
