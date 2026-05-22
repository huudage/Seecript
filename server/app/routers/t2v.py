"""Module 7 — Text-to-Video routing (v0.9, Seecript 第 7 个 AI 干预点).

Endpoints:
  POST /api/t2v/submit      → 提交生成任务，返回 task_id（< 2s 返回）
  GET  /api/t2v/query/{id}  → 轮询任务，返回 status + video_url（成功时）

Why a separate router file (vs. cramming into script.py):
  - Single Responsibility: this router only knows about the T2V flow.
  - Future T2V extensions (image-to-video, ref-to-video) will land here, not in
    the script-generation route which has its own concerns.

Defensive guards (per project rule "防御性编程"):
  - Prompt length capped both in Pydantic schema AND in this router (defense in depth).
  - User-Agent / IP not used for rate limiting in v0.9 — paid quotas live in v1.1
    when we have a user identity model. Soft warning logged so we know if a single
    client is hammering the endpoint.
  - Errors from T2VClient mapped to specific HTTP codes, never 500 unless something
    truly unexpected happens (mirrors the LLM/ASR routers).
"""
from __future__ import annotations

import logging
import time
import uuid
from typing import Optional

from fastapi import APIRouter, HTTPException, Path, Request

from ..config import get_settings
from ..schemas import T2VQueryResponse, T2VSubmitRequest, T2VSubmitResponse
from ..services.t2v_client import T2VError, get_t2v_client
from ..services.t2v_shot_prompts import merge_shot_preview_prompt


router = APIRouter()
log = logging.getLogger("seecript.t2v_route")


# Constants — per project rule, no magic numbers in business code.
_USER_ID_MIN_LEN = 6
_USER_ID_MAX_LEN = 128


def _resolve_user_id(request: Request, override: Optional[str]) -> str:
    """Resolve a 6-128 char user_id for upstream moderation.

    Priority: explicit `user_id` from request body → trace_id (already 12 hex chars,
    pad to >=6 by prefix) → fresh uuid (always safe).
    """
    if override and _USER_ID_MIN_LEN <= len(override) <= _USER_ID_MAX_LEN:
        return override
    trace_id = getattr(request.state, "trace_id", None)
    if isinstance(trace_id, str) and len(trace_id) >= _USER_ID_MIN_LEN:
        # trace_id is 12 hex chars by default — well within bounds.
        return f"seecript-{trace_id}"
    return f"seecript-{uuid.uuid4().hex[:12]}"


@router.post("/submit", response_model=T2VSubmitResponse)
async def submit(request: Request, payload: T2VSubmitRequest) -> T2VSubmitResponse:
    """Kick off an async video generation job. Returns task_id immediately."""
    trace_id = getattr(request.state, "trace_id", "-")
    settings = get_settings()
    started = time.perf_counter()

    # ---- Defensive prompt length check (Pydantic also enforces; belt+suspenders) ----
    prompt = (payload.prompt or "").strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="prompt 不能为空")

    duration_override: Optional[int] = None
    if payload.shot_preview_mode:
        prompt = merge_shot_preview_prompt(prompt, settings.t2v_max_prompt_chars)
        duration_override = 10
    elif payload.duration_seconds in (5, 10):
        duration_override = payload.duration_seconds

    if len(prompt) > settings.t2v_max_prompt_chars:
        raise HTTPException(
            status_code=400,
            detail=(
                f"prompt 过长（{len(prompt)} 字 > 上限 {settings.t2v_max_prompt_chars} 字）。"
                "智谱 CogVideoX 官方限制 512 字；请精简描述（建议结构：主体 + 环境 + 镜头 + 氛围）。"
            ),
        )

    user_id = _resolve_user_id(request, payload.user_id)
    log.info(
        "[%s] t2v submit | size=%s | quality=%s | with_audio=%s | shot_preview=%s | dur=%s | prompt_len=%d | user=%s",
        trace_id,
        payload.size,
        payload.quality,
        payload.with_audio,
        payload.shot_preview_mode,
        duration_override,
        len(prompt),
        user_id,
    )

    client = get_t2v_client(settings)
    try:
        result = await client.submit(
            prompt,
            size=payload.size,
            quality=payload.quality,
            with_audio=payload.with_audio,
            user_id=user_id,
            duration_seconds=duration_override,
        )
    except T2VError as e:
        log.error("[%s] t2v submit error | code=%s upstream=%s msg=%s",
                  trace_id, e.code, e.upstream_status, e)
        raise HTTPException(status_code=_map_t2v_error_to_http(e), detail=str(e)) from e
    except Exception as e:  # pragma: no cover - last-resort net
        log.exception("[%s] t2v submit unexpected error", trace_id)
        raise HTTPException(status_code=500, detail=f"视频提交失败：{e}") from e

    elapsed_ms = int((time.perf_counter() - started) * 1000)
    return T2VSubmitResponse(
        task_id=result.task_id,
        request_id=result.request_id,
        model=result.model,
        provider=client.name,
        status="pending",
        elapsed_ms=elapsed_ms,
    )


@router.get("/query/{task_id}", response_model=T2VQueryResponse)
async def query(
    request: Request,
    task_id: str = Path(..., min_length=4, max_length=128, description="submit 返回的 task_id"),
) -> T2VQueryResponse:
    """Poll the status of an async generation job.

    Frontend should poll every 5s; CogVideoX typical completion is 30s-3min.
    Stop polling after ~8 minutes (frontend-side timer) and surface a friendly
    "still cooking" toast so users can manually retry later.
    """
    trace_id = getattr(request.state, "trace_id", "-")
    started = time.perf_counter()
    settings = get_settings()

    client = get_t2v_client(settings)
    try:
        result = await client.query(task_id)
    except T2VError as e:
        log.warning(
            "[%s] t2v query error | task_id=%s | code=%s upstream=%s",
            trace_id, task_id, e.code, e.upstream_status,
        )
        raise HTTPException(status_code=_map_t2v_error_to_http(e), detail=str(e)) from e
    except Exception as e:  # pragma: no cover
        log.exception("[%s] t2v query unexpected error | task_id=%s", trace_id, task_id)
        raise HTTPException(status_code=500, detail=f"视频查询失败：{e}") from e

    elapsed_ms = int((time.perf_counter() - started) * 1000)
    log.info(
        "[%s] t2v query ok | task_id=%s | status=%s | %dms",
        trace_id, task_id, result.status, elapsed_ms,
    )
    return T2VQueryResponse(
        task_id=result.task_id,
        status=result.status,
        model=result.model,
        provider=client.name,
        video_url=result.video_url,
        cover_image_url=result.cover_image_url,
        fail_reason=result.fail_reason,
        elapsed_ms=elapsed_ms,
    )


# --------------------------------------------------------------------------
# Error mapping (single-source-of-truth for upstream → HTTP code)
# --------------------------------------------------------------------------
def _map_t2v_error_to_http(err: T2VError) -> int:
    """Translate T2V provider errors to user-facing HTTP codes.

    - T2V_NO_KEY / T2V_BAD_REQUEST → 400 (config or input issue)
    - T2V_TASK_NOT_FOUND           → 404 (task unknown / expired)
    - 4xx upstream                 → 422 (client-fixable: bad prompt, content review reject)
    - 5xx upstream / network / timeout → 502 (upstream temporarily down — user should retry)
    - default                      → 502 (be conservative; never let a T2V error 500)
    """
    code = err.code
    if code in ("T2V_NO_KEY", "T2V_BAD_REQUEST"):
        return 400
    if code == "T2V_TASK_NOT_FOUND":
        return 404
    if err.upstream_status and 400 <= err.upstream_status < 500:
        return 422
    return 502
