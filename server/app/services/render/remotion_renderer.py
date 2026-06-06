"""server/app/services/render/remotion_renderer.py

Remotion CLI 调用包装：把 AI 生图渲染成带动效的 mp4 视频片段。

设计原则：
- 进程外调用 `npx remotion render`，不依赖任何 node 绑定，CLI 错误透传。
- 输入是已落盘的图片路径 + AnimationSpec dict；输出是 mp4 文件路径。
- 失败时返回 None，调用方应回落到 ffmpeg image_to_video 静帧路径。
- 渲染期间临时把图片 copy 到 remotion/public（staticFile() 仅识别此目录）；输出回原路径。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("seecript.remotion")

# 仓库根 → remotion 项目根（git checkout 默认结构）。
# 在生产 /opt/seecript 下也是同样的相对位置（server 与 remotion 同级）。
_DEFAULT_REPO_ROOT = Path(__file__).resolve().parents[4]


def _remotion_root() -> Path:
    env = os.environ.get("SEECRIPT_REMOTION_ROOT")
    if env:
        p = Path(env).resolve()
        if p.exists():
            return p
    return _DEFAULT_REPO_ROOT / "remotion"


def remotion_available() -> bool:
    """检测 remotion 项目是否就位（node_modules 已装 + index.tsx 存在）。

    没装 npm 依赖时直接返回 False，调用方会回落到 ffmpeg image_to_video 静帧渲染。
    """
    root = _remotion_root()
    if not (root / "package.json").is_file():
        return False
    if not (root / "node_modules" / "remotion").is_dir():
        return False
    if not (root / "src" / "AnimatedImage.tsx").is_file():
        return False
    return True


def _ratio_to_size(ratio: str | None) -> tuple[int, int]:
    """画面比例 → 输出像素尺寸。统一锁 720p 长边（与 Seedance 配置同源）。"""
    r = (ratio or "9:16").strip()
    if r in ("16:9", "horizontal"):
        return 1280, 720
    if r in ("1:1", "square"):
        return 720, 720
    return 720, 1280  # 默认 9:16 竖屏


def _resolve_npx() -> list[str]:
    """跨平台 npx 调用 prefix。Windows 下走 npx.cmd；Linux/macOS 直接 npx。"""
    if os.name == "nt":
        # Win 上 npx 是 .cmd；交给 shell=True 处理路径解析
        return ["npx.cmd"]
    return ["npx"]


async def render_animated_image(
    *,
    image_paths: list[Path],
    duration_seconds: float,
    output_path: Path,
    animation_type: str = "ken-burns",
    ratio: str = "9:16",
    intensity: float = 0.3,
    motion_direction: str = "in",
    transition: str = "cross-fade",
    transition_duration: float = 0.4,
    timeout_seconds: float = 180.0,
) -> Optional[Path]:
    """渲染 AnimatedImage composition 输出 mp4。

    image_paths：本地已落盘的图片路径（须存在）。多张时按顺序映射 storyboard / keyframe_morph。
    渲染期：把图片复制到 remotion/public/seecript_temp/<run-id>/，用 staticFile 协议引用。

    返回 None → 渲染失败；调用方回落 ffmpeg。
    """
    if not remotion_available():
        log.warning("[remotion] not available; skip render_animated_image")
        return None
    if not image_paths:
        return None
    valid_paths = [p for p in image_paths if p.exists()]
    if not valid_paths:
        log.warning("[remotion] no valid image paths in %s", image_paths)
        return None

    root = _remotion_root()
    public_dir = root / "public" / "seecript_temp"
    public_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_path.resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # 用进程级随机 run-id 隔离，避免并发覆盖。完成后清理。
    import uuid as _uuid
    run_id = _uuid.uuid4().hex[:10]
    run_dir = public_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    try:
        rel_urls: list[str] = []
        for i, p in enumerate(valid_paths):
            dst = run_dir / f"img-{i:02d}{p.suffix.lower() or '.png'}"
            shutil.copy2(p, dst)
            # staticFile 路径相对 remotion/public/
            rel_urls.append(f"seecript_temp/{run_id}/{dst.name}")

        w, h = _ratio_to_size(ratio)
        props = {
            "engine": "remotion",
            "image_urls": rel_urls,
            "animation_type": animation_type,
            "duration_seconds": float(duration_seconds),
            "motion_direction": motion_direction,
            "intensity": float(intensity),
            "transition": transition,
            "transition_duration": float(transition_duration),
        }
        props_path = run_dir / "props.json"
        props_path.write_text(json.dumps(props, ensure_ascii=False), encoding="utf-8")

        cmd = _resolve_npx() + [
            "remotion", "render",
            "AnimatedImage",
            str(output_path),
            "--codec=h264",
            "--pixel-format=yuv420p",
            f"--props={props_path.as_posix()}",
            f"--width={w}",
            f"--height={h}",
            "--overwrite",
            "--log=warn",
        ]
        log.info("[remotion] render %s → %s (%dx%d, %s)", animation_type, output_path.name, w, h, ratio)

        loop = asyncio.get_event_loop()

        def _run() -> tuple[int, str, str]:
            proc = subprocess.run(
                cmd,
                cwd=str(root),
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                shell=(os.name == "nt"),  # Win 下让 cmd 处理 npx.cmd 解析
            )
            return proc.returncode, proc.stdout, proc.stderr

        try:
            code, stdout, stderr = await loop.run_in_executor(None, _run)
        except subprocess.TimeoutExpired:
            log.warning("[remotion] render timeout (%ss)", timeout_seconds)
            return None
        except FileNotFoundError as exc:
            log.warning("[remotion] npx not found: %s", exc)
            return None

        if code != 0:
            log.warning("[remotion] render failed code=%d stderr=%s", code, (stderr or "")[:500])
            return None
        if not output_path.exists() or output_path.stat().st_size == 0:
            log.warning("[remotion] render produced empty file: %s", output_path)
            return None
        log.info("[remotion] ok %s (%d bytes)", output_path.name, output_path.stat().st_size)
        return output_path
    finally:
        # 清理 staticFile 临时目录
        try:
            shutil.rmtree(run_dir, ignore_errors=True)
        except Exception:  # noqa: BLE001
            pass
