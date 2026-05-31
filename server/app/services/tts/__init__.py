"""TTS package — Seecript 口播合成层。

provider 选择：
- mock          无 Key 时回落，用 numpy 合成"被节奏调制的正弦波" wav，能听出节拍但听不到字
- volc          火山引擎 TTS（独立鉴权 APP_ID + ACCESS_TOKEN）

对外 API：
- synthesize(text, voice, sample_rate) -> bytes(wav)
- backend_name() -> "mock" | "volc"
"""
from .client import TTSError, backend_name, synthesize
from .scene_voice import synthesize_scene_voice, synthesize_with_alignment

__all__ = [
    "TTSError", "backend_name", "synthesize",
    "synthesize_scene_voice", "synthesize_with_alignment",
]
