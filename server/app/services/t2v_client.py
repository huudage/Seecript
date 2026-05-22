"""Text-to-Video client abstraction (v0.9, Seecript 第 7 个 AI 干预点).

Design pattern: **Adapter + Factory + Async-task polling**, parallel to LLMClient/ASRClient.

Architecture rationale:
  - Routers depend only on the abstract `T2VClient` (DIP).
  - Adding a new provider (e.g. 通义万相 / 可灵) only requires writing a new subclass and
    registering it in `_PROVIDERS` (OCP).
  - The mock client lets the whole product run end-to-end without any video API key —
    essential for offline demo, CI, and frontend-only iteration.

Why we keep submit/query as two separate calls (instead of one blocking call):
  - Zhipu CogVideoX generation takes 30s-3min. A blocking call would block the event
    loop AND time out at any reverse-proxy default (nginx 60s, gunicorn 30s).
  - Async submit returns a task_id in <2 seconds; the frontend polls every 5s.
  - Same shape as ASR async版 (we briefly used in v0.3 before switching to 极速版),
    so the frontend `pollTask()` helper is reusable.

Why NOT use the official `zai-sdk` Python package:
  - Adds a new pip dependency for what is effectively two HTTP calls.
  - Can't reuse our existing httpx-based timeout / error-mapping conventions.
  - Bigmodel's REST API is stable and OpenAPI-documented; rolling our own keeps us
    in full control of error handling and observability.

References:
  - 提交：POST /paas/v4/videos/generations
    https://docs.bigmodel.cn/api-reference/模型-api/视频生成异步
  - 查询：GET /paas/v4/async-result/{id}
    https://docs.bigmodel.cn/api-reference/模型-api/查询异步结果
  - CogVideoX-3（默认）：5s/10s，fps 30/60，分辨率见 OpenAPI `CogVideoX3Request`
    https://docs.bigmodel.cn/cn/guide/models/video-generation/cogvideox-3
  - CogVideoX-2（可选）：0.5 元/次固定约 6 秒，尺寸枚举与 v3 略有差异
    https://docs.bigmodel.cn/cn/guide/models/video-generation/cogvideox-2
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, Literal, Optional

import httpx

from ..config import Settings, get_settings


log = logging.getLogger("seecript.t2v")


# --------------------------------------------------------------------------
# Constants — extracted to avoid magic numbers (per project rule).
# --------------------------------------------------------------------------
HTTP_OK = 200

# Zhipu task_status enum (server-side strings; we map to our own TaskStatus below
# so changes upstream don't ripple into the rest of the codebase).
_ZHIPU_STATUS_PROCESSING = "PROCESSING"
_ZHIPU_STATUS_SUCCESS = "SUCCESS"
_ZHIPU_STATUS_FAIL = "FAIL"

# Mock-mode timing: how long a fake video task takes to "complete" so the UI can
# render a real progress state. 8 seconds = enough for a user to read the loading
# tip without making demos feel sluggish. Tweak via T2V_MOCK_DURATION_SECONDS env.
DEFAULT_MOCK_DURATION_SECONDS = 8.0

# Mock-mode sample asset — a tiny placeholder so the <video> tag has something
# real to play. We point at Zhipu's CDN sample (publicly hosted, used in their
# own docs) so the mock works zero-config and on any browser.
DEFAULT_MOCK_VIDEO_URL = "https://aigc-files.bigmodel.cn/api/cogvideo/_sample.mp4"
DEFAULT_MOCK_COVER_URL = "https://cdn.bigmodel.cn/markdown/1752547801491cogvideo4.png"


# Public-facing task status — stable across providers, even if Zhipu adds
# new states later. Frontend only sees these values.
TaskStatus = Literal["pending", "succeeded", "failed"]


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------
class T2VError(RuntimeError):
    """Wraps any T2V-related failure with a stable code so the API layer can map to HTTP."""

    def __init__(
        self,
        message: str,
        code: str = "T2V_ERROR",
        upstream_status: Optional[int] = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.upstream_status = upstream_status


# --------------------------------------------------------------------------
# Domain types
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class SubmitResult:
    """Result of a submit() call. Frozen so business code can't accidentally mutate it."""

    task_id: str
    request_id: str
    model: str


@dataclass(frozen=True)
class QueryResult:
    """Result of a query() call.

    Why a separate class (vs returning a raw dict): the route layer needs typed
    access for Pydantic serialization, and we don't want to leak Zhipu-specific
    field shapes (e.g. video_result[0].url) up the stack — the adapter is the
    single point of translation.
    """

    task_id: str
    status: TaskStatus
    model: str
    video_url: Optional[str] = None
    cover_image_url: Optional[str] = None
    fail_reason: Optional[str] = None


# --------------------------------------------------------------------------
# Abstract interface
# --------------------------------------------------------------------------
class T2VClient(ABC):
    """The thin contract every T2V adapter implements."""

    name: str = "abstract"

    @abstractmethod
    async def submit(
        self,
        prompt: str,
        *,
        size: str,
        quality: Literal["speed", "quality"] = "speed",
        with_audio: bool = False,
        user_id: str,
        duration_seconds: Optional[int] = None,
    ) -> SubmitResult:
        """Submit a generation task. Returns immediately with task_id.

        Args:
          prompt:    Text description (≤ 500 chars enforced by routers/schemas).
          size:      "WxH" string，须与所选模型 OpenAPI 枚举一致（v3 默认 720x1280）。
          quality:   "speed" (~30s) or "quality" (~60-120s). Default speed.
          with_audio: If True, model adds AI-generated audio track. Default False
                     because creators usually layer their own narration/BGM.
          user_id:   End-user marker (per Zhipu API spec, 6-128 chars). Required
                     for moderation traceability.
          duration_seconds: When 5 or 10 and model is cogvideox-3, overrides Settings
                     duration; otherwise None uses config default.
        """

    @abstractmethod
    async def query(self, task_id: str) -> QueryResult:
        """Poll a task. Returns the latest status; never raises on PROCESSING.

        Raises T2VError only on transport failure or upstream error responses.
        """


# --------------------------------------------------------------------------
# Mock implementation (zero-config, time-progressive)
# --------------------------------------------------------------------------
@dataclass
class _MockTask:
    """Internal state for a single mock task."""

    task_id: str
    request_id: str
    model: str
    started_at: float = field(default_factory=time.time)


class MockT2VClient(T2VClient):
    """In-memory mock — submit returns a UUID, query returns PROCESSING for a few seconds, then SUCCESS.

    Why we don't just always return SUCCESS:
      The frontend's poll loop / progress UI is the riskiest part of v0.9. A mock
      that flips immediately would hide bugs in the polling state machine. We
      keep ~8s of fake "PROCESSING" so manual testing exercises the real loop.

    Concurrency note: The dict is keyed by task_id (UUID), so simultaneous tasks
    don't collide. We don't bother with locks because tasks only ever transition
    PROCESSING → SUCCESS — no contention possible.
    """

    name = "mock"

    def __init__(self, mock_duration_seconds: float = DEFAULT_MOCK_DURATION_SECONDS) -> None:
        self._tasks: Dict[str, _MockTask] = {}
        self._duration = mock_duration_seconds

    async def submit(
        self,
        prompt: str,
        *,
        size: str,
        quality: Literal["speed", "quality"] = "speed",
        with_audio: bool = False,
        user_id: str,
        duration_seconds: Optional[int] = None,
    ) -> SubmitResult:
        # Simulate network latency so frontend's submit-button spinner is visible.
        await asyncio.sleep(0.4)

        task_id = f"mock-{uuid.uuid4().hex[:16]}"
        request_id = f"req-{uuid.uuid4().hex[:12]}"
        self._tasks[task_id] = _MockTask(
            task_id=task_id,
            request_id=request_id,
            model="cogvideox-2-mock",
        )
        log.info(
            "mock t2v submit | task_id=%s | size=%s | quality=%s | prompt_len=%d | dur=%s",
            task_id, size, quality, len(prompt), duration_seconds,
        )
        return SubmitResult(task_id=task_id, request_id=request_id, model="cogvideox-2-mock")

    async def query(self, task_id: str) -> QueryResult:
        await asyncio.sleep(0.05)  # tiny latency, mostly so async behavior matches real path
        task = self._tasks.get(task_id)
        if task is None:
            raise T2VError(f"mock task not found: {task_id}", code="T2V_TASK_NOT_FOUND")

        elapsed = time.time() - task.started_at
        if elapsed < self._duration:
            return QueryResult(task_id=task_id, status="pending", model=task.model)

        return QueryResult(
            task_id=task_id,
            status="succeeded",
            model=task.model,
            video_url=DEFAULT_MOCK_VIDEO_URL,
            cover_image_url=DEFAULT_MOCK_COVER_URL,
        )


# --------------------------------------------------------------------------
# Zhipu CogVideoX（cogvideox-3 / cogvideox-2 / flash）实现
# --------------------------------------------------------------------------
class ZhipuT2VClient(T2VClient):
    """Calls Zhipu Bigmodel async video-generation API.

    Auth: Bearer token (api_key). Same auth scheme as Zhipu's chat API, so a
    single Zhipu account can later power both LLM + T2V — if you decide to
    consolidate providers, only one key needs rotating.
    """

    name = "zhipu"

    def __init__(self, settings: Settings) -> None:
        if not settings.zhipu_api_key:
            raise T2VError(
                "ZHIPU_API_KEY is empty but T2V_PROVIDER=zhipu. "
                "Set the key in server/.env or switch T2V_PROVIDER=mock.",
                code="T2V_NO_KEY",
            )
        self._api_key = settings.zhipu_api_key
        self._base_url = settings.zhipu_base_url.rstrip("/")
        self._model = settings.zhipu_video_model
        self._timeout = settings.t2v_timeout_seconds
        self._fps = settings.zhipu_video_fps
        self._duration = settings.zhipu_video_duration

    # ----- public API -----
    async def submit(
        self,
        prompt: str,
        *,
        size: str,
        quality: Literal["speed", "quality"] = "speed",
        with_audio: bool = False,
        user_id: str,
        duration_seconds: Optional[int] = None,
    ) -> SubmitResult:
        request_id = f"seecript-{uuid.uuid4().hex}"
        url = f"{self._base_url}/videos/generations"
        # OpenAPI：cogvideox-3 支持 fps + duration（5/10）；cogvideox-2 不支持这两项，
        # 传入会 400 —— 故按模型名分支注入。
        payload: Dict[str, Any] = {
            "model": self._model,
            "prompt": prompt,
            "quality": quality,
            "with_audio": with_audio,
            "size": size,
            "request_id": request_id,
            "user_id": user_id,
        }
        if "cogvideox-3" in (self._model or "").lower():
            payload["fps"] = self._fps
            eff_dur = self._duration
            if duration_seconds in (5, 10):
                eff_dur = duration_seconds
            payload["duration"] = eff_dur

        started = time.perf_counter()
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(url, headers=self._headers(), json=payload)
        except httpx.TimeoutException as e:
            raise T2VError(
                f"智谱视频生成请求超时（{self._timeout}s）", code="T2V_TIMEOUT",
            ) from e
        except httpx.HTTPError as e:
            raise T2VError(f"智谱视频生成网络错误：{e}", code="T2V_NETWORK") from e

        elapsed_ms = int((time.perf_counter() - started) * 1000)
        log.info(
            "zhipu submit | http=%d | %dms | request_id=%s",
            resp.status_code, elapsed_ms, request_id,
        )
        self._raise_for_status(resp)

        data = self._safe_json(resp)
        task_id = data.get("id")
        if not isinstance(task_id, str) or not task_id:
            raise T2VError(
                f"智谱响应缺少 id 字段：{str(data)[:300]}", code="T2V_BAD_RESPONSE",
            )

        return SubmitResult(
            task_id=task_id,
            request_id=data.get("request_id") or request_id,
            model=data.get("model") or self._model,
        )

    async def query(self, task_id: str) -> QueryResult:
        if not task_id or not isinstance(task_id, str):
            raise T2VError("task_id 不能为空", code="T2V_BAD_REQUEST")

        url = f"{self._base_url}/async-result/{task_id}"
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.get(url, headers=self._headers())
        except httpx.TimeoutException as e:
            raise T2VError(
                f"智谱查询超时（{self._timeout}s）", code="T2V_TIMEOUT",
            ) from e
        except httpx.HTTPError as e:
            raise T2VError(f"智谱查询网络错误：{e}", code="T2V_NETWORK") from e

        # Zhipu can return 404 if the task_id doesn't exist or has been GC'd
        # (results are cached for 24h). We surface this distinctly so the route
        # layer maps it to 404 not 502.
        if resp.status_code == 404:
            raise T2VError(
                f"任务 {task_id} 不存在或结果已过期（智谱默认仅缓存 24 小时）",
                code="T2V_TASK_NOT_FOUND",
                upstream_status=404,
            )
        self._raise_for_status(resp)

        data = self._safe_json(resp)
        upstream_status = (data.get("task_status") or "").upper()

        if upstream_status == _ZHIPU_STATUS_PROCESSING:
            return QueryResult(
                task_id=task_id,
                status="pending",
                model=data.get("model") or self._model,
            )

        if upstream_status == _ZHIPU_STATUS_SUCCESS:
            video_result = data.get("video_result") or []
            first = video_result[0] if video_result and isinstance(video_result, list) else {}
            return QueryResult(
                task_id=task_id,
                status="succeeded",
                model=data.get("model") or self._model,
                video_url=first.get("url"),
                cover_image_url=first.get("cover_image_url"),
            )

        if upstream_status == _ZHIPU_STATUS_FAIL:
            # Zhipu doesn't return structured fail reasons in the public schema;
            # surface the whole payload for diagnosis (truncated to keep logs sane).
            return QueryResult(
                task_id=task_id,
                status="failed",
                model=data.get("model") or self._model,
                fail_reason=str(data)[:300],
            )

        # Unexpected status — treat as failure but log loudly so we can patch the
        # status mapping when Zhipu adds new states.
        log.error("zhipu query | unexpected task_status=%r | task_id=%s", upstream_status, task_id)
        return QueryResult(
            task_id=task_id,
            status="failed",
            model=data.get("model") or self._model,
            fail_reason=f"未知状态：{upstream_status}",
        )

    # ----- helpers -----
    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

    @staticmethod
    def _raise_for_status(resp: httpx.Response) -> None:
        """Convert HTTP-level errors into typed T2VError.

        We don't use httpx's raise_for_status() because we want a single error
        type at the boundary (T2VError) — that's the contract the routers
        depend on (LSP).
        """
        if resp.status_code == HTTP_OK:
            return
        snippet = resp.text[:300]
        raise T2VError(
            f"智谱 HTTP {resp.status_code}: {snippet}",
            code=f"T2V_HTTP_{resp.status_code}",
            upstream_status=resp.status_code,
        )

    @staticmethod
    def _safe_json(resp: httpx.Response) -> Dict[str, Any]:
        try:
            data = resp.json()
        except (ValueError, TypeError) as e:
            raise T2VError(
                f"智谱响应不是合法 JSON：{resp.text[:300]}", code="T2V_BAD_JSON",
            ) from e
        if not isinstance(data, dict):
            raise T2VError(
                f"智谱响应顶层不是对象：{str(data)[:300]}", code="T2V_BAD_JSON",
            )
        return data


# --------------------------------------------------------------------------
# Factory
# --------------------------------------------------------------------------
_PROVIDERS = {
    "mock": MockT2VClient,
    "zhipu": ZhipuT2VClient,
}

# Module-level singleton for the mock client so its in-memory task store
# survives across requests (each route call does NOT instantiate a new client).
_mock_singleton: Optional[MockT2VClient] = None


def get_t2v_client(settings: Optional[Settings] = None) -> T2VClient:
    """Return the configured T2V client. Falls back to mock if key missing."""
    global _mock_singleton
    s = settings or get_settings()
    provider = s.t2v_provider
    if provider == "zhipu" and not s.zhipu_api_key:
        log.warning("T2V_PROVIDER=zhipu but ZHIPU_API_KEY is empty -> using mock")
        provider = "mock"
    if provider == "mock":
        if _mock_singleton is None:
            _mock_singleton = MockT2VClient(s.t2v_mock_duration_seconds)
        return _mock_singleton
    if provider == "zhipu":
        return ZhipuT2VClient(s)
    log.warning("Unknown T2V_PROVIDER=%r -> using mock", provider)
    if _mock_singleton is None:
        _mock_singleton = MockT2VClient(s.t2v_mock_duration_seconds)
    return _mock_singleton
