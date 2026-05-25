"""Module 5 — Plan 组装。

`POST /api/plan/build` 把『样例 manifest + 用户素材 + 缺口补全』揉成最终 Plan。

阶段 1：返回结构合法的 mock Plan，主轨 5 个 scene + 包装轨 3 个 item。
阶段 3 接入真实组装逻辑。
"""
from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter

from ..schemas import BGMConfig, PackagingItem, Plan, PlanBuildRequest, Scene

log = logging.getLogger("seecript.plan")
router = APIRouter()


@router.post("/plan/build", response_model=Plan)
async def build_plan(req: PlanBuildRequest) -> Plan:
    plan_id = f"plan-{uuid.uuid4().hex[:10]}"
    log.info("[plan] build plan=%s sample=%s materials=%d fills=%d variant=%s",
             plan_id, req.sample_id, len(req.selected_materials), len(req.fills), req.variant)

    main_track = [
        Scene(scene_id="sc-0", section="hook", source="user_material",
              source_ref=req.selected_materials[0] if req.selected_materials else "mat-mock-001",
              start=0.0, duration=3.0, narration="[mock] 痛点开场"),
        Scene(scene_id="sc-1", section="body", source="user_material",
              source_ref=req.selected_materials[1] if len(req.selected_materials) > 1 else "mat-mock-002",
              start=3.0, duration=6.0, narration="[mock] 产品展示"),
        Scene(scene_id="sc-2", section="body", source="aigc_t2i",
              source_ref="aigc-mock-001", start=9.0, duration=4.0,
              narration="[mock] Seedream 补全画面"),
        Scene(scene_id="sc-3", section="body", source="sample",
              source_ref="sample-shot-007", start=13.0, duration=5.0,
              narration="[mock] 样例镜头复用"),
        Scene(scene_id="sc-4", section="cta", source="user_material",
              source_ref="mat-mock-004", start=18.0, duration=4.0,
              narration="[mock] 收尾点赞"),
    ]

    packaging_track = [
        PackagingItem(item_id="pkg-title", kind="title_bar", start=0.0, end=3.0,
                      text="痛点开场", style={"size": 64, "color": "#FFF"}),
        PackagingItem(item_id="pkg-sub-1", kind="subtitle", start=3.0, end=18.0,
                      text="动态字幕跟随口播", style={"size": 48, "stroke": "#000"}),
        PackagingItem(item_id="pkg-cta", kind="sticker", start=18.0, end=22.0,
                      text="点赞收藏", style={"size": 56, "color": "#FFE600"}),
    ]

    return Plan(
        plan_id=plan_id,
        sample_id=req.sample_id,
        variant=req.variant,
        duration_seconds=22.0,
        main_track=main_track,
        packaging_track=packaging_track,
        bgm=BGMConfig(track_url=None, volume=0.6),
    )
