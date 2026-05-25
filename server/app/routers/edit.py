"""Module 7 — 自然语言编辑。

`POST /api/edit/apply`  LLM tool calling → 改 Plan → 返回新 Plan（前端推到 undo 栈）

阶段 1：忽略 instruction，只回一个标记为「[mock] 已应用」的克隆 Plan。
阶段 3 接 LLMClient.complete_with_tools + 真实 Plan 编辑。
"""
from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter

from ..schemas import BGMConfig, EditApplyRequest, PackagingItem, Plan, Scene

log = logging.getLogger("seecript.edit")
router = APIRouter()


@router.post("/edit/apply", response_model=Plan)
async def apply_edit(req: EditApplyRequest) -> Plan:
    log.info("[edit] plan=%s instruction=%r marks=%d", req.plan_id, req.instruction, len(req.marks))
    new_plan_id = f"plan-{uuid.uuid4().hex[:10]}"
    return Plan(
        plan_id=new_plan_id,
        sample_id="sample-mock",
        variant="A",
        duration_seconds=22.0,
        main_track=[
            Scene(scene_id="sc-0", section="hook", source="user_material",
                  source_ref="mat-mock-001", start=0.0, duration=3.0,
                  narration=f"[mock 已应用指令: {req.instruction[:30]}]"),
            Scene(scene_id="sc-1", section="body", source="user_material",
                  source_ref="mat-mock-002", start=3.0, duration=14.0,
                  narration="[mock] body 段口播"),
            Scene(scene_id="sc-2", section="cta", source="user_material",
                  source_ref="mat-mock-004", start=17.0, duration=5.0,
                  narration="[mock] CTA"),
        ],
        packaging_track=[
            PackagingItem(item_id="pkg-sub", kind="subtitle", start=0.0, end=22.0,
                          text="[mock] 编辑后字幕", style={}),
        ],
        bgm=BGMConfig(volume=0.6),
    )
