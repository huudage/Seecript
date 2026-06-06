"""Clarify Router —— 视频工坊 step 1 意图澄清的 SSE + JSON 接口。

`GET  /api/clarify/round?p=<base64>`  text/event-stream — 跑一轮 LLM 澄清,流式推 token + 最后一条 done。
`POST /api/clarify/finalize`          application/json — 一键定稿(等价于 force_finalize=True 的 round)。

为什么 round 走 GET + base64:浏览器 EventSource 只支持 GET,无法在 body 里塞 transcript。
统一用 `?p=<base64(JSON)>` 编码请求体——前端 ClarifyPanel 与本路由对称即可,sse.ts 不必改造。

服务端职责:
- 3 轮硬上限:`is_final = round_no >= 3 or force_finalize`,LLM 越权多问被 clarify_agent 丢弃。
- 错误:任何 LLMError / 解析失败 → SSE `event: error`,前端进 phase='error' 但保留 streaming 文本。
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, ValidationError

from ..services.agent.clarify_agent import (
    ClarifyTurn,
    DraftDone,
    RoundDone,
    TokenDelta,
    run_clarify_round,
)
from ..services.llm_client import LLMError

log = logging.getLogger("seecript.clarify")
router = APIRouter()

MAX_ROUNDS = 3


class ClarifyRoundRequest(BaseModel):
    """`/clarify/round` 请求体——通过 base64(JSON) 塞进 query。

    transcript 长度反推 round_no:transcript=[]→第 1 轮,len=1→第 2 轮,以此类推。
    """

    initial_brief: str = Field(..., max_length=4000)
    transcript: list[ClarifyTurn] = Field(default_factory=list)
    force_finalize: bool = False


class ClarifyFinalizeRequest(BaseModel):
    initial_brief: str = Field(..., max_length=4000)
    transcript: list[ClarifyTurn] = Field(default_factory=list)


class ClarifyFinalizeResponse(BaseModel):
    final_brief: str
    round: int


def _decode_payload(p: str) -> ClarifyRoundRequest:
    try:
        raw = base64.urlsafe_b64decode(p + "=" * (-len(p) % 4))
        data = json.loads(raw.decode("utf-8"))
    except (binascii.Error, ValueError, UnicodeDecodeError) as e:
        raise HTTPException(status_code=400, detail=f"invalid base64/json payload: {e}") from e
    try:
        return ClarifyRoundRequest.model_validate(data)
    except ValidationError as e:
        raise HTTPException(status_code=422, detail=e.errors()) from e


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@router.get("/clarify/round")
async def clarify_round(p: str = Query(..., description="base64(JSON) 的 ClarifyRoundRequest")) -> StreamingResponse:
    req = _decode_payload(p)
    round_no = len(req.transcript) + 1
    # 服务端硬 cap:超出第 3 轮强制 finalize。force_finalize=True 也走同一条路径。
    is_final = round_no >= MAX_ROUNDS or req.force_finalize

    async def event_gen():
        try:
            async for ev in run_clarify_round(
                initial_brief=req.initial_brief,
                transcript=req.transcript,
                round_no=min(round_no, MAX_ROUNDS),
                is_final=is_final,
            ):
                if isinstance(ev, TokenDelta):
                    yield _sse("progress", {
                        "step": "thinking",
                        "percent": 30,
                        "payload": {"delta": ev.text},
                    })
                elif isinstance(ev, DraftDone):
                    yield _sse("progress", {
                        "step": "draft_done",
                        "percent": 95,
                        "payload": {"draft": ev.draft},
                    })
                elif isinstance(ev, RoundDone):
                    yield _sse("done", {
                        "round": min(round_no, MAX_ROUNDS),
                        "question": ev.question,
                        "is_final": ev.is_final,
                        "final_brief": ev.final_brief,
                    })
                # 给事件循环让步,确保 token 真按到达顺序到达浏览器
                await asyncio.sleep(0)
        except LLMError as exc:
            log.warning("[clarify] LLMError code=%s: %s", exc.code, exc)
            yield _sse("error", {"detail": str(exc), "code": exc.code})
        except Exception as exc:
            log.exception("[clarify] unexpected failure")
            yield _sse("error", {"detail": str(exc), "code": "CLARIFY_INTERNAL"})

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/clarify/finalize", response_model=ClarifyFinalizeResponse)
async def clarify_finalize(req: ClarifyFinalizeRequest) -> ClarifyFinalizeResponse:
    """一键定稿——内部跑一轮 is_final=True 的 clarify,把最终 draft 直接返回。

    与流式接口不同:这里是普通 JSON,适合用户已经决定不再追问、想立刻看结果的场景。
    前端可在任意轮(transcript 可空可非空)调用。
    """
    round_no = min(len(req.transcript) + 1, MAX_ROUNDS)
    final_brief = ""
    try:
        async for ev in run_clarify_round(
            initial_brief=req.initial_brief,
            transcript=req.transcript,
            round_no=round_no,
            is_final=True,
        ):
            if isinstance(ev, RoundDone) and ev.final_brief:
                final_brief = ev.final_brief
            elif isinstance(ev, DraftDone) and not final_brief:
                final_brief = ev.draft
    except LLMError as exc:
        raise HTTPException(status_code=502, detail=f"LLM error: {exc}") from exc

    if not final_brief.strip():
        raise HTTPException(
            status_code=502,
            detail="LLM 未产出可用 draft,请稍后重试或检查 LLM_PROVIDER 配置",
        )
    return ClarifyFinalizeResponse(final_brief=final_brief.strip(), round=round_no)
