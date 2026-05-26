"""Module 5 — Plan 组装。

`POST /api/plan/build` 把『样例 manifest + 用户素材 + 缺口补全』揉成最终 Plan，
并存入 PlanStore，后续 /api/render /api/edit 通过 plan_id 拿回。

阶段 1：返回结构合法的 mock Plan，主轨 5 个 scene + 包装轨 3 个 item。
阶段 3：补 session_id 上下文 + 把 fill 结果接到 source_ref 上。
"""
from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter

from ..schemas import BGMConfig, PackagingItem, Plan, PlanBuildRequest, Scene
from ..services.plans import plan_store

log = logging.getLogger("seecript.plan")
router = APIRouter()


@router.post("/plan/build", response_model=Plan)
async def build_plan(req: PlanBuildRequest) -> Plan:
    plan_id = f"plan-{uuid.uuid4().hex[:10]}"
    log.info("[plan] build plan=%s sample=%s materials=%d fills=%d variant=%s session=%s brief=%s",
             plan_id, req.sample_id, len(req.selected_materials), len(req.fills),
             req.variant, req.session_id, (req.brief or "")[:30])

    # 优先用 fills 里 aigc 生成的 new_material_id，其次落到用户上传材料。
    # 都没有时，回落到样例的具体镜头索引（source="sample"），让"纯文本"流程也能跑通。
    fill_by_section = {f.gap_id.split("-")[1] if "-" in f.gap_id else "body": f
                       for f in req.fills if f.new_material_id}

    def _pick(section: str, sample_shot_idx: int, idx: int = 0) -> tuple[str, str]:
        # 返回 (source, source_ref)
        fill = fill_by_section.get(section)
        if fill and fill.new_material_id:
            return ("aigc_t2v", fill.new_material_id)
        if idx < len(req.selected_materials):
            return ("user_material", req.selected_materials[idx])
        # 无素材时直接借样例镜头，避免引用不存在的 mat-mock-XXX
        return ("sample", f"sample-shot-{sample_shot_idx:02d}")

    src0, ref0 = _pick("hook", 0, 0)
    src1, ref1 = _pick("body", 3, 1)
    src2, ref2 = _pick("body", 5, 2)
    src3, ref3 = ("sample", "sample-shot-07")
    src4, ref4 = _pick("cta", 10, 3)

    main_track = [
        Scene(scene_id="sc-0", section="hook", source=src0, source_ref=ref0,  # type: ignore[arg-type]
              start=0.0, duration=3.0, narration="痛点开场"),
        Scene(scene_id="sc-1", section="body", source=src1, source_ref=ref1,  # type: ignore[arg-type]
              start=3.0, duration=6.0, narration="产品展示"),
        Scene(scene_id="sc-2", section="body", source=src2, source_ref=ref2,  # type: ignore[arg-type]
              start=9.0, duration=4.0, narration="对比卖点"),
        Scene(scene_id="sc-3", section="body", source=src3, source_ref=ref3,  # type: ignore[arg-type]
              start=13.0, duration=5.0, narration="样例镜头复用"),
        Scene(scene_id="sc-4", section="cta", source=src4, source_ref=ref4,  # type: ignore[arg-type]
              start=18.0, duration=4.0, narration="点赞收藏"),
    ]

    packaging_track = [
        PackagingItem(item_id="pkg-title", kind="title_bar", start=0.0, end=3.0,
                      text="痛点开场", style={"size": 64, "color": "#FFF"}),
        PackagingItem(item_id="pkg-sub-1", kind="subtitle", start=3.0, end=18.0,
                      text="动态字幕跟随口播", style={"size": 48, "stroke": "#000"}),
        PackagingItem(item_id="pkg-cta", kind="sticker", start=18.0, end=22.0,
                      text="点赞收藏", style={"size": 56, "color": "#FFE600"}),
    ]

    plan = Plan(
        plan_id=plan_id,
        sample_id=req.sample_id,
        session_id=req.session_id,
        brief=req.brief,
        variant=req.variant,
        duration_seconds=22.0,
        main_track=main_track,
        packaging_track=packaging_track,
        bgm=BGMConfig(track_url=None, volume=0.6),
    )
    plan_store.put(plan)
    return plan
