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
from ..seedream_client import SeedreamError, get_seedream_client
from ..t2v_client import T2VError, get_t2v_client
from ...schemas import (
    AdaptedSection,
    AnimationSpec,
    FillAction,
    FillResult,
    Gap,
    Material,
    SampleManifest,
    SectionRole,
    TextCardSpec,
    role_is_closing,
    role_is_opening,
    role_is_peak,
)

log = logging.getLogger("seecript.agent.gap")


SEEDANCE_MAX_SECONDS = 12


_COPY_SYSTEM = (
    "你是短视频字卡画面作者。你的输出会被 ffmpeg 渲染成纯文字短片（『字卡画面』），"
    "填补这一段没有视频素材的视觉空缺。\n"
    "\n"
    "因此你产出的不是『一句口播』，而是『一张字卡的内容』——\n"
    "  • main_text：大字主标（≤20 字最佳，最多 24 字）。占满画面 60% 视觉权重。\n"
    "  • sub_text：小字副标（≤30 字，可空）。注释 / 反问 / 数据补充。\n"
    "  • alternatives：2 组备选 {main_text, sub_text}（给前端三选一）。\n"
    "\n"
    "—— 上下文锚点（按优先级，从高到低）——\n"
    "  1. 用户指定核心信息 core_message：本段最该说的（最高优先，几乎必须命中）\n"
    "  2. 本段内容要求 content_description：决定字卡到底说什么\n"
    "  3. 情绪钩子 emotional_hook：anxiety/wow/anticipation/twist/resonance —— 决定语气与字数节奏\n"
    "  4. 强制关键词 forced_keywords：必须自然嵌入到 main_text 或 sub_text，不要硬塞\n"
    "  5. 全局调性 tone + 平台 platform：决定遣词节奏\n"
    "  6. 视频整体背景 brief / 目的 goal：决定语气和品类\n"
    "  7. 创作者补充 prompt_hint：用户面板手填的特殊要求（再低）\n"
    "\n"
    "—— 情绪钩子语气速查 ——\n"
    "  • anxiety       戳痛点 / 危机感 → 短反问：『再不...？』\n"
    "  • wow           惊艳反差 / 强结果 → 数字 / 极限词：『3 秒，换一个你』\n"
    "  • anticipation  铺垫扣子 / 预告 → 未完成感：『接下来发生的——』\n"
    "  • twist         先抑后扬 / 颠覆 → 转折：『没想到，反而是...』\n"
    "  • resonance     共鸣共情 → 第二人称：『你也是这样吗』\n"
    "\n"
    "—— 字卡范式 ——\n"
    "  Good：main_text=『3 秒，治好你的拖延』 sub_text=『试试清单法』（点题 + 强承诺）\n"
    "  Good：main_text=『没想到，沉默才是答案』 sub_text=『（看完你就懂）』（转折 + 副标埋扣）\n"
    "  Bad ：main_text=『本段展示主角面对挑战的内心活动展开...』（注释化 / 写成镜头脚本）\n"
    "\n"
    "—— 硬约束 ——\n"
    "  - main_text 长度 ≤24 字；sub_text 长度 ≤30 字（可空）\n"
    "  - 必须命中 core_message 与 forced_keywords；缺即视为失败\n"
    "  - 紧扣『本段内容要求』，不要泛化到整体背景层\n"
    "  - 不出现段落角色名（opening/development/climax/closing/intro/step_N/hook/item_N 等）\n"
    "  - 不出现『本段』『第 X 段』等元数据自指\n"
    "  - 不要 markdown / ASCII 引号\n"
    "  - 平台 douyin/xiaohongshu 倾向极短句；wechat/bilibili 可稍长\n"
    "\n"
    "返回 JSON：\n"
    "{\"main_text\": \"...\", \"sub_text\": \"...\", "
    "\"alternatives\": [{\"main_text\":\"...\",\"sub_text\":\"...\"}, {\"main_text\":\"...\",\"sub_text\":\"...\"}]}"
)


# ---- TextCardSpec 装配 ------------------------------------------------------

_VALID_FONT = {"bold_sans", "serif_classic", "handwriting", "tech_mono"}
_VALID_LAYOUT = {"center", "top", "bottom", "split_top_bottom"}
_VALID_BG_MODE = {"solid", "gradient", "image_blur", "dark_overlay"}
_VALID_ANIM = {"fade_in", "typewriter", "bounce_word", "zoom_pop"}

import re as _re_textcard
_HEX_PATTERN = _re_textcard.compile(r"^#[0-9A-Fa-f]{6}$")


def _hex_or(value: Any, fallback: str) -> str:
    s = str(value or "").strip()
    if _HEX_PATTERN.match(s):
        return s
    return fallback


def _enum_or(value: Any, valid: set[str], fallback: str) -> str:
    s = str(value or "").strip().lower()
    return s if s in valid else fallback


_HOOK_DEFAULT_SPEC: dict[str, dict[str, str]] = {
    "anxiety": dict(font_family="bold_sans", bg_mode="dark_overlay",
                    bg_color="#0F172A", text_color="#F97316", accent_color="#FCA5A5",
                    animation="zoom_pop"),
    "wow": dict(font_family="tech_mono", bg_mode="solid",
                bg_color="#0B1220", text_color="#FACC15", accent_color="#38BDF8",
                animation="zoom_pop"),
    "anticipation": dict(font_family="bold_sans", bg_mode="gradient",
                         bg_color="#1E1B4B", text_color="#FBBF24", accent_color="#A78BFA",
                         animation="typewriter"),
    "twist": dict(font_family="serif_classic", bg_mode="solid",
                  bg_color="#111111", text_color="#F5D547", accent_color="#EF4444",
                  animation="bounce_word"),
    "resonance": dict(font_family="handwriting", bg_mode="solid",
                      bg_color="#FFF7ED", text_color="#1F2937", accent_color="#DB7C5C",
                      animation="fade_in"),
}


def _build_text_card_spec_from_params(
    params: dict[str, Any],
    *,
    main_text: str,
    sub_text: str,
    emotional_hook: str,
    section_duration: float,
    existing_style: dict[str, str] | None = None,
) -> TextCardSpec:
    """从 fill 请求的 params 装配最终 TextCardSpec。

    params 里所有字段都可选——前端把 outline 默认值 + 用户改的字段一起回传；
    缺失字段：先看 existing_style（同 plan 内已有字卡的众数风格，保持一致），
    再按 emotional_hook 预设兜底；颜色必须是 6 位 hex，否则用 hook 默认。
    """
    hook_preset = _HOOK_DEFAULT_SPEC.get(emotional_hook, _HOOK_DEFAULT_SPEC["resonance"])
    # existing_style 字段优先级高于 hook_preset：保证后续生成的字卡延续已有版式
    style_pre = {**hook_preset, **(existing_style or {})}

    emoji_raw = params.get("emoji_decor") or []
    emoji_clean: list[str] = []
    if isinstance(emoji_raw, list):
        for it in emoji_raw:
            s = str(it).strip()
            if s and len(s) <= 8:
                emoji_clean.append(s)
            if len(emoji_clean) >= 3:
                break

    # 时长：用户给的 > section 时长 > 默认 4s；最终 clamp 到 1.5–15s
    raw_dur = params.get("duration_seconds")
    if raw_dur is None:
        raw_dur = section_duration
    try:
        duration = float(raw_dur)
    except (TypeError, ValueError):
        duration = float(section_duration or 4.0)
    duration = max(1.5, min(15.0, duration))

    return TextCardSpec(
        main_text=main_text[:24],
        sub_text=sub_text[:40],
        font_family=_enum_or(params.get("font_family"), _VALID_FONT, style_pre["font_family"]),  # type: ignore[arg-type]
        layout=_enum_or(
            params.get("layout"), _VALID_LAYOUT,
            "split_top_bottom" if sub_text else "center",
        ),  # type: ignore[arg-type]
        bg_mode=_enum_or(params.get("bg_mode"), _VALID_BG_MODE, style_pre["bg_mode"]),  # type: ignore[arg-type]
        bg_color=_hex_or(params.get("bg_color"), style_pre["bg_color"]),
        text_color=_hex_or(params.get("text_color"), style_pre["text_color"]),
        accent_color=_hex_or(params.get("accent_color"), style_pre["accent_color"]),
        animation=_enum_or(params.get("animation"), _VALID_ANIM, style_pre["animation"]),  # type: ignore[arg-type]
        emoji_decor=emoji_clean,
        duration_seconds=duration,
    )


def _is_high_impact_role(role: str, pattern: str = "dramatic") -> bool:
    """开场/峰值/收尾类视为高冲击；这些段位需要优先匹配高分素材。"""
    return (
        role_is_opening(role, pattern)
        or role_is_peak(role, pattern)
        or role_is_closing(role, pattern)
    )


def _summarize_existing_card_style(existing_cards: list[dict[str, Any]]) -> str:
    """把已有字卡的风格摘成一句话给 LLM 当版式参考——同一 plan 内字卡保持视觉一致。

    输入：[{font_family, bg_mode, bg_color, text_color, accent_color, layout, animation, main_text}, ...]
    输出："已有 3 张字卡：字体多用 bold_sans；底色 #0F172A；字色 #F97316；高频 emotional_hook anxiety"
    无字卡返回 ""。
    """
    if not existing_cards:
        return ""
    from collections import Counter

    def _top(field: str) -> str | None:
        vals = [str(c.get(field) or "").strip() for c in existing_cards if c.get(field)]
        if not vals:
            return None
        return Counter(vals).most_common(1)[0][0]

    parts: list[str] = [f"已有 {len(existing_cards)} 张字卡可参考版式（保持一致）"]
    if (font := _top("font_family")):
        parts.append(f"字体常用 {font}")
    if (bg_mode := _top("bg_mode")):
        parts.append(f"底色模式 {bg_mode}")
    if (bg := _top("bg_color")):
        parts.append(f"底色 {bg}")
    if (txt := _top("text_color")):
        parts.append(f"主字色 {txt}")
    if (acc := _top("accent_color")):
        parts.append(f"点缀色 {acc}")
    if (layout := _top("layout")):
        parts.append(f"布局 {layout}")
    if (anim := _top("animation")):
        parts.append(f"动画 {anim}")
    samples = [c.get("main_text") for c in existing_cards if c.get("main_text")][:3]
    if samples:
        parts.append("现有主标示例：" + " / ".join(str(s) for s in samples))
    return "；".join(parts)


def _existing_card_style_overrides(existing_cards: list[dict[str, Any]]) -> dict[str, str]:
    """从已有字卡里取众数字段，在 _build_text_card_spec_from_params 里用作 hook 兜底之上的优先 fallback。

    覆盖 font_family/bg_mode/bg_color/text_color/accent_color/animation/layout 七个视觉字段——
    layout 同样要复制：用户在样板段里选择了 'top'/'bottom'/'split_top_bottom'，
    后续段也应保持同样的位置排版（之前只跟着 sub_text 有无切，导致版式飘）。
    main_text/sub_text 是当前段独有的，不参与抄。
    """
    if not existing_cards:
        return {}
    from collections import Counter
    out: dict[str, str] = {}
    for field in ("font_family", "bg_mode", "bg_color", "text_color", "accent_color", "animation", "layout"):
        vals = [str(c.get(field) or "").strip() for c in existing_cards if c.get(field)]
        if vals:
            out[field] = Counter(vals).most_common(1)[0][0]
    return out


_ROLE_REQUIREMENT_HINTS: dict[str, str] = {
    # dramatic
    "opening":     "开场 · 钩子/氛围铺垫（强构图近景或大字标题）",
    "development": "主体铺陈 · 演示/对比/信息展开（中景或叙事镜头）",
    "climax":      "高潮 · 情绪/视觉/冲突顶点（强构图特写或快剪）",
    "closing":     "收尾 · 行动引导/余韵/落版（大字幕或定格）",
    # stepwise
    "intro":       "引入 · 介绍任务/工具（开场近景或全景）",
    "recap":       "总结 · 重点回顾/收束（落版字幕）",
    # listicle
    "hook":        "钩子 · 引出榜单/疑问（强构图大字）",
    "closer":      "收尾 · 总结/CTA（落版字幕）",
    # atmospheric
    "establish":   "起势 · 空镜/氛围（大全景或慢推）",
    "flow":        "流转 · 情绪推进（中景或长镜头）",
    "peak":        "顶点 · 情绪高点（特写或慢动作）",
    "resolve":     "余韵 · 落定收束（定格或缓出）",
    # info_dense
    "title_card":  "标题卡 · 大字入场（强字幕色块）",
    "info_block":  "信息块 · 数据/要点呈现（图表叠加）",
    "payoff":      "落版 · 结论/CTA（粗字幕）",
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
        if _is_high_impact_role(role):
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
        section_impact = "high" if _is_high_impact_role(sec.role) else "medium"
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


def _ratio_from_plan_for_gap(gap: Gap) -> Optional[str]:
    """从 gap 反查 plan.compose_settings 推断画幅 ratio。找不到时返回 None 让调用方走客户端默认。"""
    plan, _ = _lookup_plan_section_for_gap(gap)
    if not plan:
        return None
    settings = getattr(plan, "compose_settings", None)
    if not settings:
        return None
    from ..video.aspect import aspect_for_settings
    return aspect_for_settings(settings).ratio


def _slot_requirement(
    role: SectionRole,
    theme: str,
    content_description: str,
    manifest: SampleManifest,
) -> str:
    """段落创作要求——把 content_description（前 60 字）+ theme + role 基线拼成一句话。

    示例输出：『竖屏展示模糊的废教学图…·痛点开场·开场·钩子/氛围铺垫（大字加描边）』
    动态 role（step_N/item_N）兜底到对应类的通用提示。
    """
    import re as _re

    style = manifest.packaging.subtitle_style
    base = _ROLE_REQUIREMENT_HINTS.get(role)
    if base is None:
        if _re.match(r"^step_\d+$", role or ""):
            base = "步骤 · 操作/演示（中景或近景，清晰指示）"
        elif _re.match(r"^item_\d+$", role or ""):
            base = "条目 · 列点呈现（大字标题 + 短演示）"
        else:
            base = "主体 · 演示/对比中景"
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
        settings = plan.settings if plan else None
        global_tone = settings.tone if settings else ""
        global_platform = settings.target_platform if settings else ""
        global_cta = (settings.cta or "").strip() if settings else ""
        global_keywords = list(settings.keywords) if settings else []
        section_duration = float(section.duration_seconds) if section else 4.0

        # outline / 用户调参（来自 FillCopyPanel outline 阶段）
        core_message = str(params.get("core_message") or "").strip()
        emotional_hook = str(params.get("emotional_hook") or "").strip().lower()
        tone_override = str(params.get("tone_override") or "").strip()
        forced_keywords_raw = params.get("forced_keywords") or []
        forced_keywords: list[str] = []
        if isinstance(forced_keywords_raw, list):
            for k in forced_keywords_raw:
                s = str(k).strip()[:24]
                if s and s not in forced_keywords:
                    forced_keywords.append(s)
        forced_keywords = forced_keywords[:3]

        # 用户在前端面板里改过的字卡 spec 字段（每个都是可选覆盖；缺失值 LLM 兜底）
        user_main_text = str(params.get("main_text") or "").strip()[:24]
        user_sub_text = str(params.get("sub_text") or "").strip()[:40]

        # 同 plan 内已生成的字卡——版式参考（保持视觉一致），优先来自 params；
        # 如果 caller 没传（旧路径），从 plan.main_track 自动收集 text_card_spec 非空的段，排除当前 gap 对应的段。
        existing_cards_raw = params.get("existing_text_cards")
        existing_cards: list[dict[str, Any]] = []
        if isinstance(existing_cards_raw, list):
            for it in existing_cards_raw:
                # 至少有一个视觉字段就算样板（早期 fill 可能只有 main_text，不该被丢）
                if isinstance(it, dict) and any(
                    it.get(k) for k in ("font_family", "bg_color", "text_color", "accent_color")
                ):
                    existing_cards.append(it)
        if not existing_cards and plan is not None:
            current_section_id = section.section_id if section else None
            for sc in plan.main_track:
                if sc.text_card_spec is None:
                    continue
                # 跳过当前段（防止自己抄自己导致风格越走越偏）
                m = _re_textcard.match(r"^sc-(\d+)$", sc.scene_id or "")
                if m and current_section_id:
                    sec = next(
                        (s for s in plan.adapted_sections if s.order == int(m.group(1))), None
                    )
                    if sec and sec.section_id == current_section_id:
                        continue
                existing_cards.append(sc.text_card_spec.model_dump())
        existing_style = _existing_card_style_overrides(existing_cards)
        existing_style_hint = _summarize_existing_card_style(existing_cards)

        user_lines: list[str] = []
        if brief:
            user_lines.append(f"视频整体背景：{brief}")
        if goal:
            user_lines.append(f"视频目的：{goal}")
        if global_tone:
            user_lines.append(f"全局调性 tone：{global_tone}")
        if global_platform:
            user_lines.append(f"目标平台 platform：{global_platform}")
        if global_cta:
            user_lines.append(f"结尾 CTA：{global_cta}")
        if global_keywords:
            user_lines.append(f"全局可选关键词（参考）：{', '.join(global_keywords)}")
        if content_desc:
            user_lines.append(f"本段内容要求 content_description：{content_desc}")
        if theme:
            user_lines.append(f"本段主题词：{theme}")
        user_lines.append(f"原始槽位需求（兜底）：{gap.requirement}")
        # 用户在 outline 阶段调出来的强约束
        if core_message:
            user_lines.append(f"用户指定核心信息 core_message：{core_message}")
        if emotional_hook:
            user_lines.append(f"情绪钩子 emotional_hook：{emotional_hook}")
        if forced_keywords:
            user_lines.append(f"强制关键词 forced_keywords（必须出现）：{', '.join(forced_keywords)}")
        if tone_override:
            user_lines.append(f"调性微调 tone_lean：{tone_override}")
        if user_main_text:
            user_lines.append(f"用户已写主标 main_text（必须保留语义）：{user_main_text}")
        if user_sub_text:
            user_lines.append(f"用户已写副标 sub_text（必须保留语义）：{user_sub_text}")
        if params.get("tag_hint"):
            user_lines.append(f"可参考素材标签：{params['tag_hint']}")
        if params.get("prompt_hint"):
            user_lines.append(f"创作者补充 prompt_hint（低优）：{params['prompt_hint']}")
        if existing_style_hint:
            # 让 LLM 在写文案 + 提建议色 / 字体时延续已有版式（一致性优先于"每段都换花样"）
            user_lines.append(existing_style_hint)
        # 个性知识库注入 narration scope（文案风格偏好），放最前避免被截断。
        try:
            from ..profile import collect_active_rules, format_rules_for_prompt
            kb_text = format_rules_for_prompt(
                collect_active_rules(), scopes=["narration"], max_per_scope=6,
            )
            if kb_text:
                user_lines.insert(0, kb_text)
        except Exception as exc:  # noqa: BLE001
            log.warning("[gap-agent] KB 注入失败（跳过）: %s", exc)
        user_lines.append("请输出 main_text + sub_text + 2 组 alternatives 的 JSON。")
        user = "\n".join(user_lines)

        main_text = ""
        sub_text = ""
        alternatives_full: list[dict[str, str]] = []
        try:
            data = await llm.complete_json(_COPY_SYSTEM, user)
            if isinstance(data, dict):
                main_text = str(data.get("main_text") or "").strip()[:24]
                sub_text = str(data.get("sub_text") or "").strip()[:40]
                raw_alts = data.get("alternatives") or []
                if isinstance(raw_alts, list):
                    for alt in raw_alts:
                        if isinstance(alt, dict):
                            am = str(alt.get("main_text") or "").strip()[:24]
                            asub = str(alt.get("sub_text") or "").strip()[:40]
                            if am:
                                alternatives_full.append({"main_text": am, "sub_text": asub})
                        elif isinstance(alt, str) and alt.strip():
                            # 兼容旧 LLM 返回纯字符串备选
                            alternatives_full.append({"main_text": alt.strip()[:24], "sub_text": ""})
                        if len(alternatives_full) >= 3:
                            break
        except Exception as exc:
            log.warning("llm copy failed: %s", exc)

        # 用户已写就用用户的；否则用 LLM 的；都没有就 fallback
        if user_main_text:
            main_text = user_main_text
        if user_sub_text:
            sub_text = user_sub_text
        if not main_text:
            main_text = (core_message or theme or gap.requirement or "字卡占位")[:24]

        # 组装最终 TextCardSpec：用户在 params 里的每个字段都可覆盖 LLM 推荐
        spec = _build_text_card_spec_from_params(
            params,
            main_text=main_text,
            sub_text=sub_text,
            emotional_hook=emotional_hook,
            section_duration=section_duration,
            existing_style=existing_style,
        )

        # narration 仅作 TTS 与 LLM 上下文兼容字段（main_text + sub_text）
        narration_combined = main_text
        if sub_text:
            narration_combined = f"{main_text}。{sub_text}"

        # alternatives 仍以字符串列表暴露（兼容旧前端三选一编辑器：main_text + sub_text 拼接）
        alternatives_compat = [
            (a["main_text"] + ("。" + a["sub_text"] if a["sub_text"] else "")).strip()
            for a in alternatives_full if a["main_text"]
        ]

        return FillResult(
            gap_id=gap.gap_id, action="copy",
            narration=narration_combined,
            alternatives=alternatives_compat,
            text_card_spec=spec,
            status="ok", note="字卡画面生成完成",
            section_id=gap.section_id,
        )

    if action == "aigc":
        return await _fill_with_seedance(gap, params)

    if action == "aigc_image":
        return await _fill_with_seedream_image(gap, params)

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
    # L1: 用户没传时按本段 AdaptedSection.duration_seconds 作默认，避免视频时长 < 段时长触发尾部黑屏。
    raw_dur = params.get("duration_seconds")
    if raw_dur in (None, "", 0):
        _, _sec_for_dur = _lookup_plan_section_for_gap(gap)
        if _sec_for_dur is not None:
            raw_dur = float(_sec_for_dur.duration_seconds)
        else:
            raw_dur = 5.0
    requested = float(raw_dur)
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
        "ratio": _normalize_ratio(params.get("ratio") or params.get("size")) or _ratio_from_plan_for_gap(gap),
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
        gap_id=gap.gap_id,
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
    gap_id: str,
) -> list[dict[str, Any]]:
    """顺序生成 N 个 chunk；前一段的尾帧（base64 data URL）作为后一段 first_frame。

    每个元素：{status, task_id, video_url, cover_url, fail_reason, started, ended}
    出错的 chunk 立刻终止后续生成，但已生成的 chunk 仍保留。
    video_url 已经被 _persist_aigc_video 改写为同源 /aigc-videos/...（失败时回落原 CDN）。
    """
    t2v = get_t2v_client()
    results: list[dict[str, Any]] = []
    prev_tail_data_url: Optional[str] = None
    gap_id_for_persist = gap_id

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
                # 把豆包临时签名 URL 立刻落地为同源静态资源——前端 <video> 才能播。
                persisted_url = (
                    await _persist_aigc_video(q.video_url, gap_id_for_persist)
                    if q.video_url else None
                )
                results.append({
                    "status": "succeeded",
                    "task_id": task_id,
                    "video_url": persisted_url or q.video_url,
                    "cover_url": q.cover_url,
                    "fail_reason": None,
                    "elapsed": int(time.time() - started),
                })
                # 抽尾帧给下一段（仍用原 CDN url，ffmpeg 抽帧不受跨域影响）
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


async def _persist_aigc_video(video_url: str, gap_id: str, *, timeout: float = 90.0) -> Optional[str]:
    """把 Seedance 临时签名 URL 下载到 var/aigc_videos/，返回 /aigc-videos/... 同源路径。

    失败返回 None（caller 保留原 URL，体验回退到 <a> 新窗打开兜底）。
    豆包 TOS 签名 URL 有时效（1h-7d）且阻塞 <video> 跨域预检，落盘后用同源静态服务规避双杀。
    """
    if not video_url or not video_url.startswith("http"):
        return None
    try:
        from ...config import get_settings
        settings = get_settings()
        target_dir = settings.log_dir.parent / "var" / "aigc_videos"
        target_dir.mkdir(parents=True, exist_ok=True)
        filename = f"{gap_id}-{int(time.time())}-{uuid.uuid4().hex[:6]}.mp4"
        target_path = target_dir / filename
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            resp = await client.get(video_url)
            resp.raise_for_status()
            target_path.write_bytes(resp.content)
        log.info("[gap-fill] aigc video persisted gap=%s size=%d → %s",
                 gap_id, len(resp.content), filename)
        return f"/aigc-videos/{filename}"
    except Exception as exc:  # noqa: BLE001
        log.warning("[gap-fill] persist video failed gap=%s url=%s: %s",
                    gap_id, video_url[:80], exc)
        return None


async def _persist_aigc_image(image_url: str, gap_id: str, *, timeout: float = 60.0) -> Optional[str]:
    """把 Seedream CDN URL 下载到 var/aigc_images/，返回 /aigc-images/... 同源路径。

    与 _persist_aigc_video 同理：豆包 CDN 1h-7d 过期 + 跨域预检会让前端 <img> 间歇性失败，
    落盘后走同源静态。失败返回 None，caller 应保留原 URL 兜底。
    """
    if not image_url or not image_url.startswith("http"):
        return None
    try:
        from ...config import get_settings
        settings = get_settings()
        target_dir = settings.log_dir.parent / "var" / "aigc_images"
        target_dir.mkdir(parents=True, exist_ok=True)
        # 从 URL path 猜后缀；猜不到默认 .png（Seedream 返 PNG/JPG 都常见）
        suffix = ".png"
        try:
            from urllib.parse import urlparse
            p = urlparse(image_url).path.lower()
            for ext in (".png", ".jpg", ".jpeg", ".webp"):
                if p.endswith(ext):
                    suffix = ext if ext != ".jpeg" else ".jpg"
                    break
        except Exception:
            pass
        filename = f"{gap_id}-{int(time.time())}-{uuid.uuid4().hex[:6]}{suffix}"
        target_path = target_dir / filename
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            resp = await client.get(image_url)
            resp.raise_for_status()
            target_path.write_bytes(resp.content)
        log.info("[gap-fill] aigc image persisted gap=%s size=%d → %s",
                 gap_id, len(resp.content), filename)
        return f"/aigc-images/{filename}"
    except Exception as exc:  # noqa: BLE001
        log.warning("[gap-fill] persist image failed gap=%s url=%s: %s",
                    gap_id, image_url[:80], exc)
        return None


# ============================================================
# 自动主体抽取 & 动效推荐 —— "AI 生图再渲染" 的核心 DSL
# ============================================================

_SUBJECT_SPLIT_PAT = __import__("re").compile(r"[、，,；;／/]")
_BULLET_PAT = __import__("re").compile(r"^\s*[-*•·●◦▪►]\s*(.+)$", flags=__import__("re").MULTILINE)
_STOPWORDS = {
    "本段", "本段画面", "口播", "画面", "镜头", "通过", "突出", "强化", "展示",
    "呈现", "等", "和", "与", "及", "以及", "比如", "例如", "比如说",
}


def _extract_subjects_from_section(section: "AdaptedSection | None") -> list[str]:
    """从 section 抽取主体清单（按优先级）。

    stage-24 起最高优先级是 `section.shots`——plan_agent 已经替我们拆好了
    分镜 subject，gap_agent 直接消费即可，不必再做 content_description 文本解析。
    其余兜底路径保留给老 plan / 缺少 shots 的边界。

    策略（按命中优先级降序）：
    0. **stage-24** section.shots 非空 → 直接取 ShotPlan.subject + visual 拼短串
    1. 显式分行项目符号（- / * / • ...）：直接当作主体列表
    2. 段中包含『主体：A、B、C』模式：抽取冒号后的并列项
    3. 全文按逗号/顿号切分，过滤停用词，取前 4 个名词性短语
    4. 都不行 → 返回 []（调用方退化为单图模式）

    返回 list[str]：每项是中文短语（≤30 字）。
    """
    if section is None:
        return []
    # stage-24: 优先吃 plan_agent 给的分镜
    # stage-25: 若分镜带 targets（人/物/场景目标），按 target 展开——每个 target 一张图
    shots = getattr(section, "shots", None) or []
    if shots:
        subs: list[str] = []
        for sh in shots:
            tgts = getattr(sh, "targets", None) or []
            if tgts:
                # 每个 target 单独成一张图：以 target.name + visual_hint 为主体
                shot_subj = (sh.subject or "").strip()
                for t in tgts:
                    name = (t.name or "").strip()
                    if not name:
                        continue
                    hint = (t.visual_hint or "").strip()
                    label = f"{name}（{hint}）" if hint else name
                    if shot_subj and shot_subj not in label:
                        label = f"{shot_subj} - {label}"
                    subs.append(label[:30])
                    if len(subs) >= 4:
                        break
            else:
                label = (sh.subject or "").strip() or (sh.visual or "").strip()
                if label:
                    subs.append(label[:30])
            if len(subs) >= 4:  # gap_agent 多图最多 4 张
                break
        if subs:
            return subs[:4]

    text = (section.content_description or "").strip()
    if not text:
        return []

    # 1. 项目符号列表
    bullets = [m.group(1).strip() for m in _BULLET_PAT.finditer(text)]
    bullets = [b for b in bullets if b]
    if 2 <= len(bullets) <= 4:
        return [b[:30] for b in bullets]

    # 2. "主体：A、B、C" 模式
    import re as _re_mod
    m = _re_mod.search(r"主体[:：]\s*([^。\n]+)", text)
    if m:
        parts = [p.strip() for p in _SUBJECT_SPLIT_PAT.split(m.group(1)) if p.strip()]
        parts = [p for p in parts if p not in _STOPWORDS]
        if 2 <= len(parts) <= 4:
            return [p[:30] for p in parts]

    # 3. 全文并列名词识别（启发式：找 "X、Y、Z" 至少 3 项的串）
    candidates = []
    for sent in _re_mod.split(r"[。!?！？\n]", text):
        items = [p.strip() for p in _SUBJECT_SPLIT_PAT.split(sent) if p.strip()]
        items = [p for p in items if 2 <= len(p) <= 20 and p not in _STOPWORDS]
        if len(items) >= 2:
            candidates.append(items)
    if candidates:
        # 取最长的一组，截断到 4 项
        best = max(candidates, key=len)
        return [s[:30] for s in best[:4]]

    return []


def _suggest_animation_spec(
    section: "AdaptedSection | None",
    n_images: int,
    user_override: dict | None,
) -> "AnimationSpec":
    """Stage 4 动效 DSL：根据 section 角色 / 节奏 / 图片数自动推荐 Remotion 动效。

    优先级：user_override > rule-based 推荐。
    user_override 是前端可选的 partial dict，只覆盖被指定的字段。

    规则（与 SectionRole 协同）：
    - opening + fast/peak  → ken-burns in（推近营造冲击）
    - closing             → ken-burns out（拉远收束情绪）
    - climax + peak       → keyframe_morph（多图）或 ken-burns in 高强度
    - development         → parallax（单图）或 storyboard（多图）
    """
    role = (section.role if section else "development") or "development"
    tempo = (section.tempo if section else None) or "medium"

    if n_images > 1:
        # 多图模式默认 storyboard；峰值或开/收尾可用 keyframe_morph 更紧密
        if role == "climax" or tempo in ("peak", "fast"):
            anim_type = "keyframe_morph"
        else:
            anim_type = "storyboard"
        motion = "in"
        intensity = 0.4 if tempo in ("peak", "fast") else 0.3
    else:
        # 单图模式：根据角色挑动效
        if role == "opening":
            anim_type = "ken-burns"
            motion = "in"
            intensity = 0.4
        elif role == "closing":
            anim_type = "ken-burns"
            motion = "out"
            intensity = 0.3
        elif role == "climax":
            anim_type = "ken-burns"
            motion = "in"
            intensity = 0.6
        else:
            anim_type = "parallax"
            motion = "in"
            intensity = 0.35

    defaults: dict = {
        "engine": "remotion",
        "animation_type": anim_type,
        "motion_direction": motion,
        "intensity": intensity,
        "transition": "cross-fade",
        "transition_duration": 0.4,
    }
    if isinstance(user_override, dict):
        for k, v in user_override.items():
            if k in defaults and v is not None:
                defaults[k] = v
    return AnimationSpec.model_validate(defaults)


async def _fill_with_seedream_image(gap: Gap, params: dict[str, Any]) -> FillResult:
    """调 Seedream 文生图填补槽位（静态画面分支）。

    与 _fill_with_seedance 共享准备链：参考图分析 → 提示词生成 → 用户调参；
    最后一步把 Seedance 视频生成换成 Seedream 单图，省去链式 chunk / 尾帧延续逻辑。

    多镜头模式（path B）：
    - params['n_shots'] (int, 1-4)：把本段拆成 N 张图
    - params['subjects'] (list[str])：每张图的差异化描述（可选，缺省按 prompt 自分镜）
    - params['prompts'] (list[str])：显式 N 段 prompt（最高优先级；前端 UI 输出）
    走 Seedream sequential 故事板 → 单次调用拿到 N 张视觉一致图；
    plan.py 把 AdaptedSection 展开成 N 个等长子 Scene。

    自动模式（"AI 生图再渲染"，无 n_shots/subjects 传入）：
    - 从 section.content_description 自动抽取主体清单（标点切分 + 关键词识别）
    - 自动用 Remotion 引擎渲染：ken-burns 单图 / keyframe_morph 多图
    """
    base_prompt = (params.get("prompt") or "").strip() or f"短视频画面：{gap.requirement}"
    ratio = _normalize_ratio(params.get("ratio") or params.get("size")) or _ratio_from_plan_for_gap(gap) or "9:16"
    watermark = bool(params.get("watermark") or False)

    n_shots_raw = params.get("n_shots") or 0
    try:
        n_shots = max(0, min(4, int(n_shots_raw)))
    except (TypeError, ValueError):
        n_shots = 0

    explicit_prompts = params.get("prompts")
    subjects = params.get("subjects")
    # 当前 section 上下文，用于自动主体抽取与动效推荐
    _, current_section = _lookup_plan_section_for_gap(gap)

    # 自动模式：用户没给 n_shots / subjects / prompts，从 content_description 推断
    auto_inferred_subjects: list[str] = []
    if not isinstance(explicit_prompts, list) and not isinstance(subjects, list) and n_shots == 0:
        auto_inferred_subjects = _extract_subjects_from_section(current_section)
        if auto_inferred_subjects:
            subjects = auto_inferred_subjects
            n_shots = len(auto_inferred_subjects)
            log.info(
                "[gap-fill] %s auto inferred %d subjects from section: %s",
                gap.gap_id, n_shots, auto_inferred_subjects,
            )

    n_shots = max(1, min(4, n_shots or 1))

    multi_prompts: list[str] = []
    if isinstance(explicit_prompts, list) and explicit_prompts:
        multi_prompts = [str(p).strip() for p in explicit_prompts if str(p).strip()]
    elif isinstance(subjects, list) and subjects:
        # subjects = ["主体 A 的描述", "主体 B 的描述", ...] → 拼上 base_prompt 主调
        multi_prompts = [
            f"{base_prompt}（聚焦主体：{str(s).strip()}）"
            for s in subjects if str(s).strip()
        ]
    multi_prompts = multi_prompts[:4]

    # 校正 n_shots 与 prompts 的一致性
    if multi_prompts:
        n_shots = max(1, min(4, len(multi_prompts)))
    elif n_shots > 1:
        # 用户指定 n_shots 但没给 subjects/prompts → 让 Seedream 自分镜
        multi_prompts = [base_prompt for _ in range(n_shots)]

    started = time.time()
    seedream = get_seedream_client()
    try:
        if n_shots > 1:
            images = await seedream.generate_sequence(
                multi_prompts, ratio=ratio, watermark=watermark,
            )
        else:
            images = await seedream.generate(base_prompt, ratio=ratio, n=1, watermark=watermark)
    except SeedreamError as exc:
        log.warning("[gap-fill] %s seedream failed: %s", gap.gap_id, exc)
        return FillResult(
            gap_id=gap.gap_id, action="aigc_image",
            status="warn",
            note=f"Seedream 出图失败：{exc}",
            section_id=gap.section_id,
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("[gap-fill] %s seedream unexpected: %s", gap.gap_id, exc)
        return FillResult(
            gap_id=gap.gap_id, action="aigc_image",
            status="warn",
            note=f"Seedream 出图异常：{exc}",
            section_id=gap.section_id,
        )

    if not images:
        return FillResult(
            gap_id=gap.gap_id, action="aigc_image",
            status="warn",
            note="Seedream 返回 0 张图",
            section_id=gap.section_id,
        )

    persisted_urls: list[str] = []
    for idx, img in enumerate(images[:n_shots]):
        suffix_id = gap.gap_id if idx == 0 else f"{gap.gap_id}-shot{idx+1}"
        persisted = await _persist_aigc_image(img.url, suffix_id)
        persisted_urls.append(persisted or img.url)

    elapsed = int(time.time() - started)
    first_url = persisted_urls[0] if persisted_urls else ""
    log.info(
        "[gap-fill] %s seedream ok ratio=%s n=%d elapsed=%ds → %s",
        gap.gap_id, ratio, len(persisted_urls), elapsed, first_url[:80],
    )

    # animation_spec：用户通过 params 把 Remotion 动效偏好带过来。
    # 缺省由 _suggest_animation_spec 推荐（Stage 4 LLM DSL → Remotion props 简化版），
    # 让前端默认就能享受 Remotion 渲染；user_override 来自前端 panel。
    user_override = params.get("animation_spec") if isinstance(params.get("animation_spec"), dict) else None
    try:
        animation_spec_obj = _suggest_animation_spec(
            current_section, len(persisted_urls), user_override,
        )
        # 多图时同时把 image_urls 一起带上，让 Scene.animation_spec 自描述
        if len(persisted_urls) > 1:
            animation_spec_obj = animation_spec_obj.model_copy(update={"image_urls": persisted_urls})
    except Exception as exc:  # noqa: BLE001
        log.warning("[gap-fill] %s animation_spec 推荐失败 → 走 ffmpeg 静帧: %s", gap.gap_id, exc)
        animation_spec_obj = None

    return FillResult(
        gap_id=gap.gap_id, action="aigc_image",
        new_material_id=f"img-{uuid.uuid4().hex[:8]}",
        status="ok",
        aigc_image_url=first_url,
        aigc_image_urls=persisted_urls if len(persisted_urls) > 1 else [],
        cover_url=first_url,  # 单图直接当封面，前端列表可复用 cover_url 缩略
        note=f"Seedream 出图完成（{ratio}，{len(persisted_urls)} 张，{elapsed}s）",
        section_id=gap.section_id,
        animation_spec=animation_spec_obj,
    )


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
            video_urls=[await _persist_aigc_video(q.video_url, gap.gap_id) or q.video_url],
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
