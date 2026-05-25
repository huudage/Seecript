"""In-memory Job orchestration + SSE channels.

为什么是『内存』JobStore：
- 单进程 FastAPI + BackgroundTasks 足够覆盖比赛 demo 场景（拆解 ~30s、渲染 ~2min）。
- 不引 Celery/Redis，避免额外运维成本。
- 跨进程/重启不持久化——这是 trade-off，比赛后再考虑外部队列。

公开 API：
    from .jobs import job_store, JobChannel
    job_id = job_store.create("decompose", payload={...})
    job_store.publish(job_id, step="scene_detect", percent=10.0, payload={"shots": 12})
    job_store.complete(job_id, payload={"manifest": ...})
    async for event in job_store.subscribe(job_id):
        yield event
"""
from .store import JobChannel, JobStore, job_store

__all__ = ["JobStore", "JobChannel", "job_store"]
