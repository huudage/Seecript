"""Voice router —— Compose 页"口播轨"动作入口。

Endpoints（prefix=/api）：
- POST   /voice/synthesize       { plan_id, scene_id, text? }      → 单段合成
- POST   /voice/synthesize-all   { plan_id }                       → 全 scene 一键合成
- DELETE /voice/{plan_id}/{scene_id}                              → 清除单段

合成完成后立刻把 plan.main_track[i].voiceover_url 字段更新并持久化，
让渲染 pipeline 在 voice_mix 步骤能拾到。

时间对齐策略（synthesize-all 与 synthesize 都生效）：
1. 始终先按 speed=1.0 合成，读 wav 头算实际秒数（TTS 自带 speed_ratio 会爆音，禁用）
2. 若 |actual - target| / target 在保音质区间 [0.75, 1.30]：用 ffmpeg `atempo` 滤镜
   做保音高时长重整，对齐到 scene.duration
3. 若超出保音质区间：忠于口播稿按 1.0x 自然时长返回，不强对齐——
   note 字段标记 truncated=True 让前端在字幕轨提示"音频长于 scene.duration，可能跨段"
   （由渲染端处理：amix duration=first 默认按视频长度截，不会撕裂下一段开头）
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..schemas import Plan
from ..services.plans import plan_store
from ..services.tts import TTSError, backend_name, synthesize_with_alignment
from ..services.tts import store as voice_store

log = logging.getLogger("seecript.voice")
router = APIRouter()


class VoiceSynthesizeRequest(BaseModel):
    plan_id: str
    scene_id: str
    text: Optional[str] = Field(
        default=None,
        max_length=500,
        description="覆盖 scene.narration 用的临时文案；不传则用 scene.narration。",
    )
    voice: Optional[str] = Field(default=None, description="覆盖 plan.settings.tts_voice")


class VoiceSynthesizeResponse(BaseModel):
    plan_id: str
    scene_id: str
    voiceover_url: str
    backend: str
    chars: int
    truncated: bool = False  # True 表示 atempo 区间外回退到 1.0x，音频自然时长 > scene.duration（不再截尾）


class VoiceSynthesizeAllRequest(BaseModel):
    plan_id: str


class VoiceSynthesizeAllResponse(BaseModel):
    plan_id: str
    backend: str
    synthesized: list[VoiceSynthesizeResponse] = Field(default_factory=list)
    skipped_scene_ids: list[str] = Field(default_factory=list)
    truncated_scene_ids: list[str] = Field(default_factory=list)
    failures: list[dict] = Field(default_factory=list)


def _require_plan(plan_id: str) -> Plan:
    plan = plan_store.get(plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail=f"plan_id 不存在：{plan_id}")
    return plan


def _scene_idx(plan: Plan, scene_id: str) -> int:
    for i, sc in enumerate(plan.main_track):
        if sc.scene_id == scene_id:
            return i
    raise HTTPException(status_code=404, detail=f"scene_id 不存在于 plan：{scene_id}")


@router.post("/voice/synthesize", response_model=VoiceSynthesizeResponse)
async def synthesize_one(req: VoiceSynthesizeRequest) -> VoiceSynthesizeResponse:
    plan = _require_plan(req.plan_id)
    idx = _scene_idx(plan, req.scene_id)
    scene = plan.main_track[idx]
    text = (req.text or scene.narration or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="scene 没有 narration 也未提供 text")

    voice = (req.voice or plan.settings.tts_voice).strip()
    try:
        wav_bytes, truncated = await asyncio.to_thread(
            synthesize_with_alignment,
            text, voice, float(scene.duration or 0.0),
        )
    except TTSError as exc:
        log.warning("[voice] synthesize failed plan=%s scene=%s code=%s err=%s",
                    req.plan_id, req.scene_id, exc.code, exc)
        raise HTTPException(status_code=502, detail=f"TTS failed: {exc}")

    url = voice_store.save_wav(req.plan_id, req.scene_id, wav_bytes)
    scene.voiceover_url = url
    if req.text and req.text.strip():
        scene.narration = req.text.strip()
    plan_store.put(plan)
    log.info(
        "[voice] synthesized plan=%s scene=%s chars=%d voice=%s backend=%s url=%s truncated=%s",
        req.plan_id, req.scene_id, len(text), voice, backend_name(), url, truncated,
    )
    return VoiceSynthesizeResponse(
        plan_id=req.plan_id,
        scene_id=req.scene_id,
        voiceover_url=url,
        backend=backend_name(),
        chars=len(text),
        truncated=truncated,
    )


@router.post("/voice/synthesize-all", response_model=VoiceSynthesizeAllResponse)
async def synthesize_all(req: VoiceSynthesizeAllRequest) -> VoiceSynthesizeAllResponse:
    plan = _require_plan(req.plan_id)
    if not plan.settings.voiceover_enabled:
        raise HTTPException(
            status_code=400,
            detail="该 plan 的 voiceover_enabled=False；请先在 Compose 设置打开口播开关。",
        )

    voice = plan.settings.tts_voice
    results: list[VoiceSynthesizeResponse] = []
    skipped: list[str] = []
    truncated_ids: list[str] = []
    failures: list[dict] = []

    for scene in plan.main_track:
        text = (scene.narration or "").strip()
        if not text:
            skipped.append(scene.scene_id)
            continue
        try:
            wav_bytes, truncated = await asyncio.to_thread(
                synthesize_with_alignment,
                text, voice, float(scene.duration or 0.0),
            )
        except TTSError as exc:
            failures.append({"scene_id": scene.scene_id, "code": exc.code, "error": str(exc)})
            continue
        url = voice_store.save_wav(plan.plan_id, scene.scene_id, wav_bytes)
        scene.voiceover_url = url
        if truncated:
            truncated_ids.append(scene.scene_id)
        results.append(VoiceSynthesizeResponse(
            plan_id=plan.plan_id,
            scene_id=scene.scene_id,
            voiceover_url=url,
            backend=backend_name(),
            chars=len(text),
            truncated=truncated,
        ))

    plan_store.put(plan)
    log.info(
        "[voice] synthesize_all plan=%s ok=%d skipped=%d truncated=%d failed=%d backend=%s",
        req.plan_id, len(results), len(skipped), len(truncated_ids), len(failures), backend_name(),
    )
    return VoiceSynthesizeAllResponse(
        plan_id=req.plan_id,
        backend=backend_name(),
        synthesized=results,
        skipped_scene_ids=skipped,
        truncated_scene_ids=truncated_ids,
        failures=failures,
    )


@router.delete("/voice/{plan_id}/{scene_id}", response_model=Plan)
async def delete_voice(plan_id: str, scene_id: str) -> Plan:
    plan = _require_plan(plan_id)
    idx = _scene_idx(plan, scene_id)
    voice_store.delete(plan_id, scene_id)
    plan.main_track[idx].voiceover_url = None
    plan_store.put(plan)
    return plan
