"""Module 4 — 缺口识别与补全 (Gap Agent)。

`POST /api/gap/detect`  根据 plan_id（反查 sample manifest）+ session_id（反查用户素材）
                        算槽位匹配，返回 Gap[]；结果存进 GapStore，让 fill 直接 lookup。
`POST /api/gap/fill`    按 gap_id 从 GapStore 拿 Gap，分发到 rerank / copy / aigc。

修复点（相对于阶段 3）：
- plan_id 不再被忽略——通过 plan_store 反查真 sample_id；
- session_id 反查 MaterialStore 拿真素材，空 session 才走 mock；
- detect 结果持久化，fill 不再重跑 detect。
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..routers.library import _LIBRARY, _stub_manifest
from ..schemas import FillResult, Gap, GapDetectRequest, GapFillRequest, Material
from ..services.agent.gap_agent import detect_gaps, fill_gap, refresh_aigc_task
from ..services.materials import gap_store, material_store
from ..services.plans import plan_store

log = logging.getLogger("seecript.gap")
router = APIRouter()


def _mock_materials() -> list[Material]:
    """session 为空时的兜底素材——保留以便没上传也能跑通 UI demo。"""
    return [
        Material(material_id="mat-mock-001", filename="hook-1.mp4", media_type="video",
                 duration_seconds=3.2, tags=["[mock] 近景", "[mock] 口播"], recommended_section="hook"),
        Material(material_id="mat-mock-002", filename="body-1.mp4", media_type="video",
                 duration_seconds=6.0, tags=["[mock] 产品", "[mock] 特写"], recommended_section="body"),
        Material(material_id="mat-mock-003", filename="body-2.mp4", media_type="video",
                 duration_seconds=5.0, tags=["[mock] 对比", "[mock] 实拍"], recommended_section="body"),
        Material(material_id="mat-mock-004", filename="cta-1.mp4", media_type="video",
                 duration_seconds=4.0, tags=["[mock] 大字幕"], recommended_section="cta"),
    ]


def _resolve_manifest(plan_id: str):
    """plan_id → 真 sample_id → manifest；找不到回退 _LIBRARY[0] 并打 warning。"""
    plan = plan_store.get(plan_id)
    if plan is None:
        log.warning("[gap] plan_id=%s 未找到，回退 _LIBRARY[0]", plan_id)
        sample = _LIBRARY[0]
        return _stub_manifest(sample.id, sample)
    sample = next((s for s in _LIBRARY if s.id == plan.sample_id), _LIBRARY[0])
    return _stub_manifest(sample.id, sample)


def _resolve_materials(session_id: str | None, allow_mock: bool) -> list[Material]:
    if session_id:
        items = material_store.list(session_id)
        if items:
            return sorted(items, key=lambda m: m.sort_order)
        log.info("[gap] session=%s 暂无上传素材", session_id)
    if allow_mock:
        return _mock_materials()
    return []


@router.post("/gap/detect", response_model=list[Gap])
async def detect(req: GapDetectRequest) -> list[Gap]:
    manifest = _resolve_manifest(req.plan_id)
    materials = _resolve_materials(req.session_id, req.allow_mock)
    gaps = detect_gaps(manifest, materials)
    gap_store.put(req.plan_id, gaps)
    return gaps


@router.post("/gap/fill", response_model=FillResult)
async def fill(req: GapFillRequest) -> FillResult:
    gap = gap_store.get(req.gap_id)
    if gap is None:
        raise HTTPException(
            status_code=404,
            detail=f"gap not found: {req.gap_id}（请先调用 /gap/detect）",
        )
    return await fill_gap(gap, req.action, req.params)


class AigcRefreshRequest(BaseModel):
    """`POST /api/gap/aigc-refresh` —— 用 task_id 再查一次 Seedance 任务状态。

    用于上次 fill 已超时返回 warn + task_id 后，前端按钮触发重查；
    避免反复重新提交，省 Seedance 配额。
    """

    gap_id: str
    task_id: str


@router.post("/gap/aigc-refresh", response_model=FillResult)
async def aigc_refresh(req: AigcRefreshRequest) -> FillResult:
    gap = gap_store.get(req.gap_id)
    if gap is None:
        raise HTTPException(
            status_code=404,
            detail=f"gap not found: {req.gap_id}（请先调用 /gap/detect）",
        )
    return await refresh_aigc_task(gap, req.task_id)
