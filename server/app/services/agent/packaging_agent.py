"""Packaging Agent —— LLM 推荐段落转场 + 封面方案，并回写 plan.packaging_track。

为什么独立成 agent：
- 转场和封面都是"看完主轨结构再决策"的工作，比拆解和缺口识别都晚一步。
- 同一 plan 多次调用应当幂等：每次清掉旧的 transition/cover PackagingItem 再写新的。
- 阶段 3 之前是写死规则，阶段 5 上 LLM。

主入口：
- recommend_packaging(plan, *, apply=True, preferences=None) -> PackagingRecommendation
    1) 解析 preferences（preset → 字段展开 + 与 plan.settings.packaging_prefs 合并）。
    2) LLM 一次性给出 transitions + cover；失败则规则兜底。
    3) 输出按 prefs 钳制：style 落白名单、duration ≤ max_transition_duration、
       cover.title 按 cover_text_source 路由（auto / video_goal / custom）。
    4) apply=True 时把建议落到 plan_store（包装轨 kind=cover；主轨 Scene.transition_in）。
    5) 返回 PackagingRecommendation 供前端展示。

落地约定（render 端配合使用）：
- 转场 → Scene.transition_in（xfade 滤镜），不再走 packaging_track。
- cover PackagingItem 占用 0.0 ~ prefs.cover_duration 作为开场封面停留窗，
  style 含 layout/palette/style_note/subtitle/font_size/position/background/bilingual
  （这些字段供 burn_packaging_track 决定字幕样式渲染参数）。
"""
from __future__ import annotations

import logging
import re
from typing import Any, Optional

from ..llm_client import LLMError, get_llm_client
from ..plans import plan_store
from ...schemas import (
    CoverDesign,
    PackagingItem,
    PackagingPreferences,
    PackagingPreset,
    PackagingRecommendation,
    Plan,
    Scene,
    SceneTransition,
    TransitionStyle,
    TransitionSuggestion,
)

log = logging.getLogger("seecript.agent.packaging")


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


# 预设展开表 —— preset 不为 custom 时，这里的字段会覆盖用户 prefs 的对应字段。
# 设计意图：只覆盖"风格定义性"字段（白名单/字幕样式/封面策略），不动 llm_temperature；
# 用户在 UI 上动了任何具体字段后前端把 preset 切回 custom，停止覆盖。
_PRESET_EXPANSIONS: dict[PackagingPreset, dict[str, Any]] = {
    "minimalist": {
        "allowed_transition_styles": ["hard_cut", "dissolve"],
        "max_transition_duration": 0.4,
        "subtitle_font_size": "small",
        "subtitle_position": "bottom",
        "subtitle_background": "none",
        "subtitle_bilingual": False,
        "cover_text_source": "video_goal",
        "cover_duration": 1.0,
        "cover_with_subtitle": False,
    },
    "energetic": {
        "allowed_transition_styles": ["hard_cut", "dissolve", "slide", "zoom", "whip", "wipe"],
        "max_transition_duration": 1.0,
        "subtitle_font_size": "large",
        "subtitle_position": "bottom",
        "subtitle_background": "shadow",
        "subtitle_bilingual": False,
        "cover_text_source": "auto",
        "cover_duration": 1.5,
        "cover_with_subtitle": True,
    },
    "info_feed": {
        "allowed_transition_styles": ["dissolve", "slide", "wipe"],
        "max_transition_duration": 0.6,
        "subtitle_font_size": "medium",
        "subtitle_position": "top",
        "subtitle_background": "gradient",
        "subtitle_bilingual": False,
        "cover_text_source": "auto",
        "cover_duration": 1.5,
        "cover_with_subtitle": True,
    },
    "dialogue": {
        "allowed_transition_styles": ["hard_cut", "dissolve"],
        "max_transition_duration": 0.5,
        "subtitle_font_size": "large",
        "subtitle_position": "bottom",
        "subtitle_background": "shadow",
        "subtitle_bilingual": True,
        "cover_text_source": "auto",
        "cover_duration": 1.2,
        "cover_with_subtitle": True,
    },
}


def expand_preset(prefs: PackagingPreferences) -> PackagingPreferences:
    """preset != 'custom' 时按表展开覆盖具体字段；custom 直接原样返回。

    路由层在合并 plan.settings.packaging_prefs + 请求体 preferences 之后调一次，
    确保 PackagingAgent 拿到的是"已展开"的纯字段视图。
    """
    if prefs.preset == "custom":
        return prefs
    overrides = _PRESET_EXPANSIONS.get(prefs.preset)
    if not overrides:
        return prefs
    return prefs.model_copy(update=overrides)


def _build_system_prompt(prefs: PackagingPreferences) -> str:
    """根据 prefs 动态拼系统提示，把白名单/max_duration/cover 策略喂给 LLM。"""
    allowed = ", ".join(prefs.allowed_transition_styles)
    max_dur = prefs.max_transition_duration
    cover_hint = {
        "auto": "封面主标题由你自由发挥，强冲击 ≤12 字",
        "video_goal": "封面主标题应紧贴用户的 video_goal 文本（你看到的『创作者主题』），≤12 字",
        "custom": "封面主标题字段会被用户自定义文本替代，你给的 title 可被忽略；仍按 ≤12 字给一个候选",
    }[prefs.cover_text_source]
    bilingual_hint = ""
    if prefs.subtitle_bilingual:
        bilingual_hint = "\n注意：本次开启双语字幕，封面 subtitle 字段请给一句英文翻译（≤20 字）。"
    return (
        "你是短视频包装设计师。根据给定的主轨分镜（每段标了 role+theme）与创作者主题文本，"
        "请输出两类建议：(a) 相邻段落切换处的转场风格；(b) 一份开场封面方案。\n"
        f"转场只能从这些风格里选：[{allowed}]，duration 必须 ≤ {max_dur:.2f}s。\n"
        f"{cover_hint}。{bilingual_hint}\n"
        "返回 JSON：{"
        "\"transitions\": [{\"at_seconds\": number, \"from_section\": str, \"to_section\": str, "
        "\"style\": one of allowed, "
        f"\"duration\": number (0.1-{max_dur:.2f}), "
        "\"reason\": str (≤30 字)}], "
        "\"cover\": {\"title\": str (≤12 字, 强冲击), \"subtitle\": str (≤18 字, 可空), "
        "\"palette\": [hex 颜色 2-3 个, 主色 + 强调色], "
        "\"layout\": one of [center, left, split, stacked], "
        "\"style_note\": str (≤30 字, 字号/色/排版)}"
        "}。\n"
        "from_section/to_section 必须是这 4 个 role 之一：opening / development / climax / closing。\n"
        "转场风格指导：opening→development 切到主体用节奏感强的；"
        "development→climax 进入高潮用冲击感强的；"
        "climax→closing 或 development→closing 切到收尾用情绪缓冲的。"
    )


def _section_pairs(scenes: list[Scene]) -> list[tuple[Scene, Scene]]:
    """相邻 scene 的 section 不同时算一次段落切换。"""
    out: list[tuple[Scene, Scene]] = []
    for a, b in zip(scenes, scenes[1:]):
        if a.section != b.section:
            out.append((a, b))
    return out


def _rule_based_transitions(
    plan: Plan,
    prefs: PackagingPreferences,
) -> list[TransitionSuggestion]:
    """LLM 不可用时按 _RULE_TRANSITION 给一组规则化建议。

    prefs.allowed_transition_styles 内未命中时回落到白名单首项；
    duration 受 max_transition_duration 钳制。
    """
    whitelist = set(prefs.allowed_transition_styles)
    primary: TransitionStyle = prefs.allowed_transition_styles[0]
    duration = min(0.4, prefs.max_transition_duration)
    suggestions: list[TransitionSuggestion] = []
    for idx, (a, b) in enumerate(_section_pairs(plan.main_track)):
        key = (a.section, b.section)
        raw: TransitionStyle = _RULE_TRANSITION.get(key, primary)
        style: TransitionStyle = raw if raw in whitelist else primary
        suggestions.append(TransitionSuggestion(
            item_id=f"pkg-tr-{idx:02d}",
            at_seconds=float(b.start),
            from_section=a.section,
            to_section=b.section,
            style=style,
            duration=duration,
            reason=f"规则兜底：{a.section}→{b.section} 选 {style}",
        ))
    return suggestions


def _rule_based_cover(plan: Plan, prefs: PackagingPreferences) -> CoverDesign:
    """LLM 不可用时造一份通用封面。title 来源遵循 prefs.cover_text_source。"""
    title = _resolve_cover_title("规则兜底封面", plan, prefs)
    return CoverDesign(
        title=title,
        subtitle=None,
        palette=["#FFE600", "#1F2937"],
        layout="center",
        style_note="规则兜底：大字标题居中，黑底黄字，无副标题。",
    )


def _resolve_cover_title(
    llm_title: str,
    plan: Plan,
    prefs: PackagingPreferences,
) -> str:
    """按 cover_text_source 路由封面主标题：
    - custom      用 prefs.cover_custom_text（≤12 字）
    - video_goal  用 plan.video_goal 前 12 字
    - auto        用 LLM 给的 title
    任何来源为空时回落到下一档（custom→video_goal→llm→兜底）。
    """
    if prefs.cover_text_source == "custom" and prefs.cover_custom_text:
        return prefs.cover_custom_text.strip()[:12]
    if prefs.cover_text_source == "video_goal" and (plan.video_goal or "").strip():
        return plan.video_goal.strip()[:12]  # type: ignore[union-attr]
    title = (llm_title or "").strip()[:12]
    if title:
        return title
    return ((plan.brief or "").strip() or "短视频封面")[:12]


def _coerce_transition(
    raw: Any,
    fallback_idx: int,
    prefs: PackagingPreferences,
) -> Optional[TransitionSuggestion]:
    """LLM 单条 transition 验证 + 钳制。

    - style 不在 prefs.allowed_transition_styles 中 → 替换为白名单首项（不丢条目，保证转场覆盖率）
    - duration 超过 max_transition_duration → clamp 到上限
    - 角色无效 / at_seconds 不可解析 → 丢弃整条
    """
    if not isinstance(raw, dict):
        return None
    style = str(raw.get("style", "")).strip()
    if style not in _ALLOWED_STYLES:
        return None
    if style not in prefs.allowed_transition_styles:
        style = prefs.allowed_transition_styles[0]
    try:
        at_s = float(raw.get("at_seconds", 0.0))
        dur = float(raw.get("duration", 0.4))
    except (TypeError, ValueError):
        return None
    dur = max(0.1, min(prefs.max_transition_duration, dur))
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


def _coerce_cover(
    raw: Any,
    plan: Plan,
    prefs: PackagingPreferences,
) -> Optional[CoverDesign]:
    if not isinstance(raw, dict):
        return None
    title = _resolve_cover_title(str(raw.get("title", "") or ""), plan, prefs)
    if not title:
        return None
    subtitle_raw = raw.get("subtitle")
    subtitle = str(subtitle_raw).strip()[:18] if isinstance(subtitle_raw, str) else None
    if subtitle == "":
        subtitle = None
    if not prefs.cover_with_subtitle:
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
    goal = plan.video_goal or "(创作者未提供 video_goal)"
    return (
        f"创作者主题：{brief}\n"
        f"video_goal：{goal}\n"
        f"plan_id：{plan.plan_id}\n"
        f"总时长：{plan.duration_seconds:.1f} 秒\n"
        f"主轨分镜（[role] 起止 · 口播）：\n" + "\n".join(scene_lines)
    )


async def recommend_packaging(
    plan: Plan,
    *,
    apply: bool = True,
    preferences: Optional[PackagingPreferences] = None,
) -> PackagingRecommendation:
    """LLM 一次性给出 transitions + cover；失败时规则兜底；apply=True 时回写 plan.packaging_track。

    preferences：传入时（router 已合并 plan.settings.packaging_prefs + 请求体）按其约束输出；
    None 时直接读 plan.settings.packaging_prefs（兼容老调用方）。一律走 expand_preset 展开 preset。
    """
    raw_prefs = preferences or plan.settings.packaging_prefs
    prefs = expand_preset(raw_prefs)

    notes: list[str] = []
    transitions: list[TransitionSuggestion] = []
    cover: Optional[CoverDesign] = None

    system_prompt = _build_system_prompt(prefs)
    user = _build_user_prompt(plan)
    try:
        llm = get_llm_client()
        data = await llm.complete_json(
            system_prompt, user, temperature=prefs.llm_temperature
        )
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
                tr = _coerce_transition(raw, idx, prefs)
                if tr is not None:
                    transitions.append(tr)
        cover = _coerce_cover(data.get("cover"), plan, prefs)
        if not transitions:
            notes.append("LLM 未返回有效 transitions，转场用规则兜底")
        if cover is None:
            notes.append("LLM 未返回有效 cover，封面用规则兜底")

    if not transitions:
        transitions = _rule_based_transitions(plan, prefs)
    if cover is None:
        cover = _rule_based_cover(plan, prefs)

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
        _write_to_plan(plan, rec, prefs)
        notes.append(f"已落地：transitions={len(transitions)}，cover=1")

    return rec


def _write_to_plan(
    plan: Plan,
    rec: PackagingRecommendation,
    prefs: PackagingPreferences,
) -> None:
    """把建议转成主轨 Scene.transition_in + 包装轨 cover PackagingItem。

    - 转场不再走 packaging_track（kind='transition' 是历史包装项，已废弃；
      真转场需要 ffmpeg xfade 修改主轨相邻段时长，concat demuxer 做不出来）。
    - 落点：每条 TransitionSuggestion 找到 start≈at_seconds 的 to_scene，把
      SceneTransition(style, duration) 写到 to_scene.transition_in。
    - 幂等：写入前先清掉所有 main_track scene 的 transition_in，再按本次建议刷一遍。
    - cover 仍写 packaging_track（透明 PNG overlay，不占主轨时长，OK）。
    - cover.style 同时携带 prefs.subtitle_*：burn_packaging_track 用它决定字幕渲染参数。
    """
    # 包装轨：保留 subtitle/title_bar/sticker，丢弃旧 transition / cover（cover 一会重写）
    keep = [it for it in plan.packaging_track if it.kind not in ("transition", "cover")]

    # 主轨：先清掉所有 transition_in（幂等）
    for sc in plan.main_track:
        sc.transition_in = None

    # 按 scene.start 建索引，便于 at_seconds → to_scene 反查
    scenes_by_start = sorted(plan.main_track, key=lambda s: s.start)

    transition_count = 0
    for tr in rec.transitions:
        if tr.at_seconds <= 0 or tr.at_seconds >= plan.duration_seconds:
            continue
        # 在 ±0.5s 容差内找最匹配 to_scene；找不到则跳过（不再回落到包装轨 transition）
        target = None
        best_delta = 0.6
        for sc in scenes_by_start:
            if sc is plan.main_track[0]:
                continue  # sc-0 没有上一段
            delta = abs(sc.start - tr.at_seconds)
            if delta <= best_delta:
                best_delta = delta
                target = sc
        if target is None:
            log.info("[packaging] transition skip: 找不到 at=%.2fs 对应的 scene", tr.at_seconds)
            continue
        target.transition_in = SceneTransition(style=tr.style, duration=tr.duration)
        transition_count += 1

    # 把 prefs 的字幕样式钉到所有现存 subtitle PackagingItem 上（burn 用）
    for it in keep:
        if it.kind == "subtitle":
            it.style = {
                **(it.style or {}),
                "font_size": prefs.subtitle_font_size,
                "position": prefs.subtitle_position,
                "background": prefs.subtitle_background,
                "bilingual": prefs.subtitle_bilingual,
            }

    new_items: list[PackagingItem] = []
    if rec.cover is not None:
        first_dur = plan.main_track[0].duration if plan.main_track else plan.duration_seconds
        cover_end = max(0.6, min(prefs.cover_duration, first_dur))
        new_items.append(PackagingItem(
            item_id=rec.cover.item_id or "pkg-cover",
            kind="cover",
            start=0.0,
            end=cover_end,
            text=rec.cover.title,
            style={
                "subtitle": rec.cover.subtitle if prefs.cover_with_subtitle else None,
                "palette": rec.cover.palette,
                "layout": rec.cover.layout,
                "style_note": rec.cover.style_note,
            },
        ))

    plan.packaging_track = keep + new_items
    plan_store.replace(plan)
    log.info("[packaging] plan=%s wrote %d scene.transition_in + cover=%s",
             plan.plan_id, transition_count, rec.cover is not None)
