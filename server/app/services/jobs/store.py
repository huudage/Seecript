"""In-memory JobStore + per-job asyncio.Queue based SSE channels.

设计要点：
- 每个 job 配一个 asyncio.Queue（容量无界，单个 job 步骤数 < 20，不会爆）。
- publish() / complete() / fail() 都把同一种结构塞进 Queue，再由 router 的 SSE generator 消费。
- 终态（succeeded/failed/cancelled）后塞 sentinel None 触发 generator 退出。
- Queue 在 Job 终态 + 所有订阅者断开后由 GC 回收；中间态不主动清理。
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import AsyncIterator, Optional

from ...schemas import Job, JobStatus, ProgressEvent

log = logging.getLogger("seecript.jobs")


@dataclass
class JobChannel:
    """单个 Job 的内部状态 + 事件队列。"""

    job: Job
    queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    # 完结后保留最近一次事件，用于 late-subscribe 客户端立即拿到终态。
    last_event: Optional[dict] = None


class JobStore:
    """进程内全局 Job 注册表。线程不安全（FastAPI 单 event loop 内调度即可）。"""

    def __init__(self) -> None:
        self._channels: dict[str, JobChannel] = {}

    # ---- lifecycle ----

    def create(self, kind: str, payload: Optional[dict] = None) -> str:
        job_id = uuid.uuid4().hex[:16]
        now = time.time()
        ch = JobChannel(
            job=Job(
                job_id=job_id,
                kind=kind,  # type: ignore[arg-type]
                status="pending",
                percent=0.0,
                created_at=now,
                updated_at=now,
                payload=payload or {},
            )
        )
        self._channels[job_id] = ch
        log.info("[%s] job created (kind=%s)", job_id, kind)
        return job_id

    def get(self, job_id: str) -> Optional[Job]:
        ch = self._channels.get(job_id)
        return ch.job if ch else None

    def find_active(self, kind: str, plan_id: str) -> Optional[str]:
        """找同 plan 仍在 pending/running 的同类 job；用于 submit 去重，避免并发重复渲染。

        返回最近创建的活跃 job_id（payload.plan_id 匹配），无则 None。
        """
        candidates = [
            ch.job for ch in self._channels.values()
            if ch.job.kind == kind
            and ch.job.status in ("pending", "running")
            and ch.job.payload.get("plan_id") == plan_id
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda j: j.created_at).job_id

    # ---- producer side ----

    def start(self, job_id: str) -> None:
        ch = self._require(job_id)
        ch.job.status = "running"
        ch.job.updated_at = time.time()

    def publish(self, job_id: str, step: str, percent: float, payload: Optional[dict] = None) -> None:
        ch = self._require(job_id)
        ch.job.status = "running"
        ch.job.percent = max(0.0, min(100.0, percent))
        ch.job.updated_at = time.time()
        event = ProgressEvent(step=step, percent=ch.job.percent, payload=payload or {})
        message = {"event": "progress", "data": event.model_dump()}
        ch.last_event = message
        ch.queue.put_nowait(message)

    def complete(self, job_id: str, payload: Optional[dict] = None) -> None:
        ch = self._require(job_id)
        ch.job.status = "succeeded"
        ch.job.percent = 100.0
        ch.job.updated_at = time.time()
        if payload:
            ch.job.payload.update(payload)
        message = {"event": "done", "data": {"job_id": job_id, "payload": ch.job.payload}}
        ch.last_event = message
        ch.queue.put_nowait(message)
        ch.queue.put_nowait(None)  # sentinel

    def fail(self, job_id: str, error: str) -> None:
        ch = self._require(job_id)
        ch.job.status = "failed"
        ch.job.error = error
        ch.job.updated_at = time.time()
        message = {"event": "error", "data": {"job_id": job_id, "detail": error}}
        ch.last_event = message
        ch.queue.put_nowait(message)
        ch.queue.put_nowait(None)

    # ---- consumer side ----

    async def subscribe(self, job_id: str) -> AsyncIterator[dict]:
        ch = self._require(job_id)
        # 已结束的 job：直接回放最后一条
        if ch.job.status in ("succeeded", "failed", "cancelled"):
            if ch.last_event:
                yield ch.last_event
            return
        while True:
            item = await ch.queue.get()
            if item is None:
                return
            yield item

    # ---- internal ----

    def _require(self, job_id: str) -> JobChannel:
        ch = self._channels.get(job_id)
        if ch is None:
            raise KeyError(f"job not found: {job_id}")
        return ch


_TERMINAL: set[JobStatus] = {"succeeded", "failed", "cancelled"}

job_store = JobStore()
