"""Module 4 — 缺口识别与补全 (Gap Agent)。

`POST /api/gap/detect`  根据 plan_id 算槽位匹配，返回 Gap[]（含 ok/warn/miss）
`POST /api/gap/fill`    对单个缺口做 rerank / copy / aigc 补全

阶段 3 现状：detect 走简化匹配（从 routers/library 的内置样例 manifest 取 sections，
配套 mock 素材列表），fill 走真 LLM/T2I（mock 模式下都会回落到 fixture）。
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException

from ..routers.library import _LIBRARY, _stub_manifest
from ..schemas import FillResult, Gap, GapDetectRequest, GapFillRequest, Material
from ..services.agent.gap_agent import detect_gaps, fill_gap

log = logging.getLogger("seecript.gap")
router = APIRouter()


def _mock_materials() -> list[Material]:
    """阶段 3 还没接 session_id → materials 持久化，先用 mock 素材跑通 UI。"""
    return [
        Material(material_id="mat-mock-001", filename="hook-1.mp4", media_type="video",
                 duration_seconds=3.2, tags=["近景", "口播"], recommended_section="hook"),
        Material(material_id="mat-mock-002", filename="body-1.mp4", media_type="video",
                 duration_seconds=6.0, tags=["产品", "特写"], recommended_section="body"),
        Material(material_id="mat-mock-003", filename="body-2.mp4", media_type="video",
                 duration_seconds=5.0, tags=["对比", "实拍"], recommended_section="body"),
        Material(material_id="mat-mock-004", filename="cta-1.mp4", media_type="video",
                 duration_seconds=4.0, tags=["大字幕"], recommended_section="cta"),
    ]


@router.post("/gap/detect", response_model=list[Gap])
async def detect(req: GapDetectRequest) -> list[Gap]:
    # 简化：plan_id 暂未与 manifest 绑定，先固定取第一个内置样例。
    sample = _LIBRARY[0]
    manifest = _stub_manifest(sample.id, sample)
    return detect_gaps(manifest, _mock_materials())


@router.post("/gap/fill", response_model=FillResult)
async def fill(req: GapFillRequest) -> FillResult:
    # 阶段 3：用占位 Gap，下一阶段把 detect 结果存起来再按 gap_id lookup。
    sample = _LIBRARY[0]
    manifest = _stub_manifest(sample.id, sample)
    gaps = detect_gaps(manifest, _mock_materials())
    gap = next((g for g in gaps if g.gap_id == req.gap_id), None)
    if gap is None:
        raise HTTPException(status_code=404, detail=f"gap not found: {req.gap_id}")
    return await fill_gap(gap, req.action, req.params)
