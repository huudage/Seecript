"""ASR (Automatic Speech Recognition) client abstraction.

Concrete adapters:
- `MockASRClient`         : returns a canned transcript so feature-1 works without a real key.
- `DoubaoBigmodelASRClient`: Volcengine 豆包大模型录音文件识别 2.0（seedasr / bigmodel auc）—
  **异步双阶段**：POST /submit 拿任务 ID（其实 task_id 就是请求时传的 X-Api-Request-Id），
  然后轮询 POST /query 直到 X-Api-Status-Code=20000000 拿到 result.text。

Why 2.0 over 极速版 (1.0 turbo / flash)：
- 2.0 准确率更高，支持上下文/多语言/视觉上下文等高阶能力
- 但 2.0 强制 `audio.url`：不再接受 base64 inline，必须给火山服务器一个**公网可达**的 URL
- 我们靠 `settings.public_audio_base_url` 把本地 /samples/<id>/video.mp4 拼成公网地址
  （例：cloudflared tunnel / ngrok / 火山 TOS 桶）

鉴权两种模式（自动识别）：
- 新控制台：只发 X-Api-Key（doubao_api_key 是 UUID 串）
- 旧控制台：发 X-Api-App-Key (=AppID) + X-Api-Access-Key (=AccessToken)，
  当 doubao_access_key 非空时启用

豆包 2.0 lifecycle:
  POST /api/v3/auc/bigmodel/submit
    headers: X-Api-Key, X-Api-Resource-Id=volc.bigasr.auc,
             X-Api-Request-Id=<uuid>, X-Api-Sequence=-1
    body:    {"user":{"uid":"<key>"},"audio":{"format":"mp4","url":"https://.../video.mp4"},
              "request":{"model_name":"bigmodel","enable_itn":true,"enable_punc":true}}
  → 200 OK, X-Api-Status-Code=20000000  (Body 为空)

  POST /api/v3/auc/bigmodel/query     # 同 X-Api-Request-Id
    headers: 同上 (无 X-Api-Sequence)
    body:    {}
  → loop:
      X-Api-Status-Code=20000001 → 处理中, 继续等
      X-Api-Status-Code=20000002 → 队列中, 继续等
      X-Api-Status-Code=20000000 → 完成, body 含 {"result":{"text":"..."}}
      其他                          → 失败
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import httpx

from ..config import Settings, get_settings


log = logging.getLogger("seecript.asr")


# Volcengine status codes (统一覆盖 1.0 极速版 + 2.0 异步)。
DOUBAO_STATUS_SUCCESS = 20000000
DOUBAO_STATUS_PROCESSING = 20000001
DOUBAO_STATUS_QUEUED = 20000002
DOUBAO_STATUS_SILENT_AUDIO = 20000003


@dataclass
class Utterance:
    """逐句时间戳。start/end 单位是秒——内部统一从毫秒换算掉。"""
    text: str
    start: float
    end: float


@dataclass
class ASRTranscript:
    """transcribe_url 返回值：整段文本 + 逐句时间戳。

    show_utterances=true 时豆包返回 result.utterances；旧调用方只关心 text 仍可直接读 .text。
    decompose_agent 用 utterances 按 shot 时间窗映射，避免字符比例切分误把英文单词截断。
    """
    text: str
    utterances: List[Utterance] = field(default_factory=list)

# Map upstream codes to user-friendly Chinese messages.
_DOUBAO_ERROR_HINTS = {
    20000003: "音频静音或无人声，无法识别。",
    45000001: "请求参数无效（请检查音频格式 / URL 是否公网可达 / 资源 ID 是否开通）。",
    45000002: "音频为空。",
    45000010: "X-Api-Key 无效（鉴权失败）。",
    45000131: "超过半小时提交音频长度上限（默认 500h），请降速。",
    45000132: "上传音频超过大小限制（< 512MB）。",
    45000151: "音频格式不正确（仅支持 mp3 / wav / ogg / opus / mp4）。",
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

    豆包 2.0 强制 audio.url，所以**主入口是 transcribe_url**。bytes 入口仅 mock 保留 —
    真服务下 transcribe_bytes 会要求调用方先把字节托管到一个公网可达的 URL。
    """

    name: str = "abstract"

    async def transcribe_url(self, audio_url: str, *, audio_format: str = "mp4") -> ASRTranscript:
        raise NotImplementedError

    async def transcribe_bytes(self, audio_bytes: bytes, *, audio_format: str = "mp3") -> str:
        # 默认行为：mock 直接返回，doubao 子类必须自行处理或委托给 transcribe_url。
        raise NotImplementedError


# --------------------------------------------------------------------------
# Mock
# --------------------------------------------------------------------------
class MockASRClient(ASRClient):
    name = "mock"

    async def transcribe_bytes(self, audio_bytes: bytes, *, audio_format: str = "mp3") -> str:
        await asyncio.sleep(0.5)
        return _MOCK_TRANSCRIPT

    async def transcribe_url(self, audio_url: str, *, audio_format: str = "mp4") -> ASRTranscript:
        await asyncio.sleep(0.5)
        # 假装 8 句逐句时间戳，避免 decompose_agent 的字符切分降级路径
        text = _MOCK_TRANSCRIPT
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        utts: List[Utterance] = []
        cursor = 0.0
        per = 5.0  # 每句 5 秒
        for ln in lines:
            utts.append(Utterance(text=ln, start=cursor, end=cursor + per))
            cursor += per
        return ASRTranscript(text=text, utterances=utts)


# --------------------------------------------------------------------------
# Doubao 2.0 异步（seedasr / bigmodel auc）
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
        self._access_key = settings.doubao_access_key
        self._resource_id = settings.doubao_resource_id
        self._submit_url = settings.doubao_submit_url
        self._query_url = settings.doubao_query_url
        self._timeout = settings.asr_timeout_seconds
        self._poll_interval = settings.asr_poll_interval_seconds
        self._poll_max = settings.asr_poll_max_seconds
        # 双模式鉴权：access_key 非空 → 旧控制台双头；否则新控制台单 X-Api-Key
        self._auth_mode = "legacy" if self._access_key else "new"
        log.info(
            "doubao asr client | auth=%s | resource=%s | submit=%s",
            self._auth_mode, self._resource_id, self._submit_url,
        )

    # ------------------------------------------------------------------
    # 主入口：URL 模式（2.0 真实路径）
    # ------------------------------------------------------------------
    async def transcribe_url(self, audio_url: str, *, audio_format: str = "mp4") -> ASRTranscript:
        if not audio_url:
            raise ASRError("audio_url 为空", code="ASR_NO_URL")
        if not (audio_url.startswith("http://") or audio_url.startswith("https://")):
            raise ASRError(
                f"豆包 2.0 需要公网可达的 audio.url，收到非 http(s) URL: {audio_url}",
                code="ASR_BAD_URL",
            )

        request_id = str(uuid.uuid4())
        body = {
            "user": {"uid": self._api_key},
            "audio": {
                "format": audio_format,
                "url": audio_url,
            },
            "request": {
                "model_name": "bigmodel",
                "enable_itn": True,
                "enable_punc": True,
                # 拿到逐句时间戳，让 decompose_agent 按 shot 时间窗映射 transcript，
                # 不再用"按时长占比切字符"的退化算法（会把英文单词从中间截断）。
                "show_utterances": True,
            },
        }

        await self._submit_task(request_id, body)
        return await self._poll_query(request_id)

    # ------------------------------------------------------------------
    # bytes 入口：2.0 必须 URL，所以这里直接拒绝并提示
    # ------------------------------------------------------------------
    async def transcribe_bytes(self, audio_bytes: bytes, *, audio_format: str = "mp3") -> str:
        raise ASRError(
            "豆包 2.0 不支持 base64 inline 上传，请改调 transcribe_url(public_url)；"
            "需要在 .env 配 PUBLIC_AUDIO_BASE_URL 把本地 /samples 暴露成公网。",
            code="ASR_BYTES_UNSUPPORTED",
        )

    # ------------------------------------------------------------------
    # 内部：submit + poll
    # ------------------------------------------------------------------
    def _build_headers(self, request_id: str, *, with_sequence: bool) -> Dict[str, str]:
        if self._auth_mode == "legacy":
            h = {
                "X-Api-App-Key": self._api_key,
                "X-Api-Access-Key": self._access_key,
                "X-Api-Resource-Id": self._resource_id,
                "X-Api-Request-Id": request_id,
                "Content-Type": "application/json",
            }
        else:
            h = {
                "X-Api-Key": self._api_key,
                "X-Api-Resource-Id": self._resource_id,
                "X-Api-Request-Id": request_id,
                "Content-Type": "application/json",
            }
        if with_sequence:
            h["X-Api-Sequence"] = "-1"
        return h

    async def _submit_task(self, request_id: str, body: Dict[str, Any]) -> None:
        headers = self._build_headers(request_id, with_sequence=True)
        log.info(
            "doubao 2.0 submit | request_id=%s | url=%s",
            request_id, body["audio"].get("url"),
        )
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            try:
                resp = await client.post(self._submit_url, headers=headers, json=body)
            except httpx.TimeoutException as e:
                raise ASRError(f"豆包 submit 请求超时（{self._timeout}s）", code="ASR_TIMEOUT") from e
            except httpx.HTTPError as e:
                raise ASRError(f"豆包 submit 网络错误：{e}", code="ASR_NETWORK") from e

        api_status = self._parse_status(resp)
        logid = resp.headers.get("X-Tt-Logid", "-")
        log.info(
            "doubao 2.0 submit done | request_id=%s | http=%d | x-api-status=%s | logid=%s",
            request_id, resp.status_code, api_status, logid,
        )

        if resp.status_code >= 400:
            raise ASRError(
                f"豆包 submit HTTP {resp.status_code} (logid={logid}): {resp.text[:300]}",
                code=f"ASR_HTTP_{resp.status_code}",
                upstream_status=resp.status_code,
            )
        if api_status != DOUBAO_STATUS_SUCCESS:
            hint = _DOUBAO_ERROR_HINTS.get(api_status or -1, f"未知状态码 {api_status}")
            raise ASRError(
                f"豆包 submit 失败：{hint} (logid={logid})",
                code=f"ASR_API_{api_status}",
                upstream_status=api_status,
            )

    async def _poll_query(self, request_id: str) -> ASRTranscript:
        headers = self._build_headers(request_id, with_sequence=False)
        deadline = time.monotonic() + self._poll_max
        attempt = 0
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            while True:
                attempt += 1
                try:
                    resp = await client.post(self._query_url, headers=headers, json={})
                except httpx.TimeoutException as e:
                    raise ASRError(f"豆包 query 超时（{self._timeout}s）", code="ASR_TIMEOUT") from e
                except httpx.HTTPError as e:
                    raise ASRError(f"豆包 query 网络错误：{e}", code="ASR_NETWORK") from e

                api_status = self._parse_status(resp)
                logid = resp.headers.get("X-Tt-Logid", "-")
                log.info(
                    "doubao 2.0 query #%d | request_id=%s | http=%d | x-api-status=%s | logid=%s",
                    attempt, request_id, resp.status_code, api_status, logid,
                )

                if resp.status_code >= 400:
                    raise ASRError(
                        f"豆包 query HTTP {resp.status_code} (logid={logid}): {resp.text[:300]}",
                        code=f"ASR_HTTP_{resp.status_code}",
                        upstream_status=resp.status_code,
                    )

                if api_status == DOUBAO_STATUS_SUCCESS:
                    return self._extract_result(resp)
                if api_status in (DOUBAO_STATUS_PROCESSING, DOUBAO_STATUS_QUEUED):
                    if time.monotonic() >= deadline:
                        raise ASRError(
                            f"豆包 query 轮询超时（>{self._poll_max}s 仍 status={api_status}, logid={logid}）",
                            code="ASR_POLL_TIMEOUT",
                            upstream_status=api_status,
                        )
                    await asyncio.sleep(self._poll_interval)
                    continue

                hint = _DOUBAO_ERROR_HINTS.get(api_status or -1, f"未知状态码 {api_status}")
                raise ASRError(
                    f"豆包 query 失败：{hint} (logid={logid})",
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
    def _extract_result(resp: httpx.Response) -> ASRTranscript:
        """解析豆包 2.0 query 返回，抽取 text + utterances。

        utterances 字段在 show_utterances=true 时返回；start_time/end_time 单位为毫秒，
        本方法统一换算为秒，并兜底兼容字段名 (start/end / start_ms/end_ms)。
        """
        try:
            data: Dict[str, Any] = resp.json()
        except json.JSONDecodeError as e:
            raise ASRError(f"豆包响应不是合法 JSON：{e}", code="ASR_BAD_JSON") from e

        result = data.get("result")
        if not isinstance(result, dict):
            raise ASRError(
                f"豆包响应缺少 result 字段：{json.dumps(data, ensure_ascii=False)[:300]}",
                code="ASR_NO_TEXT",
            )

        text = result.get("text") if isinstance(result.get("text"), str) else ""

        utts: List[Utterance] = []
        raw_utts = result.get("utterances")
        if isinstance(raw_utts, list):
            for u in raw_utts:
                if not isinstance(u, dict):
                    continue
                u_text = u.get("text") or ""
                if not isinstance(u_text, str) or not u_text.strip():
                    continue
                start_raw = u.get("start_time", u.get("start", u.get("start_ms")))
                end_raw = u.get("end_time", u.get("end", u.get("end_ms")))
                try:
                    start_ms = float(start_raw) if start_raw is not None else 0.0
                    end_ms = float(end_raw) if end_raw is not None else start_ms
                except (TypeError, ValueError):
                    continue
                # 豆包默认毫秒；如果数值小到像秒（end<60 且 text 长），按秒处理保险
                divisor = 1000.0 if end_ms >= 60 else 1.0
                utts.append(
                    Utterance(
                        text=u_text.strip(),
                        start=start_ms / divisor,
                        end=end_ms / divisor,
                    )
                )

        # text 缺失但 utterances 有 → 拼起来；都没有就报错
        if not text.strip() and utts:
            text = "".join(u.text for u in utts)

        if not text.strip():
            raise ASRError(
                f"豆包响应缺少 result.text 字段：{json.dumps(data, ensure_ascii=False)[:300]}",
                code="ASR_NO_TEXT",
            )

        return ASRTranscript(text=text.strip(), utterances=utts)


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
