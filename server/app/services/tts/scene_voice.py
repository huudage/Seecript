"""Scene-level voice synthesis —— 给 main_track 单个 scene 重写口播的共享入口。

被 3 处调用：
- routers/voice.py            手动触发的"单段合成 / 全段合成"
- routers/gap.py              copy fill 后的自动 TTS（_maybe_auto_tts）
- routers/edit.py             NL 编辑 voice 轨道改 narration 后的重合成

所有调用方都是 async handler，且底层 synthesize 是同步阻塞 httpx 调用——
**必须用 `await asyncio.to_thread(synthesize_scene_voice, ...)` 包一层**，
否则会卡死 gunicorn 单 worker 的 event loop（参考 voice.py 顶部注释）。

对齐策略（2026-06 重构）：
- TTS 始终用 speed_ratio=1.0 合成——TTS 内置的 speed_ratio 在 >1.15 会卷舌/爆音，
  且发现 0.85 已经听得出机械感，质量不可控。
- 拿到 1.0x 的 wav 后用 ffmpeg `atempo` 滤镜做保音高时长重整，质量明显优于 TTS 自身的 speed_ratio。
- atempo 安全区间锁在 [_ATEMPO_MIN, _ATEMPO_MAX]——超出区间不再强行对齐，
  让音频按 1.0x 自然时长播放，由渲染端处理"音频长于 scene.duration"的情况。
  （依据用户决策："如果不行，则忠于口播稿读一遍即可，不需要强行对齐时间"）
"""
from __future__ import annotations

import io
import logging
import shutil
import subprocess
import tempfile
import wave
from pathlib import Path
from typing import Optional

from ...schemas import Plan
from . import store as voice_store
from .client import TTSError, synthesize

log = logging.getLogger("seecript.tts.scene_voice")

# atempo 保音质区间——超出此范围回退到 1.0x（不强对齐）。
# - 0.75 以下会有金属混响、0.7 已经能感到 "拖泥带水"
# - 1.30 以上轻微机械感开始堆积；1.40 起就能听到节奏抖动
_ATEMPO_MIN = 0.75
_ATEMPO_MAX = 1.30


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


def _build_atempo_chain(ratio: float) -> str:
    """构造 atempo 滤镜链。单个 atempo 接受 [0.5, 2.0]，超出需链式拆分。

    实际上调用方已经把 ratio 夹在 [0.75, 1.30] 之内，链式拆分仅是防御性兜底。
    """
    if 0.5 <= ratio <= 2.0:
        return f"atempo={ratio:.4f}"
    parts: list[float] = []
    r = ratio
    while r > 2.0:
        parts.append(2.0)
        r /= 2.0
    while r < 0.5:
        parts.append(0.5)
        r /= 0.5
    parts.append(r)
    return ",".join(f"atempo={x:.4f}" for x in parts)


def _apply_atempo(wav_bytes: bytes, ratio: float) -> bytes:
    """用 ffmpeg atempo 做保音高时长重整。失败抛 RuntimeError，由 caller 决定降级。"""
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg not in PATH")
    chain = _build_atempo_chain(ratio)
    # ffmpeg stdin 不接受非 seek 的 wav，落临时文件最稳。
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tin:
        tin.write(wav_bytes)
        in_path = Path(tin.name)
    out_path = in_path.with_name(in_path.stem + "-atempo.wav")
    try:
        cmd = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(in_path),
            "-filter:a", chain,
            "-c:a", "pcm_s16le",
            str(out_path),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if proc.returncode != 0:
            raise RuntimeError(f"atempo ffmpeg failed: {proc.stderr.strip()}")
        return out_path.read_bytes()
    finally:
        try:
            in_path.unlink(missing_ok=True)
            out_path.unlink(missing_ok=True)
        except OSError:
            pass


def synthesize_with_alignment(
    text: str, voice: str, target_seconds: float, sample_rate: int = 24000,
) -> tuple[bytes, bool]:
    """合成并尝试用 atempo 对齐到 target_seconds。返回 (wav_bytes, overflows_target)。

    overflows_target=True 表示音频最终时长仍然超过 target_seconds（atempo 区间外回退到 1.0x），
    渲染端据此决定是否扩展 scene 时长或允许跨段播放——**不再做截尾**。
    """
    wav = synthesize(text, voice=voice, sample_rate=sample_rate, speed_ratio=1.0)
    if target_seconds <= 0:
        return wav, False
    actual = _wav_duration_seconds(wav)
    if actual <= 0:
        return wav, False
    # ffmpeg atempo 的 ratio 语义：>1 加速（短），<1 减速（长）。
    desired_ratio = actual / target_seconds
    if abs(desired_ratio - 1.0) < 0.02:
        return wav, actual > target_seconds
    if desired_ratio < _ATEMPO_MIN or desired_ratio > _ATEMPO_MAX:
        # 出区间——不强对齐，按 1.0x 自然时长播放
        log.info(
            "tts align skipped (out of quality range): actual=%.2fs target=%.2fs ratio=%.2f",
            actual, target_seconds, desired_ratio,
        )
        return wav, actual > target_seconds
    try:
        adjusted = _apply_atempo(wav, desired_ratio)
    except Exception as exc:  # noqa: BLE001
        log.warning("atempo failed (%s), falling back to 1.0x", exc)
        return wav, actual > target_seconds
    new_actual = _wav_duration_seconds(adjusted)
    log.info(
        "tts align via atempo: actual=%.2fs → %.2fs target=%.2fs ratio=%.2f",
        actual, new_actual, target_seconds, desired_ratio,
    )
    return adjusted, new_actual > target_seconds + 0.05


def synthesize_scene_voice(
    plan: Plan,
    scene_id: str,
    *,
    text: Optional[str] = None,
    voice: Optional[str] = None,
) -> Optional[tuple[str, bool, int]]:
    """给 plan.main_track[scene_id] 重写口播。in-place 更新 plan，返回 (url, overflows, chars)。

    - text 未传则用 scene.narration；text 已传时会同步覆写 scene.narration
    - voice 未传则用 plan.settings.tts_voice
    - text 解析后为空 → 返回 None（caller 自己决定怎么处理）
    - 找不到 scene_id → 返回 None
    - TTS 合成失败 → 抛 TTSError（caller 决定 fallback 还是返回错误）

    overflows=True 表示最终音频时长仍 > scene.duration（atempo 出区间已回退到 1.0x），
    由渲染端决定是否扩段或允许跨段。

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
    wav, overflows = synthesize_with_alignment(
        final_text, used_voice, float(scene.duration or 0.0),
    )
    url = voice_store.save_wav(plan.plan_id, scene_id, wav)
    scene.voiceover_url = url
    if text is not None:
        scene.narration = final_text
    return url, overflows, len(final_text)
