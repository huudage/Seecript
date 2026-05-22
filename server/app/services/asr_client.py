"""ASR (Automatic Speech Recognition) client abstraction.

Concrete adapters:
- `MockASRClient`         : returns a canned transcript so feature-1 works without a real key.
- `DoubaoBigmodelASRClient`: Volcengine Doubao 大模型录音文件极速版 (turbo / flash) — synchronous,
  one HTTP request → result. Reference PDF stored at
  `c:\\Users\\1\\Desktop\\豆包语音_大模型录音文件极速版识别API_*.pdf` (2025/06).

Why极速版 (turbo) over the standard async version:
- One round-trip vs submit/query polling → 10× lower P95 latency
- Accepts base64 audio inline → **no need to expose a public URL** to Volcengine
- Same authentication; only the resource ID and endpoint change

Why this design (Adapter + Factory):
- Routers depend on the abstract `ASRClient`, not on concrete providers (DIP).
- Adding a new provider = new subclass + register in `_PROVIDERS` (OCP).
- Mock client lets the whole product run without any key (essential for offline dev / CI).

Doubao 极速版 lifecycle (one request):
  POST https://openspeech.bytedance.com/api/v3/auc/bigmodel/recognize/flash
    headers: X-Api-Key, X-Api-Resource-Id=volc.bigasr.auc_turbo,
             X-Api-Request-Id=<uuid>, X-Api-Sequence=-1
    body:    {"user":{"uid":"<key>"}, "audio":{"data":"<base64>"}, "request":{"model_name":"bigmodel"}}
  response: 200 OK + X-Api-Status-Code=20000000 + JSON { "result": { "text": "..." } }
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
import uuid
from abc import ABC, abstractmethod
from typing import Any, Dict, Optional

import httpx

from ..config import Settings, get_settings


log = logging.getLogger("seecript.asr")


# Volcengine status codes (from official 极速版 docs).
DOUBAO_STATUS_SUCCESS = 20000000
DOUBAO_STATUS_PROCESSING = 20000001  # not used by flash, kept for forward compat
DOUBAO_STATUS_QUEUED = 20000002      # not used by flash
DOUBAO_STATUS_SILENT_AUDIO = 20000003

# Map upstream codes to user-friendly Chinese messages.
# Sourced verbatim from the 极速版 PDF "错误码" table.
_DOUBAO_ERROR_HINTS = {
    20000003: "音频静音或无人声，无法识别。",
    45000001: "请求参数无效（请检查音频内容是否为合法 mp3/m4a/wav；是否开通了 volc.bigasr.auc_turbo 资源）。",
    45000002: "音频为空。",
    45000151: "音频格式不正确（仅支持 mp3 / wav / ogg / opus）。",
    55000031: "火山引擎服务繁忙，请稍后重试。",
}


class ASRError(RuntimeError):
    def __init__(self, message: str, code: str = "ASR_ERROR", upstream_status: Optional[int] = None) -> None:
        super().__init__(message)
        self.code = code
        self.upstream_status = upstream_status


# --------------------------------------------------------------------------
# Abstract interface
# --------------------------------------------------------------------------
class ASRClient(ABC):
    """The abstract contract every ASR adapter implements.

    极速版 directly accepts bytes — no need for a publicly-reachable URL — so the
    primary entry point is `transcribe_bytes`. The legacy `transcribe_url` method
    is kept (default-implemented) for backward compatibility with old callers.
    """

    name: str = "abstract"

    @abstractmethod
    async def transcribe_bytes(self, audio_bytes: bytes, *, audio_format: str = "mp3") -> str:
        """Given raw audio bytes, return the transcript text."""

    async def transcribe_url(self, audio_url: str, *, audio_format: str = "mp3") -> str:
        """Default: fetch the URL ourselves then delegate to transcribe_bytes.

        极速版 doesn't strictly need this path (we have the bytes already from the
        upload), but it gives us forward-compat for callers that still pass URLs.
        """
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(audio_url)
            r.raise_for_status()
            return await self.transcribe_bytes(r.content, audio_format=audio_format)


# --------------------------------------------------------------------------
# Mock
# --------------------------------------------------------------------------
class MockASRClient(ASRClient):
    name = "mock"

    async def transcribe_bytes(self, audio_bytes: bytes, *, audio_format: str = "mp3") -> str:
        # Simulate some processing latency (real Doubao 极速版 is typically 1-5s).
        await asyncio.sleep(0.5)
        return _MOCK_TRANSCRIPT


# --------------------------------------------------------------------------
# Doubao 极速版 (Volcengine bigmodel turbo / flash)
# --------------------------------------------------------------------------
class DoubaoBigmodelASRClient(ASRClient):
    name = "doubao"

    def __init__(self, settings: Settings) -> None:
        if not settings.doubao_api_key:
            raise ASRError(
                "DOUBAO_API_KEY is empty but ASR_PROVIDER=doubao. "
                "Set the key in server/.env or switch ASR_PROVIDER=mock.",
                code="ASR_NO_KEY",
            )
        self._api_key = settings.doubao_api_key
        self._resource_id = settings.doubao_resource_id
        self._recognize_url = settings.doubao_recognize_url
        self._timeout = settings.asr_timeout_seconds

    async def transcribe_bytes(self, audio_bytes: bytes, *, audio_format: str = "mp3") -> str:
        if not audio_bytes:
            raise ASRError("音频字节为空", code="ASR_EMPTY", upstream_status=45000002)

        request_id = str(uuid.uuid4())
        # Per PDF, the new console only requires X-Api-Key. We do NOT send X-Api-App-Key /
        # X-Api-Access-Key so we don't trigger the legacy auth path (which would require both).
        headers = {
            "X-Api-Key": self._api_key,
            "X-Api-Resource-Id": self._resource_id,
            "X-Api-Request-Id": request_id,
            "X-Api-Sequence": "-1",
            "Content-Type": "application/json",
        }
        # uid per PDF: "你的AppKey". The new-console key is the AppKey, so reuse it here.
        body = {
            "user": {"uid": self._api_key},
            "audio": {"data": base64.b64encode(audio_bytes).decode("ascii")},
            "request": {
                "model_name": "bigmodel",
                # ITN (e.g. 一百二十 → 120) and punctuation hugely improve transcripts; turn on.
                "enable_itn": True,
                "enable_punc": True,
            },
        }

        started = time.perf_counter()
        log.info(
            "doubao flash start | request_id=%s | bytes=%d | format=%s",
            request_id,
            len(audio_bytes),
            audio_format,
        )

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            try:
                resp = await client.post(self._recognize_url, headers=headers, json=body)
            except httpx.TimeoutException as e:
                raise ASRError(f"豆包请求超时（{self._timeout}s）", code="ASR_TIMEOUT") from e
            except httpx.HTTPError as e:
                raise ASRError(f"豆包网络错误：{e}", code="ASR_NETWORK") from e

        api_status = self._parse_status(resp)
        logid = resp.headers.get("X-Tt-Logid", "-")
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        log.info(
            "doubao flash done | request_id=%s | http=%d | x-api-status=%s | logid=%s | %dms",
            request_id,
            resp.status_code,
            api_status,
            logid,
            elapsed_ms,
        )

        # HTTP-level error (rare; usually status_code=200 even on app errors)
        if resp.status_code >= 500:
            raise ASRError(
                f"豆包 HTTP {resp.status_code}: {resp.text[:300]}",
                code=f"ASR_HTTP_{resp.status_code}",
                upstream_status=resp.status_code,
            )
        # 4xx (auth / quota / route) — surface logid to help debugging
        if resp.status_code >= 400:
            raise ASRError(
                f"豆包 HTTP {resp.status_code} (logid={logid}): {resp.text[:300]}",
                code=f"ASR_HTTP_{resp.status_code}",
                upstream_status=resp.status_code,
            )

        # API-level status (the real source of truth)
        if api_status == DOUBAO_STATUS_SUCCESS:
            return self._extract_text(resp)

        if api_status is None:
            raise ASRError(
                f"豆包响应缺少 X-Api-Status-Code 头 (logid={logid})",
                code="ASR_NO_STATUS",
            )

        hint = _DOUBAO_ERROR_HINTS.get(api_status, f"未知状态码 {api_status}")
        raise ASRError(
            f"豆包识别失败：{hint} (logid={logid})",
            code=f"ASR_API_{api_status}",
            upstream_status=api_status,
        )

    @staticmethod
    def _parse_status(resp: httpx.Response) -> Optional[int]:
        raw = resp.headers.get("X-Api-Status-Code")
        if not raw:
            return None
        try:
            return int(raw)
        except ValueError:
            return None

    @staticmethod
    def _extract_text(resp: httpx.Response) -> str:
        """Pull `result.text` out of the response body.

        Schema (per 极速版 PDF):
            {"audio_info":{...},"result":{"text":"...","utterances":[...]}}
        We are tolerant of a couple of legacy variants (data.result.text) just in case.
        """
        try:
            data: Dict[str, Any] = resp.json()
        except json.JSONDecodeError as e:
            raise ASRError(f"豆包响应不是合法 JSON：{e}", code="ASR_BAD_JSON") from e

        result = data.get("result")
        if isinstance(result, dict):
            text = result.get("text")
            if isinstance(text, str) and text.strip():
                return text.strip()

        # Legacy nested fallback (some standard-version responses).
        nested = data.get("data") or {}
        if isinstance(nested, dict):
            r2 = nested.get("result") or {}
            if isinstance(r2, dict):
                text = r2.get("text")
                if isinstance(text, str) and text.strip():
                    return text.strip()

        raise ASRError(
            f"豆包响应缺少 result.text 字段：{json.dumps(data, ensure_ascii=False)[:300]}",
            code="ASR_NO_TEXT",
        )


# --------------------------------------------------------------------------
# Factory
# --------------------------------------------------------------------------
_PROVIDERS = {
    "mock": MockASRClient,
    "doubao": DoubaoBigmodelASRClient,
}


def get_asr_client(settings: Optional[Settings] = None) -> ASRClient:
    s = settings or get_settings()
    if s.asr_provider == "doubao" and not s.doubao_api_key:
        log.warning("ASR_PROVIDER=doubao but DOUBAO_API_KEY is empty -> using mock")
        return MockASRClient()
    cls = _PROVIDERS.get(s.asr_provider, MockASRClient)
    if cls is DoubaoBigmodelASRClient:
        return DoubaoBigmodelASRClient(s)
    return cls()


# --------------------------------------------------------------------------
# Mock fixture
# --------------------------------------------------------------------------
_MOCK_TRANSCRIPT = """[00:00] 90% 的人冰箱都用错了，你以为塞满才划算，其实越满越浪费。
[00:05] 我家以前也是这样，每周扔掉的食材能堆成小山。
[00:15] 后来我学到一个三步法，今天分享给你。
[00:20] 第一步：分区。冰箱不是仓库，是有逻辑的工作台。
[00:40] 第二步：打标。任何打开过的食材都贴上日期。
[01:00] 第三步：周清。每周固定一天，清掉过期或濒临过期的。
[01:30] 整理之后，我家每月伙食费降了 600 块。
[01:50] 你家冰箱属于哪一种？把首字母打在评论区，我下期挨个点评。"""
