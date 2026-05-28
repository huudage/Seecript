"""缺口识别与补全 Agent。

两个核心函数：
- detect_gaps(manifest, materials) → list[Gap]
    简化版槽位匹配：按 section.role + slot 顺位枚举样例需求，挨个找匹配的 user material。
    匹配规则（阶段 3 简化版）：
      * 推荐 role 命中 → status=ok
      * role 不一致但媒体类型可用 → status=warn
      * 没有任何素材可用 → status=miss
    现在以 SectionRole 为单位（opening/development/climax/closing），不再按 video_type 三选一。
- fill_gap(gap, action, params) → FillResult
    分发到 rerank（纯 Python） / copy（LLM 文案） / aigc（Seedance T2V 短片生成）。
    aigc 路径走 doubao-seedance-2-0-fast-260128：submit → 轮询 → 返回 task_id 作为新素材引用。

阶段 3 此版本足以驱动前端 UI；阶段 5 比赛前再做槽位匹配的真算法（cos-sim + role 推荐 + theme 语义匹配）。
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
    AdaptedSection,
    FillAction,
    FillResult,
    Gap,
    Material,
    SampleManifest,
    SectionRole,
)

log = logging.getLogger("seecript.agent.gap")


_COPY_SYSTEM = (
    "你是短视频口播作者。根据『槽位需求』和『可参考素材标签』，"
    "生成一句口语化的中文口播主推文案（不超过 40 字），同时再写 2 句风格不同的备选。"
    "返回 JSON：{\"gap_fill_narration\": str, \"alternatives\": [str, str]}。"
)


# 给定 section role 的「重要度」—— 影响 Gap.impact 字段。
# 开场/收尾/高潮 这三类都算 high；development（铺陈/主体）算 medium。
_HIGH_IMPACT_ROLES: set[SectionRole] = {"opening", "climax", "closing"}


# 段落 role → 槽位语义模板。和旧 _SECTION_REQUIREMENT_HINTS 等价，但只剩 4 个 role；
# theme 在 _slot_requirement 里以前缀方式叠加，反映本段真实在讲什么。
_ROLE_REQUIREMENT_HINTS: dict[SectionRole, str] = {
    "opening": "开场 · 钩子/氛围铺垫（强构图近景或大字标题）",
    "development": "主体铺陈 · 演示/对比/信息展开（中景或叙事镜头）",
    "climax": "高潮 · 情绪/视觉/冲突顶点（强构图特写或快剪）",
    "closing": "收尾 · 行动引导/余韵/落版（大字幕或定格）",
}


def detect_gaps(
    adapted_sections: list[AdaptedSection],
    manifest: SampleManifest,
    materials: list[Material],
) -> list[Gap]:
    """槽位匹配——按改编后的 AdaptedSection 迭代，每段拿 1-3 个槽位。

    新版（v3）：段落源从 manifest.sections 切换到 plan.adapted_sections（LLM 基于 brief +
    video_goal 改编后的结构）。每个 Gap 携带：
    - `section_id` 关联回 AdaptedSection，前端按段分组
    - `requirement` 接入 content_description 前缀（让创作者看到本段该呈现什么）
    - `sample_thumbnail_url` 从 source_shot_indices 反查样例缩略图
    """
    # adapted 中真实出现的 roles（去重保序），用于按 role 归类素材
    seen_roles: list[SectionRole] = []
    for sec in adapted_sections:
        if sec.role not in seen_roles:
            seen_roles.append(sec.role)
    if not seen_roles:
        seen_roles = ["development"]

    fallback_role: SectionRole = (
        "development" if "development" in seen_roles else seen_roles[len(seen_roles) // 2]
    )
    by_role: dict[SectionRole, list[Material]] = {r: [] for r in seen_roles}
    for m in materials:
        rec = m.recommended_section if m.recommended_section in seen_roles else fallback_role
        by_role.setdefault(rec, []).append(m)

    for role, pool in by_role.items():
        if role in _HIGH_IMPACT_ROLES:
            pool.sort(key=lambda m: (-m.highlight_score, m.sort_order))

    shot_thumb: dict[int, str | None] = {s.index: s.thumbnail_url for s in manifest.shots}

    def _section_thumb(shot_indices: list[int], slot: int) -> str | None:
        if shot_indices:
            target = shot_indices[min(slot, len(shot_indices) - 1)]
            url = shot_thumb.get(target)
            if url:
                return url
            for idx in shot_indices:
                if shot_thumb.get(idx):
                    return shot_thumb[idx]
        return None

    spillover_queue = [m for lst in by_role.values() for m in lst]
    spillover_used: set[str] = set()
    fallback_idx = 0

    def _take_spillover(exclude_role: SectionRole) -> Material | None:
        nonlocal fallback_idx
        for m in spillover_queue:
            if m.material_id in spillover_used:
                continue
            if m.recommended_section == exclude_role:
                continue
            spillover_used.add(m.material_id)
            return m
        candidates = [m for m in spillover_queue if m.recommended_section != exclude_role]
        if not candidates:
            candidates = spillover_queue
        if not candidates:
            return None
        pick = candidates[fallback_idx % len(candidates)]
        fallback_idx += 1
        return pick

    gaps: list[Gap] = []
    # 同 role 在 adapted 中可能多次（多段 development）—gap_id 加 seq 避免冲突
    role_section_counter: dict[SectionRole, int] = {}
    for sec in adapted_sections:
        section_seq = role_section_counter.get(sec.role, 0)
        role_section_counter[sec.role] = section_seq + 1

        slot_count = max(1, min(3, len(sec.source_shot_indices) or 1))
        section_impact = "high" if sec.role in _HIGH_IMPACT_ROLES else "medium"
        gap_id_prefix = f"gap-{sec.role}-{section_seq}" if section_seq > 0 else f"gap-{sec.role}"

        for slot in range(slot_count):
            requirement = _slot_requirement(
                sec.role, sec.theme, sec.content_description,
                slot, slot_count, manifest,
            )
            thumb = _section_thumb(sec.source_shot_indices, slot)
            pool = by_role.get(sec.role, [])

            if slot < len(pool):
                m = pool[slot]
                gaps.append(Gap(
                    gap_id=f"{gap_id_prefix}-{slot}",
                    section=sec.role,
                    section_id=sec.section_id,
                    slot_index=slot,
                    requirement=requirement,
                    status="ok",
                    impact=section_impact,
                    matched_material_id=m.material_id,
                    note=f"匹配素材 {m.filename}",
                    sample_thumbnail_url=thumb,
                ))
            else:
                spillover = _take_spillover(sec.role)
                if spillover:
                    gaps.append(Gap(
                        gap_id=f"{gap_id_prefix}-{slot}",
                        section=sec.role,
                        section_id=sec.section_id,
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
                        gap_id=f"{gap_id_prefix}-{slot}",
                        section=sec.role,
                        section_id=sec.section_id,
                        slot_index=slot,
                        requirement=requirement,
                        status="miss",
                        impact=section_impact,
                        note="无可用素材，建议 Seedance T2V 生成",
                        sample_thumbnail_url=thumb,
                    ))
    return gaps


def _slot_requirement(
    role: SectionRole,
    theme: str,
    content_description: str,
    slot: int,
    slot_count: int,
    manifest: SampleManifest,
) -> str:
    """槽位语义描述——把 content_description 前 40 字接到 role/theme 基线之前。

    示例输出：『紧扣用户主题给一句口播 · 开场钩子 · 开场 · 钩子/氛围铺垫 · 1/2（大字加描边）』
    """
    style = manifest.packaging.subtitle_style
    base = _ROLE_REQUIREMENT_HINTS.get(role, "主体 · 演示/对比中景")
    theme_clean = (theme or "").strip()
    content_clean = (content_description or "").strip().replace("\n", " ")
    content_short = content_clean[:40]
    parts: list[str] = []
    if content_short:
        parts.append(content_short)
    if theme_clean:
        parts.append(theme_clean)
    parts.append(base)
    pos = f"{slot + 1}/{slot_count}"
    if role in ("development", "climax") and slot > 0:
        return f"{' · '.join(parts)} #{slot + 1} · {pos}（{style}）"
    return f"{' · '.join(parts)} · {pos}（{style}）"


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
            f"section role：{gap.section}\n"
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
    轮询超时（默认 180s）后返回 warn + task_id，前端可基于 task_id 继续刷新或重试。
    """
    t2v = get_t2v_client()
    prompt = params.get("prompt") or f"短视频画面：{gap.requirement}"
    duration_seconds = int(params.get("duration_seconds") or 5)
    first_frame = params.get("first_frame_url")
    last_frame = params.get("last_frame_url")
    reference_images = params.get("reference_images") or None
    reference_video = params.get("reference_video_url")
    reference_audio = params.get("reference_audio_url")
    ratio = params.get("ratio") or params.get("size")
    if ratio and "x" in str(ratio):
        ratio = None
    generate_audio = params.get("generate_audio")
    watermark = params.get("watermark")
    poll_interval = float(params.get("poll_interval_seconds") or 4.0)
    max_wait = float(params.get("max_wait_seconds") or 180.0)

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
            return FillResult(
                gap_id=gap.gap_id, action="aigc",
                new_material_id=task_id, status="warn",
                note=f"Seedance 仍在渲染（{last_status}，已 {int(time.time() - started)}s），请稍后刷新（task={task_id}）",
            )
        await asyncio.sleep(poll_interval)


async def refresh_aigc_task(gap: Gap, task_id: str) -> FillResult:
    """前端轮询入口：根据已有 task_id 再去查 Seedance 一次状态。

    - succeeded → 回写 FillResult(status=ok, new_material_id=task_id)
    - failed    → status=warn + fail_reason
    - pending/processing → 仍 warn，note 带最新状态，前端可再点一次刷新
    """
    t2v = get_t2v_client()
    try:
        q = await t2v.query(task_id)
    except T2VError as exc:
        log.warning("[gap-refresh] t2v query failed task=%s: %s", task_id, exc)
        return FillResult(
            gap_id=gap.gap_id, action="aigc",
            new_material_id=task_id, status="warn",
            note=f"Seedance 查询失败：{exc}（task={task_id}，请稍后再试）",
        )

    if q.status == "succeeded":
        return FillResult(
            gap_id=gap.gap_id, action="aigc",
            new_material_id=task_id, status="ok",
            note=f"Seedance 生成完成（{q.provider}）",
        )
    if q.status == "failed":
        return FillResult(
            gap_id=gap.gap_id, action="aigc",
            new_material_id=task_id, status="warn",
            note=f"Seedance 生成失败：{q.fail_reason or 'unknown'}",
        )
    return FillResult(
        gap_id=gap.gap_id, action="aigc",
        new_material_id=task_id, status="warn",
        note=f"Seedance 仍在 {q.status}，请稍后再点刷新（task={task_id}）",
    )
