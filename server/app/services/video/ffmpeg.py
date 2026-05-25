"""FFmpeg subprocess 包装。

依赖：系统 ffmpeg / ffprobe；未找到时函数会抛 FileNotFoundError（不做 mock，因为没有 ffmpeg
比赛 demo 跑不起来）。

公开函数：
- ffmpeg_available() / ffmpeg_version()
- probe(path)                          → dict {duration, width, height, fps, has_audio}
- extract_frame(video, time, dst)      → 抽指定时间点的关键帧 jpg
- concat(inputs, dst)                  → demuxer concat（要求同编码同分辨率）
- overlay(base, overlay, dst, opts)    → 主轨叠加包装轨
- mix_bgm(video, bgm, dst, volume)     → 主轨 + BGM 音轨混音
"""
from __future__ import annotations

import json
import logging
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("seecript.video.ffmpeg")


class FFmpegError(RuntimeError):
    pass


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def ffmpeg_version() -> str:
    if not ffmpeg_available():
        return "missing"
    out = subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True, check=False)
    line = (out.stdout or "").splitlines()[0] if out.stdout else "unknown"
    return line


@dataclass
class ProbeResult:
    duration_seconds: float
    width: int
    height: int
    fps: float
    has_audio: bool


def probe(path: str | Path) -> ProbeResult:
    if not ffmpeg_available():
        raise FFmpegError("ffprobe not found in PATH")
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(p)
    cmd = [
        "ffprobe", "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", str(p),
    ]
    out = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if out.returncode != 0:
        raise FFmpegError(f"ffprobe failed: {out.stderr.strip()}")
    info = json.loads(out.stdout)
    duration = float(info.get("format", {}).get("duration", 0.0))
    width = height = 0
    fps = 0.0
    has_audio = False
    for s in info.get("streams", []):
        if s.get("codec_type") == "video":
            width = int(s.get("width", 0))
            height = int(s.get("height", 0))
            # avg_frame_rate "30000/1001" → 29.97
            rate = s.get("avg_frame_rate", "0/1")
            num, _, den = rate.partition("/")
            try:
                fps = float(num) / float(den) if float(den) else 0.0
            except (TypeError, ValueError):
                fps = 0.0
        elif s.get("codec_type") == "audio":
            has_audio = True
    return ProbeResult(duration_seconds=duration, width=width, height=height, fps=fps, has_audio=has_audio)


def extract_frame(video_path: str | Path, time_seconds: float, dst: str | Path) -> Path:
    if not ffmpeg_available():
        raise FFmpegError("ffmpeg not found in PATH")
    src = Path(video_path)
    out = Path(dst)
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-ss", f"{time_seconds:.3f}", "-i", str(src),
        "-frames:v", "1", "-q:v", "2", str(out),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise FFmpegError(f"extract_frame failed: {proc.stderr.strip()}")
    return out


def concat(inputs: list[str | Path], dst: str | Path, *, reencode: bool = False) -> Path:
    """concat demuxer。reencode=True 时用 -c copy（要求同编码），否则统一转 H.264 + AAC。"""
    if not ffmpeg_available():
        raise FFmpegError("ffmpeg not found in PATH")
    if not inputs:
        raise ValueError("concat: empty inputs")
    out = Path(dst)
    out.parent.mkdir(parents=True, exist_ok=True)
    list_file = out.with_suffix(".list.txt")
    list_file.write_text(
        "\n".join(f"file '{Path(p).resolve().as_posix()}'" for p in inputs),
        encoding="utf-8",
    )
    if reencode:
        cmd = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-f", "concat", "-safe", "0", "-i", str(list_file),
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "22",
            "-c:a", "aac", "-b:a", "128k",
            str(out),
        ]
    else:
        cmd = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-f", "concat", "-safe", "0", "-i", str(list_file),
            "-c", "copy", str(out),
        ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    list_file.unlink(missing_ok=True)
    if proc.returncode != 0:
        raise FFmpegError(f"concat failed: {proc.stderr.strip()}")
    return out


def overlay(
    base_path: str | Path,
    overlay_path: str | Path,
    dst: str | Path,
    *,
    position: str = "0:0",
) -> Path:
    """主轨 + 透明 overlay 合成。overlay 应为带 alpha 通道的 WebM/MOV。"""
    if not ffmpeg_available():
        raise FFmpegError("ffmpeg not found in PATH")
    out = Path(dst)
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(base_path), "-i", str(overlay_path),
        "-filter_complex", f"[0:v][1:v]overlay={position}:format=auto",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "22",
        "-c:a", "copy",
        str(out),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise FFmpegError(f"overlay failed: {proc.stderr.strip()}")
    return out


def mix_bgm(
    video_path: str | Path,
    bgm_path: str | Path,
    dst: str | Path,
    *,
    bgm_volume: float = 0.6,
) -> Path:
    if not ffmpeg_available():
        raise FFmpegError("ffmpeg not found in PATH")
    out = Path(dst)
    out.parent.mkdir(parents=True, exist_ok=True)
    # 原音轨保留 0dB，BGM 按 bgm_volume 衰减；最短输入决定输出长度
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(video_path), "-i", str(bgm_path),
        "-filter_complex",
        f"[1:a]volume={bgm_volume}[bgm];[0:a][bgm]amix=inputs=2:duration=shortest:dropout_transition=2[aout]",
        "-map", "0:v", "-map", "[aout]",
        "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
        str(out),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise FFmpegError(f"mix_bgm failed: {proc.stderr.strip()}")
    return out
