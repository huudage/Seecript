"""Module 6 — 原创分镜脚本 (Final Script).

Flow:
  Frontend collects skeleton (from /api/skeleton/extract) + answers (from /api/qa/next loop)
  → POST /api/script/generate → DeepSeek returns hook_narration + scenes[] + cta_narration + full_text
  → Frontend renders the final script panel and enables 「复制纯文本」.
"""
from __future__ import annotations

import json
import logging
import time

from fastapi import APIRouter, HTTPException, Request

from ..config import get_settings
from ..schemas import ScriptRequest, ScriptResponse, ScriptScene
from ..services.llm_client import LLMError, get_llm_client
from ..services.prompts import SCRIPT_SYSTEM_PROMPT


router = APIRouter()
log = logging.getLogger("seecript.script")


@router.post("/generate", response_model=ScriptResponse)
async def generate_script(req: ScriptRequest, request: Request) -> ScriptResponse:
    """Combine skeleton + Q&A answers into a ready-to-record original script."""
    trace_id = getattr(request.state, "trace_id", "-")
    started = time.perf_counter()

    user_msg = _build_user_message(req)

    settings = get_settings()
    client = get_llm_client(settings)
    try:
        data = await client.complete_json(
            SCRIPT_SYSTEM_PROMPT,
            user_msg,
            max_tokens=settings.llm_script_max_tokens,
        )
    except LLMError as e:
        log.error("[%s] script LLM error: %s", trace_id, e)
        raise HTTPException(status_code=502, detail=f"LLM 调用失败：{e}") from e

    if not isinstance(data, dict):
        raise HTTPException(status_code=502, detail="LLM 返回格式异常")

    try:
        scenes = [ScriptScene(**s) for s in (data.get("scenes") or [])]
        hook_narration = str(data["hook_narration"]).strip()
        cta_narration = str(data["cta_narration"]).strip()
        full_text = str(data.get("full_text") or "").strip()
    except Exception as e:
        log.warning("[%s] script schema mismatch: %s | raw=%s", trace_id, e, data)
        raise HTTPException(status_code=502, detail=f"LLM 返回字段不符合 schema：{e}") from e

    # If LLM forgot full_text, synthesize it from the structured pieces so the
    # frontend's "复制纯文本" button always has something usable.
    if not full_text:
        log.warning("[%s] script full_text missing, synthesizing from parts", trace_id)
        full_text = _synthesize_full_text(hook_narration, scenes, cta_narration)

    elapsed_ms = int((time.perf_counter() - started) * 1000)
    log.info(
        "[%s] script ok | provider=%s | %dms | scenes=%d | full_text_chars=%d",
        trace_id,
        client.name,
        elapsed_ms,
        len(scenes),
        len(full_text),
    )
    return ScriptResponse(
        hook_narration=hook_narration,
        scenes=scenes,
        cta_narration=cta_narration,
        full_text=full_text,
        model_used=client.name,
        elapsed_ms=elapsed_ms,
    )


def _build_user_message(req: ScriptRequest) -> str:
    skeleton_json = json.dumps(req.skeleton, ensure_ascii=False, indent=2)
    persona_block = f"\n【当前人设】\n{req.persona_hint}" if req.persona_hint else ""
    # brief 与 QA 阶段共享同一份用户自填创作要求——脚本阶段必须延续这套约束，
    # 否则会出现「问答时按 30s 紧凑节奏选了选项，最终脚本却写了 1200 字」的不一致体验。
    brief_block = (
        f"\n【用户自填的创作要求（必须遵守）】\n{req.brief}" if req.brief else ""
    )
    transcript_block = f"\n【原视频台词（仅供识别「不能照抄」的反面教材）】\n{req.transcript}" if req.transcript else ""

    if req.answers:
        answer_lines = []
        for a in req.answers:
            answer_lines.append(f"  - 第 {a.round} 题：{a.question}\n    用户选了：{a.choice}")
        answer_block = "\n【用户在 3 轮单选题里给出的关键决策】\n" + "\n".join(answer_lines)
    else:
        answer_block = "\n【用户在 3 轮单选题里给出的关键决策】\n  （无回答，请按骨架默认改写）"

    return (
        f"【对标视频骨架】\n{skeleton_json}"
        f"{persona_block}"
        f"{brief_block}"
        f"{transcript_block}"
        f"{answer_block}\n"
        f"\n请严格按系统提示词的 JSON 格式返回完整的原创分镜脚本。"
    )


def _synthesize_full_text(hook: str, scenes: list[ScriptScene], cta: str) -> str:
    """Last-resort fallback when LLM omits full_text."""
    parts = [f"【Hook · 0:00-0:03】{hook}"]
    for s in scenes:
        parts.append(f"\n【{s.title} · {s.timestamp}】{s.narration}")
    parts.append(f"\n【CTA · 收尾】{cta}")
    return "\n".join(parts)
