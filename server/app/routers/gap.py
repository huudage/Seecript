"""Module 4 — 缺口识别与补全 (Gap Agent)。

`POST /api/gap/detect`  根据 plan_id 算槽位匹配，返回 Gap[]（含 ok/warn/miss）
`POST /api/gap/fill`    对单个缺口做 rerank / copy / aigc 补全

阶段 1：返回固定 5 个示例 gap，3 ok + 1 warn + 1 miss；fill 端 mock 一个成功结果。
阶段 3 接入真实槽位匹配算法 + LLM/Seedream。
"""
from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter

from ..schemas import FillResult, Gap, GapDetectRequest, GapFillRequest

log = logging.getLogger("seecript.gap")
router = APIRouter()


@router.post("/gap/detect", response_model=list[Gap])
async def detect_gaps(req: GapDetectRequest) -> list[Gap]:
    return [
        Gap(gap_id="gap-hook-0", section="hook", slot_index=0,
            requirement="3 秒痛点提问近景", status="ok", impact="high",
            matched_material_id="mat-mock-001", note="[mock] 用户素材 1 完美匹配"),
        Gap(gap_id="gap-body-0", section="body", slot_index=0,
            requirement="产品展示中景", status="ok", impact="medium",
            matched_material_id="mat-mock-002"),
        Gap(gap_id="gap-body-1", section="body", slot_index=1,
            requirement="使用场景实拍", status="warn", impact="medium",
            matched_material_id="mat-mock-003", note="[mock] 时长偏短，建议复用"),
        Gap(gap_id="gap-body-2", section="body", slot_index=2,
            requirement="对比效果特写", status="miss", impact="high",
            note="[mock] 无匹配素材，建议 Seedream 生成"),
        Gap(gap_id="gap-cta-0", section="cta", slot_index=0,
            requirement="收尾大字幕", status="ok", impact="low",
            matched_material_id="mat-mock-004"),
    ]


@router.post("/gap/fill", response_model=FillResult)
async def fill_gap(req: GapFillRequest) -> FillResult:
    log.info("[gap-fill] gap=%s action=%s params=%s", req.gap_id, req.action, req.params)
    if req.action == "rerank":
        return FillResult(gap_id=req.gap_id, action="rerank",
                          new_material_id=f"mat-rerank-{uuid.uuid4().hex[:6]}",
                          note="[mock] 已重排到其他槽位", status="ok")
    if req.action == "copy":
        return FillResult(gap_id=req.gap_id, action="copy",
                          narration="[mock] 这是 LLM 生成的补全口播。",
                          note="LLM 文案补全完成", status="ok")
    if req.action == "aigc":
        return FillResult(gap_id=req.gap_id, action="aigc",
                          new_material_id=f"aigc-{uuid.uuid4().hex[:8]}",
                          note="[mock] Seedream 4.0 生成完成", status="ok")
    return FillResult(gap_id=req.gap_id, action=req.action, status="warn",
                      note=f"unknown action: {req.action}")
