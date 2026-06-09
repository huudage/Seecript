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
_BGM_LLM_SYSTEM = """你是短视频 BGM 顾问。听完整段 BGM，结合 brief / video_goal，告诉用户：
1) 整首曲子的『能量走向』是什么（不是机械切 4 段，是一句话定调）
2) 有没有真正值得对齐的高潮/鼓点节奏点（可以 0 个：很多曲子就是全程平稳）
3) 有哪些可以承载长口播 / 慢镜头的平稳区间
4) 一段总体建议：高潮该放视频哪段？平稳处怎么用？如果全程平稳，这首曲适合什么类型视频？

严格返回 JSON，不要 markdown，不要解释：
{
  "title_guess": "<曲风/曲目猜测，例：流行电子 / 钢琴抒情 / 弦乐治愈>",
  "mood_tags": ["3-6 个情绪 tag，如 燃 / 紧张 / 治愈 / 大气"],
  "energy_shape": "flat | single_peak | multi_peak | build_up | wave",
  "energy_shape_reason": "<≤120 字，听到了什么所以判定为这种形态，并讲这种形态适合什么类型视频>",
  "theme_fit_score": 0.0-1.0,
  "theme_fit_reason": "<≤120 字，曲子与 brief 是否契合，为什么>",
  "climaxes": [
    {"at_seconds": 18.5, "kind": "climax|drop|build_start|release|break",
     "label": "≤12 字，例『副歌入』『鼓点 drop』",
     "fit_with_video": "≤40 字，建议对齐到视频的什么动作（卖点/反转/CTA）"}
  ],
  "calm_segments": [
    {"start": 0.0, "end": 12.0, "note": "≤30 字，为什么这段适合做铺垫/压口播"}
  ],
  "overall_advice": "<≤180 字，叙事性总建议：曲子的高潮放视频哪段、平稳处怎么承载内容、整体节奏怎么把>"
}

规则（重要，违反就重写）：
- energy_shape 必须是 flat / single_peak / multi_peak / build_up / wave 五选一
- climaxes 长度 0-3 个，只标真正值得让用户对齐的『鼓点』/『副歌入』/『drop』，
  曲子如果全程平稳没有明显高潮，必须返回空数组 []，不要硬凑
- calm_segments 长度 0-3 个，标真正能承载长口播 / 慢镜头的"安全区间"
- 不要再把整曲机械切成 4-6 段平铺色块，重点是『关键节点』不是『段落罗列』
- mood_tags 至少 3 个，最多 6 个
- 不要编造曲名/版权信息，title_guess 用风格描述就好
- 时间用浮点秒，不要超过曲子总时长（duration_seconds 见 user 提示）
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

    # 生产环境：缺 key 直接抛错；单测可显式 LLM_PROVIDER=mock 走 fixture
    if provider == "mock":
        return _mock_bgm_analysis(duration_seconds)
    if provider != "doubao_ark":
        raise RuntimeError(
            f"BGM 分析需要 LLM_PROVIDER=doubao_ark（当前 {provider!r}），生产 .env 必须显式配置。"
        )
    if not settings.ark_api_key:
        raise RuntimeError(
            "BGM 分析需要 ARK_API_KEY（多模态音频理解走 ARK）；生产 .env 缺 key。"
        )

    public_url = _public_audio_url(file_url)
    if public_url is None:
        log.warning("[bgm_llm] PUBLIC_AUDIO_BASE_URL 未配置，跳过 LLM 音频分析（保留 librosa 兜底）")
        return None

    audio_fmt = _audio_format_from_url(public_url)
    user_text = (
        f"BGM 时长约 {duration_seconds:.1f} 秒。\n"
        f"视频 brief：{(brief or '（未提供）').strip()[:200]}\n"
        f"视频目标：{(video_goal or '（未提供）').strip()[:200]}\n\n"
        "请听完整段曲子后按 system 里的 schema 返回 JSON。"
        "重点：先定 energy_shape，再决定要不要标 climaxes；"
        "如果整曲就是平稳/治愈/Lo-fi 之类，climaxes 必须留空 []。"
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
        "[bgm_llm] ok | %dms | shape=%s | climaxes=%d | calm=%d | fit=%.2f",
        elapsed_ms,
        parsed.get("energy_shape"),
        len(parsed.get("climaxes") or []),
        len(parsed.get("calm_segments") or []),
        parsed.get("theme_fit_score", 0.0),
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


_ENERGY_SHAPES = {"flat", "single_peak", "multi_peak", "build_up", "wave"}
_HIGHLIGHT_KINDS = {"climax", "drop", "build_start", "release", "break"}


def _normalize_bgm_analysis(raw: dict[str, Any], duration_seconds: float) -> dict[str, Any]:
    """把模型偶尔的越界字段拍回 schema 允许的范围，避免 pydantic 校验失败弃掉整个分析结果。"""
    out: dict[str, Any] = {}
    out["title_guess"] = str(raw.get("title_guess") or "未知风格")[:60]

    tags = raw.get("mood_tags") or []
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",") if t.strip()]
    out["mood_tags"] = [str(t)[:20] for t in tags][:6]

    shape = str(raw.get("energy_shape") or "wave").lower()
    if shape not in _ENERGY_SHAPES:
        shape = "wave"
    out["energy_shape"] = shape
    out["energy_shape_reason"] = str(raw.get("energy_shape_reason") or "")[:140]

    try:
        score = float(raw.get("theme_fit_score") or 0.5)
    except (TypeError, ValueError):
        score = 0.5
    out["theme_fit_score"] = max(0.0, min(1.0, score))
    out["theme_fit_reason"] = str(raw.get("theme_fit_reason") or "")[:140]
    out["overall_advice"] = str(raw.get("overall_advice") or "")[:200]

    climaxes_raw = raw.get("climaxes") or []
    climaxes: list[dict[str, Any]] = []
    for hl in climaxes_raw[:6]:
        if not isinstance(hl, dict):
            continue
        try:
            at = max(0.0, float(hl.get("at_seconds") or 0.0))
        except (TypeError, ValueError):
            continue
        if duration_seconds > 0:
            at = min(at, duration_seconds)
        kind = str(hl.get("kind") or "climax").lower()
        if kind not in _HIGHLIGHT_KINDS:
            kind = "climax"
        climaxes.append({
            "at_seconds": round(at, 2),
            "kind": kind,
            "label": str(hl.get("label") or "")[:24],
            "fit_with_video": str(hl.get("fit_with_video") or "")[:80],
        })
    # flat 形态时硬性清空——模型偶尔会硬凑
    if shape == "flat":
        climaxes = []
    out["climaxes"] = climaxes[:4]

    calm_raw = raw.get("calm_segments") or []
    calm: list[dict[str, Any]] = []
    for seg in calm_raw[:6]:
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
        if end <= start:
            continue
        calm.append({
            "start": round(start, 2),
            "end": round(end, 2),
            "note": str(seg.get("note") or "")[:60],
        })
    out["calm_segments"] = calm[:4]

    return out


def _mock_bgm_analysis(duration_seconds: float) -> dict[str, Any]:
    """无 ARK key / 单测兜底：典型单峰电子节拍 fixture。"""
    d = max(8.0, duration_seconds or 30.0)
    climax_at = round(d * 0.55, 2)  # 副歌大约 55% 处
    return {
        "title_guess": "[mock] 现代电子节拍",
        "mood_tags": ["燃", "节奏感", "活力"],
        "energy_shape": "single_peak",
        "energy_shape_reason": "[mock] 前奏铺垫 → 鼓点推进 → 副歌爆发的典型单峰结构，适合带 CTA / 卖点对比的种草型短视频。",
        "theme_fit_score": 0.72,
        "theme_fit_reason": "[mock] 节拍清晰、有副歌爆发，适合带 CTA 的种草/营销类短视频。",
        "climaxes": [
            {
                "at_seconds": climax_at,
                "kind": "climax",
                "label": "副歌入",
                "fit_with_video": "对齐到视频最强卖点出现的瞬间",
            },
        ],
        "calm_segments": [
            {
                "start": 0.0,
                "end": round(d * 0.35, 2),
                "note": "前奏铺垫，可以压一段口播开场",
            },
        ],
        "overall_advice": (
            "[mock] 把视频高潮（卖点对比 / 反转）放在副歌爆发的瞬间，前奏期间承载口播开场；"
            "结尾让曲子自然 fade-out 收 CTA，不要硬切。"
        ),
        "backend": "mock",
    }


# ---------------------------------------------------------------------------
# LLM 多模态：样例视频音轨理解（拆解阶段专用，区别于上面的 BGM 选曲视角）
#
# 上面那套 prompt 对象是『一首背景音乐』，结论是『这首曲子配什么类型视频好』；
# 拆解阶段拿到的是【整段样例视频的混音】（口播 + 配乐 + 环境音任意组合），
# 用户需要的结论是『这条样例的音轨教会我什么节奏，迁移时可以怎么压』——视角完全不同。
# 复用 BGMAnalysis schema 不破坏前端类型，但 prompt 强制 LLM 用样例迁移视角填字段。
# ---------------------------------------------------------------------------
_SAMPLE_AUDIO_LLM_SYSTEM = """你是短视频节奏分析师。听完一条样例视频的整段音轨（包含口播、配乐、环境音、静音段中的任意组合），
你的目标不是为这段音去推荐 BGM，而是告诉【后续要复刻这条样例的创作者】：
这段音轨是怎么把观众从开头拉到结尾的？关键节奏点落在哪？哪些段落可以承载长口播 / 慢镜头？

严格返回 JSON，不要 markdown，不要解释：
{
  "title_guess": "<一句话定调整段音轨的听觉走向（不是猜 BGM 曲名）。例：『口播主导 + 配乐做底』『前段静默铺氛围→中段配乐推进→尾段口播收 CTA』『纯叙述无配乐』『高密度信息口播+稀疏环境音』>",
  "mood_tags": ["3-6 个听感关键词，描述听完这段音轨的整体感受。例：信息密集 / 沉浸感 / 紧张推进 / 治愈舒缓 / 反差感 / 节奏鲜明"],
  "energy_shape": "flat | single_peak | multi_peak | build_up | wave",
  "energy_shape_reason": "<≤120 字，听到了什么决定了这个能量形态。要点出关键节奏来自什么——是 BGM 鼓点？口播爆词？还是静默切换？>",
  "theme_fit_score": 0.0-1.0,
  "theme_fit_reason": "<≤120 字，这段音轨的能量走向与视频题材是否吻合（user 提示里有视频题材线索）。低分要明确指出哪里冲突>",
  "climaxes": [
    {"at_seconds": 18.5, "kind": "climax|drop|build_start|release|break",
     "label": "≤12 字，这一刻听到了什么。例『口播爆词』『鼓点切入』『副歌入』『静默→配乐起』『情绪反转』",
     "fit_with_video": "≤40 字，建议复刻者把视频画面的什么动作压到这一刻（卖点呈现/反转/CTA/钩子）"}
  ],
  "calm_segments": [
    {"start": 0.0, "end": 12.0, "note": "≤30 字，这段为什么听感平稳，可以承载什么（长口播/产品细节/转场喘息）"}
  ],
  "overall_advice": "<≤180 字，给【迁移者】的具体建议：复刻这条样例时，音轨教会我的节奏是什么？哪段做钩子？哪段压信息？尾段怎么收？不要写『建议选什么 BGM』——选曲是 Compose 阶段的事>"
}

规则（违反就重写）：
- 你听到的是【样例视频的整段混音】，不是纯 BGM。所以：
  ① 口播 / 配乐 / 环境音 / 静默 都是节奏元素，要明确写出主导是什么、配什么
  ② 不要把口播当噪声，口播的语速 / 停顿 / 情绪爆词 都是 climaxes 候选
  ③ 没有音乐高潮就从口播爆词、静默→配乐起切换处取节奏点；硬找『副歌』是错的
  ④ title_guess 必须描述音轨结构（主导是谁、走向如何），禁止猜 BGM 曲名
- energy_shape 必须是 flat / single_peak / multi_peak / build_up / wave 五选一
- climaxes 长度 0-3 个，没真正的节奏点就空数组 []，不要硬凑
- calm_segments 长度 0-3 个，标真正能承载长口播 / 慢镜头的"安全区间"
- mood_tags 至少 3 个，最多 6 个，必须是听感关键词，不是 BGM 风格 tag
- 时间用浮点秒，不要超过音轨总时长（duration_seconds 见 user 提示）
- overall_advice 是『迁移指导』视角，不是『选曲指导』视角；写给后续要按这条样例复刻的人看
"""


async def analyze_sample_audio_with_llm(
    *,
    file_url: str,
    duration_seconds: float,
    sample_title: str,
    nl_prompt: str,
) -> Optional[dict[str, Any]]:
    """拆解阶段用：把样例视频整段音轨送 doubao-seed 拿『听觉节奏画像』。

    与 analyze_bgm_with_llm 的区别：
    - 视角：迁移参考（这条样例怎么把观众拉起来的） vs 选曲（这首 BGM 配什么视频）
    - 输入：可能含口播/配乐/环境音的混合音轨 vs 纯 BGM
    - theme_fit_score 含义：音轨与本视频题材的契合度 vs 与用户 brief 的契合度

    失败/超时/未配 ARK_API_KEY/未配 PUBLIC_AUDIO_BASE_URL → 返回 None（前端兜底走 librosa）。
    """
    settings = get_settings()
    provider = settings.llm_provider

    if provider == "mock":
        return _mock_sample_audio_analysis(duration_seconds)
    if provider != "doubao_ark":
        raise RuntimeError(
            f"样例音轨分析需要 LLM_PROVIDER=doubao_ark（当前 {provider!r}），生产 .env 必须显式配置。"
        )
    if not settings.ark_api_key:
        raise RuntimeError(
            "样例音轨分析需要 ARK_API_KEY（多模态音频理解走 ARK）；生产 .env 缺 key。"
        )

    public_url = _public_audio_url(file_url)
    if public_url is None:
        log.warning("[sample_audio_llm] PUBLIC_AUDIO_BASE_URL 未配置，跳过 LLM 音频分析")
        return None

    audio_fmt = _audio_format_from_url(public_url)
    user_text = (
        f"样例视频音轨总时长约 {duration_seconds:.1f} 秒。\n"
        f"样例标题/主题线索：{(sample_title or '（未知）').strip()[:80]}\n"
        f"创作者补充语境：{(nl_prompt or '（未提供）').strip()[:200]}\n\n"
        "请按 system 里的 schema 返回 JSON。重点：\n"
        "1) 先判断音轨【主导是什么】（口播主导 / 配乐主导 / 混合 / 纯环境音 / 纯静默+少量提示音）；\n"
        "2) 再决定 energy_shape；\n"
        "3) climaxes 来源不限于音乐，口播爆词 / 静默→配乐起 都是合法节奏点；\n"
        "4) overall_advice 写给【迁移复刻者】看，不要给选曲建议。"
    )
    payload = {
        "model": settings.ark_llm_model,
        "messages": [
            {"role": "system", "content": _SAMPLE_AUDIO_LLM_SYSTEM},
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
        log.warning("[sample_audio_llm] HTTP error: %s", exc)
        return None

    if resp.status_code != 200:
        log.warning("[sample_audio_llm] HTTP %d: %s", resp.status_code, resp.text[:200])
        return None

    try:
        data = resp.json()
    except ValueError:
        log.warning("[sample_audio_llm] malformed response body")
        return None

    elapsed_ms = int((time.perf_counter() - started) * 1000)
    choices = data.get("choices") or []
    if not choices:
        log.warning("[sample_audio_llm] empty choices")
        return None
    content = choices[0].get("message", {}).get("content")
    if not isinstance(content, str) or not content.strip():
        log.warning("[sample_audio_llm] empty content")
        return None

    parsed = _parse_bgm_json(content)
    if parsed is None:
        log.warning("[sample_audio_llm] bad JSON snippet=%r", content[:200])
        return None

    parsed["backend"] = "doubao_ark"
    parsed = _normalize_bgm_analysis(parsed, duration_seconds)
    log.info(
        "[sample_audio_llm] ok | %dms | shape=%s | climaxes=%d | calm=%d | fit=%.2f",
        elapsed_ms,
        parsed.get("energy_shape"),
        len(parsed.get("climaxes") or []),
        len(parsed.get("calm_segments") or []),
        parsed.get("theme_fit_score", 0.0),
    )
    return parsed


def _mock_sample_audio_analysis(duration_seconds: float) -> dict[str, Any]:
    """无 ARK key / 单测兜底：用样例迁移视角写一份听感解读 fixture（区别于 _mock_bgm_analysis）。"""
    d = max(8.0, duration_seconds or 30.0)
    climax_at = round(d * 0.6, 2)
    return {
        "title_guess": "[mock] 口播主导 · 中段配乐推进收 CTA",
        "mood_tags": ["信息密集", "节奏鲜明", "情绪推进"],
        "energy_shape": "build_up",
        "energy_shape_reason": "[mock] 开场口播铺垫 → 中段加入配乐推进信息 → 尾段配乐升、口播加重收尾，整体能量持续抬升。",
        "theme_fit_score": 0.7,
        "theme_fit_reason": "[mock] 信息向短视频典型节奏：开头钩子 + 中段细节 + 尾段 CTA，音轨能量与叙事重心吻合。",
        "climaxes": [
            {
                "at_seconds": climax_at,
                "kind": "build_start",
                "label": "配乐起 · 口播加重",
                "fit_with_video": "压到本片最强卖点 / 反转出现的瞬间",
            },
        ],
        "calm_segments": [
            {
                "start": 0.0,
                "end": round(d * 0.35, 2),
                "note": "开头铺垫段，承接长口播或慢镜头",
            },
        ],
        "overall_advice": (
            "[mock] 复刻时按『开场口播钩子 → 中段配乐起承接卖点 → 尾段配乐推情绪+口播收 CTA』走；"
            "前 1/3 留足口播空间，中段配乐起的瞬间务必同步画面最强镜头，尾段不要硬切要 fade。"
        ),
        "backend": "mock",
    }
