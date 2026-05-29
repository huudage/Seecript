"""Module 4 — 缺口识别与补全 (Gap Agent)。

`POST /api/gap/detect`  根据 plan_id（反查 sample manifest）+ session_id（反查用户素材）
                        算槽位匹配，返回 Gap[]；结果存进 GapStore，让 fill 直接 lookup。
`POST /api/gap/fill`    按 gap_id 从 GapStore 拿 Gap，分发到 rerank / copy / aigc。
`POST /api/gap/fill-all` 一键 AI 生成所有 status≠ok 的 gap（顺序执行，遇错即停）。
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..routers.library import _LIBRARY, _load_real_manifest, _stub_manifest
from ..schemas import (
    AdaptedSection,
    FillResult,
    Gap,
    GapDetectRequest,
    GapFillAllRequest,
    GapFillAllResponse,
    GapFillRequest,
    Material,
    SampleManifest,
)
from ..services.agent.gap_agent import detect_gaps, fill_gap, refresh_aigc_task
from ..services.materials import gap_store, material_store
from ..services.plans import plan_store

log = logging.getLogger("seecript.gap")
router = APIRouter()


def _mock_materials() -> list[Material]:
    """session 为空时的兜底素材——保留以便没上传也能跑通 UI demo。"""
    return [
        Material(material_id="mat-mock-001", filename="opening-1.mp4", media_type="video",
                 duration_seconds=3.2, tags=["[mock] 近景", "[mock] 口播"], recommended_section="opening"),
        Material(material_id="mat-mock-002", filename="dev-1.mp4", media_type="video",
                 duration_seconds=6.0, tags=["[mock] 产品", "[mock] 特写"], recommended_section="development"),
        Material(material_id="mat-mock-003", filename="dev-2.mp4", media_type="video",
                 duration_seconds=5.0, tags=["[mock] 对比", "[mock] 实拍"], recommended_section="development"),
        Material(material_id="mat-mock-004", filename="closing-1.mp4", media_type="video",
                 duration_seconds=4.0, tags=["[mock] 大字幕"], recommended_section="closing"),
    ]


def _resolve_manifest(plan_id: str) -> SampleManifest:
    """plan_id → 真 sample_id → manifest；优先真预解析 manifest.json，None 时才回落 stub。"""
    plan = plan_store.get(plan_id)
    if plan is None:
        log.warning("[gap] plan_id=%s 未找到，回退 _LIBRARY[0]", plan_id)
        sample = _LIBRARY[0]
        return _load_real_manifest(sample.id) or _stub_manifest(sample.id, sample)
    sample = next((s for s in _LIBRARY if s.id == plan.sample_id), _LIBRARY[0])
    return _load_real_manifest(sample.id) or _stub_manifest(sample.id, sample)


def _legacy_wrap(manifest: SampleManifest) -> list[AdaptedSection]:
    """老 plan 没有 adapted_sections——把 manifest.sections 1:1 包成 AdaptedSection 让 gap 流程能跑。"""
    out: list[AdaptedSection] = []
    for i, sec in enumerate(manifest.sections):
        out.append(AdaptedSection(
            section_id=f"sec-{i}",
            role=sec.role,
            theme=sec.theme or "段落",
            content_description=f"[legacy] {sec.role} 段，由 manifest.sections 包装。",
            source_section_indices=[i],
            source_shot_indices=list(sec.shot_indices or []),
            order=i,
            duration_seconds=4.0,
        ))
    return out


def _resolve_materials(session_id: str | None, allow_mock: bool) -> list[Material]:
    if session_id:
        items = material_store.list(session_id)
        if items:
            return sorted(items, key=lambda m: m.sort_order)
        log.info("[gap] session=%s 暂无上传素材", session_id)
    if allow_mock:
        return _mock_materials()
    return []


def _section_duration_for_gap(gap: Gap) -> Optional[float]:
    """根据 gap.section_id 反查所属 AdaptedSection.duration_seconds，找不到返回 None。"""
    if not gap.section_id:
        return None
    # 暴力扫描 plan_store——gap_store 不存 plan_id，靠 gap_id 反查 plan 麻烦，干脆遍历
    for plan_id in plan_store.all_ids():
        plan = plan_store.get(plan_id)
        if not plan:
            continue
        sec = next((s for s in plan.adapted_sections if s.section_id == gap.section_id), None)
        if sec:
            return float(sec.duration_seconds)
    return None


def _inject_section_duration(gap: Gap, params: dict) -> dict:
    """若 caller 未指定 duration_seconds，从 gap.section_id 反查 AdaptedSection 的 duration_seconds。"""
    out = dict(params or {})
    if "duration_seconds" not in out:
        dur = _section_duration_for_gap(gap)
        if dur is not None and dur > 0:
            out["duration_seconds"] = dur
    return out


@router.post("/gap/detect", response_model=list[Gap])
async def detect(req: GapDetectRequest) -> list[Gap]:
    manifest = _resolve_manifest(req.plan_id)
    materials = _resolve_materials(req.session_id, req.allow_mock)
    plan = plan_store.get(req.plan_id)
    adapted = (
        plan.adapted_sections if (plan and plan.adapted_sections) else _legacy_wrap(manifest)
    )
    gaps = detect_gaps(adapted, manifest, materials)
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
    params = _inject_section_duration(gap, req.params) if req.action == "aigc" else req.params
    return await fill_gap(gap, req.action, params)


@router.post("/gap/fill-all", response_model=GapFillAllResponse)
async def fill_all(req: GapFillAllRequest) -> GapFillAllResponse:
    """一键 AI 生成：把 plan 下所有 status≠ok 的 gap 顺序走 Seedance aigc。

    顺序执行（保证 prompt 链式生成时机正确），遇错即停（不浪费配额）。
    """
    plan = plan_store.get(req.plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail=f"plan not found: {req.plan_id}")
    all_gaps = gap_store.list_by_plan(req.plan_id)
    if not all_gaps:
        return GapFillAllResponse(
            plan_id=req.plan_id, fills=[],
            stopped_reason="该 plan 没有缺口（请先 /gap/detect）",
        )
    pending = [g for g in all_gaps if g.status != "ok"]
    if not pending:
        return GapFillAllResponse(
            plan_id=req.plan_id, fills=[],
            stopped_reason="所有缺口已 ok，无需生成",
        )

    log.info("[gap-fill-all] plan=%s pending=%d", req.plan_id, len(pending))

    fills: list[FillResult] = []
    failed_gap_id: Optional[str] = None
    stopped_reason: Optional[str] = None
    template = (req.prompt_template or "").strip()

    for gap in pending:
        prompt = template or f"短视频画面：{gap.requirement}"
        params = _inject_section_duration(gap, {"prompt": prompt})
        try:
            result = await fill_gap(gap, "aigc", params)
        except Exception as exc:
            log.exception("[gap-fill-all] gap=%s raised", gap.gap_id)
            failed_gap_id = gap.gap_id
            stopped_reason = f"生成异常：{exc}"
            break
        fills.append(result)
        if result.status != "ok":
            failed_gap_id = gap.gap_id
            stopped_reason = f"{gap.gap_id} 失败：{result.note or result.status}"
            break

    return GapFillAllResponse(
        plan_id=req.plan_id,
        fills=fills,
        failed_gap_id=failed_gap_id,
        stopped_reason=stopped_reason,
    )


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
