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


def extract_audio_mp3(
    video_path: str | Path,
    dst: str | Path,
    *,
    sample_rate: int = 44100,
    bitrate: str = "128k",
) -> Path:
    """抽视频音轨成 mp3。doubao 多模态音频理解只接公网拉的小体积音频（mp3/m4a 优）。

    - 单声道 + 128k 比特率：3min 视频 ≈ 3MB，远低于 LLM 单请求 10MB 限制
    - 失败抛 FFmpegError，上层 try/except 当作"无 LLM 分析"降级即可
    """
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
        "-codec:a", "libmp3lame", "-b:a", bitrate,
        str(out),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise FFmpegError(f"extract_audio_mp3 failed: {proc.stderr.strip()}")
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


# xfade 滤镜支持的样式 → 内部 TransitionStyle 的映射。
# hard_cut 映射到极短 fade（0.01s）以保持 filter graph 一致；
# whip 用 smoothleft 模拟（横向甩入），wipe 用 wipeleft，slide 用 slideleft，zoom 用 zoomin。
_TRANSITION_STYLE_TO_FFMPEG: dict[str, str] = {
    "hard_cut": "fade",
    "dissolve": "fade",
    "slide": "slideleft",
    "zoom": "zoomin",
    "whip": "smoothleft",
    "wipe": "wipeleft",
}


def concat_with_transitions(
    inputs: list[str | Path],
    transitions: list[dict | None],
    dst: str | Path,
    *,
    canvas: tuple[int, int] | None = None,
    fps: int = 30,
) -> Path:
    """xfade 滤镜拼接：相邻两段按 transitions[i] 给的 style+duration overlap 衔接。

    - `transitions` 长度必须等于 `inputs`；transitions[0] 必须为 None（首段没有入场转场）
    - 每个 transition dict 形如 {"style": "dissolve", "duration": 0.4}
    - hard_cut / None → 走极短 fade（0.01s），保持 filter graph 一致避免分支
    - 全部为 hard_cut / None → 退化为既有 concat(reencode=True)，省一次 filter_complex 编译
    - 所有 inputs **必须**已统一到相同的 canvas / fps / 像素格式；否则 xfade 会报错。
      pipeline 的 _trim_segment / _normalize_to_canvas 已保证这一点，本函数只兜底
      在 canvas 参数给出时再 scale+pad+fps 一次。

    返回：dst 路径。
    """
    if not ffmpeg_available():
        raise FFmpegError("ffmpeg not found in PATH")
    if not inputs:
        raise ValueError("concat_with_transitions: empty inputs")
    if len(transitions) != len(inputs):
        raise ValueError(
            f"concat_with_transitions: transitions len {len(transitions)} != inputs len {len(inputs)}"
        )
    if transitions and transitions[0] is not None:
        log.warning("[ffmpeg] concat_with_transitions: transitions[0] should be None; ignoring")
        transitions = [None] + list(transitions[1:])

    # 全 None / hard_cut → 退化到 concat
    def _is_real(t: dict | None) -> bool:
        if t is None:
            return False
        style = (t.get("style") or "hard_cut").strip()
        return style != "hard_cut" and style in _TRANSITION_STYLE_TO_FFMPEG

    if not any(_is_real(t) for t in transitions):
        return concat(inputs, dst, reencode=True)

    if len(inputs) == 1:
        # 单段不需要转场；直接转 reencode 拷一份
        return concat(inputs, dst, reencode=True)

    out = Path(dst)
    out.parent.mkdir(parents=True, exist_ok=True)

    # 各段时长：用 probe 拿真实秒数（xfade offset 必须精确，否则视频末尾会黑场）
    durations: list[float] = []
    for p in inputs:
        try:
            d = probe(p).duration_seconds
        except Exception as exc:  # noqa: BLE001
            raise FFmpegError(f"concat_with_transitions probe {p} failed: {exc}")
        if d <= 0:
            raise FFmpegError(f"concat_with_transitions: input {p} has zero duration")
        durations.append(float(d))

    # 预处理 filter：每段先 scale+pad 到 canvas（若给定）+ fps + setpts/asetpts 重置时基
    pre_video: list[str] = []
    pre_audio: list[str] = []
    canvas_w, canvas_h = canvas if canvas else (0, 0)
    for i in range(len(inputs)):
        v_chain = []
        if canvas and canvas_w > 0 and canvas_h > 0:
            v_chain.append(
                f"scale={canvas_w}:{canvas_h}:force_original_aspect_ratio=decrease,"
                f"pad={canvas_w}:{canvas_h}:(ow-iw)/2:(oh-ih)/2:black"
            )
        v_chain.append(f"fps={fps}")
        v_chain.append("format=yuv420p")
        v_chain.append("setpts=PTS-STARTPTS")
        pre_video.append(f"[{i}:v]" + ",".join(v_chain) + f"[v{i}]")
        pre_audio.append(f"[{i}:a]asetpts=PTS-STARTPTS,aresample=async=1[a{i}]")

    # 链式 xfade / acrossfade：v0+v1→v01；v01+v2→v02；…
    chain_video: list[str] = []
    chain_audio: list[str] = []
    prev_v = "v0"
    prev_a = "a0"
    cumulative_offset = durations[0]
    for i in range(1, len(inputs)):
        tr = transitions[i] or {}
        style_raw = (tr.get("style") or "hard_cut").strip()
        ff_style = _TRANSITION_STYLE_TO_FFMPEG.get(style_raw, "fade")
        if style_raw == "hard_cut":
            d = 0.01
        else:
            d = max(0.05, min(1.5, float(tr.get("duration") or 0.4)))
        # offset：xfade 在 prev 链当前时长 - d 时开始与下一段 overlap
        offset = max(0.0, cumulative_offset - d)
        new_v = f"vx{i}"
        new_a = f"ax{i}"
        chain_video.append(
            f"[{prev_v}][v{i}]xfade=transition={ff_style}:duration={d:.3f}:offset={offset:.3f}[{new_v}]"
        )
        chain_audio.append(
            f"[{prev_a}][a{i}]acrossfade=d={d:.3f}:c1=tri:c2=tri[{new_a}]"
        )
        prev_v = new_v
        prev_a = new_a
        cumulative_offset = cumulative_offset + durations[i] - d

    filter_complex = ";".join(pre_video + pre_audio + chain_video + chain_audio)
    cmd: list[str] = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]
    for p in inputs:
        cmd += ["-i", str(p)]
    cmd += [
        "-filter_complex", filter_complex,
        "-map", f"[{prev_v}]", "-map", f"[{prev_a}]",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "22",
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart",
        str(out),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise FFmpegError(
            f"concat_with_transitions failed: {proc.stderr.strip()[:800]}"
        )
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


# frame.md typography_display → 本机字体文件路径。
# packaging_agent 给的 typography_display 通常是 'Bebas Neue' / 'Lato' / '思源黑体' 等英文/中文家族名，
# Linux 服务器上不一定装。这里只兜底中文家族名 → 已知 CJK 路径；找不到时返回 None，
# burn_packaging_track 会再走 find_cjk_font() 通用兜底。命中就拿到带『黑体感』『宋体感』的差别字。
_TYPO_FAMILY_TO_FONT: dict[str, tuple[str, ...]] = {
    "黑体": (r"C:\Windows\Fonts\simhei.ttf",
             r"/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
             r"/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc"),
    "微软雅黑": (r"C:\Windows\Fonts\msyh.ttc",
                  r"/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
    "雅黑": (r"C:\Windows\Fonts\msyh.ttc",
             r"/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
    "宋体": (r"C:\Windows\Fonts\simsun.ttc",
             r"/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc"),
    "思源黑体": (r"/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
                  r"C:\Windows\Fonts\msyh.ttc"),
    "思源宋体": (r"/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc",
                  r"C:\Windows\Fonts\simsun.ttc"),
    "PingFang": (r"/System/Library/Fonts/PingFang.ttc",
                 r"C:\Windows\Fonts\msyh.ttc"),
    "Bebas Neue": (r"/usr/share/fonts/truetype/Bebas/BebasNeue-Regular.ttf",),
    "Lato": (r"/usr/share/fonts/truetype/lato/Lato-Regular.ttf",),
    "JetBrains Mono": (r"/usr/share/fonts/truetype/jetbrains-mono/JetBrainsMono-Regular.ttf",
                       r"C:\Windows\Fonts\Consola.ttf"),
}


def resolve_typography_font(family: str | None) -> str | None:
    """把 frame.md typography_display 家族名翻成本机字体文件路径。

    服务器上没有对应字体文件时返回 None，让 caller 走 find_cjk_font() 通用兜底。
    匹配策略：先按家族名整体精确查表，命中后返回第一个存在的文件；否则按前缀部分匹配
    （避免 LLM 给『微软雅黑 Bold』这类带后缀的版本时漏掉）。
    """
    if not family:
        return None
    name = family.strip()
    if not name:
        return None
    # 整体精确
    if name in _TYPO_FAMILY_TO_FONT:
        for candidate in _TYPO_FAMILY_TO_FONT[name]:
            if Path(candidate).is_file():
                return candidate
    # 前缀部分匹配（『微软雅黑 Bold』包含『微软雅黑』）
    for key, candidates in _TYPO_FAMILY_TO_FONT.items():
        if key in name:
            for candidate in candidates:
                if Path(candidate).is_file():
                    return candidate
    return None


def apply_frame_styling(
    src: str | Path,
    dst: str | Path,
    *,
    grain: bool = False,
    vignette: bool = False,
    grain_strength: int = 12,
    vignette_angle: float = 0.4,
) -> Path:
    """把 frame.md 的 grain_overlay / vignette 真烧到滤镜链。

    - grain：`noise=alls=N:allf=t` 加胶片颗粒；t=temporal，每帧噪点不同避免静态网纹。
      strength 12 在 1080p 上刚好能感受到颗粒感又不糊画面；超过 20 像 90 年代 VHS。
    - vignette：`vignette=angle=PI*X` 暗角；angle 越小越深。0.4 ≈ PI/2.5 是『电影感』
      档位，明显但不抢主体。

    都为 False 时直接拷贝（不重编码也不报错；caller 不必预判）。

    重编码用 libx264 + crf 22，与 trim/concat 链路一致；下游 mix_voiceovers/mix_bgm
    都是 -c:v copy，所以本步是滤镜唯一注入点，再往后视频流不变。
    """
    if not ffmpeg_available():
        raise FFmpegError("ffmpeg not found in PATH")
    src_p = Path(src)
    if not src_p.exists():
        raise FileNotFoundError(src_p)
    out = Path(dst)
    out.parent.mkdir(parents=True, exist_ok=True)

    filters: list[str] = []
    if grain:
        s = max(1, min(40, int(grain_strength)))
        filters.append(f"noise=alls={s}:allf=t")
    if vignette:
        a = max(0.1, min(1.4, float(vignette_angle)))
        filters.append(f"vignette=angle=PI*{a:.3f}")

    if not filters:
        # 都没开 → 直接拷贝（不烧任何滤镜）
        out.write_bytes(src_p.read_bytes())
        return out

    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(src_p),
        "-vf", ",".join(filters),
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "22",
        "-pix_fmt", "yuv420p",
        "-c:a", "copy",
        str(out),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise FFmpegError(f"apply_frame_styling failed: {proc.stderr.strip()[:500]}")
    return out


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


# 字幕样式映射：与 schemas.PackagingPreferences.subtitle_* 对齐。
# burn_packaging_track 渲 kind="subtitle" 时按 item.style 中的对应字段查这两张表。
_SUBTITLE_FONT_SIZE_PX = {"small": 36, "medium": 48, "large": 64}
_SUBTITLE_POSITION_Y = {
    # 表达式直接拼到 drawtext y= 字段；w/h 是画面宽高，text_w/text_h 是文字本身宽高。
    "top": "h*0.10",
    "middle": "(h-text_h)/2",
    "bottom": "h-text_h-160",
}

# title_bar：V2 写到 item.style 的 font_size / position / color / background_color
_TITLE_BAR_FONT_SIZE_PX = {"small": 44, "medium": 56, "large": 72}
_TITLE_BAR_POSITION_Y = {
    "top": "h*0.08",
    "middle": "(h-text_h)/2",
}

# sticker：四种锚点 → 位置表达式
_STICKER_POS_XY = {
    "bottom-center": ("(w-text_w)/2", "h-text_h-340"),
    "top-right":     ("w-text_w-60",  "h*0.08"),
    "bottom-right":  ("w-text_w-60",  "h-text_h-340"),
    "middle":        ("(w-text_w)/2", "(h-text_h)/2"),
}

# cover：layout → (title_x, title_y, sub_x, sub_y)
_COVER_LAYOUT_XY = {
    "center":  ("(w-text_w)/2", "(h-text_h)/2-60", "(w-text_w)/2", "(h-text_h)/2+80"),
    "left":    ("w*0.08",       "(h-text_h)/2-60", "w*0.08",       "(h-text_h)/2+80"),
    "split":   ("(w-text_w)/2", "h*0.18",          "(w-text_w)/2", "h-text_h-260"),
    "stacked": ("(w-text_w)/2", "h*0.32",          "(w-text_w)/2", "h*0.32+90"),
}


def _hex_for_drawtext(hex_str: str | None, fallback: str) -> str:
    """`#RRGGBB` → `0xRRGGBB`；非法值回退 fallback（也按 0xRRGGBB 形式给）。"""
    if isinstance(hex_str, str):
        s = hex_str.strip()
        if s.startswith("#") and len(s) == 7:
            try:
                int(s[1:], 16)
                return "0x" + s[1:].upper()
            except ValueError:
                pass
        if s.startswith("0x") and len(s) == 8:
            try:
                int(s[2:], 16)
                return s.upper().replace("0X", "0x")
            except ValueError:
                pass
    return fallback


def _subtitle_filters(
    text: str,
    esc: str,
    start: float,
    end: float,
    style: dict,
    fontfile_arg: str,
) -> list[str]:
    """构造 subtitle drawtext 滤镜（含字号/位置/底色/双语换行）。

    style 字段：
    - font_size: small/medium/large（默认 medium=48；老 item.style.size 数字也兼容）
    - position : top/middle/bottom（默认 bottom）
    - background: none/shadow/gradient（默认 shadow）
    - bilingual: bool；True 时 text 里若含『\n』分两行渲染（drawtext 原生支持换行符）
    """
    size_key = str(style.get("font_size", "medium")).lower()
    fontsize = _SUBTITLE_FONT_SIZE_PX.get(size_key)
    if fontsize is None:
        # 兼容老 item.style.size = 数字
        try:
            fontsize = int(style.get("size", 48))
        except (TypeError, ValueError):
            fontsize = 48

    pos_key = str(style.get("position", "bottom")).lower()
    y_expr = _SUBTITLE_POSITION_Y.get(pos_key, _SUBTITLE_POSITION_Y["bottom"])

    bg_key = str(style.get("background", "shadow")).lower()
    if bg_key == "none":
        # 描边代替底色（borderw + bordercolor）
        bg_arg = ":borderw=4:bordercolor=black@0.85"
    elif bg_key == "gradient":
        # 厚 box + 高不透明度近似渐变底；ffmpeg drawtext 没有真渐变，蹭加大 borderw 做层次
        bg_arg = ":box=1:boxcolor=black@0.45:boxborderw=32:borderw=2:bordercolor=black@0.6"
    else:  # shadow（默认）
        bg_arg = ":box=1:boxcolor=black@0.55:boxborderw=18"

    enable = f"between(t\\,{start:.3f}\\,{end:.3f})"
    # bilingual 通过文本里的 \n 实现两行；drawtext 原生支持 line break
    base = (
        f"drawtext=text='{esc}':fontcolor=white:fontsize={fontsize}"
        f"{bg_arg}:"
        f"x=(w-text_w)/2:y={y_expr}:"
        f"enable='{enable}'{fontfile_arg}"
    )
    return [base]


def burn_packaging_track(
    base_path: str | Path,
    items: list[dict],
    dst: str | Path,
    *,
    font_path: str | None = None,
) -> Path:
    """把 packaging_track 用 drawtext / drawbox 烧到主轨视频上。

    Remotion 不可用时的 fallback——避免文案/标题/贴纸/封面只在前端时间线显示、
    最终视频里看不到。

    支持的 kind:
    - subtitle  : 底部居中字幕（黑底白字 box）
    - title_bar : 顶部标题条（深底白字）
    - sticker   : 中下方贴纸（黑底黄字 CTA）
    - cover     : 屏幕中央大字（仅在 start..end 显示，通常是开场 1-1.5s）

    注意：老版本 kind='transition' 是包装层假转场（白闪 drawbox），已废弃；
    真转场走 Scene.transition_in + concat_with_transitions（xfade 滤镜）。
    本函数遇到 kind='transition' 仅 log skip，不再绘制白闪。
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
            log.warning(
                "[ffmpeg] legacy transition packaging item skipped (id=%s); "
                "use Scene.transition_in for real xfade",
                it.get("item_id"),
            )
            continue
        if not text:
            continue
        esc = _escape_drawtext_text(text)
        style = it.get("style") or {}
        if not isinstance(style, dict):
            style = {}
        if kind == "subtitle":
            filters.extend(
                _subtitle_filters(text, esc, start, end, style, fontfile_arg)
            )
        elif kind == "title_bar":
            size_key = str(style.get("font_size", "medium")).lower()
            fontsize = _TITLE_BAR_FONT_SIZE_PX.get(size_key, 56)
            pos_key = str(style.get("position", "top")).lower()
            y_expr = _TITLE_BAR_POSITION_Y.get(pos_key, _TITLE_BAR_POSITION_Y["top"])
            color = _hex_for_drawtext(style.get("color"), "0xFFFFFF")
            bg_color = _hex_for_drawtext(style.get("background_color"), "0x14181F")
            filters.append(
                f"drawtext=text='{esc}':fontcolor={color}:fontsize={fontsize}:"
                f"box=1:boxcolor={bg_color}@0.85:boxborderw=22:"
                f"x=(w-text_w)/2:y={y_expr}:"
                f"enable='{enable}'{fontfile_arg}"
            )
        elif kind == "sticker":
            color = _hex_for_drawtext(style.get("color"), "0xFFE600")
            bg_color = _hex_for_drawtext(style.get("background_color"), "0x000000")
            pos_key = str(style.get("position", "bottom-center")).lower()
            x_expr, y_expr = _STICKER_POS_XY.get(pos_key, _STICKER_POS_XY["bottom-center"])
            filters.append(
                f"drawtext=text='{esc}':fontcolor={color}:fontsize=72:"
                f"box=1:boxcolor={bg_color}@0.7:boxborderw=20:"
                f"x={x_expr}:y={y_expr}:"
                f"enable='{enable}'{fontfile_arg}"
            )
        elif kind == "cover":
            # palette: [title_color, bg_color, subtitle_color]（hex）
            palette_raw = style.get("palette")
            palette = palette_raw if isinstance(palette_raw, list) else []
            title_color = _hex_for_drawtext(
                palette[0] if len(palette) > 0 else None, "0xFFE600",
            )
            bg_color = _hex_for_drawtext(
                palette[1] if len(palette) > 1 else None, "0x14181F",
            )
            sub_color = _hex_for_drawtext(
                palette[2] if len(palette) > 2 else None, "0xFFFFFF",
            )
            layout_key = str(style.get("layout", "center")).lower()
            t_x, t_y, s_x, s_y = _COVER_LAYOUT_XY.get(
                layout_key, _COVER_LAYOUT_XY["center"],
            )
            # 先铺底色 drawbox（整屏不透明矩形），再叠 title / subtitle
            filters.append(
                f"drawbox=x=0:y=0:w=iw:h=ih:color={bg_color}@1.0:t=fill:"
                f"enable='{enable}'"
            )
            filters.append(
                f"drawtext=text='{esc}':fontcolor={title_color}:fontsize=110:"
                f"x={t_x}:y={t_y}:"
                f"enable='{enable}'{fontfile_arg}"
            )
            sub = style.get("subtitle")
            if isinstance(sub, str) and sub.strip():
                esc_sub = _escape_drawtext_text(sub.strip())
                filters.append(
                    f"drawtext=text='{esc_sub}':fontcolor={sub_color}:fontsize=52:"
                    f"x={s_x}:y={s_y}:"
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


def change_speed(
    src: str | Path,
    dst: str | Path,
    *,
    target_duration: float,
    sample_rate: int = 44100,
) -> Path:
    """变速到 target_duration（保持总时长一致；视频用 setpts，音频用 atempo）。

    用于 user_material 段落实际时长 ≠ scene.duration 时，做轻量节奏拉伸/压缩——
    避免简单 trim 切掉关键动作，或 freeze 让画面发愣。

    - ratio = target / actual：
        - ratio > 1（target 比原始长）→ 放慢，setpts=PTS*ratio，atempo=1/ratio
        - ratio < 1（target 比原始短）→ 加快，setpts=PTS*ratio，atempo=1/ratio
    - atempo 单次只能 [0.5, 2.0]，超出范围连续叠加 atempo 滤镜（如 0.4 = 0.5*0.8）
    - 极端比例（>4× 或 <0.25×）画面会糊或抖；调用方先做范围保护
    """
    if not ffmpeg_available():
        raise FFmpegError("ffmpeg not found in PATH")
    src_p = Path(src)
    if not src_p.exists():
        raise FileNotFoundError(src_p)
    out = Path(dst)
    out.parent.mkdir(parents=True, exist_ok=True)

    info = probe(src_p)
    actual = float(info.duration_seconds or 0.0)
    if actual <= 0.05:
        raise FFmpegError(f"change_speed: src duration too small ({actual}s)")
    target = float(target_duration)
    if target <= 0.05:
        raise FFmpegError(f"change_speed: target_duration too small ({target}s)")

    ratio = target / actual          # 视频 setpts 倍率：>1 慢，<1 快
    atempo_inv = 1.0 / ratio         # 音频反向：>1 快，<1 慢

    # atempo 单次范围 [0.5, 2.0]，叠加多次直到把 atempo_inv 拆完
    atempo_chain: list[float] = []
    remaining = atempo_inv
    if remaining <= 0:
        atempo_chain.append(1.0)
    else:
        while remaining > 2.0:
            atempo_chain.append(2.0)
            remaining /= 2.0
        while remaining < 0.5:
            atempo_chain.append(0.5)
            remaining /= 0.5
        atempo_chain.append(remaining)
    af = ",".join(f"atempo={v:.4f}" for v in atempo_chain)

    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(src_p),
        "-vf", f"setpts={ratio:.4f}*PTS",
    ]
    if info.has_audio:
        cmd += ["-af", af]
    else:
        cmd += [
            "-f", "lavfi", "-t", f"{target:.3f}",
            "-i", f"anullsrc=channel_layout=stereo:sample_rate={sample_rate}",
            "-shortest",
        ]
    cmd += [
        "-t", f"{target:.3f}",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "22",
        "-c:a", "aac", "-b:a", "128k",
        "-pix_fmt", "yuv420p",
        str(out),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise FFmpegError(f"change_speed failed: {proc.stderr.strip()}")
    return out


def extend_freeze_tail(
    src: str | Path,
    dst: str | Path,
    *,
    target_duration: float,
    sample_rate: int = 44100,
) -> Path:
    """L4: 把 src 的尾帧冻结延长到 target_duration（秒）。

    用 tpad=stop_mode=clone:stop_duration=Δ 让最后一帧持续 Δ 秒；音频则用 apad
    在尾部填静音同步。仅在实际时长 < target_duration 时使用，否则用 trim 截短即可。

    若 src 没有音频流，apad 会失败——这里通过 -af 加 anullsrc 兜底；不过实测多数 AIGC
    视频自带轨，这里直接 apad 足够。
    """
    if not ffmpeg_available():
        raise FFmpegError("ffmpeg not found in PATH")
    src_p = Path(src)
    if not src_p.exists():
        raise FileNotFoundError(src_p)
    out = Path(dst)
    out.parent.mkdir(parents=True, exist_ok=True)

    info = probe(src_p)
    actual = float(info.duration_seconds or 0.0)
    delta = float(target_duration) - actual
    if delta <= 0.05:
        # 已经够长 / 几乎相等：直接拷贝
        out.write_bytes(src_p.read_bytes())
        return out

    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(src_p),
        "-vf", f"tpad=stop_mode=clone:stop_duration={delta:.3f}",
    ]
    if info.has_audio:
        cmd += ["-af", f"apad=pad_dur={delta:.3f}"]
    else:
        # 无音频：合成一段静音音轨
        cmd += [
            "-f", "lavfi", "-t", f"{target_duration:.3f}",
            "-i", f"anullsrc=channel_layout=stereo:sample_rate={sample_rate}",
            "-shortest",
        ]
    cmd += [
        "-t", f"{target_duration:.3f}",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "22",
        "-c:a", "aac", "-b:a", "128k",
        "-pix_fmt", "yuv420p",
        str(out),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise FFmpegError(f"extend_freeze_tail failed: {proc.stderr.strip()}")
    return out


def image_to_video(
    image: str | Path,
    duration: float,
    dst: str | Path,
    *,
    width: int = 1080,
    height: int = 1920,
    fps: int = 30,
    sample_rate: int = 44100,
) -> Path:
    """把一张静态图 loop 成给定时长的 mp4（静帧 + 静音）。

    用途：source=aigc_image 的 scene——Seedream 出图本质是单帧，定时填到主轨需要先包成 mp4。
    与 color_clip 同样带静音音轨，concat reencode 时音视频流一致。

    缩放策略：scale + pad 居中，保持原图比例避免拉伸；超出画布的部分被裁/留黑边。
    """
    if not ffmpeg_available():
        raise FFmpegError("ffmpeg not found in PATH")
    src = Path(image)
    if not src.exists():
        raise FFmpegError(f"image_to_video: source not found: {src}")
    out = Path(dst)
    out.parent.mkdir(parents=True, exist_ok=True)
    dur = max(0.5, float(duration))
    vf = (
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=0x14181F,"
        f"setsar=1,format=yuv420p,fps={fps}"
    )
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-loop", "1", "-t", f"{dur:.3f}", "-i", str(src),
        "-f", "lavfi", "-i", f"anullsrc=channel_layout=stereo:sample_rate={sample_rate}",
        "-t", f"{dur:.3f}",
        "-vf", vf,
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "22",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k",
        "-shortest",
        str(out),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise FFmpegError(f"image_to_video failed: {proc.stderr.strip()}")
    return out


# 字卡画面用：把 TextCardSpec 字段映射到 ffmpeg drawtext + fade。font_family 没有 4 套
# 真字体文件，只能用 CJK 单字体 + 字号/字重/边框模拟差别。
_TEXT_CARD_FONT_STYLE = {
    "bold_sans":     {"main_size_factor": 1.0, "main_borderw": 6, "sub_size_factor": 0.45, "sub_borderw": 2},
    "serif_classic": {"main_size_factor": 0.95, "main_borderw": 3, "sub_size_factor": 0.40, "sub_borderw": 1},
    "handwriting":   {"main_size_factor": 0.92, "main_borderw": 2, "sub_size_factor": 0.42, "sub_borderw": 1},
    "tech_mono":     {"main_size_factor": 0.88, "main_borderw": 4, "sub_size_factor": 0.40, "sub_borderw": 1},
}

# 布局 → (main_y, sub_y) 表达式
_TEXT_CARD_LAYOUT_Y = {
    "center":           ("(h-text_h)/2-text_h*0.6", "(h-text_h)/2+text_h*1.4"),
    "top":              ("h*0.18", "h*0.18+text_h*1.4"),
    "bottom":           ("h*0.62", "h*0.62+text_h*1.4"),
    "split_top_bottom": ("h*0.25", "h*0.65"),
}


def _drawtext_alpha_expr(animation: str, dur: float) -> str:
    """每种动画的 alpha 表达式（0-1）。

    - fade_in: 0→1 在 0.6s 内
    - typewriter: 阶梯，每 0.08s 一档；caller 不再单独切字符（粗略近似为快速渐入 + 长稳定）
    - bounce_word: 短暂 0.4s 渐入然后稳定
    - zoom_pop: 0.3s 内快速冲到 1
    """
    if animation == "fade_in":
        return "if(lt(t,0.6),t/0.6,1)"
    if animation == "typewriter":
        return "if(lt(t,0.4),t/0.4,1)"
    if animation == "bounce_word":
        return "if(lt(t,0.35),t/0.35,1)"
    if animation == "zoom_pop":
        return "if(lt(t,0.25),t/0.25,1)"
    return "1"


def _drawtext_y_offset(animation: str, base_y: str) -> str:
    """bounce_word 给 y 加微小正弦偏移；其它动画 y 不变。"""
    if animation == "bounce_word":
        # 头 0.6 秒衰减振荡：sin(2π*t*3)*15 * exp(-2*t)
        return f"({base_y})+if(lt(t,0.7),sin(2*PI*t*3)*15*(1-t/0.7),0)"
    return base_y


def text_card_clip(
    spec_dict: dict,
    dst: str | Path,
    *,
    width: int = 1080,
    height: int = 1920,
    fps: int = 30,
    sample_rate: int = 44100,
) -> Path:
    """按 TextCardSpec 渲染纯字卡 mp4。

    spec_dict 是 TextCardSpec.model_dump()——这一层不 import schemas 避免循环。

    实现策略：
    1. 用 color filter 或 gradient filter 出底背景
    2. drawtext 渲染 main_text（+ optional sub_text）
    3. fontcolor / fontsize / borderw / 位置 / alpha 全部按 spec 字段映射
    4. emoji_decor 用一个额外 drawtext 在底部居中拼字符串
    5. 输出含静音音轨，与 color_clip 一致
    """
    if not ffmpeg_available():
        raise FFmpegError("ffmpeg not found in PATH")
    out = Path(dst)
    out.parent.mkdir(parents=True, exist_ok=True)

    main_text = (spec_dict.get("main_text") or "").strip()[:24]
    sub_text = (spec_dict.get("sub_text") or "").strip()[:40]
    duration = max(0.5, float(spec_dict.get("duration_seconds") or 4.0))
    bg_mode = spec_dict.get("bg_mode") or "solid"
    bg_color = spec_dict.get("bg_color") or "#0F172A"
    text_color = spec_dict.get("text_color") or "#FFFFFF"
    accent_color = spec_dict.get("accent_color") or "#22D3EE"
    font_family = spec_dict.get("font_family") or "bold_sans"
    layout = spec_dict.get("layout") or "center"
    animation = spec_dict.get("animation") or "fade_in"
    emoji_decor = spec_dict.get("emoji_decor") or []

    font_style = _TEXT_CARD_FONT_STYLE.get(font_family, _TEXT_CARD_FONT_STYLE["bold_sans"])
    # 字号按竖屏 1080×1920 基准；横屏 1920×1080 自动按 min(w,h) 缩
    base_size = min(width, height)
    try:
        size_pct = float(spec_dict.get("font_size_pct") or 1.0)
    except (TypeError, ValueError):
        size_pct = 1.0
    size_pct = max(0.6, min(1.6, size_pct))
    main_size = int(base_size * 0.12 * font_style["main_size_factor"] * size_pct)
    sub_size = int(base_size * 0.12 * font_style["sub_size_factor"] * size_pct)

    main_y_expr, sub_y_expr = _TEXT_CARD_LAYOUT_Y.get(layout, _TEXT_CARD_LAYOUT_Y["center"])

    # 背景 input 表达式
    bg_hex_for_ffmpeg = bg_color.replace("#", "0x")
    accent_hex_for_ffmpeg = accent_color.replace("#", "0x")
    text_hex_for_ffmpeg = text_color.replace("#", "0x")

    if bg_mode == "gradient":
        # ffmpeg gradients filter（lavfi 源）；c0=bg, c1=accent 渐变
        bg_input = (
            f"gradients=size={width}x{height}:duration={duration:.3f}"
            f":c0={bg_hex_for_ffmpeg}:c1={accent_hex_for_ffmpeg}:speed=0.01:rate={fps}"
        )
    elif bg_mode == "dark_overlay":
        # 仍是纯色但更深一档（lavfi color 不支持简单叠半透明黑），直接拿 bg_color
        # 让前端理解 dark_overlay 含义即可
        bg_input = f"color=c={bg_hex_for_ffmpeg}:s={width}x{height}:r={fps}:d={duration:.3f}"
    elif bg_mode == "image_blur":
        # 没有上段尾帧的输入，回落纯色（pipeline 上游可选传 fallback；此处简化）
        bg_input = f"color=c={bg_hex_for_ffmpeg}:s={width}x{height}:r={fps}:d={duration:.3f}"
    else:  # solid
        bg_input = f"color=c={bg_hex_for_ffmpeg}:s={width}x{height}:r={fps}:d={duration:.3f}"

    font = find_cjk_font()
    fontfile_arg = _fontfile_arg(font)

    # main drawtext
    main_alpha_expr = _drawtext_alpha_expr(animation, duration)
    main_y = _drawtext_y_offset(animation, main_y_expr)
    main_esc = _escape_drawtext_text(main_text or " ")
    main_drawtext = (
        f"drawtext=text='{main_esc}':fontcolor={text_hex_for_ffmpeg}"
        f":fontsize={main_size}:borderw={font_style['main_borderw']}"
        f":bordercolor=black@0.55"
        f":x=(w-text_w)/2:y={main_y}"
        f":alpha='{main_alpha_expr}'"
        f"{fontfile_arg}"
    )

    filters: list[str] = [main_drawtext]

    if sub_text:
        sub_alpha_expr = _drawtext_alpha_expr(animation, duration)
        # 副标 alpha 比主标延后 0.2s
        sub_alpha = f"if(lt(t,0.2),0,{sub_alpha_expr})"
        sub_esc = _escape_drawtext_text(sub_text)
        sub_drawtext = (
            f"drawtext=text='{sub_esc}':fontcolor={accent_hex_for_ffmpeg}"
            f":fontsize={sub_size}:borderw={font_style['sub_borderw']}"
            f":bordercolor=black@0.45"
            f":x=(w-text_w)/2:y={sub_y_expr}"
            f":alpha='{sub_alpha}'"
            f"{fontfile_arg}"
        )
        filters.append(sub_drawtext)

    if emoji_decor:
        emoji_text = " ".join(emoji_decor)[:24]
        emoji_esc = _escape_drawtext_text(emoji_text)
        emoji_drawtext = (
            f"drawtext=text='{emoji_esc}':fontcolor={accent_hex_for_ffmpeg}"
            f":fontsize={int(main_size*0.4)}"
            f":x=(w-text_w)/2:y=h*0.88"
            f":alpha='if(lt(t,0.3),0,if(lt(t,0.7),(t-0.3)/0.4,1))'"
            f"{fontfile_arg}"
        )
        filters.append(emoji_drawtext)

    filter_chain = ",".join(filters)

    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", bg_input,
        "-f", "lavfi", "-i", f"anullsrc=channel_layout=stereo:sample_rate={sample_rate}",
        "-vf", filter_chain,
        "-t", f"{duration:.3f}",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "22",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k",
        "-shortest",
        str(out),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise FFmpegError(f"text_card_clip failed: {proc.stderr.strip()[:500]}")
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

