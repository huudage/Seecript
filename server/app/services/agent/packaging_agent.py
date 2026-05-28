"""Packaging Agent —— LLM 推荐段落转场 + 封面方案，并回写 plan.packaging_track。

为什么独立成 agent：
- 转场和封面都是"看完主轨结构再决策"的工作，比拆解和缺口识别都晚一步。
- 同一 plan 多次调用应当幂等：每次清掉旧的 transition/cover PackagingItem 再写新的。
- 阶段 3 之前是写死规则，阶段 5 上 LLM。

主入口：
- recommend_packaging(plan, *, apply=True) -> PackagingRecommendation
    1) LLM 一次性给出 transitions + cover；失败则规则兜底。
    2) apply=True 时把建议落到 plan_store（包装轨 kind=transition / kind=cover）。
    3) 返回 PackagingRecommendation 供前端展示。

落地约定（render 端配合使用）：
- transition PackagingItem.start = at_seconds - duration/2，end = at_seconds + duration/2，
  style={"transition_style": "...", "from": "...", "to": "..."}。Remotion overlay 时按
  transition_style 渲对应的转场片段。
- cover PackagingItem 占用 0.0 ~ min(1.5s, scene[0].duration) 作为开场封面停留窗，
  style 含 layout/palette/style_note/subtitle。
"""
from __future__ import annotations

import logging
import re
import uuid
from typing import Any, Optional

from ..llm_client import LLMError, get_llm_client
from ..plans import plan_store
from ...schemas import (
    CoverDesign,
    PackagingItem,
    PackagingRecommendation,
    Plan,
    Scene,
    TransitionStyle,
    TransitionSuggestion,
)

log = logging.getLogger("seecript.agent.packaging")


_PACKAGING_SYSTEM = (
    "你是短视频包装设计师。根据给定的主轨分镜（每段标了 role+theme）与创作者主题文本，"
    "请输出两类建议：(a) 相邻段落切换处的转场风格；(b) 一份开场封面方案。\n"
    "返回 JSON：{"
    "\"transitions\": [{\"at_seconds\": number, \"from_section\": str, \"to_section\": str, "
    "\"style\": one of [hard_cut, dissolve, slide, zoom, whip, wipe], "
    "\"duration\": number (0.1-1.5), \"reason\": str (≤30 字)}], "
    "\"cover\": {\"title\": str (≤12 字, 强冲击), \"subtitle\": str (≤18 字, 可空), "
    "\"palette\": [hex 颜色 2-3 个, 主色 + 强调色], "
    "\"layout\": one of [center, left, split, stacked], "
    "\"style_note\": str (≤30 字, 字号/色/排版)}"
    "}。\n"
    "from_section/to_section 必须是这 4 个 role 之一：opening / development / climax / closing。\n"
    "转场风格指导：opening→development 切到主体用 hard_cut 或 whip 制造节奏；"
    "development→climax 进入高潮用 whip 或 zoom 给冲击；"
    "climax→closing 或 development→closing 切到收尾用 dissolve 或 zoom 给情绪缓冲。"
)


_ALLOWED_STYLES: tuple[TransitionStyle, ...] = (
    "hard_cut", "dissolve", "slide", "zoom", "whip", "wipe",
)
_ALLOWED_LAYOUTS = ("center", "left", "split", "stacked")
_ALLOWED_ROLES = ("opening", "development", "climax", "closing")
_HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")

# 规则兜底：role 对 → 转场风格。LLM 失败时按这张表给。
# 4 元 role 共 4*3=12 组有序对，覆盖常见的相邻切换。
_RULE_TRANSITION: dict[tuple[str, str], TransitionStyle] = {
    ("opening", "development"): "hard_cut",
    ("opening", "climax"): "whip",
    ("opening", "closing"): "dissolve",
    ("development", "development"): "hard_cut",
    ("development", "climax"): "whip",
    ("development", "closing"): "zoom",
    ("climax", "closing"): "dissolve",
    ("climax", "development"): "slide",
    ("closing", "development"): "dissolve",
}


def _section_pairs(scenes: list[Scene]) -> list[tuple[Scene, Scene]]:
    """相邻 scene 的 section 不同时算一次段落切换。"""
    out: list[tuple[Scene, Scene]] = []
    for a, b in zip(scenes, scenes[1:]):
        if a.section != b.section:
            out.append((a, b))
    return out


def _rule_based_transitions(plan: Plan) -> list[TransitionSuggestion]:
    """LLM 不可用时按 _RULE_TRANSITION 给一组规则化建议。每段切换都给一条。"""
    suggestions: list[TransitionSuggestion] = []
    for idx, (a, b) in enumerate(_section_pairs(plan.main_track)):
        key = (a.section, b.section)
        style: TransitionStyle = _RULE_TRANSITION.get(key, "hard_cut")
        suggestions.append(TransitionSuggestion(
            item_id=f"pkg-tr-{idx:02d}",
            at_seconds=float(b.start),
            from_section=a.section,
            to_section=b.section,
            style=style,
            duration=0.4,
            reason=f"规则兜底：{a.section}→{b.section} 默认 {style}",
        ))
    return suggestions


def _rule_based_cover(plan: Plan) -> CoverDesign:
    """LLM 不可用时按 brief（或第一个 scene 的 narration）造一份通用封面。"""
    raw_title = (plan.brief or "").strip() or (
        plan.main_track[0].narration if plan.main_track else "短视频封面"
    )
    title = raw_title[:12] or "短视频封面"
    return CoverDesign(
        title=title,
        subtitle=None,
        palette=["#FFE600", "#1F2937"],
        layout="center",
        style_note="规则兜底：大字标题居中，黑底黄字，无副标题。",
    )


def _coerce_transition(raw: Any, fallback_idx: int) -> Optional[TransitionSuggestion]:
    if not isinstance(raw, dict):
        return None
    style = str(raw.get("style", "")).strip()
    if style not in _ALLOWED_STYLES:
        return None
    try:
        at_s = float(raw.get("at_seconds", 0.0))
        dur = float(raw.get("duration", 0.4))
    except (TypeError, ValueError):
        return None
    dur = max(0.1, min(1.5, dur))
    from_sec = str(raw.get("from_section", "")).strip()
    to_sec = str(raw.get("to_section", "")).strip()
    if from_sec not in _ALLOWED_ROLES or to_sec not in _ALLOWED_ROLES:
        return None
    reason = str(raw.get("reason", "") or "")[:60]
    return TransitionSuggestion(
        item_id=f"pkg-tr-{fallback_idx:02d}",
        at_seconds=at_s,
        from_section=from_sec,  # type: ignore[arg-type]
        to_section=to_sec,  # type: ignore[arg-type]
        style=style,  # type: ignore[arg-type]
        duration=dur,
        reason=reason or f"{from_sec}→{to_sec}",
    )


def _coerce_cover(raw: Any) -> Optional[CoverDesign]:
    if not isinstance(raw, dict):
        return None
    title = str(raw.get("title", "") or "").strip()[:12]
    if not title:
        return None
    subtitle_raw = raw.get("subtitle")
    subtitle = str(subtitle_raw).strip()[:18] if isinstance(subtitle_raw, str) else None
    if subtitle == "":
        subtitle = None
    layout = str(raw.get("layout", "center"))
    if layout not in _ALLOWED_LAYOUTS:
        layout = "center"
    palette_raw = raw.get("palette") or []
    palette: list[str] = []
    if isinstance(palette_raw, list):
        for c in palette_raw[:3]:
            s = str(c).strip()
            if _HEX_RE.match(s):
                palette.append(s.upper())
    if not palette:
        palette = ["#FFE600", "#1F2937"]
    style_note = str(raw.get("style_note", "") or "")[:60] or "LLM 未给说明"
    return CoverDesign(
        title=title,
        subtitle=subtitle,
        palette=palette,
        layout=layout,  # type: ignore[arg-type]
        style_note=style_note,
    )


def _build_user_prompt(plan: Plan) -> str:
    scene_lines = []
    for sc in plan.main_track:
        scene_lines.append(
            f"  - [{sc.section}] {sc.start:.1f}-{sc.start + sc.duration:.1f}s · "
            f"{sc.narration or '(无口播)'}"
        )
    brief = plan.brief or "(创作者未提供主题文本)"
    return (
        f"创作者主题：{brief}\n"
        f"plan_id：{plan.plan_id}\n"
        f"总时长：{plan.duration_seconds:.1f} 秒\n"
        f"主轨分镜（[role] 起止 · 口播）：\n" + "\n".join(scene_lines)
    )


async def recommend_packaging(plan: Plan, *, apply: bool = True) -> PackagingRecommendation:
    """LLM 一次性给出 transitions + cover；失败时规则兜底；apply=True 时回写 plan.packaging_track。"""
    notes: list[str] = []
    transitions: list[TransitionSuggestion] = []
    cover: Optional[CoverDesign] = None

    user = _build_user_prompt(plan)
    try:
        llm = get_llm_client()
        data = await llm.complete_json(_PACKAGING_SYSTEM, user)
    except LLMError as exc:
        log.warning("[packaging] LLM failed: %s; using rule fallback", exc)
        notes.append(f"LLM 失败，规则兜底：{exc}")
        data = None
    except Exception as exc:
        log.warning("[packaging] LLM unexpected: %s; using rule fallback", exc)
        notes.append(f"LLM 异常，规则兜底：{exc}")
        data = None

    if isinstance(data, dict):
        raw_trs = data.get("transitions")
        if isinstance(raw_trs, list):
            for idx, raw in enumerate(raw_trs):
                tr = _coerce_transition(raw, idx)
                if tr is not None:
                    transitions.append(tr)
        cover = _coerce_cover(data.get("cover"))
        if not transitions:
            notes.append("LLM 未返回有效 transitions，转场用规则兜底")
        if cover is None:
            notes.append("LLM 未返回有效 cover，封面用规则兜底")

    if not transitions:
        transitions = _rule_based_transitions(plan)
    if cover is None:
        cover = _rule_based_cover(plan)

    # 把 transition 的 at_seconds 对齐到 plan 真实的段落切换点（防止 LLM 时间凭空乱写）
    real_pairs = _section_pairs(plan.main_track)
    real_pairs_by_kind: dict[tuple[str, str], float] = {
        (a.section, b.section): float(b.start) for a, b in real_pairs
    }
    aligned: list[TransitionSuggestion] = []
    for tr in transitions:
        anchor = real_pairs_by_kind.get((tr.from_section, tr.to_section))
        if anchor is not None and abs(tr.at_seconds - anchor) > 0.5:
            aligned.append(tr.model_copy(update={"at_seconds": anchor}))
        else:
            aligned.append(tr)
    transitions = aligned

    rec = PackagingRecommendation(
        plan_id=plan.plan_id,
        transitions=transitions,
        cover=cover,
        notes=notes,
    )

    if apply:
        _write_to_plan(plan, rec)
        notes.append(f"已落地：transitions={len(transitions)}，cover=1")

    return rec


def _write_to_plan(plan: Plan, rec: PackagingRecommendation) -> None:
    """把建议转成 PackagingItem 写入 plan.packaging_track（清掉旧的 transition/cover，幂等）。

    其它 kind（subtitle/title_bar/sticker）保留原样，不动 plan.build 给的内容。
    """
    keep = [it for it in plan.packaging_track if it.kind not in ("transition", "cover")]

    new_items: list[PackagingItem] = []
    for tr in rec.transitions:
        # 防御：scene.start 可能超过实际 plan.duration_seconds（plan.py 的 _build_scene
        # 会把 timeline_duration 截短到 shot_duration，但 timeline_start 是硬编码的）。
        # 落不到时间线内的转场跳过，不让 packaging_track 里出现 end<start 的非法 item。
        if tr.at_seconds <= 0 or tr.at_seconds >= plan.duration_seconds:
            continue
        half = tr.duration / 2
        start = max(0.0, tr.at_seconds - half)
        end = min(plan.duration_seconds, tr.at_seconds + half)
        if end <= start:
            continue
        new_items.append(PackagingItem(
            item_id=tr.item_id or f"pkg-tr-{uuid.uuid4().hex[:6]}",
            kind="transition",
            start=start,
            end=end,
            text=None,
            style={
                "transition_style": tr.style,
                "from": tr.from_section,
                "to": tr.to_section,
                "reason": tr.reason,
            },
        ))

    if rec.cover is not None:
        first_dur = plan.main_track[0].duration if plan.main_track else plan.duration_seconds
        cover_end = max(0.8, min(1.5, first_dur))
        new_items.append(PackagingItem(
            item_id=rec.cover.item_id or "pkg-cover",
            kind="cover",
            start=0.0,
            end=cover_end,
            text=rec.cover.title,
            style={
                "subtitle": rec.cover.subtitle,
                "palette": rec.cover.palette,
                "layout": rec.cover.layout,
                "style_note": rec.cover.style_note,
            },
        ))

    plan.packaging_track = keep + new_items
    plan_store.replace(plan)
    log.info("[packaging] plan=%s wrote %d transitions + cover=%s",
             plan.plan_id, len(rec.transitions), rec.cover is not None)
