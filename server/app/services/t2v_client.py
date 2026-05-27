"""T2V (Text-to-Video) client — Seedance 2.0 多模态视频生成。

接 ARCHITECTURE 阶段 5：默认 `doubao-seedance-2-0-fast-260128`（fast 变体，480p/720p、
4-15s、低延迟低成本，适合 demo / 高频迭代）；如需 1080p 或更高保真切回标准版
`doubao-seedance-2-0-260128`。两者请求体结构完全相同，差别只在模型名。
支持 prompt + 多张 reference_image + reference_video + reference_audio + generate_audio。
对外保留 first_frame / last_frame 旧字段（自动归入 reference_images），
让 seedance_chain.py 的首尾帧拼接逻辑无需改写。

Providers：
- `MockT2VClient`         离线 fixture，submit 立即返回 task_id；query 按 wall-clock 假装进度。
- `DoubaoArkT2VClient`    火山方舟 Seedance 2.0 提交/查询两段式 API。

请求 body 字段以 Volc Ark Seedance 2.0 控制台返回为准：
    POST /contents/generations/tasks
    {
      "model": "doubao-seedance-2-0-fast-260128",
      "content": [
        {"type": "text",      "text": "..."},
        {"type": "image_url", "image_url": {"url": "..."}, "role": "reference_image"},
        {"type": "video_url", "video_url": {"url": "..."}, "role": "reference_video"},
        {"type": "audio_url", "audio_url": {"url": "..."}, "role": "reference_audio"}
      ],
      "ratio": "16:9",
      "duration": 5,
      "generate_audio": false,
      "watermark": false
    }
"""
from __future__ import annotations

import logging
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import List, Literal, Optional

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
        reference_images: Optional[List[str]] = None,
        reference_video: Optional[str] = None,
        reference_audio: Optional[str] = None,
        duration_seconds: int = 5,
        ratio: Optional[str] = None,
        generate_audio: Optional[bool] = None,
        watermark: Optional[bool] = None,
    ) -> SubmitResult: ...

    @abstractmethod
    async def query(self, task_id: str) -> QueryResult: ...


def _merge_reference_images(
    first_frame: Optional[str],
    last_frame: Optional[str],
    reference_images: Optional[List[str]],
) -> List[dict]:
    """归一化所有图像引用 → [{url, role}] 列表，保持原序：first_frame → refs → last_frame。

    旧调用方（gap_agent / seedance_chain）只传 first_frame/last_frame；
    新调用方可以直接传 reference_images 多图。
    """
    items: list[dict] = []
    if first_frame:
        items.append({"url": first_frame, "role": "first_frame"})
    for url in (reference_images or []):
        items.append({"url": url, "role": "reference_image"})
    if last_frame:
        items.append({"url": last_frame, "role": "last_frame"})
    return items


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
        reference_images: Optional[List[str]] = None,
        reference_video: Optional[str] = None,
        reference_audio: Optional[str] = None,
        duration_seconds: int = 5,
        ratio: Optional[str] = None,
        generate_audio: Optional[bool] = None,
        watermark: Optional[bool] = None,
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
    """火山方舟 Seedance 2.0。

    Submit  POST {base_url}/contents/generations/tasks
                  body 见模块顶部 docstring。
            → {"id": "cgt-..."}
    Query   GET  {base_url}/contents/generations/tasks/{id}
            → {"status": "queued|running|succeeded|failed",
               "content": {"video_url": "...", "cover_url": "..."},
               "error": {"code": "...", "message": "..."}}

    注意：Seedance 2.0 拒绝 duration<5；纯文本提交不需要任何 image_url；
    带 reference_audio 时大概率要把 generate_audio=true，否则音轨被丢弃。
    """

    name = "doubao_ark"

    def __init__(self, settings: Settings) -> None:
        api_key = settings.t2v_api_key
        if not api_key:
            raise T2VError(
                "ARK_T2V_API_KEY / ARK_API_KEY 都为空，但 T2V_PROVIDER=doubao_ark。",
                code="T2V_NO_KEY",
            )
        self._api_key = api_key
        self._base_url = settings.ark_base_url.rstrip("/")
        self._model = settings.ark_t2v_model
        self._timeout = settings.t2v_timeout_seconds
        self._default_ratio = settings.t2v_default_ratio
        self._default_generate_audio = settings.t2v_generate_audio
        self._default_watermark = settings.t2v_watermark

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"}

    async def submit(
        self,
        prompt: str,
        *,
        first_frame: Optional[str] = None,
        last_frame: Optional[str] = None,
        reference_images: Optional[List[str]] = None,
        reference_video: Optional[str] = None,
        reference_audio: Optional[str] = None,
        duration_seconds: int = 5,
        ratio: Optional[str] = None,
        generate_audio: Optional[bool] = None,
        watermark: Optional[bool] = None,
    ) -> SubmitResult:
        url = f"{self._base_url}/contents/generations/tasks"
        content: list[dict] = [{"type": "text", "text": prompt}]
        for img in _merge_reference_images(first_frame, last_frame, reference_images):
            content.append({
                "type": "image_url",
                "role": img["role"],
                "image_url": {"url": img["url"]},
            })
        if reference_video:
            content.append({
                "type": "video_url",
                "role": "reference_video",
                "video_url": {"url": reference_video},
            })
        if reference_audio:
            content.append({
                "type": "audio_url",
                "role": "reference_audio",
                "audio_url": {"url": reference_audio},
            })

        body: dict = {
            "model": self._model,
            "content": content,
            "ratio": ratio or self._default_ratio,
            "duration": int(duration_seconds),
            "generate_audio": (
                self._default_generate_audio if generate_audio is None else bool(generate_audio)
            ),
            "watermark": (
                self._default_watermark if watermark is None else bool(watermark)
            ),
        }
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
        if not s.t2v_api_key:
            log.warning("T2V_PROVIDER=doubao_ark but no T2V/ARK key set -> using mock")
            return MockT2VClient(mock_duration_seconds=s.t2v_mock_duration_seconds)
        return DoubaoArkT2VClient(s)
    return MockT2VClient(mock_duration_seconds=s.t2v_mock_duration_seconds)
