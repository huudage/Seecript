"""结构改编 Agent —— 把样例的段落骨架按用户主题 + 视频目的改编成新结构。

数据流（与本期 Compose 升级配套）：
1. decompose_agent 已经把样例真模型拆成了 manifest.sections（含 role + theme + shot_indices）
2. 用户在 Compose 页填 brief（主题/卖点） + video_goal（视频要求与目的）
3. 本 agent 把"样例骨架 + 用户意图"喂给 LLM，让它**改编**而不是照抄
   - 允许：增/删/合并/重排段落
   - 硬约束：首=opening、末=closing、≤1 climax、中间皆 development、总段数 3-7
4. 每段除了 role/theme，额外产出 `content_description`——告诉创作者本段画面/口播该呈什么
5. 落地为 list[AdaptedSection]，section_id = f"sec-{order}"，gap_agent 据此分槽位

为什么需要：旧 plan/build 硬编码 5 段把 manifest 直接丢掉，所有视频都被压成同模板。
新版让样例的真实结构成果（已经过真模型校准）成为改编基线，再用 LLM 二次创作贴合用户需求。
"""
from __future__ import annotations

import logging
from typing import Optional

from ..llm_client import get_llm_client, _extract_json
from ...schemas import (
    AdaptedSection,
    SampleManifest,
    SectionRole,
)

log = logging.getLogger("seecript.agent.plan")


_ADAPT_SYSTEM = (
    "你是短视频结构改编师。给定样例视频的真实段落结构、视频画像，以及创作者的"
    "主题与视频目的，请把样例的『骨架』改编为本次新视频的段落结构。\n\n"
    "允许：增加段落、删除冗余段落、合并相邻段落、调整顺序。\n\n"
    "硬约束：\n"
    "1. 第一段 role 必须是 opening\n"
    "2. 最后一段 role 必须是 closing\n"
    "3. 整支视频最多 1 段 climax（可以没有）\n"
    "4. 中间段都是 development（不允许中间出现 opening/closing）\n"
    "5. 总段数 3-7\n\n"
    "每段返回字段：\n"
    "- role: opening | development | climax | closing\n"
    "- theme: 中文短标签（≤8 字），紧贴创作者主题，不照抄样例\n"
    "- content_description: 内容说明（30-100 字）—— 告诉创作者画面该呈现什么、"
    "口播该说什么、为什么放在这个位置，紧扣 brief + video_goal\n"
    "- source_section_indices: 改编自原样例哪些段落下标；纯新增段为 []\n\n"
    "返回 JSON：{\"adapted_sections\": [{\"role\": str, \"theme\": str, "
    "\"content_description\": str, \"source_section_indices\": [int]}]}"
)


_ALLOWED_ROLES: set[SectionRole] = {"opening", "development", "climax", "closing"}


async def adapt_structure(
    manifest: SampleManifest,
    brief: Optional[str],
    video_goal: Optional[str],
) -> list[AdaptedSection]:
    """改编样例段落骨架成新结构。失败时回落 1:1 拷贝 manifest.sections。

    user payload 必须包含字面字符串 `原样例共 N 段`，让 mock 能 regex 解析段数。
    """
    sample_sections = list(manifest.sections)
    n_src = len(sample_sections)
    if n_src == 0:
        log.warning("[plan-agent] manifest.sections 为空，无法改编 → fallback")
        return _fallback_adaptation(sample_sections)

    brief_text = (brief or "").strip() or "（未提供主题）"
    goal_text = (video_goal or "").strip() or "（未提供具体目的）"

    sample_lines: list[str] = []
    for i, sec in enumerate(sample_sections):
        theme = sec.theme or "（无主题标签）"
        summary = (sec.summary or "").strip()[:60]
        shots = ",".join(str(idx) for idx in sec.shot_indices) or "-"
        sample_lines.append(
            f"[{i}] role={sec.role} | theme={theme} | shots={shots} | summary={summary}"
        )

    understanding = manifest.understanding
    arche = understanding.archetype if understanding else "通用短视频"
    narrative = understanding.narrative_summary if understanding else "（无画像）"
    tone = understanding.tone if understanding else "（无基调）"

    user = (
        f"样例视频画像：\n"
        f"- archetype：{arche}\n"
        f"- narrative：{narrative}\n"
        f"- tone：{tone}\n\n"
        f"创作者输入：\n"
        f"- 主题/卖点（brief）：{brief_text}\n"
        f"- 视频要求与目的（video_goal）：{goal_text}\n\n"
        f"原样例共 {n_src} 段：\n" + "\n".join(sample_lines) + "\n\n"
        f"请基于以上信息改编段落结构（3-7 段，遵守硬约束）。"
    )

    llm = get_llm_client()
    try:
        text = await llm.complete(_ADAPT_SYSTEM, user)
        data = _extract_json(text)
        raw = data.get("adapted_sections", []) if isinstance(data, dict) else []
        items = _parse_raw_items(raw)
        if items:
            items = _enforce_hard_constraints(items, n_src)
            return _materialize(items, sample_sections)
    except Exception as exc:
        log.warning("[plan-agent] adapt_structure LLM failed: %s → fallback", exc)

    return _fallback_adaptation(sample_sections)


def _parse_raw_items(raw: list) -> list[dict]:
    """清洗 LLM 输出：保留合法 role + 截断超长字段。"""
    out: list[dict] = []
    if not isinstance(raw, list):
        return out
    for item in raw:
        if not isinstance(item, dict):
            continue
        role = item.get("role", "")
        if role not in _ALLOWED_ROLES:
            continue
        theme = str(item.get("theme", "") or "").strip()[:20]
        content = str(item.get("content_description", "") or "").strip()[:300]
        if not content:
            continue
        src_idx_raw = item.get("source_section_indices", []) or []
        src_idx: list[int] = []
        if isinstance(src_idx_raw, list):
            for x in src_idx_raw:
                try:
                    src_idx.append(int(x))
                except (TypeError, ValueError):
                    continue
        out.append({
            "role": role,
            "theme": theme,
            "content_description": content,
            "source_section_indices": src_idx,
        })
    return out


def _enforce_hard_constraints(items: list[dict], n_src: int) -> list[dict]:
    """强约束修正：首=opening、末=closing、中间无 opening/closing、≤1 climax、长度 3-7。"""
    if not items:
        return items

    n = len(items)

    # 首段强制 opening
    items[0]["role"] = "opening"
    if not items[0].get("theme"):
        items[0]["theme"] = "开场钩子"

    # 末段强制 closing（n≥2）
    if n >= 2:
        items[-1]["role"] = "closing"
        if not items[-1].get("theme"):
            items[-1]["theme"] = "行动引导"

    # 中间段：不允许 opening/closing；至多 1 个 climax
    climax_seen = 0
    for i in range(1, n - 1):
        role = items[i].get("role")
        if role in ("opening", "closing"):
            items[i]["role"] = "development"
        elif role == "climax":
            climax_seen += 1
            if climax_seen > 1:
                items[i]["role"] = "development"

    # 长度修正：<3 走 fallback；>7 截断
    if n < 3:
        return []  # 触发上层 fallback
    if n > 7:
        kept: list[dict] = [items[0]]
        # 保留首个 climax + 前若干 development
        climax_item = next((it for it in items[1:-1] if it.get("role") == "climax"), None)
        developments = [it for it in items[1:-1] if it.get("role") == "development"]
        # 目标段数 7，预留 opening/closing/climax 后还能装 4-5 个 development
        budget = 7 - 2 - (1 if climax_item else 0)
        kept.extend(developments[:budget])
        if climax_item:
            kept.append(climax_item)
        kept.append(items[-1])
        items = kept

    return items


def _materialize(items: list[dict], sample_sections) -> list[AdaptedSection]:
    """把清洗后的 dict 列表落地为 AdaptedSection，计算 source_shot_indices + section_id。

    纯新增段（source_section_indices=[]）借用相邻段的 shots，让前端缩略图能展示。
    """
    if not items:
        return []

    out: list[AdaptedSection] = []
    n_src = len(sample_sections)
    last_shots: list[int] = []

    for order, it in enumerate(items):
        src_idx = [i for i in it.get("source_section_indices", []) if 0 <= i < n_src]
        shot_indices: list[int] = []
        for i in src_idx:
            shot_indices.extend(sample_sections[i].shot_indices or [])
        # 去重保序
        seen = set()
        deduped: list[int] = []
        for s in shot_indices:
            if s not in seen:
                seen.add(s)
                deduped.append(s)
        shot_indices = deduped

        if not shot_indices and last_shots:
            # 纯新增段：借上一段最后 1 个 shot 当占位缩略图
            shot_indices = [last_shots[-1]]

        out.append(AdaptedSection(
            section_id=f"sec-{order}",
            role=it["role"],
            theme=it.get("theme", "") or _default_theme(it["role"]),
            content_description=it["content_description"],
            source_section_indices=src_idx,
            source_shot_indices=shot_indices,
            order=order,
        ))
        if shot_indices:
            last_shots = shot_indices

    return out


def _fallback_adaptation(sample_sections) -> list[AdaptedSection]:
    """LLM 失败/为空时的兜底：1:1 拷贝样例段落，content_description 填占位。"""
    out: list[AdaptedSection] = []
    n = len(sample_sections)
    if n == 0:
        return out
    for order, sec in enumerate(sample_sections):
        out.append(AdaptedSection(
            section_id=f"sec-{order}",
            role=sec.role,
            theme=sec.theme or _default_theme(sec.role),
            content_description=(
                f"[fallback] 沿用样例 {sec.role} 段结构，"
                f"建议按本段镜头节奏组织画面与口播。"
            ),
            source_section_indices=[order],
            source_shot_indices=list(sec.shot_indices or []),
            order=order,
        ))
    return out


def _default_theme(role: SectionRole) -> str:
    return {
        "opening": "开场钩子",
        "development": "主体铺陈",
        "climax": "卖点高潮",
        "closing": "行动引导",
    }[role]
