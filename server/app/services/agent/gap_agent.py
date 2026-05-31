"""缺口识别与补全 Agent。

两个核心函数：
- detect_gaps(adapted_sections, manifest, materials) → list[Gap]
- fill_gap(gap, action, params) → FillResult
    分发到 rerank（纯 Python） / copy（LLM 文案） / aigc（Seedance T2V 短片生成）。
    aigc 路径会读 AdaptedSection.duration_seconds：>12s 走链式 N 段，用前一段尾帧驱动下一段
    首帧，输出 N 个 video_urls。

阶段 3 此版本足以驱动前端 UI；阶段 5 比赛前再做槽位匹配的真算法（cos-sim + role 推荐 + theme 语义匹配）。
"""
from __future__ import annotations

import asyncio
import base64
import logging
import math
import mimetypes
import time
import uuid
from pathlib import Path
from typing import Any, Optional

import httpx

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


SEEDANCE_MAX_SECONDS = 12


_COPY_SYSTEM = (
    "你是短视频口播 + 文案作者。你的输出会被前端用作两种用途：\n"
    "  1) 段落口播旁白（≤40 字）\n"
    "  2) 当该段没有合适视频素材时，作为『大字画面 text_card』兜底——把这句文案做成全屏字卡。\n"
    "\n"
    "因此文案必须『紧扣本段的内容要求』+『与整体视频背景一致』+『口播友好且适合做大字』。\n"
    "\n"
    "你会收到三类锚点信息（按优先级）：\n"
    "  - 视频整体背景（brief）：决定语气和品类\n"
    "  - 本段的内容要求（content_description）：决定本句到底要讲什么——最高优先\n"
    "  - 创作者补充（prompt_hint）：用户在面板里手填的特殊要求，权重低于上面两条\n"
    "\n"
    "硬约束：\n"
    "  - 主文案 ≤ 40 字中文，备选 2 句各 ≤ 40 字\n"
    "  - 紧扣『内容要求』，不要泛化到整体背景层\n"
    "  - 不出现段落角色名（opening/development/climax/closing）和『本段』『第 X 段』等元数据词\n"
    "  - 不要 markdown / ASCII 引号\n"
    "\n"
    "返回 JSON：{\"gap_fill_narration\": str, \"alternatives\": [str, str]}。"
)


_HIGH_IMPACT_ROLES: set[SectionRole] = {"opening", "climax", "closing"}


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

        # 一个 AdaptedSection = 一个 Gap：LLM 已经把这一段的创作要求合成在 content_description 里，
        # 历史版本按 source_shot_indices 长度切 1-3 个 slot 会让 UI 出现完全一样的 requirement 重复 N 次。
        # 真正需要"段内分两个独立 ask"时，应在 compose_agent 拆出两个 AdaptedSection，而不是同段多 slot。
        section_impact = "high" if sec.role in _HIGH_IMPACT_ROLES else "medium"
        gap_id_prefix = f"gap-{sec.role}-{section_seq}" if section_seq > 0 else f"gap-{sec.role}"

        requirement = _slot_requirement(
            sec.role, sec.theme, sec.content_description, manifest,
        )
        thumb = _section_thumb(sec.source_shot_indices, 0)
        pool = by_role.get(sec.role, [])

        if pool:
            m = pool[0]
            gaps.append(Gap(
                gap_id=f"{gap_id_prefix}-0",
                section=sec.role,
                section_id=sec.section_id,
                slot_index=0,
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
                    gap_id=f"{gap_id_prefix}-0",
                    section=sec.role,
                    section_id=sec.section_id,
                    slot_index=0,
                    requirement=requirement,
                    status="warn",
                    impact="medium",
                    matched_material_id=spillover.material_id,
                    note=f"跨段借用 {spillover.filename}，建议重排或 Seedance T2V 补全",
                    sample_thumbnail_url=thumb,
                ))
            else:
                gaps.append(Gap(
                    gap_id=f"{gap_id_prefix}-0",
                    section=sec.role,
                    section_id=sec.section_id,
                    slot_index=0,
                    requirement=requirement,
                    status="miss",
                    impact=section_impact,
                    note="无可用素材，建议 Seedance T2V 生成",
                    sample_thumbnail_url=thumb,
                ))
    return gaps


def _lookup_plan_section_for_gap(gap: Gap):
    """gap.section_id → (Plan | None, AdaptedSection | None)；延迟导入 plan_store 避免循环依赖。"""
    if not gap.section_id:
        return None, None
    from ..plans import plan_store  # 延迟导入：plans → agent 路径上无环

    for plan_id in plan_store.all_ids():
        plan = plan_store.get(plan_id)
        if not plan:
            continue
        sec = next((s for s in plan.adapted_sections if s.section_id == gap.section_id), None)
        if sec:
            return plan, sec
    return None, None


def _slot_requirement(
    role: SectionRole,
    theme: str,
    content_description: str,
    manifest: SampleManifest,
) -> str:
    """段落创作要求——把 content_description（前 60 字）+ theme + role 基线拼成一句话。

    示例输出：『竖屏展示模糊的废教学图…·痛点开场·开场·钩子/氛围铺垫（大字加描边）』
    """
    style = manifest.packaging.subtitle_style
    base = _ROLE_REQUIREMENT_HINTS.get(role, "主体 · 演示/对比中景")
    theme_clean = (theme or "").strip()
    content_clean = (content_description or "").strip().replace("\n", " ")
    content_short = content_clean[:60]
    parts: list[str] = []
    if content_short:
        parts.append(content_short)
    if theme_clean:
        parts.append(theme_clean)
    parts.append(base)
    return f"{' · '.join(parts)}（{style}）"


async def fill_gap(gap: Gap, action: FillAction, params: dict[str, Any]) -> FillResult:
    """分发到三种动作：rerank（重排） / copy（LLM 文案） / aigc（Seedance T2V）。"""
    log.info("[gap-fill] %s action=%s", gap.gap_id, action)
    if action == "rerank":
        target = params.get("target_material_id") or f"mat-rerank-{uuid.uuid4().hex[:6]}"
        return FillResult(
            gap_id=gap.gap_id, action="rerank",
            new_material_id=target, status="ok",
            note="已重排到该槽位",
            section_id=gap.section_id,
        )

    if action == "copy":
        llm = get_llm_client()
        # 反查 plan / section 上下文以注入提示词锚点；找不到时降级到老格式。
        plan, section = _lookup_plan_section_for_gap(gap)
        brief = (plan.brief.strip() if plan and plan.brief else "")
        goal = (plan.video_goal.strip() if plan and plan.video_goal else "")
        content_desc = (section.content_description.strip() if section and section.content_description else "")
        theme = (section.theme.strip() if section and section.theme else "")

        user_lines: list[str] = []
        if brief:
            user_lines.append(f"视频整体背景：{brief}")
        if goal:
            user_lines.append(f"视频目的：{goal}")
        if content_desc:
            user_lines.append(f"本段内容要求：{content_desc}")
        if theme:
            user_lines.append(f"本段主题词：{theme}")
        user_lines.append(f"原始槽位需求（兜底）：{gap.requirement}")
        if params.get("tag_hint"):
            user_lines.append(f"可参考素材标签：{params['tag_hint']}")
        if params.get("prompt_hint"):
            user_lines.append(f"创作者补充（低优）：{params['prompt_hint']}")
        user_lines.append("请输出主文案 + 2 句备选，紧扣『本段内容要求』。")
        user = "\n".join(user_lines)

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
            section_id=gap.section_id,
        )

    if action == "aigc":
        return await _fill_with_seedance(gap, params)

    return FillResult(
        gap_id=gap.gap_id, action=action, status="warn",
        note=f"未知动作：{action}", section_id=gap.section_id,
    )


async def _fill_with_seedance(gap: Gap, params: dict[str, Any]) -> FillResult:
    """调 Seedance T2V 生成填补槽位。

    若 params['duration_seconds'] > SEEDANCE_MAX_SECONDS（12s），自动切 N 个等长 chunk，
    顺序生成；每个 chunk 用上一段尾帧作首帧实现画面延续。

    返回 FillResult：
    - video_urls：N 段 CDN URL（按时序）
    - cover_url：第一段封面（前端预览）
    - chunks_count：N
    - chunk_task_ids：每段对应的 Seedance task_id，refresh 接口按此重试单段
    - new_material_id：首段 task_id，兼容旧前端
    """
    prompt = (params.get("prompt") or "").strip() or f"短视频画面：{gap.requirement}"
    requested = float(params.get("duration_seconds") or 5.0)
    requested = max(2.0, min(60.0, requested))

    # 分段策略：均分到每段 ≤12s
    n_chunks = max(1, math.ceil(requested / SEEDANCE_MAX_SECONDS))
    per_chunk = max(2.0, min(float(SEEDANCE_MAX_SECONDS), requested / n_chunks))

    base_params: dict[str, Any] = {
        "first_frame": params.get("first_frame_url"),
        "last_frame": params.get("last_frame_url"),
        "reference_images": params.get("reference_images") or None,
        "reference_video": params.get("reference_video_url"),
        "reference_audio": params.get("reference_audio_url"),
        "ratio": _normalize_ratio(params.get("ratio") or params.get("size")),
        "generate_audio": params.get("generate_audio"),
        "watermark": params.get("watermark"),
    }
    poll_interval = float(params.get("poll_interval_seconds") or 4.0)
    max_wait = float(params.get("max_wait_seconds") or 180.0)

    log.info(
        "[gap-fill] %s seedance: requested=%.1fs → %d chunks × %.1fs",
        gap.gap_id, requested, n_chunks, per_chunk,
    )

    chunk_results = await _generate_chunks(
        prompt=prompt,
        n_chunks=n_chunks,
        per_chunk_seconds=int(round(per_chunk)),
        base_params=base_params,
        poll_interval=poll_interval,
        max_wait=max_wait,
    )

    return _build_fill_result(gap, chunk_results, n_chunks)


async def _generate_chunks(
    *,
    prompt: str,
    n_chunks: int,
    per_chunk_seconds: int,
    base_params: dict[str, Any],
    poll_interval: float,
    max_wait: float,
) -> list[dict[str, Any]]:
    """顺序生成 N 个 chunk；前一段的尾帧（base64 data URL）作为后一段 first_frame。

    每个元素：{status, task_id, video_url, cover_url, fail_reason, started, ended}
    出错的 chunk 立刻终止后续生成，但已生成的 chunk 仍保留。
    """
    t2v = get_t2v_client()
    results: list[dict[str, Any]] = []
    prev_tail_data_url: Optional[str] = None

    for i in range(n_chunks):
        first_frame = prev_tail_data_url or base_params.get("first_frame")
        # 链式生成时只有最后一段才允许带 last_frame
        last_frame = base_params.get("last_frame") if i == n_chunks - 1 else None
        chunk_prompt = (
            prompt if i == 0
            else f"{prompt}（第 {i + 1}/{n_chunks} 段，画面自然衔接前段尾帧，保持构图与色调一致）"
        )
        started = time.time()
        try:
            submit = await t2v.submit(
                prompt=chunk_prompt,
                first_frame=first_frame,
                last_frame=last_frame,
                reference_images=base_params.get("reference_images") if i == 0 else None,
                reference_video=base_params.get("reference_video"),
                reference_audio=base_params.get("reference_audio") if i == 0 else None,
                duration_seconds=per_chunk_seconds,
                ratio=base_params.get("ratio"),
                generate_audio=base_params.get("generate_audio"),
                watermark=base_params.get("watermark"),
            )
        except T2VError as exc:
            log.warning("[gap-fill] chunk %d submit failed: %s", i + 1, exc)
            results.append({
                "status": "failed",
                "task_id": None,
                "video_url": None,
                "cover_url": None,
                "fail_reason": f"submit: {exc}",
                "elapsed": int(time.time() - started),
            })
            break

        task_id = submit.task_id
        last_query: Optional[Any] = None
        timed_out = False
        while True:
            try:
                q = await t2v.query(task_id)
            except T2VError as exc:
                log.warning("[gap-fill] chunk %d query failed task=%s: %s", i + 1, task_id, exc)
                results.append({
                    "status": "warn",
                    "task_id": task_id,
                    "video_url": None,
                    "cover_url": None,
                    "fail_reason": f"query: {exc}",
                    "elapsed": int(time.time() - started),
                })
                break
            last_query = q
            if q.status == "succeeded":
                results.append({
                    "status": "succeeded",
                    "task_id": task_id,
                    "video_url": q.video_url,
                    "cover_url": q.cover_url,
                    "fail_reason": None,
                    "elapsed": int(time.time() - started),
                })
                # 抽尾帧给下一段
                if i < n_chunks - 1 and q.video_url:
                    try:
                        prev_tail_data_url = await _extract_tail_frame_data_url(q.video_url)
                    except Exception as exc:
                        log.warning("[gap-fill] tail-frame extract failed: %s → 下段无首帧约束", exc)
                        prev_tail_data_url = None
                break
            if q.status == "failed":
                results.append({
                    "status": "failed",
                    "task_id": task_id,
                    "video_url": None,
                    "cover_url": None,
                    "fail_reason": q.fail_reason or "unknown",
                    "elapsed": int(time.time() - started),
                })
                break
            if time.time() - started > max_wait:
                timed_out = True
                results.append({
                    "status": "warn",
                    "task_id": task_id,
                    "video_url": None,
                    "cover_url": None,
                    "fail_reason": f"timeout after {int(max_wait)}s ({q.status})",
                    "elapsed": int(time.time() - started),
                })
                break
            await asyncio.sleep(poll_interval)

        # 当前 chunk 没成功就停（无法抽尾帧给下一段；且失败应及时反馈用户）
        if not results or results[-1]["status"] != "succeeded":
            break
        # 链式时如果没拿到尾帧 url 也无法继续
        if i < n_chunks - 1 and not prev_tail_data_url:
            log.warning("[gap-fill] 链式中断：第 %d 段无尾帧可用，停止生成", i + 1)
            break

    return results


def _build_fill_result(gap: Gap, chunks: list[dict[str, Any]], expected: int) -> FillResult:
    """把 chunks 折叠成 FillResult。"""
    succeeded_urls = [c["video_url"] for c in chunks if c.get("status") == "succeeded" and c.get("video_url")]
    chunk_task_ids = [c["task_id"] for c in chunks if c.get("task_id")]
    cover_url = next((c.get("cover_url") for c in chunks if c.get("cover_url")), None)
    total_elapsed = sum(int(c.get("elapsed") or 0) for c in chunks)

    if not chunks:
        return FillResult(
            gap_id=gap.gap_id, action="aigc",
            status="warn",
            note="Seedance 未提交任何任务（参数错误？）",
            chunks_count=0,
            section_id=gap.section_id,
        )

    last = chunks[-1]
    if len(succeeded_urls) == expected:
        return FillResult(
            gap_id=gap.gap_id, action="aigc",
            new_material_id=chunk_task_ids[0] if chunk_task_ids else None,
            status="ok",
            video_urls=succeeded_urls,
            cover_url=cover_url,
            chunks_count=expected,
            chunk_task_ids=chunk_task_ids,
            note=(
                f"Seedance 链式生成完成（{expected} 段，{total_elapsed}s）"
                if expected > 1
                else f"Seedance 生成完成（{total_elapsed}s）"
            ),
            section_id=gap.section_id,
        )
    # 部分成功 / 全失败
    note = (
        f"Seedance 仅完成 {len(succeeded_urls)}/{expected} 段，最后一段：{last.get('fail_reason') or last.get('status')}"
    )
    return FillResult(
        gap_id=gap.gap_id, action="aigc",
        new_material_id=chunk_task_ids[0] if chunk_task_ids else None,
        status="warn",
        video_urls=succeeded_urls,
        cover_url=cover_url,
        chunks_count=len(succeeded_urls),
        chunk_task_ids=chunk_task_ids,
        note=note,
        section_id=gap.section_id,
    )


def _normalize_ratio(ratio: Optional[str]) -> Optional[str]:
    if not ratio:
        return None
    if "x" in str(ratio).lower():
        return None
    return str(ratio)


async def _extract_tail_frame_data_url(video_url: str, *, timeout: float = 60.0) -> str:
    """下载 chunk 视频到临时目录，ffmpeg 抽倒数 0.5s 帧，转 base64 data URL。"""
    from ..video import ffmpeg as ffmpeg_svc  # 延迟导入避免循环依赖

    if not ffmpeg_svc.ffmpeg_available():
        raise RuntimeError("ffmpeg unavailable")

    tmp_root = Path("server/var/seedance_chain_tmp")
    tmp_root.mkdir(parents=True, exist_ok=True)
    base = tmp_root / f"chunk-{uuid.uuid4().hex[:8]}"
    mp4_path = base.with_suffix(".mp4")
    jpg_path = base.with_suffix(".jpg")

    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        resp = await client.get(video_url)
        resp.raise_for_status()
        mp4_path.write_bytes(resp.content)

    info = ffmpeg_svc.probe(mp4_path)
    t = max(0.0, info.duration_seconds - 0.5)
    await asyncio.to_thread(ffmpeg_svc.extract_frame, mp4_path, t, jpg_path)

    mime, _ = mimetypes.guess_type(jpg_path.name)
    mime = mime or "image/jpeg"
    payload = base64.b64encode(jpg_path.read_bytes()).decode("ascii")
    # 清理 mp4（保留 jpg 不重要——临时目录会随重启清空）
    try:
        mp4_path.unlink(missing_ok=True)
    except Exception:
        pass
    return f"data:{mime};base64,{payload}"


async def refresh_aigc_task(gap: Gap, task_id: str) -> FillResult:
    """前端轮询入口：根据已有 task_id 再去查 Seedance 一次状态。

    单 chunk 接口；批量 refresh 应该走 /gap/fill 重新生成。
    """
    t2v = get_t2v_client()
    try:
        q = await t2v.query(task_id)
    except T2VError as exc:
        log.warning("[gap-refresh] t2v query failed task=%s: %s", task_id, exc)
        return FillResult(
            gap_id=gap.gap_id, action="aigc",
            new_material_id=task_id, status="warn",
            chunks_count=0,
            note=f"Seedance 查询失败：{exc}（task={task_id}，请稍后再试）",
            section_id=gap.section_id,
        )

    if q.status == "succeeded":
        # Seedance 偶发：status=succeeded 但 video_url 还没落盘 / 鉴权窗口内空。
        # 这种情况下不能回 ok（前端会停轮询），强转 warn 让 caller 再次 refresh。
        if not q.video_url:
            return FillResult(
                gap_id=gap.gap_id, action="aigc",
                new_material_id=task_id, status="warn",
                chunks_count=0,
                chunk_task_ids=[task_id],
                note=f"Seedance 已完成但 URL 暂未回包，请稍后再点刷新（task={task_id}）",
                section_id=gap.section_id,
            )
        return FillResult(
            gap_id=gap.gap_id, action="aigc",
            new_material_id=task_id, status="ok",
            video_urls=[q.video_url],
            cover_url=q.cover_url,
            chunks_count=1,
            chunk_task_ids=[task_id],
            note=f"Seedance 生成完成（{q.provider}）",
            section_id=gap.section_id,
        )
    if q.status == "failed":
        return FillResult(
            gap_id=gap.gap_id, action="aigc",
            new_material_id=task_id, status="warn",
            chunks_count=0,
            chunk_task_ids=[task_id],
            note=f"Seedance 生成失败：{q.fail_reason or 'unknown'}",
            section_id=gap.section_id,
        )
    return FillResult(
        gap_id=gap.gap_id, action="aigc",
        new_material_id=task_id, status="warn",
        chunks_count=0,
        chunk_task_ids=[task_id],
        note=f"Seedance 仍在 {q.status}，请稍后再点刷新（task={task_id}）",
        section_id=gap.section_id,
    )
