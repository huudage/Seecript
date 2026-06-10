"""Voice store —— TTS 合成产物的本地落盘 + URL 生成。

布局：
  server/var/voiceovers/<plan_id>/<scene_id>.wav

URL 暴露：FastAPI 在 main.py 挂 `/voiceovers/` -> server/var/voiceovers/。
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from ...config import get_settings

log = logging.getLogger("seecript.tts.store")


def _voiceovers_root() -> Path:
    settings = get_settings()
    root = settings.log_dir.parent / "var" / "voiceovers"
    root.mkdir(parents=True, exist_ok=True)
    return root


def voice_path(plan_id: str, scene_id: str) -> Path:
    plan_dir = _voiceovers_root() / plan_id
    plan_dir.mkdir(parents=True, exist_ok=True)
    return plan_dir / f"{scene_id}.wav"


def voice_url(plan_id: str, scene_id: str) -> str:
    return f"/voiceovers/{plan_id}/{scene_id}.wav"


def save_wav(plan_id: str, scene_id: str, data: bytes) -> str:
    """落盘 .wav，返回相对 URL（带 cache-buster）。

    URL 形如 `/voiceovers/<plan>/<scene>.wav?v=<mtime_ms>`。
    cache-buster 让前端 <audio src> 在改文案重新合成后真的换掉旧音频——
    文件名一直是 `<scene>.wav`，浏览器/proxy 会按 URL 缓存，没 query 就听不到新版。
    """
    dst = voice_path(plan_id, scene_id)
    dst.write_bytes(data)
    log.info("[voice.store] saved plan=%s scene=%s size=%d", plan_id, scene_id, len(data))
    import time as _time
    bust = int(_time.time() * 1000)
    return f"{voice_url(plan_id, scene_id)}?v={bust}"


def delete(plan_id: str, scene_id: str) -> bool:
    dst = voice_path(plan_id, scene_id)
    if dst.exists():
        try:
            dst.unlink()
            log.info("[voice.store] deleted plan=%s scene=%s", plan_id, scene_id)
            return True
        except OSError as exc:
            log.warning("[voice.store] delete failed plan=%s scene=%s: %s", plan_id, scene_id, exc)
    return False


def url_to_local_path(url: str) -> Optional[Path]:
    """`/voiceovers/<plan>/<scene>.wav[?v=<ts>]` → 本地 Path；非 voiceovers URL 返 None。

    URL 可能带 cache-buster query（save_wav 加的 `?v=<ms>`），剥掉后再拼路径。
    """
    url = (url or "").strip()
    if not url.startswith("/voiceovers/"):
        return None
    # 剥 cache-buster query
    url = url.split("?", 1)[0]
    rel = url.removeprefix("/voiceovers/").strip("/")
    if not rel:
        return None
    candidate = _voiceovers_root() / rel
    return candidate if candidate.exists() else None
