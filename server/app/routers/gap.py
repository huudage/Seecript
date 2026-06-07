"""Module 4 — 缺口识别与补全 (Gap Agent)。

`POST /api/gap/detect`  根据 plan_id（反查 sample manifest）+ session_id（反查用户素材）
                        算槽位匹配，返回 Gap[]；结果存进 GapStore，让 fill 直接 lookup。
`POST /api/gap/fill`    按 gap_id 从 GapStore 拿 Gap，分发到 rerank / copy / aigc。
`POST /api/gap/fill-all` 一键 AI 生成所有 status≠ok 的 gap；
                        aigc T2V 链式承接走串行+遇错即停，aigc_image/copy 段间独立走 asyncio.gather 并发。
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..routers.library import _LIBRARY, _load_real_manifest, _stub_manifest
from ..schemas import (
    AdaptedSection,
    AigcImageSpecRequest,
    AigcImageSpecResponse,
    AigcPromptRequest,
    AigcPromptResponse,
    AigcSeedreamRequest,
    AigcSeedreamResponse,
    AigcTailFrameRequest,
    AigcTailFrameResponse,
    CopyOutlineRequest,
    CopyOutlineResponse,
    FillResult,
    Gap,
    GapDetectRequest,
    GapFillAllRequest,
    GapFillAllResponse,
    GapFillRequest,
    Material,
    SampleManifest,
    SeedreamImage,
)
from ..services.agent.aigc_prompt_agent import generate_aigc_prompt, generate_image_specs
from ..services.agent.copy_outline_agent import generate_copy_outline
from ..services.agent.gap_agent import (
    _extract_tail_frame_data_url,
    detect_gaps,
    fill_gap,
    refresh_aigc_task,
)
from ..services.seedream_client import SeedreamError, get_seedream_client
from ..services.materials import gap_store, material_store
from ..services.plans import plan_store
from ..services.tts import TTSError, backend_name as tts_backend_name, synthesize_scene_voice
from ..services.video.aspect import aspect_for_platform, aspect_for_settings

log = logging.getLogger("seecript.gap")
router = APIRouter()


def _resolve_manifest(plan_id: str) -> SampleManifest:
    """plan_id → 第一个参考版本对应的 manifest；优先按 (sample_id, slot_id) 精确加载，
    退而求次到 _load_real_manifest，再回落 stub。

    多样例项目（plan.reference_versions 长度 > 1）gap 视图仍以第一份为参考缩略图基线，
    因为跨样例 shot 编号会重号；plan_agent 已在跨样例段把 source_shot_indices 置空。
    """
    plan = plan_store.get(plan_id)
    if plan is None:
        log.warning("[gap] plan_id=%s 未找到，回退 _LIBRARY[0]", plan_id)
        sample = _LIBRARY[0]
        return _load_real_manifest(sample.id) or _stub_manifest(sample.id, sample)
    if plan.reference_versions:
        primary = plan.reference_versions[0]
        from ..services.library import manifest_store
        precise = manifest_store.load_version(primary.sample_id, primary.slot_id)
        if precise is not None:
            return precise
        sample_id = primary.sample_id
    else:
        sample_id = _LIBRARY[0].id
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


def _resolve_materials(session_id: str | None) -> list[Material]:
    if session_id:
        items = material_store.list(session_id)
        if items:
            return sorted(items, key=lambda m: m.sort_order)
        log.info("[gap] session=%s 暂无上传素材", session_id)
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
    - ratio：未传则取 plan.settings.aspect_ratio（v2 显式字段，回落 target_platform）
    """
    out = dict(params or {})
    plan = _plan_for_gap(gap)
    if "duration_seconds" not in out and plan is not None:
        sec = next((s for s in plan.adapted_sections if s.section_id == gap.section_id), None)
        if sec and sec.duration_seconds > 0:
            out["duration_seconds"] = float(sec.duration_seconds)
    if "ratio" not in out and "size" not in out and plan is not None:
        spec = aspect_for_settings(plan.settings)
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
    """`copy / aigc / aigc_image` 任一动作 + plan.settings.voiceover_enabled=True
    → 自动调 TTS 并把 voiceover_url 回填到 FillResult + scene.voiceover_url（让 rebuild plan
    时也能用上）。

    narration 文本来源优先级：
    1. FillResult.narration（copy / 字卡路径会带）
    2. scene.narration（aigc / aigc_image 路径下，scene 已有 plan_agent 或
       /plan/{id}/regenerate-narrations 写好的口播文本）

    失败不抛——TTS 抖动不能阻断 fill 的成功语义；只在 note 里追加诊断。
    注意：synthesize_scene_voice 是同步阻塞调用，async 调用方必须用
    `await asyncio.to_thread(_maybe_auto_tts, ...)` 包一层。
    """
    if result.action not in ("copy", "aigc", "aigc_image"):
        return result
    if not result.section_id:
        return result

    plan, scene = _resolve_plan_and_scene_for_gap_by_section(result.section_id)
    if plan is None or not plan.settings.voiceover_enabled:
        return result
    if scene is None:
        return result

    # 文本：优先 result.narration（copy 路径），否则用 scene.narration
    text = (result.narration or "").strip() or (scene.narration or "").strip()
    if not text:
        return result

    # copy 路径：把新文案同步回 scene；aigc/aigc_image 路径不动 scene.narration（保留 plan 阶段定稿）
    if result.action == "copy":
        scene.narration = text
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
    log.info("[gap] auto-tts plan=%s scene=%s action=%s backend=%s chars=%d url=%s",
             plan.plan_id, scene.scene_id, result.action, tts_backend_name(), chars, url)
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


def _resolve_plan_id_for_gap(gap: Gap) -> str:
    """gap → plan_id：gap_store._by_plan 反查；找不到回退空串（trace 仍可写，仅缺指针）。"""
    try:
        with gap_store._lock:  # type: ignore[attr-defined]
            for pid, gaps in gap_store._by_plan.items():  # type: ignore[attr-defined]
                if any(g.gap_id == gap.gap_id for g in gaps):
                    return pid
    except Exception:  # noqa: BLE001
        pass
    return ""


@router.post("/gap/detect", response_model=list[Gap])
async def detect(req: GapDetectRequest) -> list[Gap]:
    # v2 起 session_id == project_id；老前端只传 session_id 时仍可工作
    pid = (req.project_id or req.session_id or "").strip() or None
    manifest = _resolve_manifest(req.plan_id)
    materials = _resolve_materials(pid)
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
    result = await asyncio.to_thread(_maybe_auto_tts, result)
    # Trace B：用户在 gap fill 上有自然语言输入时记一条。
    # - copy   ：params.prompt_hint（用户在 copy 面板填的文案要求）
    # - aigc   ：params.prompt（用户在 AIGC 面板填的 T2V prompt）
    # 失败仅 warn 不阻塞 fill 返回。
    try:
        user_input = ""
        if req.action == "copy":
            user_input = str(req.params.get("prompt_hint") or "").strip()
        elif req.action == "aigc":
            user_input = str(req.params.get("prompt") or "").strip()
        if user_input:
            from ..services.profile import DEFAULT_USER_ID, TraceB, append_trace_b
            import time as _time
            trace = TraceB(
                ts=int(_time.time()),
                project_id=gap.project_id or "__legacy",
                plan_id=_resolve_plan_id_for_gap(gap),
                user_id=DEFAULT_USER_ID,
                context="gap_fill",
                gap_id=gap.gap_id,
                section_role=gap.section,
                user_input=user_input,
                before={
                    "requirement": gap.requirement,
                    "status": gap.status,
                    "action": req.action,
                },
                after={
                    "narration": result.narration or "",
                    "alternatives": result.alternatives or [],
                    "status": result.status,
                },
            )
            append_trace_b(DEFAULT_USER_ID, trace)
    except Exception as exc:  # noqa: BLE001
        log.warning("[gap] profile.trace_b (gap_fill) write failed: %s", exc)
    return result


@router.post("/gap/fill-all", response_model=GapFillAllResponse)
async def fill_all(req: GapFillAllRequest) -> GapFillAllResponse:
    """一键补全：把 plan 下所有 status≠ok 的 gap 走 action。

    - action="aigc"：T2V 链式承接（前段尾帧 → 后段首帧），必须串行 + 遇错即停。
    - action="aigc_image" / "copy"：每段独立，**并行执行**（asyncio.gather），
      避免 4 段串行下 nginx 60s upstream 超时（即"failed to fetch"）。
      失败段写进 stopped_reason 但不中断其它并发任务。
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
    skip_set = set(req.skip_gap_ids or [])
    if skip_set:
        before = len(pending)
        pending = [g for g in pending if g.gap_id not in skip_set]
        log.info("[gap-fill-all] plan=%s skip %d gap_ids → pending %d→%d",
                 req.plan_id, len(skip_set), before, len(pending))
    if not pending:
        return GapFillAllResponse(
            plan_id=req.plan_id, fills=[],
            stopped_reason="所有缺口已 ok，无需生成",
        )

    log.info("[gap-fill-all] plan=%s action=%s pending=%d", req.plan_id, req.action, len(pending))

    template = (req.prompt_template or "").strip()

    def _build_params(gap: Gap) -> dict:
        """根据 action 类型为单个 gap 构建 fill_gap 参数。"""
        if req.action == "aigc":
            prompt = template or f"短视频画面：{gap.requirement}"
            return _inject_aigc_params(gap, {"prompt": prompt})
        if req.action == "aigc_image":
            prompt = template or f"短视频画面：{gap.requirement}"
            params: dict = {"prompt": prompt}
            section = next(
                (s for s in plan.adapted_sections if s.section_id == gap.section_id),
                None,
            )
            if section and section.shots:
                planned_subjects = [
                    (sh.subject or "").strip()
                    for sh in section.shots
                    if (sh.subject or "").strip()
                ][:4]
                if planned_subjects:
                    params["subjects"] = planned_subjects
                    params["n_shots"] = len(planned_subjects)
            ratio = (
                plan.settings.aspect_ratio
                if plan.settings and plan.settings.aspect_ratio
                else None
            )
            if ratio:
                params["ratio"] = ratio
            return params
        # copy
        existing_cards: list[dict[str, object]] = []
        if req.existing_text_cards:
            for spec in req.existing_text_cards:
                existing_cards.append(spec.model_dump())
        else:
            for sc in plan.main_track:
                if sc.text_card_spec is None:
                    continue
                if gap.section_id:
                    m = re.match(r"^sc-(\d+)$", sc.scene_id or "")
                    if m:
                        sec = next(
                            (s for s in plan.adapted_sections if s.order == int(m.group(1))), None,
                        )
                        if sec and sec.section_id == gap.section_id:
                            continue
                existing_cards.append(sc.text_card_spec.model_dump())
        log.info(
            "[gap-fill-all] copy gap=%s existing_cards=%d (from %s)",
            gap.gap_id, len(existing_cards),
            "frontend" if req.existing_text_cards else "plan.main_track",
        )
        return {
            "prompt_hint": gap.requirement,
            "existing_text_cards": existing_cards,
        }

    fills: list[FillResult] = []
    failed_gap_id: Optional[str] = None
    stopped_reason: Optional[str] = None

    if req.action == "aigc":
        # T2V 链式承接：必须串行，前段尾帧 → 后段首帧；遇错即停（之前的 break 语义）
        for gap in pending:
            params = _build_params(gap)
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
    else:
        # aigc_image / copy：段间独立 → 并行执行（return_exceptions 保证一段挂掉
        # 不会污染 gather 的其他任务；失败的 gap 仍有 fill 占位 / stopped_reason 兜底）
        async def _run_one(gap: Gap) -> tuple[Gap, FillResult | Exception]:
            try:
                r = await fill_gap(gap, req.action, _build_params(gap))
                r = await asyncio.to_thread(_maybe_auto_tts, r)
                return gap, r
            except Exception as exc:  # noqa: BLE001
                log.exception("[gap-fill-all] gap=%s action=%s raised", gap.gap_id, req.action)
                return gap, exc

        outcomes = await asyncio.gather(*[_run_one(g) for g in pending])
        for gap, outcome in outcomes:
            if isinstance(outcome, Exception):
                if failed_gap_id is None:
                    failed_gap_id = gap.gap_id
                    stopped_reason = f"生成异常：{outcome}"
                continue
            fills.append(outcome)
            if outcome.status != "ok" and failed_gap_id is None:
                failed_gap_id = gap.gap_id
                stopped_reason = f"{gap.gap_id} 失败：{outcome.note or outcome.status}"

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
    prompt, thinking = await generate_aigc_prompt(gap, plan, section, user_hint=req.hint or "")
    return AigcPromptResponse(gap_id=gap.gap_id, prompt=prompt, thinking=thinking)


@router.post("/gap/aigc-image-spec", response_model=AigcImageSpecResponse)
async def aigc_image_spec(req: AigcImageSpecRequest) -> AigcImageSpecResponse:
    """LLM 判断本段需要的参考图清单（1-3 张）：caption + Seedream prompt + ratio。

    给前端 FillAigcPanel 的 spec 阶段消费。失败由 generate_image_specs 内部兜底
    返回 1 张 ImageSpec，保证 UI 始终能渲染。
    """
    gap = gap_store.get(req.gap_id)
    if gap is None:
        raise HTTPException(
            status_code=404,
            detail=f"gap not found: {req.gap_id}（请先调用 /gap/detect）",
        )
    plan, section = _find_section_for_gap(gap)
    default_ratio = "16:9"
    if plan is not None:
        default_ratio = aspect_for_settings(plan.settings).ratio
    specs, thinking = await generate_image_specs(
        gap, plan, section,
        user_hint=req.hint or "",
        default_ratio=default_ratio,
    )
    return AigcImageSpecResponse(gap_id=gap.gap_id, specs=specs, thinking=thinking)


@router.post("/gap/copy-outline", response_model=CopyOutlineResponse)
async def copy_outline(req: CopyOutlineRequest) -> CopyOutlineResponse:
    """LLM 给出本段口播文案的写作大纲：core_message / emotional_hook / 关键词 / 字数 / 调性微调。

    前端 FillCopyPanel 的 analyzing 阶段消费——拿到 outline 后让用户调参，
    再发 /gap/fill action=copy 携带 outline 字段做强化生成。失败时 generate_copy_outline
    内部兜底返回默认 outline，保证 UI 始终可渲染。
    """
    gap = gap_store.get(req.gap_id)
    if gap is None:
        raise HTTPException(
            status_code=404,
            detail=f"gap not found: {req.gap_id}（请先调用 /gap/detect）",
        )
    plan, section = _find_section_for_gap(gap)
    outline, thinking = await generate_copy_outline(
        gap, plan, section, user_hint=req.hint or "",
    )
    return CopyOutlineResponse(gap_id=gap.gap_id, outline=outline, thinking=thinking)


@router.post("/gap/aigc-seedream", response_model=AigcSeedreamResponse)
async def aigc_seedream(req: AigcSeedreamRequest) -> AigcSeedreamResponse:
    """直调 Seedream 文生图，返回 1-N 张图片 url。

    供 FillAigcPanel 的 image 阶段消费——用户对每个 ImageSpec 可选『上传 / Seedream』，
    选 Seedream 时调本接口。url 是 ARK 临时 CDN（豆包 1h-7d 有效），下游 Seedance
    立即消费即可，本期不落盘。
    """
    try:
        results = await get_seedream_client().generate(
            req.prompt, ratio=req.ratio, n=req.n,
        )
    except SeedreamError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Seedream 失败：{exc}（code={exc.code}）",
        ) from exc
    return AigcSeedreamResponse(
        images=[
            SeedreamImage(url=r.url, width=r.width, height=r.height)
            for r in results
        ],
    )


@router.post("/gap/aigc-tail-frame", response_model=AigcTailFrameResponse)
async def aigc_tail_frame(req: AigcTailFrameRequest) -> AigcTailFrameResponse:
    """从前一段 scene 的 aigc_video_urls 末段抽尾帧，返回 base64 data URL。

    供 FillAigcPanel 『尾帧承接前段』开关消费——勾选后前端调本接口拿 data URL，
    填到 fill 的 params.first_frame_url，让 Seedance 用上一段尾帧驱动新视频。
    """
    plan = plan_store.get(req.plan_id)
    if plan is None:
        raise HTTPException(
            status_code=404,
            detail=f"plan not found: {req.plan_id}",
        )
    cur_scene = next(
        (s for s in plan.main_track if s.scene_id == req.scene_id), None,
    )
    if cur_scene is None:
        raise HTTPException(
            status_code=404,
            detail=f"scene not found: {req.scene_id}",
        )
    # main_track 的 scene_id 形如 sc-{order}；按 order 找前一段
    sorted_scenes = sorted(plan.main_track, key=lambda s: s.scene_id)
    idx = next(
        (i for i, s in enumerate(sorted_scenes) if s.scene_id == req.scene_id), -1,
    )
    if idx <= 0:
        raise HTTPException(
            status_code=400,
            detail="本段是第一段，没有可承接的前段",
        )
    prev_scene = sorted_scenes[idx - 1]
    if not prev_scene.aigc_video_urls:
        raise HTTPException(
            status_code=400,
            detail="前一段尚未补全（缺 aigc_video_urls），无法用尾帧承接",
        )
    try:
        data_url = await _extract_tail_frame_data_url(prev_scene.aigc_video_urls[-1])
    except Exception as exc:  # noqa: BLE001
        log.warning("[gap] tail-frame extract failed plan=%s scene=%s: %s",
                    req.plan_id, req.scene_id, exc)
        raise HTTPException(
            status_code=502,
            detail=f"尾帧抽取失败：{exc}",
        ) from exc
    return AigcTailFrameResponse(frame_data_url=data_url)
