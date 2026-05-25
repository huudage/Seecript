"""Module 2 — AI 人设生成."""
from __future__ import annotations

import logging
import time

from fastapi import APIRouter, HTTPException, Request

from ..config import get_settings
from ..schemas import PersonaPlan, PersonaRequest, PersonaResponse
from ..services.llm_client import LLMError, get_llm_client
from ..services.prompts import PERSONA_SYSTEM_PROMPT


router = APIRouter()
log = logging.getLogger("seecript.persona")


@router.post("/generate", response_model=PersonaResponse)
async def generate_persona(req: PersonaRequest, request: Request) -> PersonaResponse:
    """Generate 3 differentiated creator persona plans from background/interests/resources."""
    trace_id = getattr(request.state, "trace_id", "-")
    started = time.perf_counter()

    user_msg = (
        f"【职业背景】{req.background}\n"
        f"【兴趣 / 可拍内容】{req.interests}\n"
        f"【可用资源】{req.resources}"
    )

    settings = get_settings()
    client = get_llm_client(settings)
    try:
        data = await client.complete_json(
            PERSONA_SYSTEM_PROMPT,
            user_msg,
            max_tokens=settings.llm_persona_max_tokens,
        )
    except LLMError as e:
        log.error("[%s] persona LLM error: %s", trace_id, e)
        raise HTTPException(status_code=502, detail=f"LLM 调用失败：{e}") from e
    except Exception as e:  # pragma: no cover
        log.exception("[%s] persona unexpected error", trace_id)
        raise HTTPException(status_code=500, detail=f"人设生成失败：{e}") from e

    raw_personas = data.get("personas") if isinstance(data, dict) else None
    if not isinstance(raw_personas, list) or not raw_personas:
        log.warning("[%s] persona payload malformed: %s", trace_id, data)
        raise HTTPException(status_code=502, detail="LLM 返回格式异常：缺少 personas 字段")

    try:
        plans = [PersonaPlan(**p) for p in raw_personas]
    except Exception as e:
        log.warning("[%s] persona schema mismatch: %s | raw=%s", trace_id, e, raw_personas)
        raise HTTPException(status_code=502, detail=f"LLM 返回字段不符合 schema：{e}") from e

    elapsed_ms = int((time.perf_counter() - started) * 1000)
    log.info("[%s] persona ok | provider=%s | %dms | n=%d", trace_id, client.name, elapsed_ms, len(plans))
    return PersonaResponse(personas=plans, model_used=client.name, elapsed_ms=elapsed_ms)
