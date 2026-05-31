"""Voice router —— Compose 页"口播轨"动作入口。

Endpoints（prefix=/api）：
- POST   /voice/synthesize       { plan_id, scene_id, text? }      → 单段合成
- POST   /voice/synthesize-all   { plan_id }                       → 全 scene 一键合成
- DELETE /voice/{plan_id}/{scene_id}                              → 清除单段

合成完成后立刻把 plan.main_track[i].voiceover_url 字段更新并持久化，
让渲染 pipeline 在 voice_mix 步骤能拾到。

时间对齐策略（synthesize-all 与 synthesize 都生效）：
1. 先按 speed=1.0 合成，读 wav 头算实际秒数
2. 若 actual > scene.duration：speed_ratio = min(1.15, actual / scene.duration)，
   按该 speed_ratio 重新合成一次——保证最终长度尽量贴 scene.duration 但音质不崩
3. 若 actual ≤ scene.duration：直接用，多余尾部由 ffmpeg 渲染时静音填充
4. 若 actual / scene.duration > 1.15：合成后仍超出，渲染端会截尾，
   note 字段标记 truncated=True 让前端在字幕轨提示用户缩文案
"""
from __future__ import annotations

import io
import logging
import wave
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..schemas import Plan
from ..services.plans import plan_store
from ..services.tts import TTSError, backend_name, synthesize
from ..services.tts import store as voice_store

log = logging.getLogger("seecript.voice")
router = APIRouter()


# 火山 TTS speed_ratio 安全上限——超过 1.15 音质明显劣化（卷舌、爆音）。
# 由 demo 实测得来：1.2 已经能听出机械感，1.5 完全不可用。
_SPEED_RATIO_CEILING = 1.15


def _wav_duration_seconds(wav_bytes: bytes) -> float:
    """读 wav 头算时长；解析失败返回 0（caller 跳过对齐）。"""
    try:
        with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
            frames = wf.getnframes()
            rate = wf.getframerate()
            if rate <= 0:
                return 0.0
            return frames / float(rate)
    except Exception as exc:  # noqa: BLE001
        log.warning("[voice] wav duration parse failed: %s", exc)
        return 0.0


def _synthesize_with_alignment(
    text: str, voice: str, target_seconds: float, sample_rate: int = 24000,
) -> tuple[bytes, bool]:
    """合成并按 target_seconds 做 speed_ratio 对齐。返回 (wav_bytes, truncated_flag)。

    truncated=True 表示即使加速到 _SPEED_RATIO_CEILING 仍然超长，渲染端需截尾。
    """
    wav = synthesize(text, voice=voice, sample_rate=sample_rate, speed_ratio=1.0)
    if target_seconds <= 0:
        return wav, False
    actual = _wav_duration_seconds(wav)
    if actual <= 0 or actual <= target_seconds:
        return wav, False
    desired_ratio = actual / target_seconds
    if desired_ratio <= 1.0:
        return wav, False
    applied_ratio = min(_SPEED_RATIO_CEILING, desired_ratio)
    log.info(
        "[voice] align actual=%.2fs target=%.2fs ratio=%.2f applied=%.2f",
        actual, target_seconds, desired_ratio, applied_ratio,
    )
    wav2 = synthesize(text, voice=voice, sample_rate=sample_rate, speed_ratio=applied_ratio)
    truncated = desired_ratio > _SPEED_RATIO_CEILING
    return wav2, truncated


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
    truncated: bool = False  # True 表示文案过长，即使加速到 1.15 仍超 scene.duration，渲染端会截尾


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
        wav_bytes, truncated = _synthesize_with_alignment(
            text, voice=voice, target_seconds=float(scene.duration or 0.0),
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
            wav_bytes, truncated = _synthesize_with_alignment(
                text, voice=voice, target_seconds=float(scene.duration or 0.0),
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
