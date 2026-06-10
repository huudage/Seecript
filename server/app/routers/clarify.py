"""Clarify Router —— 视频工坊 step 1 意图澄清的 SSE + JSON 接口（v2 · 五件套）。

`GET  /api/clarify/round?p=<base64>`  text/event-stream — 跑一轮 LLM 澄清，
                                       流式推 thinking token + outline_ready + 最后一条 done。
`POST /api/clarify/finalize`          application/json — 把前端拼好的五件套
                                       outline 直接拼成 brief 返回，不再调 LLM（v2 改动）。

为什么 round 走 GET + base64：浏览器 EventSource 只支持 GET，无法在 body 里塞 transcript。
统一用 `?p=<base64(JSON)>` 编码请求体——前端 ClarifyPanel 与本路由对称即可。

服务端职责：
- 3 轮硬上限：`is_final = round_no >= 3 or force_finalize`，LLM 越权多问被 clarify_agent 丢弃。
- 错误：任何 LLMError / 解析失败 → SSE `event: error`，前端进 phase='error' 但保留 streaming 文本。
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, ValidationError

from ..schemas import ClarifyOutline
from ..services.agent.clarify_agent import (
    ClarifyTurn,
    OutlineReady,
    RoundDone,
    ThinkingDelta,
    run_clarify_round,
    stitch_outline_to_brief,
)
from ..services.llm_client import LLMError

log = logging.getLogger("seecript.clarify")
router = APIRouter()

MAX_ROUNDS = 3


class ClarifyRoundRequest(BaseModel):
    """`/clarify/round` 请求体——通过 base64(JSON) 塞进 query。

    transcript 长度反推 round_no：transcript=[]→第 1 轮，len=1→第 2 轮，以此类推。
    detected_subjects 是前端从已上传素材的 VLM tags 聚合后传来的对象清单（#420），
    LLM 必须把它们写进 outline.content。
    """

    initial_brief: str = Field(..., max_length=4000)
    transcript: list[ClarifyTurn] = Field(default_factory=list)
    force_finalize: bool = False
    detected_subjects: list[str] = Field(default_factory=list, max_length=20)


class ClarifyFinalizeRequest(BaseModel):
    """v2：前端把已经编辑好的五件套 outline 直接塞过来，后端拼字段返回。
    initial_brief / transcript 留作兼容字段——若 outline 整张全空，
    回退把 initial_brief 当 final_brief 直接用，避免空 brief 漏出。"""

    outline: ClarifyOutline = Field(default_factory=ClarifyOutline)
    initial_brief: str = Field(default="", max_length=4000)
    transcript: list[ClarifyTurn] = Field(default_factory=list)


class ClarifyFinalizeResponse(BaseModel):
    final_brief: str
    outline: ClarifyOutline
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
    # 服务端硬 cap：超出第 3 轮强制 finalize。force_finalize=True 也走同一条路径。
    is_final = round_no >= MAX_ROUNDS or req.force_finalize

    async def event_gen():
        try:
            async for ev in run_clarify_round(
                initial_brief=req.initial_brief,
                transcript=req.transcript,
                round_no=min(round_no, MAX_ROUNDS),
                is_final=is_final,
                detected_subjects=req.detected_subjects,
            ):
                if isinstance(ev, ThinkingDelta):
                    yield _sse("progress", {
                        "step": "thinking",
                        "percent": 30,
                        "payload": {"delta": ev.text},
                    })
                elif isinstance(ev, OutlineReady):
                    yield _sse("progress", {
                        "step": "outline_ready",
                        "percent": 95,
                        "payload": {
                            "outline": ev.outline.model_dump(),
                            "thinking": ev.thinking,
                            "brief_subjects": ev.brief_subjects,
                            # detected_subjects 经意图清洗后的子集 + 被丢弃的陪衬物。
                            # 前端把 dropped 用删除线灰显出来，让用户看到 LLM 帮他过滤了哪些。
                            "relevant_detected_subjects": ev.relevant_detected_subjects,
                            "dropped_detected_subjects": ev.dropped_detected_subjects,
                        },
                    })
                elif isinstance(ev, RoundDone):
                    yield _sse("done", {
                        "round": min(round_no, MAX_ROUNDS),
                        "outline": ev.outline.model_dump(),
                        "question": ev.question,
                        "is_final": ev.is_final,
                    })
                # 给事件循环让步，确保 token 真按到达顺序到达浏览器
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
    """一键定稿（v2 不再调 LLM）—— 把前端编辑好的五件套 outline 拼成 brief 返回。

    为什么不调 LLM：v1 finalize 调一次 LLM 既慢、又可能把用户已经手动改好的字段
    再次「润色」掉，体感差。改成纯字段拼接后，用户每次点 OK 都 < 50 ms。
    """
    final_brief = stitch_outline_to_brief(req.outline)
    if not final_brief.strip():
        # outline 全空时回退到 initial_brief —— 至少不要给前端空字符串
        fallback = req.initial_brief.strip()
        if not fallback:
            raise HTTPException(
                status_code=400,
                detail="outline 五字段全为空且 initial_brief 也空，无法拼出 brief",
            )
        final_brief = fallback
    round_no = min(len(req.transcript) + 1, MAX_ROUNDS)
    return ClarifyFinalizeResponse(
        final_brief=final_brief.strip()[:2000],
        outline=req.outline,
        round=round_no,
    )
