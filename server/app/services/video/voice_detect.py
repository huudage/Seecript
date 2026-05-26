"""人声活性探测（VAD）—— 决定要不要走 ASR。

为什么需要这一步：
- 不少视频是纯 BGM / 环境音，没有口播。给这种视频调 ASR 既浪费 API 配额，
  又会被引擎当成"长静音"返回 20000003 错误污染日志。
- ASR 的口播切片是模块 5 字幕 burn-in 的时间轴依据；纯 BGM 视频没必要走这一步，
  字幕环节会改成"只渲染段落标题条"。

实现原理：
- librosa 加载音频 → mono 22050Hz
- STFT → 拿到频域能量
- 取 300–3400 Hz 人声频带 / 全频带能量比，按 1s 窗口聚合
- 计算"超过阈值的窗口数 / 总窗口数"作为 voice_ratio
- voice_ratio ≥ threshold ⇒ has_voice=True

无 librosa 时（mock backend）：保守判定 has_voice=True，让流水线继续走 ASR fallback 路径。

阈值：
- 默认 0.35（一段视频里至少 35% 时间命中人声频带，才认为有口播）。
- 业务上比赛素材绝大多数要么"通篇口播"（≈0.7+），要么"通篇 BGM"（≈0.05），中间地带很少；
  0.35 把界划在两端中间，对极端边界视频不敏感。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

log = logging.getLogger("seecript.video.voice_detect")

try:
    import librosa
    import numpy as np
    _BACKEND = "librosa"
except ImportError:  # pragma: no cover
    _BACKEND = "mock"


VOICE_BAND_LOW_HZ = 300.0
VOICE_BAND_HIGH_HZ = 3400.0
DEFAULT_THRESHOLD = 0.35


@dataclass
class VoiceDetectResult:
    has_voice: bool
    voice_ratio: float          # 命中人声频带的时间窗占比，[0, 1]
    threshold: float
    backend: str                # "librosa" / "mock"
    duration_seconds: float
    note: str = ""


def detect_voice(
    audio_or_video_path: str | Path,
    *,
    threshold: float = DEFAULT_THRESHOLD,
    window_seconds: float = 1.0,
    voice_band_energy_min_ratio: float = 0.18,
) -> VoiceDetectResult:
    """探测一段音频是否含口播。

    Args:
      threshold: 命中人声频带的窗口数 / 总窗口数 大于该值时 has_voice=True
      window_seconds: 时间窗大小
      voice_band_energy_min_ratio: 单窗口判"有人声"——人声带能量 / 全频带能量 ≥ 该值
    """
    path = Path(audio_or_video_path)
    if _BACKEND == "mock" or not path.exists():
        # 路径不存在或没有 librosa：保守判 has_voice=True，让上层照常走 ASR fallback
        return VoiceDetectResult(
            has_voice=True,
            voice_ratio=1.0,
            threshold=threshold,
            backend="mock",
            duration_seconds=0.0,
            note=f"librosa unavailable or path missing ({path}); defaulting has_voice=True",
        )

    try:
        y, sr = librosa.load(str(path), sr=22050, mono=True)
    except Exception as exc:  # noqa: BLE001 — 解码失败回退到保守判定
        log.warning("[voice_detect] load failed, fallback has_voice=True: %s", exc)
        return VoiceDetectResult(
            has_voice=True,
            voice_ratio=1.0,
            threshold=threshold,
            backend=_BACKEND,
            duration_seconds=0.0,
            note=f"load failed: {exc}",
        )

    duration = float(len(y) / sr)
    if duration < 0.5:
        return VoiceDetectResult(
            has_voice=False,
            voice_ratio=0.0,
            threshold=threshold,
            backend=_BACKEND,
            duration_seconds=duration,
            note="audio too short (<0.5s)",
        )

    n_fft = 2048
    hop_length = max(1, int(sr * window_seconds))  # 1s/窗
    # |STFT|^2 → 形状 (freq_bins, frames)
    spec = np.abs(librosa.stft(y=y, n_fft=n_fft, hop_length=hop_length)) ** 2
    freqs = librosa.fft_frequencies(sr=sr, n_fft=n_fft)
    voice_mask = (freqs >= VOICE_BAND_LOW_HZ) & (freqs <= VOICE_BAND_HIGH_HZ)

    voice_energy = spec[voice_mask].sum(axis=0)        # (frames,)
    total_energy = spec.sum(axis=0) + 1e-12            # 防 0
    ratio_per_frame = voice_energy / total_energy      # 每窗人声带占比

    voiced_frames = int((ratio_per_frame >= voice_band_energy_min_ratio).sum())
    total_frames = max(1, len(ratio_per_frame))
    voice_ratio = voiced_frames / total_frames
    has_voice = voice_ratio >= threshold

    log.info(
        "[voice_detect] %s | dur=%.2fs | voiced_frames=%d/%d | ratio=%.3f | has_voice=%s",
        path.name, duration, voiced_frames, total_frames, voice_ratio, has_voice,
    )
    return VoiceDetectResult(
        has_voice=has_voice,
        voice_ratio=float(voice_ratio),
        threshold=threshold,
        backend=_BACKEND,
        duration_seconds=duration,
    )


def backend_name() -> str:
    return _BACKEND
