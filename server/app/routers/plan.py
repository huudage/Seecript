"""Module 5 — Plan 组装。

`POST /api/plan/build`：
1. 反查样例 manifest（优先真预解析 `_load_real_manifest`，回落 `_stub_manifest`）
2. 走 `plan_agent.adapt_structure`，基于 brief + video_goal + settings 把样例骨架改编为 AdaptedSection[]
3. 按 AdaptedSection 一段对应一个 Scene 拼主轨——长度由 LLM 给的 duration_seconds 决定
4. 持久化 Plan（含 adapted_sections + video_goal + settings），供 /gap/detect、/render、/edit 复用
"""
from __future__ import annotations

import logging
import re
import uuid
from typing import Optional

from fastapi import APIRouter, HTTPException

from ..routers.library import _LIBRARY, _load_real_manifest, _stub_manifest
from ..schemas import (
    AdaptedSection,
    BGMConfig,
    ComposeSettings,
    FillResult,
    PackagingItem,
    Plan,
    PlanBuildRequest,
    SampleManifest,
    Scene,
)
from ..services.agent.plan_agent import adapt_structure
from ..services.assets import asset_store
from ..services.materials import gap_store
from ..services.plans import plan_store
from ..services.projects import project_store

log = logging.getLogger("seecript.plan")
router = APIRouter()


_SAMPLE_SHOT_RE = re.compile(r"sample-shot-(\d+)")


def _allocate_sample_windows(
    manifest: SampleManifest,
    adapted: list["AdaptedSection"],
) -> dict[str, tuple[float, float, int]]:
    """为每个 AdaptedSection 在样例视频上分配不重叠的切片窗口。

    返回 {section_id: (in_point, duration, shot_idx)}。

    背景：同一 shot 被多个 AdaptedSection 共享时（如 marketing 样例只有 2 shots，
    LLM 改编出 4 段都引用 shot 0），如果每段都用 _sample_shot_window 拿同一个
    (in_point, duration) → ffmpeg concat 后就是"同一片段复读 N 遍"，结果看起来
    就是样例本身。

    解决方案：按 source_shot_indices[0] 分组，组内段在该 shot 真实时间窗内均分。
    优先用 manifest.shots[i].start/duration（真分镜结果），manifest 不可用时
    回退到按 shot_count 均分总时长。
    """
    shot_groups: dict[int, list["AdaptedSection"]] = {}
    for sec in adapted:
        shot_idx = sec.source_shot_indices[0] if sec.source_shot_indices else 0
        shot_groups.setdefault(shot_idx, []).append(sec)

    shot_window: dict[int, tuple[float, float]] = {}
    for shot in manifest.shots:
        shot_window[shot.index] = (float(shot.start), max(0.5, float(shot.duration)))

    total = float(manifest.duration_seconds) or 30.0
    n_shots = max(1, len(manifest.shots))
    avg = total / n_shots

    out: dict[str, tuple[float, float, int]] = {}
    for shot_idx, secs in shot_groups.items():
        if shot_idx in shot_window:
            shot_start, shot_dur = shot_window[shot_idx]
        else:
            clamped = max(0, min(shot_idx, n_shots - 1))
            shot_start, shot_dur = clamped * avg, avg
        # 子窗口：shot 时长在该组段内均分，避免同一 shot 内重复切相同窗口
        sub_window = shot_dur / len(secs)
        for i, sec in enumerate(secs):
            sub_in = shot_start + i * sub_window
            target = float(sec.duration_seconds) if sec.duration_seconds > 0 else 4.0
            actual = min(target, sub_window) if sub_window > 0 else target
            out[sec.section_id] = (sub_in, actual, shot_idx)
    return out


def _resolve_manifest(sample_id: str) -> SampleManifest:
    """先尝试真预解析 manifest，没有则回落 stub。"""
    real = _load_real_manifest(sample_id)
    if real is not None:
        return real
    sample = next((s for s in _LIBRARY if s.id == sample_id), _LIBRARY[0])
    return _stub_manifest(sample.id, sample)


def _narration_from_content(content: str, *, limit: int = 60) -> str:
    """从 content_description 取首句（中英标点）作为口播种子，截断到 limit 字符。"""
    if not content:
        return ""
    text = content.strip()
    for sep in ("。", "！", "？", ".", "!", "?", "；", ";"):
        idx = text.find(sep)
        if 0 <= idx < limit:
            return text[: idx + 1]
    return text[:limit]


def _fill_section_lookup(fills: list[FillResult]) -> dict[str, FillResult]:
    """把 FillResult 按其所属 section_id 索引——多段同 role 时不再被压扁。

    路由优先级：
    1. `fill.section_id` 直接给的 —— v2 后 fill_gap 在所有路径都回填，最权威，不依赖进程内存
    2. `gap_store.get(f.gap_id).section_id` —— 兼容老 fill（无 section_id 字段）+ gap 仍在内存
    3. 都没有 → 丢弃 + warn 日志（提示后端可能重启 / fill 来自旧版本）
    """
    out: dict[str, FillResult] = {}
    dropped: list[str] = []
    for f in fills:
        if not f.new_material_id and not f.video_urls and not f.narration:
            continue
        sid = f.section_id
        if not sid:
            gap = gap_store.get(f.gap_id)
            sid = gap.section_id if (gap and gap.section_id) else None
        if sid is None:
            dropped.append(f.gap_id)
            continue
        out.setdefault(sid, f)
    if dropped:
        log.warning(
            "[plan] %d fill 因无法定位 section_id 被丢弃：%s（fill 来自旧版本或 gap_store 进程内存已失效）",
            len(dropped), dropped,
        )
    log.info("[plan] fill_by_section 路由：%d fills → %d sections（%s）",
             len(fills), len(out), list(out.keys()))
    return out


def _build_bgm_config(bgm_asset_id: Optional[str]) -> BGMConfig:
    """把 PlanBuildRequest.bgm_asset_id 解析为 BGMConfig；None / 找不到 / 未 ready → 无 BGM。

    资产层的 status=processing 直接落 None（避免渲染阶段拿到一个不存在的文件），
    前端在 SSE/loading 时会等到 ready 再让用户提交。
    """
    if not bgm_asset_id:
        return BGMConfig()
    asset = asset_store.get(bgm_asset_id)
    if asset is None:
        log.warning("[plan] bgm asset_id=%s 不存在，本次无 BGM", bgm_asset_id)
        return BGMConfig()
    if asset.kind != "bgm":
        log.warning("[plan] asset_id=%s kind=%s 不是 BGM，忽略", bgm_asset_id, asset.kind)
        return BGMConfig()
    if asset.status != "ready":
        log.warning("[plan] bgm asset_id=%s status=%s 未就绪，本次无 BGM", bgm_asset_id, asset.status)
        return BGMConfig()
    # 触发一次使用统计；不阻塞失败
    try:
        asset_store.touch(bgm_asset_id)
    except Exception as exc:  # noqa: BLE001
        log.debug("[plan] bgm touch failed: %s", exc)
    return BGMConfig(
        bgm_asset_id=bgm_asset_id,
        track_url=asset.file_url,
        volume=0.35,
        fade_in=1.5,
        fade_out=2.0,
        start_offset=0.0,
        duck_with_voice=True,
    )


@router.post("/plan/build", response_model=Plan)
async def build_plan(req: PlanBuildRequest) -> Plan:
    plan_id = f"plan-{uuid.uuid4().hex[:10]}"
    settings = req.settings or ComposeSettings()
    # v2 起 session_id == project_id；老前端只传 session_id 时仍可工作
    effective_project_id = (req.project_id or req.session_id or "").strip() or None
    log.info(
        "[plan] build plan=%s sample=%s project=%s materials=%d fills=%d variant=%s "
        "brief=%s goal=%s target_dur=%.0fs platform=%s tone=%s",
        plan_id, req.sample_id, effective_project_id,
        len(req.selected_materials), len(req.fills),
        req.variant, (req.brief or "")[:30], (req.video_goal or "")[:30],
        settings.target_duration_seconds, settings.target_platform, settings.tone,
    )

    # 1. 取样例 manifest
    manifest = _resolve_manifest(req.sample_id)

    # 2. LLM 改编段落结构（带 settings + 参考素材注入）
    adapted = await adapt_structure(
        manifest, req.brief, req.video_goal, settings,
        reference_asset_ids=req.reference_asset_ids,
    )
    if not adapted:
        adapted = [
            AdaptedSection(
                section_id=f"sec-{i}",
                role=sec.role,
                theme=sec.theme or "段落",
                content_description=f"[fallback] {sec.role} 段，沿用样例结构。",
                source_section_indices=[i],
                source_shot_indices=list(sec.shot_indices or []),
                order=i,
                duration_seconds=4.0,
            )
            for i, sec in enumerate(manifest.sections)
        ]

    log.info("[plan] adapted_sections=%d (sample_sections=%d) target_total=%.1fs",
             len(adapted), len(manifest.sections),
             sum(s.duration_seconds for s in adapted))

    # 3. 把 fills 按 section_id 索引——这是修复『多段 development 全被路由到第一段』bug 的关键
    fill_by_section = _fill_section_lookup(req.fills)
    # 预分配 sample 切片窗口：避免多段共享同 shot 时切出相同片段（→ 渲染像样例本身复读）
    sample_window_map = _allocate_sample_windows(manifest, adapted)
    material_cursor = 0

    def _pick(sec: AdaptedSection, sample_shot_idx: int) -> tuple[str, str, list[str], str | None]:
        """返回 (source, source_ref, aigc_video_urls, narration_override)。

        优先级：本段 fill（aigc / copy） > 用户素材 > 样例镜头。
        - aigc fill：source=aigc_t2v，source_ref=task_id，aigc_video_urls=video_urls
        - copy fill：source 仍走素材/样例兜底，narration 用 fill.narration 覆盖
        - 用户素材：顺位消费 selected_materials
        - 样例镜头：兜底引用 sample-shot-XX
        """
        nonlocal material_cursor
        fill = fill_by_section.get(sec.section_id)
        if fill and fill.action == "aigc" and (fill.video_urls or fill.new_material_id):
            return (
                "aigc_t2v",
                fill.new_material_id or (fill.video_urls[0] if fill.video_urls else "aigc"),
                list(fill.video_urls),
                None,
            )
        narration_override = None
        if fill and fill.action == "copy" and fill.narration:
            narration_override = fill.narration
        if material_cursor < len(req.selected_materials):
            ref = req.selected_materials[material_cursor]
            material_cursor += 1
            return ("user_material", ref, [], narration_override)
        return ("sample", f"sample-shot-{sample_shot_idx:02d}", [], narration_override)

    main_track: list[Scene] = []
    timeline_cursor = 0.0
    for sec in adapted:
        sample_shot_idx = sec.source_shot_indices[0] if sec.source_shot_indices else 0
        source, source_ref, aigc_urls, narration_override = _pick(sec, sample_shot_idx)
        target_duration = float(sec.duration_seconds) if sec.duration_seconds > 0 else 4.0

        in_point = 0.0
        out_point: float | None = None
        actual_duration = target_duration
        if source == "sample":
            # 用预分配表拿独立子窗口；窗口内 source_ref 仍写真实 shot_idx，
            # gap_agent 的缩略图反查照常工作
            win = sample_window_map.get(sec.section_id)
            if win is not None:
                in_point, actual_duration, real_shot_idx = win
                source_ref = f"sample-shot-{real_shot_idx:02d}"
                out_point = in_point + actual_duration
            else:
                # 极端兜底：用 target_duration 从头切，至少不会和别人完全一样
                in_point = 0.0
                actual_duration = min(target_duration, manifest.duration_seconds or target_duration)
                out_point = in_point + actual_duration
        elif source == "user_material":
            # user_material 也要切片（不切 ffmpeg 会把整段长视频塞进 4s scene 槽）
            in_point = 0.0
            actual_duration = target_duration
            out_point = actual_duration

        narration_text = narration_override or _narration_from_content(sec.content_description)
        scene = Scene(
            scene_id=f"sc-{sec.order}",
            section=sec.role,  # type: ignore[arg-type]
            source=source,  # type: ignore[arg-type]
            source_ref=source_ref,
            start=timeline_cursor,
            duration=actual_duration,
            in_point=in_point,
            out_point=out_point,
            narration=narration_text,
            aigc_video_urls=aigc_urls,
        )
        main_track.append(scene)
        timeline_cursor += actual_duration

    actual_total = sum(sc.duration for sc in main_track) or 1.0

    # 4. 包装轨：标题 + 每段独立字幕 + CTA
    packaging_track: list[PackagingItem] = []
    if main_track:
        opening = main_track[0]
        packaging_track.append(PackagingItem(
            item_id="pkg-title", kind="title_bar",
            start=opening.start, end=opening.start + opening.duration,
            text=adapted[0].theme or "开场",
            style={"size": 64, "color": "#FFF"},
        ))

    # 每个 Scene 烧一条字幕（用 scene.narration 而不是单条 placeholder）
    for idx, scene in enumerate(main_track):
        sub_text = (scene.narration or "").strip()
        if not sub_text:
            continue
        packaging_track.append(PackagingItem(
            item_id=f"pkg-sub-{idx}",
            kind="subtitle",
            start=scene.start,
            end=scene.start + scene.duration,
            text=sub_text,
            style={"size": 48, "stroke": "#000"},
        ))

    if len(main_track) >= 2:
        closing = main_track[-1]
        cta_text = (settings.cta or "").strip() or adapted[-1].theme or "点赞收藏"
        packaging_track.append(PackagingItem(
            item_id="pkg-cta", kind="sticker",
            start=closing.start, end=closing.start + closing.duration,
            text=cta_text,
            style={"size": 56, "color": "#FFE600"},
        ))

    plan = Plan(
        plan_id=plan_id,
        sample_id=req.sample_id,
        project_id=effective_project_id,
        session_id=effective_project_id,
        brief=req.brief,
        video_goal=req.video_goal,
        adapted_sections=adapted,
        variant=req.variant,
        duration_seconds=actual_total,
        main_track=main_track,
        packaging_track=packaging_track,
        bgm=_build_bgm_config(req.bgm_asset_id),
        settings=settings,
    )
    plan_store.put(plan)

    # 回写到 Project，让首页/项目详情能拿到 last_plan_id
    if effective_project_id:
        try:
            project_store.mark_planned(effective_project_id, plan_id)
        except Exception as exc:  # noqa: BLE001
            log.warning("[plan] mark_planned(%s, %s) 失败：%s", effective_project_id, plan_id, exc)

    # 诊断日志：主轨各 source 占比——『渲染结果与样例无异』时第一眼就能看出是不是全 fallback 到 sample
    source_counts: dict[str, int] = {}
    for sc in main_track:
        source_counts[sc.source] = source_counts.get(sc.source, 0) + 1
    log.info(
        "[plan] plan=%s 主轨 source 分布：%s（共 %d 段，总 %.1fs，fills 输入 %d/路由命中 %d）",
        plan_id, source_counts, len(main_track), actual_total,
        len(req.fills), len(fill_by_section),
    )
    return plan


@router.get("/plan/{plan_id}", response_model=Plan)
async def get_plan(plan_id: str) -> Plan:
    """Plan 详情查询。包装/编辑动作回写后，前端用它把 store 同步成最新版本。"""
    plan = plan_store.get(plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail=f"plan_id 不存在：{plan_id}")
    return plan
