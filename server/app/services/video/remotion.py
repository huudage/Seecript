"""Remotion 子进程包装 —— 把包装轨（字幕/标题条/转场/贴纸）渲染成透明 WebM。

工作机理：
  1. 写一份 props.json 到 remotion/ 项目目录
  2. 子进程跑 `npx remotion render PackagingTrack out.webm --props=props.json --pixel-format=yuva420p`
  3. 拿到 out.webm 后交给 video/ffmpeg.overlay 与主轨叠加

未安装 remotion/node_modules 时函数抛 FileNotFoundError；mock 模式由调用方决定怎么降级。
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from pathlib import Path

log = logging.getLogger("seecript.video.remotion")


class RemotionError(RuntimeError):
    pass


def _remotion_dir() -> Path:
    # server/app/services/video/remotion.py → 仓库根 → remotion/
    return Path(__file__).resolve().parents[4] / "remotion"


def remotion_available() -> bool:
    rd = _remotion_dir()
    return (rd / "node_modules").exists() and shutil.which("npx") is not None


def render_packaging_track(
    props: dict,
    dst: str | Path,
    *,
    composition: str = "PackagingTrack",
    pixel_format: str = "yuva420p",
) -> Path:
    """同步渲染。props 是 PackagingTrack 组件需要的数据（packaging_track + duration）。

    pixel_format=yuva420p 是透明 WebM 默认编码，与 ffmpeg overlay 配合。
    """
    if not remotion_available():
        raise FileNotFoundError(
            f"remotion not installed at {_remotion_dir()}; run `cd remotion && npm install`"
        )
    rd = _remotion_dir()
    out = Path(dst).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    props_path = out.with_suffix(".props.json")
    props_path.write_text(json.dumps(props, ensure_ascii=False), encoding="utf-8")

    cmd = [
        "npx", "remotion", "render", composition, str(out),
        "--props", str(props_path),
        "--pixel-format", pixel_format,
        "--codec", "vp8",
        "--quiet",
    ]
    log.info("[remotion] %s → %s", composition, out.name)
    proc = subprocess.run(
        cmd, cwd=str(rd), capture_output=True, text=True, check=False,
        env={**os.environ, "CI": "1"},
    )
    props_path.unlink(missing_ok=True)
    if proc.returncode != 0:
        raise RemotionError(f"remotion render failed: {proc.stderr.strip()[:500]}")
    return out
