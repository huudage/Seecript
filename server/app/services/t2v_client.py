"""T2V (Text-to-Video) client — 用于长视频首尾帧扩展。

接ARCHITECTURE 阶段 5：`doubao-seedance-1.0-pro` 图生视频-首尾帧模式，
单段 2-12s，把前段的尾帧作为下一段首帧依次拼出 30-60s。

Providers：
- `MockT2VClient`         离线 fixture，submit 立即返回 task_id；query 按 wall-clock 假装进度。
- `DoubaoArkT2VClient`    火山方舟 Seedance 提交/查询两段式 API。

接口对齐 ARCHITECTURE §5.2：
- `submit(prompt, first_frame, last_frame=None, duration_seconds, size)` → SubmitResult
- `query(task_id)` → QueryResult
"""
from __future__ import annotations

import logging
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Literal, Optional

import httpx

from ..config import Settings, get_settings

log = logging.getLogger("seecript.t2v")


class T2VError(RuntimeError):
    def __init__(self, message: str, code: str = "T2V_ERROR", upstream_status: Optional[int] = None) -> None:
        super().__init__(message)
        self.code = code
        self.upstream_status = upstream_status


T2VStatus = Literal["pending", "succeeded", "failed"]


@dataclass
class SubmitResult:
    task_id: str
    provider: str
    elapsed_ms: int


@dataclass
class QueryResult:
    task_id: str
    status: T2VStatus
    provider: str
    video_url: Optional[str] = None
    cover_url: Optional[str] = None
    fail_reason: Optional[str] = None
    elapsed_ms: int = 0


class T2VClient(ABC):
    name: str = "abstract"

    @abstractmethod
    async def submit(
        self,
        prompt: str,
        *,
        first_frame: Optional[str] = None,
        last_frame: Optional[str] = None,
        duration_seconds: int = 5,
        size: str = "1280x720",
    ) -> SubmitResult: ...

    @abstractmethod
    async def query(self, task_id: str) -> QueryResult: ...


class MockT2VClient(T2VClient):
    """Submit 立即返回；query 按 wall-clock 假装从 pending 跳到 succeeded。"""

    name = "mock"

    def __init__(self, mock_duration_seconds: float = 8.0) -> None:
        self._submit_times: dict[str, float] = {}
        self._mock_duration = mock_duration_seconds

    async def submit(
        self,
        prompt: str,
        *,
        first_frame: Optional[str] = None,
        last_frame: Optional[str] = None,
        duration_seconds: int = 5,
        size: str = "1280x720",
    ) -> SubmitResult:
        started = time.perf_counter()
        task_id = f"mock-t2v-{uuid.uuid4().hex[:10]}"
        self._submit_times[task_id] = time.time()
        return SubmitResult(task_id=task_id, provider=self.name,
                            elapsed_ms=int((time.perf_counter() - started) * 1000))

    async def query(self, task_id: str) -> QueryResult:
        started = time.perf_counter()
        submitted = self._submit_times.get(task_id)
        if submitted is None:
            return QueryResult(task_id=task_id, status="failed", provider=self.name,
                               fail_reason="task not found",
                               elapsed_ms=int((time.perf_counter() - started) * 1000))
        if time.time() - submitted < self._mock_duration:
            return QueryResult(task_id=task_id, status="pending", provider=self.name,
                               elapsed_ms=int((time.perf_counter() - started) * 1000))
        return QueryResult(
            task_id=task_id,
            status="succeeded",
            provider=self.name,
            video_url=f"/aigc/{task_id}.mp4",
            cover_url=f"/aigc/{task_id}.jpg",
            elapsed_ms=int((time.perf_counter() - started) * 1000),
        )


class DoubaoArkT2VClient(T2VClient):
    """火山方舟 Seedance 1.0 Pro。

    Submit  POST {base_url}/contents/generations/tasks
                  {"model": <endpoint_id>, "content": [{"type":"text","text":prompt},
                   {"type":"image_url","image_url":{"url":first_frame},"role":"first_frame"},
                   {"type":"image_url","image_url":{"url":last_frame},"role":"last_frame"}],
                   "duration": duration_seconds, "size": size}
            → {"id": "..."}
    Query   GET  {base_url}/contents/generations/tasks/{id}
            → {"status": "queued|running|succeeded|failed",
               "content": {"video_url": "...", "cover_url": "..."}, "error": {...}}

    注意：以上字段以方舟控制台返回为准；首尾帧 role 的具体写法在 PRD 中保留为占位，
    实际接入时按 ARK 文档微调。
    """

    name = "doubao_ark"

    def __init__(self, settings: Settings) -> None:
        if not settings.ark_api_key:
            raise T2VError("ARK_API_KEY empty but T2V_PROVIDER=doubao_ark.", code="T2V_NO_KEY")
        self._api_key = settings.ark_api_key
        self._base_url = settings.ark_base_url.rstrip("/")
        self._model = settings.ark_t2v_model
        self._timeout = settings.t2v_timeout_seconds

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"}

    async def submit(
        self,
        prompt: str,
        *,
        first_frame: Optional[str] = None,
        last_frame: Optional[str] = None,
        duration_seconds: int = 5,
        size: str = "1280x720",
    ) -> SubmitResult:
        url = f"{self._base_url}/contents/generations/tasks"
        content: list[dict] = [{"type": "text", "text": prompt}]
        if first_frame:
            content.append({"type": "image_url", "role": "first_frame",
                            "image_url": {"url": first_frame}})
        if last_frame:
            content.append({"type": "image_url", "role": "last_frame",
                            "image_url": {"url": last_frame}})
        body = {"model": self._model, "content": content,
                "duration": duration_seconds, "size": size}
        started = time.perf_counter()
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(url, headers=self._headers(), json=body)
        except httpx.TimeoutException as e:
            raise T2VError(f"seedance submit timeout {self._timeout}s", code="T2V_TIMEOUT") from e
        if resp.status_code not in (200, 201, 202):
            raise T2VError(f"seedance submit HTTP {resp.status_code}: {resp.text[:300]}",
                           code=f"T2V_HTTP_{resp.status_code}", upstream_status=resp.status_code)
        data = resp.json()
        task_id = data.get("id") or data.get("task_id")
        if not task_id:
            raise T2VError("seedance submit missing id", code="T2V_BAD_RESPONSE")
        return SubmitResult(task_id=str(task_id), provider=self.name,
                            elapsed_ms=int((time.perf_counter() - started) * 1000))

    async def query(self, task_id: str) -> QueryResult:
        url = f"{self._base_url}/contents/generations/tasks/{task_id}"
        started = time.perf_counter()
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.get(url, headers=self._headers())
        except httpx.TimeoutException as e:
            raise T2VError(f"seedance query timeout {self._timeout}s", code="T2V_TIMEOUT") from e
        if resp.status_code != 200:
            raise T2VError(f"seedance query HTTP {resp.status_code}: {resp.text[:300]}",
                           code=f"T2V_HTTP_{resp.status_code}", upstream_status=resp.status_code)
        data = resp.json()
        raw_status = (data.get("status") or "").lower()
        if raw_status in ("queued", "running", "pending"):
            status: T2VStatus = "pending"
        elif raw_status in ("succeeded", "success", "completed"):
            status = "succeeded"
        else:
            status = "failed"
        content = data.get("content") or {}
        return QueryResult(
            task_id=task_id,
            status=status,
            provider=self.name,
            video_url=content.get("video_url"),
            cover_url=content.get("cover_url"),
            fail_reason=(data.get("error") or {}).get("message") if status == "failed" else None,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
        )


def get_t2v_client(settings: Optional[Settings] = None) -> T2VClient:
    s = settings or get_settings()
    if s.t2v_provider == "doubao_ark":
        if not s.ark_api_key:
            log.warning("T2V_PROVIDER=doubao_ark but ARK_API_KEY empty -> using mock")
            return MockT2VClient(mock_duration_seconds=s.t2v_mock_duration_seconds)
        return DoubaoArkT2VClient(s)
    return MockT2VClient(mock_duration_seconds=s.t2v_mock_duration_seconds)
