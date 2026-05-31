"""Scene-level voice synthesis —— 给 main_track 单个 scene 重写口播的共享入口。

被 3 处调用：
- routers/voice.py            手动触发的"单段合成 / 全段合成"
- routers/gap.py              copy fill 后的自动 TTS（_maybe_auto_tts）
- routers/edit.py             NL 编辑 voice 轨道改 narration 后的重合成

所有调用方都是 async handler，且底层 synthesize 是同步阻塞 httpx 调用——
**必须用 `await asyncio.to_thread(synthesize_scene_voice, ...)` 包一层**，
否则会卡死 gunicorn 单 worker 的 event loop（参考 voice.py 顶部注释）。
"""
from __future__ import annotations

import io
import logging
import wave
from typing import Optional

from ...schemas import Plan
from . import store as voice_store
from .client import TTSError, synthesize

log = logging.getLogger("seecript.tts.scene_voice")

# 火山 TTS speed_ratio 安全上限——超过 1.15 音质明显劣化（卷舌、爆音）。
# 由 demo 实测得来：1.2 已经能听出机械感，1.5 完全不可用。
_SPEED_RATIO_CEILING = 1.15


def _wav_duration_seconds(wav_bytes: bytes) -> float:
    try:
        with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
            frames = wf.getnframes()
            rate = wf.getframerate()
            if rate <= 0:
                return 0.0
            return frames / float(rate)
    except Exception as exc:  # noqa: BLE001
        log.warning("wav duration parse failed: %s", exc)
        return 0.0


def synthesize_with_alignment(
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
        "align actual=%.2fs target=%.2fs ratio=%.2f applied=%.2f",
        actual, target_seconds, desired_ratio, applied_ratio,
    )
    wav2 = synthesize(text, voice=voice, sample_rate=sample_rate, speed_ratio=applied_ratio)
    truncated = desired_ratio > _SPEED_RATIO_CEILING
    return wav2, truncated


def synthesize_scene_voice(
    plan: Plan,
    scene_id: str,
    *,
    text: Optional[str] = None,
    voice: Optional[str] = None,
) -> Optional[tuple[str, bool, int]]:
    """给 plan.main_track[scene_id] 重写口播。in-place 更新 plan，返回 (url, truncated, chars)。

    - text 未传则用 scene.narration；text 已传时会同步覆写 scene.narration
    - voice 未传则用 plan.settings.tts_voice
    - text 解析后为空 → 返回 None（caller 自己决定怎么处理）
    - 找不到 scene_id → 返回 None
    - TTS 合成失败 → 抛 TTSError（caller 决定 fallback 还是返回错误）

    注意：本函数同步阻塞（synthesize 是 httpx.post），async 调用方必须用 asyncio.to_thread 包。
    本函数 **不** 调 plan_store.put——由 caller 决定何时持久化（避免并发写）。
    """
    scene = next((sc for sc in plan.main_track if sc.scene_id == scene_id), None)
    if scene is None:
        log.warning("scene_id=%s not found in plan=%s", scene_id, plan.plan_id)
        return None

    final_text = (text if text is not None else scene.narration or "").strip()
    if not final_text:
        return None

    used_voice = (voice or plan.settings.tts_voice or "").strip() or "zh_female_qingxin"
    wav, truncated = synthesize_with_alignment(
        final_text, used_voice, float(scene.duration or 0.0),
    )
    url = voice_store.save_wav(plan.plan_id, scene_id, wav)
    scene.voiceover_url = url
    if text is not None:
        scene.narration = final_text
    return url, truncated, len(final_text)
