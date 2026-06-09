"""TTS 客户端 —— 火山引擎豆包 TTS HTTP API v1 + mock 兜底。

火山引擎豆包 TTS HTTP（v1，bytedance.com/api/v1/tts）请求体：
{
  "app":     {"appid": "<APP_ID>", "token": "<ACCESS_TOKEN>", "cluster": "volcano_tts"},
  "user":    {"uid": "seecript"},
  "audio":   {"voice_type": "zh_female_qingxin", "encoding": "wav",
              "rate": 24000, "speed_ratio": 1.0, "loudness_ratio": 1.0},
  "request": {"reqid": "<uuid>", "text": "...", "operation": "query"}
}
Header: Authorization: Bearer;<ACCESS_TOKEN>   # 注意：分号后无空格

Response（200）:
{
  "reqid": "...", "code": 3000, "operation": "query",
  "data": "<base64 wav 数据>",
  "addition": {"duration": "<毫秒>"}
}
非 3000 视为失败。常见错误：
- 3001 无效请求；3050 voice_type 不存在；3003 并发超限；3005 后端服务忙；3011 文本超长。

参数取舍（与豆包 TTS v1 文档对齐）：
- 仅保留官方支持的 speed_ratio + loudness_ratio。
- pitch_ratio 文档明确"暂不支持"，传了也无效，省去避免误导。
- 旧的 volume_ratio 是历史字段名，v1 文档已统一为 loudness_ratio。

mock 模式 fallback：
- 用 numpy 合成 N 秒 220Hz 正弦波（按字数估算时长 ≈ 0.3s/字，下限 1s 上限 12s）
- 加慢调幅模拟节奏，至少能让 ffmpeg 混音链路跑通
- 返回 wav bytes 与真实接口同形
"""
from __future__ import annotations

import base64
import io
import logging
import math
import struct
import uuid
import wave
from typing import Optional

import httpx

from ...config import get_settings

log = logging.getLogger("seecript.tts")


# 火山 v1 已统一走"大模型音色"（*_bigtts），小模型 voice_type 全部 resource_not_granted。
# 前端 TTSVoice 枚举仍保留旧名（兼容已落盘 plan.settings），在最后一公里映射为实际付费音色。
# 未命中的 voice 原样下发——允许调用方直接传 *_bigtts ID（未来扩枚举不必改这里）。
_VOICE_ALIAS: dict[str, str] = {
    "zh_female_qingxin": "zh_female_zhixingnvsheng_mars_bigtts",
    "zh_female_wenrou": "zh_female_wenrouxiaoya_moon_bigtts",
    "zh_male_jieshuo": "zh_male_jingqiangkanye_moon_bigtts",
    "zh_male_xueyi": "zh_male_yangguangqingnian_moon_bigtts",
    "zh_female_xiaoyu": "zh_female_meilinvyou_moon_bigtts",
}


def _resolve_voice(voice: str) -> str:
    return _VOICE_ALIAS.get(voice, voice)


class TTSError(RuntimeError):
    def __init__(self, message: str, code: Optional[str] = None) -> None:
        super().__init__(message)
        self.code = code


def backend_name() -> str:
    """返回当前 TTS 后端名；若 provider=volc 但 key 缺失，仍返回 "volc" 让 synthesize() 抛错。
    返回 "mock" 仅当显式 TTS_PROVIDER=mock（单测路径）。
    """
    settings = get_settings()
    if settings.tts_provider == "volc":
        return "volc"
    if settings.tts_provider == "mock":
        return "mock"
    return settings.tts_provider or "volc"


def _mock_synthesize(text: str, sample_rate: int, speed_ratio: float = 1.0) -> bytes:
    """合成节奏感正弦波 wav——demo 链路用，不出真实人声。speed_ratio 仅缩放估算时长。"""
    chars = max(1, len(text.strip()))
    duration = min(12.0, max(1.0, chars * 0.32))
    if speed_ratio > 0:
        duration = duration / speed_ratio
    n_samples = int(sample_rate * duration)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        frames = bytearray()
        for i in range(n_samples):
            t = i / sample_rate
            envelope = 0.5 + 0.5 * math.sin(2 * math.pi * 2.0 * t)  # 2Hz 调幅
            sample = int(0.3 * envelope * math.sin(2 * math.pi * 220.0 * t) * 32767)
            frames.extend(struct.pack("<h", sample))
        wf.writeframes(bytes(frames))
    return buf.getvalue()


def _volc_synthesize(text: str, voice: str, sample_rate: int, speed_ratio: float = 1.0) -> bytes:
    settings = get_settings()
    resolved_voice = _resolve_voice(voice)
    payload = {
        "app": {
            "appid": settings.volc_tts_app_id,
            "token": settings.volc_tts_access_token,
            "cluster": settings.volc_tts_cluster,
        },
        "user": {"uid": "seecript"},
        "audio": {
            "voice_type": resolved_voice,
            "encoding": "wav",
            "rate": sample_rate,
            "speed_ratio": max(0.5, min(2.0, float(speed_ratio))),
            "loudness_ratio": 1.0,
        },
        "request": {
            "reqid": uuid.uuid4().hex,
            "text": text,
            "operation": "query",
        },
    }
    # 注意：分号后直接跟 token，无空格；这是豆包 TTS v1 鉴权强约束，
    # 加空格会被服务端解析为缺少 token 返回 401。
    headers = {
        "Authorization": f"Bearer;{settings.volc_tts_access_token}",
        "Content-Type": "application/json",
    }
    try:
        resp = httpx.post(
            settings.volc_tts_endpoint,
            json=payload, headers=headers,
            timeout=settings.tts_timeout_seconds,
        )
    except httpx.HTTPError as exc:
        raise TTSError(f"volc tts http failed: {exc}", code="VOLC_HTTP") from exc

    if resp.status_code != 200:
        raise TTSError(
            f"volc tts http {resp.status_code}: {resp.text[:200]}",
            code=f"VOLC_HTTP_{resp.status_code}",
        )

    try:
        body = resp.json()
    except ValueError as exc:
        raise TTSError(f"volc tts non-json response: {resp.text[:200]}", code="VOLC_PARSE") from exc

    code = body.get("code")
    if code != 3000:
        raise TTSError(
            f"volc tts code={code} msg={body.get('message')}",
            code=f"VOLC_CODE_{code}",
        )
    data_b64 = body.get("data") or ""
    if not data_b64:
        raise TTSError("volc tts empty data field", code="VOLC_EMPTY")
    try:
        return base64.b64decode(data_b64)
    except Exception as exc:  # noqa: BLE001
        raise TTSError(f"volc tts base64 decode failed: {exc}", code="VOLC_B64") from exc


def synthesize(
    text: str,
    voice: str = "zh_female_qingxin",
    sample_rate: int = 24000,
    speed_ratio: float = 1.0,
) -> bytes:
    """合成 TTS 音频 → wav bytes。

    无 Key 走 mock；接口异常时不抛——caller 自己决定要不要降级，但 TTSError 会传递。

    speed_ratio：1.0 = 正常，1.15 = 略快（火山 TTS 安全上限），用于压缩到 scene.duration。
    超 1.15 音质明显劣化；调用方应在传入前 clamp 并配合截尾策略。

    返回值：完整的 WAV 文件字节流（含 RIFF 头），可直接落盘 .wav。
    """
    text = (text or "").strip()
    if not text:
        raise TTSError("empty text", code="EMPTY_TEXT")

    settings = get_settings()
    rate = sample_rate or settings.tts_sample_rate
    backend = backend_name()
    log.info(
        "[tts] synthesize backend=%s voice=%s rate=%d chars=%d speed=%.2f",
        backend, voice, rate, len(text), speed_ratio,
    )
    if backend == "volc":
        if not settings.volc_tts_app_id or not settings.volc_tts_access_token:
            raise TTSError(
                "TTS_PROVIDER=volc 但 VOLC_TTS_APP_ID / VOLC_TTS_ACCESS_TOKEN 缺失——"
                "生产环境不允许静默降级到 mock。",
                code="TTS_NO_KEY",
            )
        return _volc_synthesize(text, voice, rate, speed_ratio)
    if backend == "mock":
        return _mock_synthesize(text, rate, speed_ratio)
    raise TTSError(
        f"未知 TTS_PROVIDER={backend!r}；生产应为 volc，单测可用 mock。",
        code="TTS_BAD_PROVIDER",
    )
