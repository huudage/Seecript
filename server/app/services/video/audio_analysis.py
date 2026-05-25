"""librosa 音频分析：RMS 能量曲线 + onset + tempo。

依赖：librosa + soundfile —— 较重；未安装时回落 mock。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("seecript.video.audio_analysis")

try:
    import librosa
    import numpy as np
    _BACKEND = "librosa"
except ImportError:  # pragma: no cover
    _BACKEND = "mock"


@dataclass
class AudioProfile:
    duration_seconds: float
    times: list[float]
    rms_energy: list[float]      # 归一化到 [0, 1]
    onset_times: list[float]     # 节拍打击点
    tempo_bpm: float


def analyze_audio(audio_or_video_path: str | Path, hop_length: int = 1024) -> AudioProfile:
    """从音频或视频抽 BGM 能量曲线。

    Mock 模式返回 30 秒余弦能量 + 120 BPM，单调可预测，足够前端把图画出来。
    """
    path = Path(audio_or_video_path)
    if _BACKEND == "mock" or not path.exists():
        log.warning("[audio_analysis] backend=mock (path_exists=%s)", path.exists())
        import math
        n = 60
        times = [i * 0.5 for i in range(n)]
        rms = [0.5 + 0.4 * math.sin(i * 0.3) for i in range(n)]
        # clamp 到 [0,1]
        rms = [max(0.0, min(1.0, v)) for v in rms]
        onsets = [i * 0.5 for i in range(0, n, 2)]
        return AudioProfile(duration_seconds=30.0, times=times, rms_energy=rms,
                            onset_times=onsets, tempo_bpm=120.0)

    y, sr = librosa.load(str(path), sr=22050, mono=True)
    duration = float(len(y) / sr)
    rms_raw = librosa.feature.rms(y=y, hop_length=hop_length)[0]
    rms_max = float(rms_raw.max()) if rms_raw.size else 1.0
    rms_norm = (rms_raw / rms_max) if rms_max > 0 else rms_raw
    times = librosa.times_like(rms_raw, sr=sr, hop_length=hop_length)
    onsets = librosa.onset.onset_detect(y=y, sr=sr, units="time")
    tempo, _ = librosa.beat.beat_track(y=y, sr=sr)
    log.info("[audio_analysis] %s | dur=%.2fs | bpm=%.1f | onsets=%d",
             path.name, duration, float(tempo), len(onsets))
    return AudioProfile(
        duration_seconds=duration,
        times=[float(t) for t in times.tolist()],
        rms_energy=[float(v) for v in rms_norm.tolist()],
        onset_times=[float(t) for t in onsets.tolist()],
        tempo_bpm=float(tempo),
    )


def backend_name() -> str:
    return _BACKEND
