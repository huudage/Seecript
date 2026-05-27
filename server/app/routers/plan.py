"""Module 5 — Plan 组装。

`POST /api/plan/build` 把『样例 manifest + 用户素材 + 缺口补全』揉成最终 Plan，
并存入 PlanStore，后续 /api/render /api/edit 通过 plan_id 拿回。

阶段 1：返回结构合法的 mock Plan，主轨 5 个 scene + 包装轨 3 个 item。
阶段 3：补 session_id 上下文 + 把 fill 结果接到 source_ref 上。
"""
from __future__ import annotations

import logging
import re
import uuid

from fastapi import APIRouter

from ..routers.library import _LIBRARY
from ..schemas import BGMConfig, PackagingItem, Plan, PlanBuildRequest, Scene
from ..services.plans import plan_store

log = logging.getLogger("seecript.plan")
router = APIRouter()


_SAMPLE_SHOT_RE = re.compile(r"sample-shot-(\d+)")


def _sample_shot_window(sample_id: str, shot_idx: int) -> tuple[float, float]:
    """根据样例的 duration_seconds + shot_count 估算第 shot_idx 个镜头的 (in_point, duration)。

    内置样例的实际 PySceneDetect 结果可能不等分，但 LibraryItem.duration / shot_count
    给的是真实平均；用这个做 fallback 让 render 切出来的片段不会全都在 t=0。
    """
    sample = next((s for s in _LIBRARY if s.id == sample_id), None)
    if sample is None or sample.shot_count <= 0:
        return (0.0, 3.0)
    avg = sample.duration_seconds / sample.shot_count
    idx = max(0, min(shot_idx, sample.shot_count - 1))
    return (idx * avg, avg)


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

    def _build_scene(
        scene_id: str,
        section: str,
        source: str,
        source_ref: str,
        timeline_start: float,
        timeline_duration: float,
        narration: str,
    ) -> Scene:
        """source="sample" 时把 in_point/out_point 写实，让 render 的 trim 真切到对应镜头。"""
        in_point = 0.0
        out_point: float | None = None
        actual_duration = timeline_duration
        if source == "sample":
            m = _SAMPLE_SHOT_RE.search(source_ref)
            if m:
                shot_idx = int(m.group(1))
                in_point, shot_duration = _sample_shot_window(req.sample_id, shot_idx)
                # 如果样例镜头本身比时间线段短：scene 时长跟着镜头走，避免拼接超出真实素材
                actual_duration = min(timeline_duration, shot_duration)
                out_point = in_point + actual_duration
        return Scene(
            scene_id=scene_id,
            section=section,  # type: ignore[arg-type]
            source=source,  # type: ignore[arg-type]
            source_ref=source_ref,
            start=timeline_start,
            duration=actual_duration,
            in_point=in_point,
            out_point=out_point,
            narration=narration,
        )

    main_track = [
        _build_scene("sc-0", "hook", src0, ref0, 0.0, 3.0, "痛点开场"),
        _build_scene("sc-1", "body", src1, ref1, 3.0, 6.0, "产品展示"),
        _build_scene("sc-2", "body", src2, ref2, 9.0, 4.0, "对比卖点"),
        _build_scene("sc-3", "body", src3, ref3, 13.0, 5.0, "样例镜头复用"),
        _build_scene("sc-4", "cta", src4, ref4, 18.0, 4.0, "点赞收藏"),
    ]

    # 计算实际总时长（按各 scene actual_duration 求和），防止 main_track 只剩 8s
    # 但 plan.duration 写死 22s 导致 seedance_chain 多扩了 14s 黑屏。
    actual_total = sum(sc.duration for sc in main_track)

    packaging_track = [
        PackagingItem(item_id="pkg-title", kind="title_bar", start=0.0, end=3.0,
                      text="痛点开场", style={"size": 64, "color": "#FFF"}),
        PackagingItem(item_id="pkg-sub-1", kind="subtitle", start=3.0, end=actual_total - 4.0,
                      text="动态字幕跟随口播", style={"size": 48, "stroke": "#000"}),
        PackagingItem(item_id="pkg-cta", kind="sticker", start=actual_total - 4.0, end=actual_total,
                      text="点赞收藏", style={"size": 56, "color": "#FFE600"}),
    ]

    plan = Plan(
        plan_id=plan_id,
        sample_id=req.sample_id,
        session_id=req.session_id,
        brief=req.brief,
        variant=req.variant,
        duration_seconds=actual_total,
        main_track=main_track,
        packaging_track=packaging_track,
        bgm=BGMConfig(track_url=None, volume=0.6),
    )
    plan_store.put(plan)
    return plan
