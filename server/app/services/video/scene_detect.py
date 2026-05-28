"""PySceneDetect 镜头切分。

依赖：scenedetect[opencv] —— 重，未安装时回落 mock（按等分时长切）。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("seecript.video.scene_detect")

try:
    from scenedetect import SceneManager, open_video
    from scenedetect.detectors import ContentDetector
    _BACKEND = "pyscenedetect"
except ImportError:  # pragma: no cover
    _BACKEND = "mock"


@dataclass
class DetectedShot:
    index: int
    start: float
    end: float
    duration: float


def detect_shots(video_path: str | Path, threshold: float = 27.0) -> list[DetectedShot]:
    """切镜头。threshold 越小越敏感（默认 27 是 PySceneDetect 推荐值）。

    Returns 镜头列表，按时间升序。Mock 模式按 3 秒一段平均切。
    """
    path = Path(video_path) if video_path else None
    if _BACKEND == "mock" or path is None or not path.is_file():
        log.warning("[scene_detect] backend=mock (path=%r)", str(path) if path else None)
        # 默认 30 秒视频切成 10 段
        return [
            DetectedShot(index=i, start=i * 3.0, end=(i + 1) * 3.0, duration=3.0)
            for i in range(10)
        ]

    video = open_video(str(path))
    sm = SceneManager()
    sm.add_detector(ContentDetector(threshold=threshold))
    sm.detect_scenes(video=video, show_progress=False)
    scene_list = sm.get_scene_list()
    shots: list[DetectedShot] = []
    for i, (start, end) in enumerate(scene_list):
        s = start.get_seconds()
        e = end.get_seconds()
        shots.append(DetectedShot(index=i, start=s, end=e, duration=e - s))
    log.info("[scene_detect] %s → %d shots", path.name, len(shots))
    return shots


def backend_name() -> str:
    return _BACKEND
