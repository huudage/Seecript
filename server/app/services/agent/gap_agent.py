"""缺口识别与补全 Agent。

两个核心函数：
- detect_gaps(manifest, materials) → list[Gap]
    简化版槽位匹配：按 section + slot 顺位枚举样例需求，挨个找匹配的 user material。
    匹配规则（阶段 3 简化版）：
      * 推荐 section 命中 → status=ok
      * section 不一致但媒体类型可用 → status=warn
      * 没有任何素材可用 → status=miss
    支持 3 种 video_type（marketing / editing / motion_graph）下的 9 个 section kind。
- fill_gap(gap, action, params) → FillResult
    分发到 rerank（纯 Python） / copy（LLM 文案） / aigc（Seedance T2V 短片生成）。
    aigc 路径走 doubao-seedance-1.0-pro：submit → 轮询 → 返回 task_id 作为新素材引用。

阶段 3 此版本足以驱动前端 UI；阶段 5 比赛前再做槽位匹配的真算法（cos-sim + section 推荐）。
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any

from ..llm_client import get_llm_client
from ..t2v_client import T2VError, get_t2v_client
from ...schemas import (
    FillAction,
    FillResult,
    Gap,
    Material,
    SampleManifest,
    SectionKind,
    kinds_for_video_type,
)

log = logging.getLogger("seecript.agent.gap")


_COPY_SYSTEM = (
    "你是短视频口播作者。根据『槽位需求』和『可参考素材标签』，"
    "生成一句口语化的中文口播主推文案（不超过 40 字），同时再写 2 句风格不同的备选。"
    "返回 JSON：{\"gap_fill_narration\": str, \"alternatives\": [str, str]}。"
)


# 给定 section kind 的「重要度」—— 影响 Gap.impact 字段。
# 开/收尾、痛点钩子、行动引导、视觉爆点 都算 high；铺垫主体类算 medium。
_HIGH_IMPACT_KINDS: set[str] = {"hook", "cta", "opening", "closing", "intro", "outro", "drop"}


def detect_gaps(manifest: SampleManifest, materials: list[Material]) -> list[Gap]:
    """简化版槽位匹配。每个 section 默认 2-3 个槽，挨个分配 material。

    支持 3 种 video_type：
    - marketing      hook / body / cta
    - editing        opening / climax / closing
    - motion_graph   intro / build / drop / outro
    """
    allowed_kinds = kinds_for_video_type(manifest.video_type)

    # 按 recommended_section 归类素材；不在允许枚举里的回落到主体段（每类的第二个 kind）
    fallback_kind = allowed_kinds[1] if len(allowed_kinds) >= 2 else allowed_kinds[0]
    by_section: dict[str, list[Material]] = {k: [] for k in allowed_kinds}
    for m in materials:
        rec = m.recommended_section if m.recommended_section in allowed_kinds else fallback_kind
        by_section.setdefault(rec, []).append(m)

    # shot index → thumbnail_url 反查，给 Gap.sample_thumbnail_url 用
    shot_thumb: dict[int, str | None] = {s.index: s.thumbnail_url for s in manifest.shots}

    def _section_thumb(section_kind: str, shot_indices: list[int], slot: int) -> str | None:
        """优先用该 section 的第 slot 个镜头，越界回落到首个有缩略图的镜头。"""
        if shot_indices:
            target = shot_indices[min(slot, len(shot_indices) - 1)]
            url = shot_thumb.get(target)
            if url:
                return url
            for idx in shot_indices:
                if shot_thumb.get(idx):
                    return shot_thumb[idx]
        return None

    # spillover 池：把所有"非本 section"的素材按 sort_order 拼成一个队列，
    # 跨段借用时按需 pop，避免每个 slot 都借同一条 mat-mock-001。
    # 注意：dict 在 Python 3.7+ 保留插入顺序，by_section 的 key 顺序与 allowed_kinds 一致。
    spillover_queue = [m for lst in by_section.values() for m in lst]
    spillover_used: set[str] = set()
    fallback_idx = 0  # 队列耗尽后用它轮转，不要总是回到第一条

    def _take_spillover(exclude_section: str) -> Material | None:
        nonlocal fallback_idx
        # 优先：未用过 + 非本 section
        for m in spillover_queue:
            if m.material_id in spillover_used:
                continue
            if m.recommended_section == exclude_section:
                continue
            spillover_used.add(m.material_id)
            return m
        # 全用完：在"非本 section"的候选里轮转
        candidates = [m for m in spillover_queue if m.recommended_section != exclude_section]
        if not candidates:
            candidates = spillover_queue
        if not candidates:
            return None
        pick = candidates[fallback_idx % len(candidates)]
        fallback_idx += 1
        return pick

    gaps: list[Gap] = []
    for sec in manifest.sections:
        # 简化：每 section 拿 sub-段数量 = min(3, len(shot_indices))
        slot_count = max(1, min(3, len(sec.shot_indices)))
        section_impact = "high" if sec.kind in _HIGH_IMPACT_KINDS else "medium"
        for slot in range(slot_count):
            requirement = _slot_requirement(sec.kind, slot, slot_count, manifest)
            thumb = _section_thumb(sec.kind, sec.shot_indices, slot)
            pool = by_section.get(sec.kind, [])
            if slot < len(pool):
                m = pool[slot]
                gaps.append(Gap(
                    gap_id=f"gap-{sec.kind}-{slot}",
                    section=sec.kind,
                    slot_index=slot,
                    requirement=requirement,
                    status="ok",
                    impact=section_impact,
                    matched_material_id=m.material_id,
                    note=f"匹配素材 {m.filename}",
                    sample_thumbnail_url=thumb,
                ))
            else:
                # 试图从其他 section 借（轮转，不重复占用同一条）
                spillover = _take_spillover(sec.kind)
                if spillover:
                    gaps.append(Gap(
                        gap_id=f"gap-{sec.kind}-{slot}",
                        section=sec.kind,
                        slot_index=slot,
                        requirement=requirement,
                        status="warn",
                        impact="medium",
                        matched_material_id=spillover.material_id,
                        note=f"跨段借用 {spillover.filename}，建议重排或 Seedance T2V 补全",
                        sample_thumbnail_url=thumb,
                    ))
                else:
                    gaps.append(Gap(
                        gap_id=f"gap-{sec.kind}-{slot}",
                        section=sec.kind,
                        slot_index=slot,
                        requirement=requirement,
                        status="miss",
                        impact=section_impact,
                        note="无可用素材，建议 Seedance T2V 生成",
                        sample_thumbnail_url=thumb,
                    ))
    return gaps


# 段落 kind → 槽位的语义模板。统一兜底到「主体中景」让未列出的 kind 也能给出合理描述。
_SECTION_REQUIREMENT_HINTS: dict[str, str] = {
    # marketing
    "hook": "开场 3 秒 · 痛点提问近景",
    "body": "主体演示 · 产品/对比中景",
    "cta": "收尾 · 大字幕行动引导",
    # editing
    "opening": "环境/氛围铺垫 · 空镜或慢推",
    "climax": "情绪/动作高潮 · 强构图特写",
    "closing": "余韵收尾 · 慢镜或长镜",
    # motion_graph
    "intro": "标题/Logo 入场 · 干净底",
    "build": "信息铺陈 · 图表/字段动画",
    "drop": "视觉爆点 · 快剪/形变",
    "outro": "落版收尾 · 品牌定格",
}


def _slot_requirement(section: SectionKind, slot: int, slot_count: int, manifest: SampleManifest) -> str:
    """根据 PackagingProfile + section + slot 给出该槽的语义描述。

    所有 section 都显示 `N/total` 编号（让前端清单不会出现"3 行长得一模一样"的视觉重复）；
    body/climax/build 这类多段主体多加一个 #N 强调段内顺序。
    """
    style = manifest.packaging.subtitle_style
    base = _SECTION_REQUIREMENT_HINTS.get(section, f"主体 · 演示/对比中景")
    pos = f"{slot + 1}/{slot_count}"  # 1/3、2/3、3/3，比 0-index 直觉
    if section in ("body", "climax", "build") and slot > 0:
        return f"{base} #{slot + 1} · {pos}（{style}）"
    return f"{base} · {pos}（{style}）"


async def fill_gap(gap: Gap, action: FillAction, params: dict[str, Any]) -> FillResult:
    """分发到三种动作：rerank（重排） / copy（LLM 文案） / aigc（Seedance T2V）。"""
    log.info("[gap-fill] %s action=%s", gap.gap_id, action)
    if action == "rerank":
        target = params.get("target_material_id") or f"mat-rerank-{uuid.uuid4().hex[:6]}"
        return FillResult(
            gap_id=gap.gap_id, action="rerank",
            new_material_id=target, status="ok",
            note="已重排到该槽位",
        )

    if action == "copy":
        llm = get_llm_client()
        user = (
            f"槽位需求：{gap.requirement}\n"
            f"section：{gap.section}\n"
            f"可参考素材标签：{params.get('tag_hint', '无')}\n"
            f"创作者补充：{params.get('prompt_hint', '')}"
        )
        narration = ""
        alternatives: list[str] = []
        try:
            data = await llm.complete_json(_COPY_SYSTEM, user)
            if isinstance(data, dict):
                narration = (data.get("gap_fill_narration") or "").strip()
                raw_alts = data.get("alternatives") or []
                if isinstance(raw_alts, list):
                    alternatives = [str(a).strip() for a in raw_alts if str(a).strip()][:3]
        except Exception as exc:
            log.warning("llm copy failed: %s", exc)
        return FillResult(
            gap_id=gap.gap_id, action="copy",
            narration=narration or "[fallback] 这里加一句口播，把刚才的对比强调一下。",
            alternatives=alternatives,
            status="ok", note="LLM 文案补全完成",
        )

    if action == "aigc":
        return await _fill_with_seedance(gap, params)

    return FillResult(gap_id=gap.gap_id, action=action, status="warn", note=f"未知动作：{action}")


async def _fill_with_seedance(gap: Gap, params: dict[str, Any]) -> FillResult:
    """调 Seedance T2V 生成 5-8s 短片填补槽位。

    流程：submit → 轮询 query → 返回 task_id 作为 new_material_id。
    轮询超时（默认 90s）后返回 warn + task_id，前端可基于 task_id 继续刷新或重试。
    """
    t2v = get_t2v_client()
    prompt = params.get("prompt") or f"短视频画面：{gap.requirement}"
    duration_seconds = int(params.get("duration_seconds") or 5)
    first_frame = params.get("first_frame_url")
    last_frame = params.get("last_frame_url")
    reference_images = params.get("reference_images") or None
    reference_video = params.get("reference_video_url")
    reference_audio = params.get("reference_audio_url")
    ratio = params.get("ratio") or params.get("size")  # 兼容旧 size 参数（仅 mock 忽略）
    if ratio and "x" in str(ratio):
        # 旧 size="1280x720" 显式忽略，让 client 走默认 ratio
        ratio = None
    generate_audio = params.get("generate_audio")
    watermark = params.get("watermark")
    poll_interval = float(params.get("poll_interval_seconds") or 4.0)
    max_wait = float(params.get("max_wait_seconds") or 90.0)

    try:
        submit = await t2v.submit(
            prompt=prompt,
            first_frame=first_frame,
            last_frame=last_frame,
            reference_images=reference_images,
            reference_video=reference_video,
            reference_audio=reference_audio,
            duration_seconds=duration_seconds,
            ratio=ratio,
            generate_audio=generate_audio,
            watermark=watermark,
        )
    except T2VError as exc:
        log.warning("[gap-fill] t2v submit failed: %s", exc)
        return FillResult(
            gap_id=gap.gap_id, action="aigc",
            status="warn", note=f"Seedance 提交失败：{exc}",
        )

    task_id = submit.task_id
    started = time.time()
    last_status = "pending"
    while True:
        try:
            q = await t2v.query(task_id)
        except T2VError as exc:
            log.warning("[gap-fill] t2v query failed: %s", exc)
            return FillResult(
                gap_id=gap.gap_id, action="aigc",
                new_material_id=task_id,
                status="warn",
                note=f"Seedance 查询失败：{exc}（task={task_id}）",
            )
        last_status = q.status
        if q.status == "succeeded":
            return FillResult(
                gap_id=gap.gap_id, action="aigc",
                new_material_id=task_id, status="ok",
                note=f"Seedance 生成完成（{q.provider}，{int(time.time() - started)}s）",
            )
        if q.status == "failed":
            return FillResult(
                gap_id=gap.gap_id, action="aigc",
                new_material_id=task_id, status="warn",
                note=f"Seedance 生成失败：{q.fail_reason or 'unknown'}",
            )
        if time.time() - started > max_wait:
            # 还在排队/渲染——返回 task_id 让前端继续轮询
            return FillResult(
                gap_id=gap.gap_id, action="aigc",
                new_material_id=task_id, status="warn",
                note=f"Seedance 仍在渲染（{last_status}，已 {int(time.time() - started)}s），请稍后刷新（task={task_id}）",
            )
        await asyncio.sleep(poll_interval)
