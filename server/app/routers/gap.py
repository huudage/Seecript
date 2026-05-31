"""Module 4 — 缺口识别与补全 (Gap Agent)。

`POST /api/gap/detect`  根据 plan_id（反查 sample manifest）+ session_id（反查用户素材）
                        算槽位匹配，返回 Gap[]；结果存进 GapStore，让 fill 直接 lookup。
`POST /api/gap/fill`    按 gap_id 从 GapStore 拿 Gap，分发到 rerank / copy / aigc。
`POST /api/gap/fill-all` 一键 AI 生成所有 status≠ok 的 gap（顺序执行，遇错即停）。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..routers.library import _LIBRARY, _load_real_manifest, _stub_manifest
from ..schemas import (
    AdaptedSection,
    AigcPromptRequest,
    AigcPromptResponse,
    FillResult,
    Gap,
    GapDetectRequest,
    GapFillAllRequest,
    GapFillAllResponse,
    GapFillRequest,
    Material,
    SampleManifest,
)
from ..services.agent.aigc_prompt_agent import generate_aigc_prompt
from ..services.agent.gap_agent import detect_gaps, fill_gap, refresh_aigc_task
from ..services.materials import gap_store, material_store
from ..services.plans import plan_store
from ..services.tts import TTSError, backend_name as tts_backend_name, synthesize_scene_voice
from ..services.video.aspect import aspect_for_platform

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
    """plan_id → 第一个 sample_id → manifest；优先真预解析 manifest.json，None 时才回落 stub。

    多样例项目（plan.sample_ids 长度 > 1）gap 视图仍以第一份样例为参考缩略图基线，
    因为跨样例 shot 编号会重号；plan_agent 已在跨样例段把 source_shot_indices 置空。
    """
    plan = plan_store.get(plan_id)
    if plan is None:
        log.warning("[gap] plan_id=%s 未找到，回退 _LIBRARY[0]", plan_id)
        sample = _LIBRARY[0]
        return _load_real_manifest(sample.id) or _stub_manifest(sample.id, sample)
    sample_id = plan.sample_ids[0] if plan.sample_ids else _LIBRARY[0].id
    sample = next((s for s in _LIBRARY if s.id == sample_id), _LIBRARY[0])
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


def _plan_for_gap(gap: Gap):
    """反查 gap 所属 plan。优先用 section_id 精准匹配；老 gap 缺 section_id 时回退 None。"""
    if not gap.section_id:
        return None
    for plan_id in plan_store.all_ids():
        plan = plan_store.get(plan_id)
        if not plan:
            continue
        if any(s.section_id == gap.section_id for s in plan.adapted_sections):
            return plan
    return None


def _inject_aigc_params(gap: Gap, params: dict) -> dict:
    """Seedance fill 前补齐 plan 派生参数：duration_seconds + ratio（画幅）。

    - duration_seconds：未传则取 AdaptedSection.duration_seconds
    - ratio：未传则取 plan.settings.target_platform → "9:16" / "16:9"
    """
    out = dict(params or {})
    plan = _plan_for_gap(gap)
    if "duration_seconds" not in out and plan is not None:
        sec = next((s for s in plan.adapted_sections if s.section_id == gap.section_id), None)
        if sec and sec.duration_seconds > 0:
            out["duration_seconds"] = float(sec.duration_seconds)
    if "ratio" not in out and "size" not in out and plan is not None:
        spec = aspect_for_platform(plan.settings.target_platform)
        out["ratio"] = spec.ratio
    return out


def _resolve_plan_and_scene_for_gap(gap: Gap):
    """gap.section_id → (plan, scene)；用 adapted_sections.order 对齐 main_track scene_id=`sc-{order}`。"""
    if not gap.section_id:
        return None, None
    for plan_id in plan_store.all_ids():
        plan = plan_store.get(plan_id)
        if not plan:
            continue
        sec = next((s for s in plan.adapted_sections if s.section_id == gap.section_id), None)
        if not sec:
            continue
        target_scene_id = f"sc-{sec.order}"
        scene = next((sc for sc in plan.main_track if sc.scene_id == target_scene_id), None)
        return plan, scene
    return None, None


def _maybe_auto_tts(result: FillResult) -> FillResult:
    """copy 动作 + plan.settings.voiceover_enabled=True → 自动调 TTS 并把 voiceover_url
    回填到 FillResult + scene.voiceover_url（让 rebuild plan 时也能用上）。

    失败不抛——TTS 抖动不能阻断 copy fill 的成功语义；只在 note 里追加诊断。
    注意：synthesize_scene_voice 是同步阻塞调用，async 调用方必须用
    `await asyncio.to_thread(_maybe_auto_tts, ...)` 包一层。
    """
    if result.action != "copy" or not (result.narration or "").strip():
        return result
    if not result.section_id:
        return result

    plan, scene = _resolve_plan_and_scene_for_gap_by_section(result.section_id)
    if plan is None or not plan.settings.voiceover_enabled:
        return result
    if scene is None:
        return result

    # 把 result.narration 同步到 scene.narration（让 synthesize_scene_voice 用到新文案）
    scene.narration = result.narration.strip()
    try:
        ret = synthesize_scene_voice(plan, scene.scene_id, text=None, voice=None)
    except TTSError as exc:
        log.warning("[gap] auto-tts failed gap=%s plan=%s code=%s: %s",
                    result.gap_id, plan.plan_id, exc.code, exc)
        return result.model_copy(update={
            "note": (result.note or "") + f" | TTS 失败：{exc}",
        })

    if ret is None:
        return result
    url, _truncated, chars = ret
    plan_store.put(plan)
    log.info("[gap] auto-tts plan=%s scene=%s backend=%s chars=%d url=%s",
             plan.plan_id, scene.scene_id, tts_backend_name(), chars, url)
    return result.model_copy(update={"voiceover_url": url})


def _resolve_plan_and_scene_for_gap_by_section(section_id: str):
    """section_id → (plan, scene)；遍历 plan_store 找到第一条匹配。"""
    for plan_id in plan_store.all_ids():
        p = plan_store.get(plan_id)
        if not p:
            continue
        sec = next((s for s in p.adapted_sections if s.section_id == section_id), None)
        if not sec:
            continue
        target_scene_id = f"sc-{sec.order}"
        scene = next((sc for sc in p.main_track if sc.scene_id == target_scene_id), None)
        return p, scene
    return None, None


@router.post("/gap/detect", response_model=list[Gap])
async def detect(req: GapDetectRequest) -> list[Gap]:
    # v2 起 session_id == project_id；老前端只传 session_id 时仍可工作
    pid = (req.project_id or req.session_id or "").strip() or None
    manifest = _resolve_manifest(req.plan_id)
    materials = _resolve_materials(pid, req.allow_mock)
    plan = plan_store.get(req.plan_id)
    adapted = (
        plan.adapted_sections if (plan and plan.adapted_sections) else _legacy_wrap(manifest)
    )
    gaps = detect_gaps(adapted, manifest, materials)
    # 把项目隔离键回写到每条 gap，避免 fill 链路再绕一圈反查
    project_id_for_gap = pid or (plan.project_id if plan else None)
    # gap_id 必须 plan-scoped：detect_gaps 生成的 `gap-{role}-{seq}-{slot}` 仅按段落定，
    # 跨 plan / project 会撞同名 ID。后缀 plan_id 的 hex 部分让 gap_store._by_gap_id 唯一。
    plan_suffix = req.plan_id.split("-", 1)[-1] if "-" in req.plan_id else req.plan_id
    updates: dict[str, str] = {}
    rewritten: list[Gap] = []
    for g in gaps:
        patch: dict = {"gap_id": f"{g.gap_id}-{plan_suffix}"}
        if project_id_for_gap:
            patch["project_id"] = project_id_for_gap
        rewritten.append(g.model_copy(update=patch))
    gaps = rewritten
    gap_store.put(req.plan_id, gaps)
    return gaps


@router.get("/gap", response_model=list[Gap])
async def list_gaps(plan_id: str) -> list[Gap]:
    """按 plan_id 列出该 plan 的全部缺口。前端进 Compose 时若已有 step snapshot
    携带的 plan_id，调本接口把 gaps 灌回 store。"""
    return gap_store.list_by_plan(plan_id)


@router.post("/gap/fill", response_model=FillResult)
async def fill(req: GapFillRequest) -> FillResult:
    gap = gap_store.get(req.gap_id)
    if gap is None:
        raise HTTPException(
            status_code=404,
            detail=f"gap not found: {req.gap_id}（请先调用 /gap/detect）",
        )
    params = _inject_aigc_params(gap, req.params) if req.action == "aigc" else req.params
    result = await fill_gap(gap, req.action, params)
    return await asyncio.to_thread(_maybe_auto_tts, result)


@router.post("/gap/fill-all", response_model=GapFillAllResponse)
async def fill_all(req: GapFillAllRequest) -> GapFillAllResponse:
    """一键补全：把 plan 下所有 status≠ok 的 gap 顺序走 action。

    顺序执行（aigc 链式生成依赖时序，copy 也走串行避免 LLM 配额抖动），遇错即停。
    action="aigc" 默认；action="copy" 用 gap.requirement 当 prompt_hint。
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

    log.info("[gap-fill-all] plan=%s action=%s pending=%d", req.plan_id, req.action, len(pending))

    fills: list[FillResult] = []
    failed_gap_id: Optional[str] = None
    stopped_reason: Optional[str] = None
    template = (req.prompt_template or "").strip()

    for gap in pending:
        if req.action == "aigc":
            prompt = template or f"短视频画面：{gap.requirement}"
            params = _inject_aigc_params(gap, {"prompt": prompt})
        else:
            # copy：复用 single-fill 的 prompt_hint 协议
            params = {"prompt_hint": gap.requirement}
        try:
            result = await fill_gap(gap, req.action, params)
        except Exception as exc:
            log.exception("[gap-fill-all] gap=%s action=%s raised", gap.gap_id, req.action)
            failed_gap_id = gap.gap_id
            stopped_reason = f"生成异常：{exc}"
            break
        result = await asyncio.to_thread(_maybe_auto_tts, result)
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


def _find_section_for_gap(gap: Gap):
    """暴扫 plan_store 找到 gap.section_id 对应的 (plan, AdaptedSection)；找不到回 (None, None)。"""
    if not gap.section_id:
        return None, None
    for plan_id in plan_store.all_ids():
        plan = plan_store.get(plan_id)
        if not plan:
            continue
        sec = next((s for s in plan.adapted_sections if s.section_id == gap.section_id), None)
        if sec:
            return plan, sec
    return None, None


@router.post("/gap/aigc-prompt", response_model=AigcPromptResponse)
async def aigc_prompt(req: AigcPromptRequest) -> AigcPromptResponse:
    """LLM 把段落上下文转写为一条完备的 Seedance T2V prompt 供前端预填。

    失败时不抛 500：aigc_prompt_agent 内部已兜底拼出一条保底 prompt，
    保证前端 textarea 始终有可编辑内容。
    """
    gap = gap_store.get(req.gap_id)
    if gap is None:
        raise HTTPException(
            status_code=404,
            detail=f"gap not found: {req.gap_id}（请先调用 /gap/detect）",
        )
    plan, section = _find_section_for_gap(gap)
    prompt = await generate_aigc_prompt(gap, plan, section, user_hint=req.hint or "")
    return AigcPromptResponse(gap_id=gap.gap_id, prompt=prompt)
