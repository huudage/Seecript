"""BGM 分析：librosa 拿时长/兜底 peak + doubao-seed 多模态拿结构 / 情绪 / 视频匹配建议。

两层职责严格分开：
- `analyze_bgm`            librosa 单遍——上传时同步跑，落 duration / tempo / peak。失败不影响 plan。
- `analyze_bgm_with_llm`   doubao-seed 多模态音频理解——plan 绑定 BGM 时跑，带 brief/video_goal 给出
                            曲风猜测、情绪 tag、4-6 段结构 + 视频节奏建议。失败回 None，前端兜底用 librosa。

LLM 拿不到本地文件，只能拿公网 URL（同 ASR：靠 settings.public_audio_base_url 把 /assets/... 拼公网）。
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import httpx

from ...config import get_settings
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


# ---------------------------------------------------------------------------
# LLM 多模态：doubao-seed 音频理解
# ---------------------------------------------------------------------------
_BGM_LLM_SYSTEM = """你是短视频 BGM 顾问。根据传入的一段背景音乐，结合 brief / video_goal，
判断曲子的曲风、情绪、能量起伏，给出『哪里激昂哪里舒缓』和『怎么和视频节奏对齐』的建议。

严格返回 JSON，不要 markdown，不要解释：
{
  "title_guess": "<曲风/曲目猜测，例：流行电子 / 鼓点节拍 / 钢琴抒情>",
  "mood_tags": ["3-6 个情绪 tag，如 燃 / 紧张 / 治愈 / 大气"],
  "theme_fit_score": 0.0-1.0,
  "theme_fit_reason": "<≤120 字，曲子与 brief 是否契合，为什么>",
  "structure": [
    {"start": 0.0, "end": 8.0, "energy": "low|mid|high",
     "label": "≤16 字段名（例：前奏铺垫 / 副歌爆发）",
     "fit_with_video": "≤40 字，建议对齐到视频哪段（opening/development/climax/closing）"}
  ],
  "overall_advice": "<≤140 字，一段总体节奏建议：曲子的高潮和低谷分别适合放在视频的哪些段落>"
}

规则：
- structure 给 4-6 段，首段 start=0，末段 end ≈ 曲子总时长（duration_seconds 见 user 提示）
- energy 必须是 low/mid/high 三选一；不允许中文
- mood_tags 至少 3 个，最多 6 个
- 不要编造曲名/版权信息，title_guess 用风格描述就好
"""


def _public_audio_url(file_url: str) -> Optional[str]:
    """把 asset.file_url（如 /assets/proj/bgm/xxx.mp3）拼成公网 URL，给 LLM 拉。

    缺失 PUBLIC_AUDIO_BASE_URL 时返 None——上层应跳过 LLM 分析（这是开发兜底，生产必配）。
    """
    settings = get_settings()
    base = (settings.public_audio_base_url or "").rstrip("/")
    if not base:
        return None
    rel = file_url if file_url.startswith("/") else f"/{file_url}"
    return f"{base}{rel}"


def _audio_format_from_url(url: str) -> str:
    suffix = Path(url.split("?", 1)[0]).suffix.lower().lstrip(".")
    if suffix in {"mp3", "wav", "m4a", "aac", "ogg", "flac"}:
        return suffix
    return "mp3"  # doubao 默认接 mp3


async def analyze_bgm_with_llm(
    *,
    file_url: str,
    duration_seconds: float,
    brief: str,
    video_goal: str,
) -> Optional[dict[str, Any]]:
    """调 doubao-seed-2.0-lite 多模态音频理解，返回可灌进 schemas.BGMAnalysis 的 dict。

    - 失败、超时、未配 ARK_API_KEY、未配 PUBLIC_AUDIO_BASE_URL → 返回 None（前端兜底走 librosa）
    - mock LLM_PROVIDER 时也返回一个轻量 fixture，让 UI 能调
    """
    settings = get_settings()
    provider = settings.llm_provider

    # mock 路径：早返一个固定 fixture，让前端 UI 在无 key 环境下也能渲染分析卡
    if provider != "doubao_ark" or not settings.ark_api_key:
        return _mock_bgm_analysis(duration_seconds)

    public_url = _public_audio_url(file_url)
    if public_url is None:
        log.warning("[bgm_llm] PUBLIC_AUDIO_BASE_URL 未配置，跳过 LLM 音频分析（保留 librosa 兜底）")
        return None

    audio_fmt = _audio_format_from_url(public_url)
    user_text = (
        f"BGM 时长约 {duration_seconds:.1f} 秒。\n"
        f"视频 brief：{(brief or '（未提供）').strip()[:200]}\n"
        f"视频目标：{(video_goal or '（未提供）').strip()[:200]}\n\n"
        "请按 system 里的 schema 返回 JSON。"
    )
    payload = {
        "model": settings.ark_llm_model,
        "messages": [
            {"role": "system", "content": _BGM_LLM_SYSTEM},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": user_text},
                    {
                        "type": "input_audio",
                        "input_audio": {"url": public_url, "format": audio_fmt},
                    },
                ],
            },
        ],
        "temperature": 0.4,
        "max_tokens": 1200,
        "stream": False,
    }
    headers = {
        "Authorization": f"Bearer {settings.ark_api_key}",
        "Content-Type": "application/json",
    }
    url = f"{settings.ark_base_url.rstrip('/')}/chat/completions"

    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=settings.llm_timeout_seconds) as client:
            resp = await client.post(url, headers=headers, json=payload)
    except (httpx.TimeoutException, httpx.HTTPError) as exc:
        log.warning("[bgm_llm] HTTP error: %s", exc)
        return None

    if resp.status_code != 200:
        log.warning("[bgm_llm] HTTP %d: %s", resp.status_code, resp.text[:200])
        return None

    try:
        data = resp.json()
    except ValueError:
        log.warning("[bgm_llm] malformed response body")
        return None

    elapsed_ms = int((time.perf_counter() - started) * 1000)
    choices = data.get("choices") or []
    if not choices:
        log.warning("[bgm_llm] empty choices")
        return None
    content = choices[0].get("message", {}).get("content")
    if not isinstance(content, str) or not content.strip():
        log.warning("[bgm_llm] empty content")
        return None

    parsed = _parse_bgm_json(content)
    if parsed is None:
        log.warning("[bgm_llm] bad JSON snippet=%r", content[:200])
        return None

    parsed["backend"] = "doubao_ark"
    parsed = _normalize_bgm_analysis(parsed, duration_seconds)
    log.info(
        "[bgm_llm] ok | %dms | tags=%s | segs=%d | fit=%.2f",
        elapsed_ms, parsed.get("mood_tags"),
        len(parsed.get("structure") or []), parsed.get("theme_fit_score", 0.0),
    )
    return parsed


def _parse_bgm_json(text: str) -> Optional[dict[str, Any]]:
    s = text.strip()
    if s.startswith("```"):
        first_nl = s.find("\n")
        if first_nl != -1:
            s = s[first_nl + 1:]
        if s.endswith("```"):
            s = s[:-3]
        s = s.strip()
    start = s.find("{")
    end = s.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        return json.loads(s[start: end + 1])
    except json.JSONDecodeError:
        return None


def _normalize_bgm_analysis(raw: dict[str, Any], duration_seconds: float) -> dict[str, Any]:
    """把模型偶尔的越界字段拍回 schema 允许的范围，避免 pydantic 校验失败弃掉整个分析结果。"""
    out: dict[str, Any] = {}
    out["title_guess"] = str(raw.get("title_guess") or "未知风格")[:60]
    tags = raw.get("mood_tags") or []
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",") if t.strip()]
    out["mood_tags"] = [str(t)[:20] for t in tags][:6]
    try:
        score = float(raw.get("theme_fit_score") or 0.5)
    except (TypeError, ValueError):
        score = 0.5
    out["theme_fit_score"] = max(0.0, min(1.0, score))
    out["theme_fit_reason"] = str(raw.get("theme_fit_reason") or "")[:140]
    out["overall_advice"] = str(raw.get("overall_advice") or "")[:160]

    segs_raw = raw.get("structure") or []
    segs: list[dict[str, Any]] = []
    for seg in segs_raw[:8]:
        if not isinstance(seg, dict):
            continue
        try:
            start = max(0.0, float(seg.get("start") or 0.0))
            end = max(start, float(seg.get("end") or 0.0))
        except (TypeError, ValueError):
            continue
        if duration_seconds > 0:
            end = min(end, duration_seconds)
            start = min(start, end)
        energy = str(seg.get("energy") or "mid").lower()
        if energy not in {"low", "mid", "high"}:
            energy = "mid"
        segs.append({
            "start": round(start, 2),
            "end": round(end, 2),
            "energy": energy,
            "label": str(seg.get("label") or "")[:40],
            "fit_with_video": str(seg.get("fit_with_video") or "")[:80],
        })
    if segs and duration_seconds > 0:
        # 收尾兜底：末段不到曲尾时贴上去（避免前端画出 BGM 里有空隙）
        last = segs[-1]
        if last["end"] < duration_seconds * 0.95:
            last["end"] = round(duration_seconds, 2)
    out["structure"] = segs
    return out


def _mock_bgm_analysis(duration_seconds: float) -> dict[str, Any]:
    """无 ARK key / 单测兜底：四段对称结构（铺垫 / 推进 / 高潮 / 收尾）。"""
    d = max(8.0, duration_seconds or 30.0)
    q = d / 4.0
    return {
        "title_guess": "[mock] 现代电子节拍",
        "mood_tags": ["燃", "节奏感", "活力"],
        "theme_fit_score": 0.72,
        "theme_fit_reason": "[mock] 节拍清晰、有副歌爆发，适合带 CTA 的种草/营销类短视频。",
        "structure": [
            {"start": 0.0, "end": round(q, 2), "energy": "low",
             "label": "前奏铺垫", "fit_with_video": "建议放在 opening，留呼吸"},
            {"start": round(q, 2), "end": round(2 * q, 2), "energy": "mid",
             "label": "节奏推进", "fit_with_video": "对齐 development 主体铺陈"},
            {"start": round(2 * q, 2), "end": round(3 * q, 2), "energy": "high",
             "label": "副歌爆发", "fit_with_video": "对齐 climax 高潮，做卖点强调"},
            {"start": round(3 * q, 2), "end": round(d, 2), "energy": "mid",
             "label": "收尾延展", "fit_with_video": "对齐 closing 行动引导"},
        ],
        "overall_advice": "[mock] 把 climax 放在副歌爆发段（2/4 ~ 3/4），收尾用 fade 延展尾声，BGM 入场可延迟到第一段 opening 结束。",
        "backend": "mock",
    }
