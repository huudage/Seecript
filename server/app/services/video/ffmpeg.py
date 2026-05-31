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


# winget 装的 ffmpeg 在用户 PATH 里，但子进程（uvicorn / python -m）很容易没继承到。
# 启动时主动探测一下常见安装位置，找到就 prepend 到 os.environ["PATH"]，subprocess 都受益。
def _bootstrap_ffmpeg_path() -> None:
    import os
    if shutil.which("ffmpeg") and shutil.which("ffprobe"):
        return
    candidates = [
        r"C:\Users\admin\AppData\Local\Microsoft\WinGet\Packages\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe\ffmpeg-8.1.1-full_build\bin",
        r"C:\Program Files\ffmpeg\bin",
        r"C:\ffmpeg\bin",
        r"/usr/local/bin",
        r"/opt/homebrew/bin",
    ]
    for c in candidates:
        if Path(c, "ffmpeg.exe").is_file() or Path(c, "ffmpeg").is_file():
            os.environ["PATH"] = c + os.pathsep + os.environ.get("PATH", "")
            log.info("ffmpeg located at %s, prepended to PATH", c)
            return


_bootstrap_ffmpeg_path()


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


def extract_audio_wav(video_path: str | Path, dst: str | Path, *, sample_rate: int = 22050) -> Path:
    """抽视频音轨成单声道 PCM wav，librosa/soundfile 才能正常读 mp4 的音频。"""
    if not ffmpeg_available():
        raise FFmpegError("ffmpeg not found in PATH")
    src = Path(video_path)
    if not src.exists():
        raise FileNotFoundError(src)
    out = Path(dst)
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(src), "-vn",
        "-ac", "1", "-ar", str(sample_rate),
        "-c:a", "pcm_s16le", str(out),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise FFmpegError(f"extract_audio_wav failed: {proc.stderr.strip()}")
    return out


def trim(
    src: str | Path,
    dst: str | Path,
    *,
    start: float,
    duration: float,
    reencode: bool = True,
    canvas: tuple[int, int] | None = None,
) -> Path:
    """按 [start, start+duration] 切片视频。

    - reencode=True：用 libx264 重编码，scene 拼接时所有切片用统一参数，concat 才不会色彩/分辨率错乱
    - reencode=False：-c copy 流复制（更快，但要求所有输入参数一致）
    - canvas=(W, H)：附加 scale+pad+setsar 把输出对齐到画布；仅 reencode=True 时生效
    """
    if not ffmpeg_available():
        raise FFmpegError("ffmpeg not found in PATH")
    src_p = Path(src)
    if not src_p.exists():
        raise FileNotFoundError(src_p)
    out = Path(dst)
    out.parent.mkdir(parents=True, exist_ok=True)

    if reencode:
        cmd = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-ss", f"{start:.3f}", "-i", str(src_p),
            "-t", f"{duration:.3f}",
        ]
        if canvas is not None:
            w, h = canvas
            cmd += ["-vf", _canvas_filter(w, h)]
        cmd += [
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "22",
            "-c:a", "aac", "-b:a", "128k",
            "-pix_fmt", "yuv420p",
            str(out),
        ]
    else:
        cmd = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-ss", f"{start:.3f}", "-i", str(src_p),
            "-t", f"{duration:.3f}",
            "-c", "copy",
            str(out),
        ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise FFmpegError(f"trim failed: {proc.stderr.strip()}")
    return out


def _canvas_filter(width: int, height: int) -> str:
    """生成 scale+pad+setsar 滤镜串：保持原宽高比，多余区域 letterbox 黑边填充。

    例：1080×1920 画布上放 16:9 素材 → 上下黑边；放 1:1 素材 → 左右黑边。
    """
    return (
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,"
        "setsar=1"
    )


def normalize_canvas(
    src: str | Path,
    dst: str | Path,
    *,
    width: int,
    height: int,
) -> Path:
    """整段视频统一到 (width, height) 画布。

    用于 aigc 段：Seedance 返回的 ratio 与 plan 设定理论上一致，但实际偶发
    略偏分辨率（如 1088×1920 而非 1080×1920），concat 会拒绝；统一过一遍画布。
    """
    if not ffmpeg_available():
        raise FFmpegError("ffmpeg not found in PATH")
    src_p = Path(src)
    if not src_p.exists():
        raise FileNotFoundError(src_p)
    out = Path(dst)
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(src_p),
        "-vf", _canvas_filter(width, height),
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "22",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k",
        str(out),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise FFmpegError(f"normalize_canvas failed: {proc.stderr.strip()}")
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
    bgm_volume: float = 0.35,
    fade_in: float = 1.5,
    fade_out: float = 2.0,
    bgm_skip_seconds: float = 0.0,
    video_delay_seconds: float = 0.0,
    duck_with_voice: bool = True,
    duck_attenuation_db: float = -9.0,
    video_has_voice: bool = True,
) -> Path:
    """生产级 BGM 混音：BGM 内部裁切 + 视频时间轴延迟入场 + fade in/out + 可选 sidechain ducking + stream_loop。

    锚点语义（v2 替代旧 start_offset）：
    - `bgm_skip_seconds` ≥ 0：丢弃 BGM 开头 N 秒（让曲子 hook 提前对齐视频高潮）
    - `video_delay_seconds` ≥ 0：BGM 在视频时间线上延迟 N 秒入场（视频前段保持原声）
    二者由 BGMConfig.video_anchor_seconds 派生：anchor≥0→video_delay；anchor<0→bgm_skip=-anchor。

    设计点：
    - `-stream_loop -1`：BGM 比视频短自动循环，比视频长会被裁
    - `atrim=start={bgm_skip}`：跳过 BGM 开头那段（曲子 hook 对齐视频高潮）
    - `adelay`：BGM 在视频时间线上整体延迟入场（视频前段保留原声）
    - `afade=t=in/out`：淡入淡出真实生效
    - `sidechaincompress`：有口播时 BGM 受口播触发自动衰减
    - `-c:v copy`：视频流零再编码，混音只重做音频流（10x 速度）
    """
    if not ffmpeg_available():
        raise FFmpegError("ffmpeg not found in PATH")
    out = Path(dst)
    out.parent.mkdir(parents=True, exist_ok=True)

    # 视频时长决定 fade_out 起点
    try:
        video_info = probe(video_path)
        duration = video_info.duration_seconds
    except (FFmpegError, FileNotFoundError):
        duration = 30.0

    fade_in = max(0.0, fade_in)
    fade_out = max(0.0, fade_out)
    bgm_skip_seconds = max(0.0, bgm_skip_seconds)
    video_delay_seconds = max(0.0, video_delay_seconds)
    # BGM 在视频时间线上的活跃区间：[video_delay, duration]
    bgm_active = max(0.0, duration - video_delay_seconds)
    fade_out_start = max(0.0, bgm_active - fade_out)

    # BGM 预处理 chain：
    # 1. atrim 跳过 BGM 开头 → 2. asetpts 重置 PTS → 3. volume → 4. fade in (基于活跃起点)
    # 5. fade out (基于活跃终点) → 6. adelay 在视频时间线上整体延迟
    bgm_filter_parts = [
        f"atrim=start={bgm_skip_seconds:.3f}:end={bgm_skip_seconds + bgm_active:.3f}",
        "asetpts=PTS-STARTPTS",
        f"volume={bgm_volume:.3f}",
        f"afade=t=in:st=0:d={fade_in:.3f}",
        f"afade=t=out:st={fade_out_start:.3f}:d={fade_out:.3f}",
    ]
    if video_delay_seconds > 0.0:
        delay_ms = int(round(video_delay_seconds * 1000))
        # adelay 单通道也要给一个值；用 `all=1` 让左右声道同步延迟
        bgm_filter_parts.append(f"adelay={delay_ms}|{delay_ms}:all=1")
    bgm_filter = ",".join(bgm_filter_parts)

    if duck_with_voice and video_has_voice:
        filter_complex = (
            f"[1:a]{bgm_filter}[bgm_pre];"
            f"[bgm_pre][0:a]sidechaincompress="
            f"threshold=0.05:ratio=8:attack=20:release=400:makeup=1[bgm_ducked];"
            f"[0:a][bgm_ducked]amix=inputs=2:duration=first:dropout_transition=2[aout]"
        )
    else:
        filter_complex = (
            f"[1:a]{bgm_filter}[bgm];"
            f"[0:a][bgm]amix=inputs=2:duration=first:dropout_transition=2[aout]"
        )

    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(video_path),
        "-stream_loop", "-1", "-i", str(bgm_path),
        "-filter_complex", filter_complex,
        "-map", "0:v", "-map", "[aout]",
        "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-ar", "44100",
        "-shortest",
        str(out),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise FFmpegError(f"mix_bgm failed: {proc.stderr.strip()[:500]}")
    return out


def mix_voiceovers(
    video_path: str | Path,
    clips: list[tuple[str | Path, float]],
    dst: str | Path,
    *,
    voice_gain_db: float = 2.0,
    duck_existing_audio_db: float = -12.0,
) -> Path:
    """把 N 段 TTS clip 按各自起始时间叠加到视频的原音轨上。

    clips：[(wav_path, start_seconds), ...]，start_seconds 是该段在视频时间线上的起点。
    实现：
    - 每个 clip 用 `adelay` 延迟到对应起始时间
    - 原音轨整体用 `volume=-12dB` 压低（避免和口播打架；voice off 时 caller 别调本函数）
    - 全部 amix 合并

    用途：copy fill + TTS 之后，pipeline 在 overlay 之前先把口播烧到视频音轨。
    """
    if not ffmpeg_available():
        raise FFmpegError("ffmpeg not found in PATH")
    out = Path(dst)
    out.parent.mkdir(parents=True, exist_ok=True)

    valid_clips = [(Path(p), float(t)) for p, t in clips if Path(p).exists() and Path(p).stat().st_size > 0]
    if not valid_clips:
        # 无有效 clip，直接复制
        out.write_bytes(Path(video_path).read_bytes())
        return out

    inputs: list[str] = ["-i", str(video_path)]
    for p, _ in valid_clips:
        inputs += ["-i", str(p)]

    # 原音轨压低
    voice_gain_linear = 10 ** (voice_gain_db / 20.0)
    duck_db_linear = 10 ** (duck_existing_audio_db / 20.0)
    voice_chains: list[str] = [f"[0:a]volume={duck_db_linear:.4f}[base]"]
    mix_labels: list[str] = ["[base]"]
    for idx, (_, start_s) in enumerate(valid_clips):
        in_idx = idx + 1
        delay_ms = max(0, int(round(start_s * 1000)))
        voice_chains.append(
            f"[{in_idx}:a]adelay={delay_ms}|{delay_ms}:all=1,"
            f"volume={voice_gain_linear:.4f}[v{idx}]"
        )
        mix_labels.append(f"[v{idx}]")

    filter_complex = (
        ";".join(voice_chains)
        + ";" + "".join(mix_labels)
        + f"amix=inputs={len(mix_labels)}:duration=first:dropout_transition=0:normalize=0[aout]"
    )

    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        *inputs,
        "-filter_complex", filter_complex,
        "-map", "0:v", "-map", "[aout]",
        "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-ar", "44100",
        "-shortest",
        str(out),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise FFmpegError(f"mix_voiceovers failed: {proc.stderr.strip()[:500]}")
    return out


_CJK_FONT_CANDIDATES = (
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\msyhbd.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
    r"C:\Windows\Fonts\simsun.ttc",
    r"/System/Library/Fonts/PingFang.ttc",
    r"/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    r"/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
)


def find_cjk_font() -> str | None:
    """挑一个能渲染 CJK 的本机字体；找不到时返回 None（drawtext 会退到默认字体，
    中文会变方块——caller 拿到 None 可以决定是否回落到不烧字幕）。"""
    for c in _CJK_FONT_CANDIDATES:
        if Path(c).is_file():
            return c
    return None


def _escape_drawtext_text(text: str) -> str:
    """drawtext text= 字段内的特殊字符转义。
    转义顺序很关键：先反斜杠再其它，否则二次转义把单引号吃掉。"""
    return (
        text.replace("\\", "\\\\")
        .replace(":", "\\:")
        .replace("'", "\\'")
        .replace("%", "\\%")
    )


def _fontfile_arg(font_path: str | None) -> str:
    """ffmpeg 滤镜里 fontfile 的 Windows 路径要：反斜杠→正斜杠 + 冒号转义。"""
    if not font_path:
        return ""
    p = font_path.replace("\\", "/").replace(":", "\\:")
    return f":fontfile='{p}'"


def burn_packaging_track(
    base_path: str | Path,
    items: list[dict],
    dst: str | Path,
    *,
    font_path: str | None = None,
) -> Path:
    """把 packaging_track 用 drawtext / drawbox 烧到主轨视频上。

    Remotion 不可用时的 fallback——避免文案/标题/贴纸/封面/转场只在前端时间线显示、
    最终视频里看不到。

    支持的 kind:
    - subtitle  : 底部居中字幕（黑底白字 box）
    - title_bar : 顶部标题条（深底白字）
    - sticker   : 中下方贴纸（黑底黄字 CTA）
    - cover     : 屏幕中央大字（仅在 start..end 显示，通常是开场 1-1.5s）
    - transition: 全屏白色闪烁，模拟 dissolve
    """
    if not ffmpeg_available():
        raise FFmpegError("ffmpeg not found in PATH")
    base = Path(base_path)
    if not base.exists():
        raise FileNotFoundError(base)
    out = Path(dst)
    out.parent.mkdir(parents=True, exist_ok=True)

    font = font_path if font_path else find_cjk_font()
    fontfile_arg = _fontfile_arg(font)

    filters: list[str] = []
    for it in items:
        kind = it.get("kind")
        try:
            start = float(it.get("start", 0.0))
            end = float(it.get("end", 0.0))
        except (TypeError, ValueError):
            continue
        if end <= start:
            continue
        text = (it.get("text") or "").strip()
        enable = f"between(t\\,{start:.3f}\\,{end:.3f})"
        if kind == "transition":
            filters.append(
                f"drawbox=x=0:y=0:w=iw:h=ih:color=white@0.45:t=fill:enable='{enable}'"
            )
            continue
        if not text:
            continue
        esc = _escape_drawtext_text(text)
        if kind == "subtitle":
            filters.append(
                f"drawtext=text='{esc}':fontcolor=white:fontsize=56:"
                f"box=1:boxcolor=black@0.55:boxborderw=18:"
                f"x=(w-text_w)/2:y=h-text_h-160:"
                f"enable='{enable}'{fontfile_arg}"
            )
        elif kind == "title_bar":
            filters.append(
                f"drawtext=text='{esc}':fontcolor=white:fontsize=64:"
                f"box=1:boxcolor=0x14181F@0.85:boxborderw=22:"
                f"x=(w-text_w)/2:y=120:"
                f"enable='{enable}'{fontfile_arg}"
            )
        elif kind == "sticker":
            filters.append(
                f"drawtext=text='{esc}':fontcolor=0xFFE600:fontsize=72:"
                f"box=1:boxcolor=black@0.6:boxborderw=22:"
                f"x=(w-text_w)/2:y=h-text_h-340:"
                f"enable='{enable}'{fontfile_arg}"
            )
        elif kind == "cover":
            filters.append(
                f"drawtext=text='{esc}':fontcolor=0xFFE600:fontsize=110:"
                f"x=(w-text_w)/2:y=(h-text_h)/2-60:"
                f"enable='{enable}'{fontfile_arg}"
            )
            style = it.get("style") or {}
            sub = style.get("subtitle") if isinstance(style, dict) else None
            if isinstance(sub, str) and sub.strip():
                esc_sub = _escape_drawtext_text(sub.strip())
                filters.append(
                    f"drawtext=text='{esc_sub}':fontcolor=white:fontsize=52:"
                    f"x=(w-text_w)/2:y=(h-text_h)/2+80:"
                    f"enable='{enable}'{fontfile_arg}"
                )

    if not filters:
        # 没东西可烧——直接复制让 pipeline 继续
        out.write_bytes(base.read_bytes())
        return out

    vf = ",".join(filters)
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(base),
        "-vf", vf,
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "22",
        "-pix_fmt", "yuv420p",
        "-c:a", "copy",
        str(out),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise FFmpegError(f"burn_packaging_track failed: {proc.stderr.strip()[:500]}")
    return out


def color_clip(
    duration: float,
    dst: str | Path,
    *,
    width: int = 1080,
    height: int = 1920,
    fps: int = 30,
    color: str = "0x14181F",
    sample_rate: int = 44100,
) -> Path:
    """生成一段「纯色背景 + 静音音轨」的 mp4。

    用途：copy-only / 未解析 scene 的文字卡底片。由 packaging 字幕在上层叠真实文案，
    底片只负责持续时间和分辨率对齐，让 concat 不掉段。

    带静音音轨是为了 concat reencode 时音视频流数一致——否则 lavfi 纯视频源和
    带 AAC 的 trim 切片混在一起，concat demuxer 会丢音轨/对不齐时间戳。
    """
    if not ffmpeg_available():
        raise FFmpegError("ffmpeg not found in PATH")
    out = Path(dst)
    out.parent.mkdir(parents=True, exist_ok=True)
    dur = max(0.5, float(duration))
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", f"color=c={color}:s={width}x{height}:r={fps}:d={dur:.3f}",
        "-f", "lavfi", "-i", f"anullsrc=channel_layout=stereo:sample_rate={sample_rate}",
        "-t", f"{dur:.3f}",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "22",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k",
        "-shortest",
        str(out),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise FFmpegError(f"color_clip failed: {proc.stderr.strip()}")
    return out


def extract_uniform_frames(
    video_path: str | Path,
    dst_dir: str | Path,
    *,
    count: int = 8,
    prefix: str = "frame",
) -> list[Path]:
    """从视频均匀抽取 N 帧到 dst_dir。用于 reference_video 资产的多模态参考。

    返回生成的 jpg 路径列表（按时间顺序）。视频太短或抽帧失败时返回已成功的部分。
    """
    if not ffmpeg_available():
        raise FFmpegError("ffmpeg not found in PATH")
    src = Path(video_path)
    if not src.exists():
        raise FileNotFoundError(src)
    out_dir = Path(dst_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    try:
        info = probe(src)
        duration = max(info.duration_seconds, 0.5)
    except FFmpegError as exc:
        raise FFmpegError(f"extract_uniform_frames probe failed: {exc}") from exc

    count = max(1, min(count, 24))
    # 均匀采样：避开首末 5%，落在内容主体
    if count == 1:
        timestamps = [duration / 2]
    else:
        pad = duration * 0.05
        usable = duration - 2 * pad
        step = usable / (count - 1)
        timestamps = [pad + i * step for i in range(count)]

    results: list[Path] = []
    for idx, t in enumerate(timestamps):
        dst_path = out_dir / f"{prefix}-{idx:02d}.jpg"
        try:
            extract_frame(src, t, dst_path)
            results.append(dst_path)
        except FFmpegError as exc:
            log.warning("[ffmpeg] extract_uniform_frames idx=%d t=%.2f failed: %s", idx, t, exc)
    return results

