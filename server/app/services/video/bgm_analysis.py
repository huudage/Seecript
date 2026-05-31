"""BGM 能量分析：上传 BGM 时自动跑一次，标记峰值时间点。

服务于 Compose 页 BGM 轨道的 peak 参考线——让用户拖 BGM bar 时
有"曲子高潮在这里"的视觉锚点，方便对齐视频高潮。

实现复用 services/video/audio_analysis.py 的 librosa 流程，
本模块只负责从 onset + RMS 能量挑出最有效的单点 peak。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .audio_analysis import analyze_audio, backend_name

log = logging.getLogger("seecript.video.bgm_analysis")


@dataclass
class BGMEnergyProfile:
    duration_seconds: float
    peak_seconds: Optional[float]   # 单点 peak（综合 onset+RMS），无法判定时 None
    tempo_bpm: float
    backend: str                    # "librosa" / "mock"


def analyze_bgm(audio_path: str | Path) -> BGMEnergyProfile:
    """读取 BGM 文件，返回时长 + peak_seconds + tempo。

    peak 选取策略：
    1. RMS 能量曲线滑动平均后取最大值时间——能量驻峰（drop / 副歌起点）
    2. 兜底：取整曲 1/2 时间点
    """
    profile = analyze_audio(audio_path)
    duration = float(profile.duration_seconds or 0.0)
    backend = backend_name()
    if duration <= 0.0:
        return BGMEnergyProfile(0.0, None, profile.tempo_bpm, backend)

    rms = list(profile.rms_energy or [])
    times = list(profile.times or [])
    peak: Optional[float] = None
    if rms and times and len(rms) == len(times):
        # 滑窗平均压噪，避免单帧峰值（hi-hat 击打）误判
        window = max(3, len(rms) // 50)
        smoothed: list[float] = []
        for i in range(len(rms)):
            lo = max(0, i - window // 2)
            hi = min(len(rms), i + window // 2 + 1)
            seg = rms[lo:hi]
            smoothed.append(sum(seg) / max(1, len(seg)))
        max_idx = max(range(len(smoothed)), key=lambda i: smoothed[i])
        peak = float(times[max_idx])
    elif rms:
        # times 缺失：按比例反推
        max_idx = max(range(len(rms)), key=lambda i: rms[i])
        peak = duration * (max_idx / max(1, len(rms) - 1))
    else:
        peak = duration / 2.0

    log.info(
        "[bgm_analysis] %s | dur=%.2fs | peak=%.2fs | bpm=%.1f | backend=%s",
        Path(audio_path).name, duration, peak or -1.0, profile.tempo_bpm, backend,
    )
    return BGMEnergyProfile(
        duration_seconds=duration,
        peak_seconds=peak,
        tempo_bpm=float(profile.tempo_bpm or 0.0),
        backend=backend,
    )
