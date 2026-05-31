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
    """落盘 .wav，返回相对 URL。"""
    dst = voice_path(plan_id, scene_id)
    dst.write_bytes(data)
    log.info("[voice.store] saved plan=%s scene=%s size=%d", plan_id, scene_id, len(data))
    return voice_url(plan_id, scene_id)


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
    """`/voiceovers/<plan>/<scene>.wav` → 本地 Path；非 voiceovers URL 返 None。"""
    url = (url or "").strip()
    if not url.startswith("/voiceovers/"):
        return None
    rel = url.removeprefix("/voiceovers/").strip("/")
    if not rel:
        return None
    candidate = _voiceovers_root() / rel
    return candidate if candidate.exists() else None
