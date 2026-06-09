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
from ..catalog import names_for_prompt as catalog_names_for_prompt, find_by_name as catalog_find_by_name
from ..plans import plan_store
from ...schemas import (
    CoverCandidate,
    CoverDesign,
    FrameDesignSystem,
    PackagingItem,
    PackagingPreferences,
    PackagingPreset,
    PackagingRecommendation,
    PackagingRecommendationV2,
    PackagingVariant,
    Plan,
    Scene,
    SceneTransition,
    StickerCandidate,
    SubtitleStyleCandidate,
    TitleBarCandidate,
    TransitionCandidateBundle,
    TransitionStyle,
    TransitionSuggestion,
    all_role_names,
)

log = logging.getLogger("seecript.agent.packaging")


_ALLOWED_STYLES: tuple[TransitionStyle, ...] = (
    "hard_cut", "dissolve", "slide", "zoom", "whip", "wipe",
)
_ALLOWED_LAYOUTS = ("center", "left", "split", "stacked")
_ALLOWED_ROLES = tuple(all_role_names()) + ("development", "opening", "climax", "closing")
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


def _frame_design_block(frame: Optional[FrameDesignSystem]) -> str:
    """把 frame.md 设计 token 转成给 LLM 的一段中文摘要。"""
    if frame is None:
        return ""
    parts: list[str] = []
    if frame.preset and frame.preset != "custom":
        parts.append(f"设计预设: {frame.preset}（请贴合该模板风格选色与排版）")
    if frame.palette:
        parts.append(f"主色板: {' / '.join(frame.palette)}（封面 palette 优先从中挑）")
    if frame.background_color:
        parts.append(f"主背景色: {frame.background_color}")
    if frame.typography_display:
        parts.append(f"标题字体: {frame.typography_display}")
    if frame.typography_body:
        parts.append(f"正文字体: {frame.typography_body}")
    if frame.motion_density:
        density_hint = {
            "minimal": "克制（建议短转场、淡入淡出为主）",
            "balanced": "适中（节奏正常，转场可多样）",
            "kinetic": "高密度（转场强冲击，cover 排版动感）",
        }.get(frame.motion_density, frame.motion_density)
        parts.append(f"动效密度: {density_hint}")
    if frame.grain_overlay:
        parts.append("叠加颗粒/胶片质感")
    if frame.vignette:
        parts.append("叠加暗角")
    if frame.notes:
        parts.append(f"额外要求: {frame.notes}")
    if not parts:
        return ""
    return "\nframe.md 设计系统约束（统一全片视觉）:\n  - " + "\n  - ".join(parts)


def _catalog_hint_block() -> str:
    """给 LLM 看的 HyperFrames catalog 选项摘要（精简版，控 token）。"""
    transitions = catalog_names_for_prompt("transition", max_n=10)
    covers = catalog_names_for_prompt("cover", max_n=8)
    if not transitions and not covers:
        return ""
    out = ["\nHyperFrames catalog 可选项（你可在 transitions[].catalog_block / cover.catalog_block 字段引用 name 给前端做风格 hint，不强制必填）:"]
    if transitions:
        lines = [f"    · {t['name']} — {t.get('description') or t.get('title')}" for t in transitions]
        out.append("  转场（transition）:\n" + "\n".join(lines))
    if covers:
        lines = [f"    · {c['name']} — {c.get('description') or c.get('title')}" for c in covers]
        out.append("  封面（cover）:\n" + "\n".join(lines))
    return "\n".join(out)


def _build_system_prompt(prefs: PackagingPreferences, frame: Optional[FrameDesignSystem] = None) -> str:
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
        f"{_frame_design_block(frame)}"
        f"{_catalog_hint_block()}\n"
        "返回 JSON：{"
        "\"transitions\": [{\"at_seconds\": number, \"from_section\": str, \"to_section\": str, "
        "\"style\": one of allowed, "
        f"\"duration\": number (0.1-{max_dur:.2f}), "
        "\"catalog_block\": str|null (HyperFrames catalog name, 可空), "
        "\"reason\": str (≤30 字)}], "
        "\"cover\": {\"title\": str (≤12 字, 强冲击), \"subtitle\": str (≤18 字, 可空), "
        "\"palette\": [hex 颜色 2-3 个, 主色 + 强调色], "
        "\"layout\": one of [center, left, split, stacked], "
        "\"catalog_block\": str|null (HyperFrames cover catalog name, 可空), "
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
    if not from_sec or not to_sec or len(from_sec) > 30 or len(to_sec) > 30:
        return None
    reason = str(raw.get("reason", "") or "")[:60]
    catalog_block = raw.get("catalog_block")
    if isinstance(catalog_block, str):
        catalog_block = catalog_block.strip() or None
        if catalog_block and catalog_find_by_name(catalog_block) is None:
            catalog_block = None
    else:
        catalog_block = None
    return TransitionSuggestion(
        item_id=f"pkg-tr-{fallback_idx:02d}",
        at_seconds=at_s,
        from_section=from_sec,  # type: ignore[arg-type]
        to_section=to_sec,  # type: ignore[arg-type]
        style=style,  # type: ignore[arg-type]
        duration=dur,
        catalog_block=catalog_block,
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
    catalog_block = raw.get("catalog_block")
    if isinstance(catalog_block, str):
        catalog_block = catalog_block.strip() or None
        if catalog_block and catalog_find_by_name(catalog_block) is None:
            catalog_block = None
    else:
        catalog_block = None
    return CoverDesign(
        title=title,
        subtitle=subtitle,
        palette=palette,
        layout=layout,  # type: ignore[arg-type]
        catalog_block=catalog_block,
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
    frame = getattr(plan.settings, "frame_design", None)

    notes: list[str] = []
    transitions: list[TransitionSuggestion] = []
    cover: Optional[CoverDesign] = None

    system_prompt = _build_system_prompt(prefs, frame=frame)
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

    aggressive_variant = PackagingVariant(
        version_id="aggressive",
        version_label="强冲击版",
        transitions=transitions,
        cover=cover,
    )
    elegant_variant = _derive_elegant_variant(aggressive_variant, plan, prefs)

    rec = PackagingRecommendation(
        plan_id=plan.plan_id,
        versions=[aggressive_variant, elegant_variant],
        notes=notes,
    )

    if apply:
        _write_to_plan(plan, rec, prefs)
        notes.append(f"已落地：transitions={len(transitions)}，cover=1（采用 {aggressive_variant.version_id} 版）")

    return rec


def _derive_elegant_variant(
    aggressive: PackagingVariant,
    plan: Plan,
    prefs: PackagingPreferences,
) -> PackagingVariant:
    """从 aggressive 派生 elegant：转场柔化 + 封面文案温和 + 调色降饱和。

    设计取舍：本期只跑一次 LLM。强冲击版完全由 LLM 输出；
    高级感版按规则从强冲击版推导（用更柔的 dissolve/wipe、把封面 layout 改 center、
    palette 朝低饱和倾斜）。LLM 算力扩成 2 倍代价不划算，规则推导足够拉出区分度。
    """
    softer = {"hard_cut": "dissolve", "whip": "dissolve", "zoom": "wipe", "slide": "wipe"}
    elegant_trs: list[TransitionSuggestion] = []
    for tr in aggressive.transitions:
        new_style = softer.get(tr.style, tr.style)
        if new_style not in prefs.allowed_transition_styles:
            new_style = "dissolve" if "dissolve" in prefs.allowed_transition_styles else tr.style
        elegant_trs.append(tr.model_copy(update={
            "item_id": tr.item_id.replace("pkg-tr-", "pkg-tr-eleg-"),
            "style": new_style,  # type: ignore[arg-type]
            "duration": min(tr.duration * 1.5, prefs.max_transition_duration),
            "reason": f"高级感版：柔化为 {new_style}",
        }))

    elegant_cover: Optional[CoverDesign] = None
    if aggressive.cover is not None:
        elegant_cover = aggressive.cover.model_copy(update={
            "item_id": "pkg-cover-eleg",
            "layout": "center",
            "style_note": "高级感版：留白居中 + 低饱和",
        })
    return PackagingVariant(
        version_id="elegant",
        version_label="高级感版",
        transitions=elegant_trs,
        cover=elegant_cover,
    )


def _write_to_plan(
    plan: Plan,
    rec: PackagingRecommendation,
    prefs: PackagingPreferences,
) -> None:
    """把建议（取 versions[0] 即 aggressive 版）转成主轨 Scene.transition_in + 包装轨 cover。

    Stage-16 起 rec.versions 是多版本列表；落 plan 时永远采用 versions[0]，
    其余 variant 仅在 PackagingPanel UI 上预览，前端切换不会立刻 mutate plan
    （需要前端发新一次 PATCH 切版本——本期暂不支持）。
    """
    if not rec.versions:
        log.warning("[packaging] rec.versions 空，跳过落地")
        return
    primary = rec.versions[0]
    transitions = primary.transitions
    cover = primary.cover

    # 包装轨：保留 subtitle/title_bar/sticker，丢弃旧 transition / cover（cover 一会重写）
    keep = [it for it in plan.packaging_track if it.kind not in ("transition", "cover")]

    # 主轨：先清掉所有 transition_in（幂等）
    for sc in plan.main_track:
        sc.transition_in = None

    # 按 scene.start 建索引，便于 at_seconds → to_scene 反查
    scenes_by_start = sorted(plan.main_track, key=lambda s: s.start)

    transition_count = 0
    for tr in transitions:
        if tr.at_seconds <= 0 or tr.at_seconds >= plan.duration_seconds:
            continue
        target = None
        best_delta = 0.6
        for sc in scenes_by_start:
            if sc is plan.main_track[0]:
                continue
            delta = abs(sc.start - tr.at_seconds)
            if delta <= best_delta:
                best_delta = delta
                target = sc
        if target is None:
            log.info("[packaging] transition skip: 找不到 at=%.2fs 对应的 scene", tr.at_seconds)
            continue
        target.transition_in = SceneTransition(style=tr.style, duration=tr.duration)
        transition_count += 1

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
    if cover is not None:
        first_dur = plan.main_track[0].duration if plan.main_track else plan.duration_seconds
        cover_end = max(0.6, min(prefs.cover_duration, first_dur))
        new_items.append(PackagingItem(
            item_id=cover.item_id or "pkg-cover",
            kind="cover",
            start=0.0,
            end=cover_end,
            text=cover.title,
            style={
                "subtitle": cover.subtitle if prefs.cover_with_subtitle else None,
                "palette": cover.palette,
                "layout": cover.layout,
                "style_note": cover.style_note,
            },
        ))

    plan.packaging_track = keep + new_items
    plan_store.replace(plan)
    log.info("[packaging] plan=%s wrote %d scene.transition_in + cover=%s (variant=%s)",
             plan.plan_id, transition_count, cover is not None, primary.version_id)


# =========================================================================
# V2 —— 5 维度独立多候选推荐
# =========================================================================

_STICKER_POSITIONS = ("bottom-center", "top-right", "bottom-right", "middle")
_TITLE_BAR_POSITIONS = ("top", "middle")
_TITLE_BAR_FONT_SIZES = ("small", "medium", "large")
_SUBTITLE_FONT_SIZES = ("small", "medium", "large")
_SUBTITLE_POSITIONS = ("top", "middle", "bottom")
_SUBTITLE_BACKGROUNDS = ("none", "shadow", "gradient")


def _build_v2_system_prompt(prefs: PackagingPreferences, frame: Optional[FrameDesignSystem] = None) -> str:
    allowed = ", ".join(prefs.allowed_transition_styles)
    max_dur = prefs.max_transition_duration
    return (
        "你是短视频包装设计师。读完主轨分镜（每段含 scene_id / role / 起止 / 口播）后，"
        "为这条片子同时给出 5 类候选包装，每类 2-4 个供创作者挑选。\n"
        f"{_frame_design_block(frame)}"
        f"{_catalog_hint_block()}\n"
        "返回 JSON（严格用这些字段名，不要 markdown 包裹）：\n"
        "{\n"
        "  \"subtitle_styles\": [  // 2-3 个字幕样式备选\n"
        "    {\"candidate_id\": str, \"label\": str(≤20字), "
        "\"font_size\": one of [small,medium,large], "
        "\"position\": one of [top,middle,bottom], "
        "\"background\": one of [none,shadow,gradient], "
        "\"bilingual\": bool, \"rationale\": str(≤30字)}\n"
        "  ],\n"
        "  \"title_bars\": [  // 2-4 个标题条/卖点卡片，每条挂到具体 scene_id，时长 1.0-1.8s\n"
        "    {\"candidate_id\": str, \"text\": str(≤16字), \"target_scene_id\": str, "
        "\"start\": number, \"end\": number, "
        "\"font_size\": one of [small,medium,large], "
        "\"color\": hex, \"background_color\": hex, "
        "\"position\": one of [top,middle], \"rationale\": str(≤30字)}\n"
        "  ],\n"
        "  \"stickers\": [  // 2-4 个 CTA/强调短语，closing 必给 1 条；时长 0.6-1.2s\n"
        "    {\"candidate_id\": str, \"text\": str(≤8字), \"target_scene_id\": str, "
        "\"start\": number, \"end\": number, "
        "\"color\": hex, \"background_color\": hex, "
        "\"position\": one of [bottom-center,top-right,bottom-right,middle], "
        "\"rationale\": str(≤30字)}\n"
        "  ],\n"
        "  \"transition_bundles\": [  // 主轨每个相邻 section 切换点 1 个 bundle，每 bundle 2-3 个 option\n"
        "    {\"candidate_id\": str, \"at_seconds\": number, "
        "\"from_section\": str, \"to_section\": str, "
        f"\"options\": [{{\"style\": one of [{allowed}], "
        f"\"duration\": number(0.1-{max_dur:.2f}), "
        "\"catalog_block\": str|null, "
        "\"reason\": str(≤20字)}], "
        "\"rationale\": str(≤40字)}\n"
        "  ],\n"
        "  \"covers\": [  // 2-3 个不同调性封面方案\n"
        "    {\"candidate_id\": str, \"title\": str(≤12字), \"subtitle\": str(≤18字 可空), "
        "\"palette\": [hex,hex,hex], "
        "\"layout\": one of [center,left,split,stacked], "
        "\"catalog_block\": str|null, "
        "\"style_note\": str(≤30字), \"rationale\": str(≤30字)}\n"
        "  ]\n"
        "}\n"
        f"转场风格只能取 [{allowed}]，duration 必须 ≤ {max_dur:.2f}s。\n"
        "title_bars/stickers 的 start/end 必须落在所选 scene 的时间窗内。\n"
        "candidate_id 用短串（≤16 字符，仅 ascii）便于前端引用。"
    )


def _build_v2_user_prompt(plan: Plan) -> str:
    scene_lines = []
    for sc in plan.main_track:
        scene_lines.append(
            f"  - scene_id={sc.scene_id} role={sc.section} "
            f"{sc.start:.1f}-{sc.start + sc.duration:.1f}s · "
            f"{(sc.narration or '(无口播)')[:40]}"
        )
    brief = plan.brief or "(创作者未提供主题文本)"
    goal = plan.video_goal or "(创作者未提供 video_goal)"
    return (
        f"创作者主题：{brief}\n"
        f"video_goal：{goal}\n"
        f"plan_id：{plan.plan_id}\n"
        f"总时长：{plan.duration_seconds:.1f} 秒\n"
        f"主轨分镜：\n" + "\n".join(scene_lines)
    )


def _valid_hex(s: Any, fallback: str) -> str:
    if isinstance(s, str) and _HEX_RE.match(s.strip()):
        return s.strip().upper()
    return fallback


def _coerce_subtitle_style(raw: Any, idx: int) -> Optional[SubtitleStyleCandidate]:
    if not isinstance(raw, dict):
        return None
    fs = str(raw.get("font_size", "medium")).lower()
    if fs not in _SUBTITLE_FONT_SIZES:
        fs = "medium"
    pos = str(raw.get("position", "bottom")).lower()
    if pos not in _SUBTITLE_POSITIONS:
        pos = "bottom"
    bg = str(raw.get("background", "shadow")).lower()
    if bg not in _SUBTITLE_BACKGROUNDS:
        bg = "shadow"
    return SubtitleStyleCandidate(
        candidate_id=f"sub-{idx:02d}",
        label=str(raw.get("label") or f"字幕方案 {idx + 1}")[:40],
        font_size=fs,  # type: ignore[arg-type]
        position=pos,  # type: ignore[arg-type]
        background=bg,  # type: ignore[arg-type]
        bilingual=bool(raw.get("bilingual", False)),
        rationale=str(raw.get("rationale", "") or "结构匀称、可读性高")[:60],
    )


def _coerce_title_bar(
    raw: Any, idx: int, plan: Plan,
) -> Optional[TitleBarCandidate]:
    if not isinstance(raw, dict):
        return None
    text = str(raw.get("text", "")).strip()[:20]
    if not text:
        return None
    target_scene_id = str(raw.get("target_scene_id", "")).strip()
    scene = next((s for s in plan.main_track if s.scene_id == target_scene_id), None)
    if scene is None:
        return None
    try:
        start = float(raw.get("start", scene.start))
        end = float(raw.get("end", scene.start + min(1.5, scene.duration)))
    except (TypeError, ValueError):
        start, end = scene.start, scene.start + min(1.5, scene.duration)
    start = max(scene.start, start)
    end = min(scene.start + scene.duration, max(start + 0.4, end))
    if end <= start:
        return None
    fs = str(raw.get("font_size", "medium")).lower()
    if fs not in _TITLE_BAR_FONT_SIZES:
        fs = "medium"
    pos = str(raw.get("position", "top")).lower()
    if pos not in _TITLE_BAR_POSITIONS:
        pos = "top"
    return TitleBarCandidate(
        candidate_id=f"tb-{idx:02d}",
        text=text,
        target_scene_id=target_scene_id,
        start=start,
        end=end,
        font_size=fs,  # type: ignore[arg-type]
        color=_valid_hex(raw.get("color"), "#FFFFFF"),
        background_color=_valid_hex(raw.get("background_color"), "#14181F"),
        position=pos,  # type: ignore[arg-type]
        rationale=str(raw.get("rationale", "") or "结构强提示")[:60],
    )


def _coerce_sticker(
    raw: Any, idx: int, plan: Plan,
) -> Optional[StickerCandidate]:
    if not isinstance(raw, dict):
        return None
    text = str(raw.get("text", "")).strip()[:10]
    if not text:
        return None
    target_scene_id = str(raw.get("target_scene_id", "")).strip()
    scene = next((s for s in plan.main_track if s.scene_id == target_scene_id), None)
    if scene is None:
        return None
    try:
        start = float(raw.get("start", scene.start + max(0.0, scene.duration - 1.0)))
        end = float(raw.get("end", scene.start + scene.duration))
    except (TypeError, ValueError):
        start = scene.start
        end = scene.start + min(0.8, scene.duration)
    start = max(scene.start, start)
    end = min(scene.start + scene.duration, max(start + 0.3, end))
    if end <= start:
        return None
    pos = str(raw.get("position", "bottom-center"))
    if pos not in _STICKER_POSITIONS:
        pos = "bottom-center"
    return StickerCandidate(
        candidate_id=f"st-{idx:02d}",
        text=text,
        target_scene_id=target_scene_id,
        start=start,
        end=end,
        color=_valid_hex(raw.get("color"), "#FFE600"),
        background_color=_valid_hex(raw.get("background_color"), "#000000"),
        position=pos,  # type: ignore[arg-type]
        rationale=str(raw.get("rationale", "") or "强化 CTA")[:60],
    )


def _coerce_transition_bundle(
    raw: Any, idx: int, prefs: PackagingPreferences,
) -> Optional[TransitionCandidateBundle]:
    if not isinstance(raw, dict):
        return None
    try:
        at_s = float(raw.get("at_seconds", 0.0))
    except (TypeError, ValueError):
        return None
    from_sec = str(raw.get("from_section", "")).strip()
    to_sec = str(raw.get("to_section", "")).strip()
    if not from_sec or not to_sec:
        return None
    raw_options = raw.get("options") or []
    if not isinstance(raw_options, list):
        return None
    options: list[TransitionSuggestion] = []
    for opt_idx, opt in enumerate(raw_options[:3]):
        if not isinstance(opt, dict):
            continue
        style = str(opt.get("style", "")).strip()
        if style not in prefs.allowed_transition_styles:
            continue
        try:
            dur = float(opt.get("duration", 0.4))
        except (TypeError, ValueError):
            dur = 0.4
        dur = max(0.1, min(prefs.max_transition_duration, dur))
        opt_catalog = opt.get("catalog_block")
        if isinstance(opt_catalog, str):
            opt_catalog = opt_catalog.strip() or None
            if opt_catalog and catalog_find_by_name(opt_catalog) is None:
                opt_catalog = None
        else:
            opt_catalog = None
        options.append(TransitionSuggestion(
            item_id=f"pkg-tr-{idx:02d}-{opt_idx}",
            at_seconds=at_s,
            from_section=from_sec,  # type: ignore[arg-type]
            to_section=to_sec,  # type: ignore[arg-type]
            style=style,  # type: ignore[arg-type]
            duration=dur,
            catalog_block=opt_catalog,
            reason=str(opt.get("reason", "") or f"{from_sec}→{to_sec}")[:60],
        ))
    if not options:
        primary = prefs.allowed_transition_styles[0]
        options.append(TransitionSuggestion(
            item_id=f"pkg-tr-{idx:02d}-0",
            at_seconds=at_s,
            from_section=from_sec,  # type: ignore[arg-type]
            to_section=to_sec,  # type: ignore[arg-type]
            style=primary,  # type: ignore[arg-type]
            duration=min(0.4, prefs.max_transition_duration),
            reason=f"兜底 {from_sec}→{to_sec}",
        ))
    return TransitionCandidateBundle(
        candidate_id=f"tb-tr-{idx:02d}",
        at_seconds=at_s,
        from_section=from_sec,
        to_section=to_sec,
        options=options,
        rationale=str(raw.get("rationale", "") or f"{from_sec}→{to_sec} 切换")[:80],
    )


def _coerce_cover_candidate(
    raw: Any, idx: int, plan: Plan,
) -> Optional[CoverCandidate]:
    if not isinstance(raw, dict):
        return None
    title = str(raw.get("title", "")).strip()[:12]
    if not title:
        return None
    subtitle_raw = raw.get("subtitle")
    subtitle = str(subtitle_raw).strip()[:18] if isinstance(subtitle_raw, str) and subtitle_raw.strip() else None
    palette_raw = raw.get("palette") or []
    palette: list[str] = []
    if isinstance(palette_raw, list):
        for c in palette_raw[:3]:
            if isinstance(c, str) and _HEX_RE.match(c.strip()):
                palette.append(c.strip().upper())
    if not palette:
        palette = ["#FFE600", "#1F2937", "#FFFFFF"]
    layout = str(raw.get("layout", "center"))
    if layout not in _ALLOWED_LAYOUTS:
        layout = "center"
    cv_catalog = raw.get("catalog_block")
    if isinstance(cv_catalog, str):
        cv_catalog = cv_catalog.strip() or None
        if cv_catalog and catalog_find_by_name(cv_catalog) is None:
            cv_catalog = None
    else:
        cv_catalog = None
    return CoverCandidate(
        candidate_id=f"cv-{idx:02d}",
        title=title,
        subtitle=subtitle,
        palette=palette,
        layout=layout,  # type: ignore[arg-type]
        catalog_block=cv_catalog,
        style_note=str(raw.get("style_note", "") or "")[:60],
        rationale=str(raw.get("rationale", "") or "")[:60],
    )


def _rule_based_v2_candidates(plan: Plan, prefs: PackagingPreferences) -> dict[str, list]:
    """所有 LLM 失败时的规则兜底：每个维度给 2 个简单候选。"""
    subs = [
        SubtitleStyleCandidate(
            candidate_id="sub-00", label="底部中字｜阴影底",
            font_size="medium", position="bottom", background="shadow",
            bilingual=False, rationale="可读性高、占用画面少",
        ),
        SubtitleStyleCandidate(
            candidate_id="sub-01", label="底部大字｜渐变底",
            font_size="large", position="bottom", background="gradient",
            bilingual=False, rationale="信息密度高时拉满字号",
        ),
    ]
    title_bars: list[TitleBarCandidate] = []
    if plan.main_track:
        first = plan.main_track[0]
        title_bars.append(TitleBarCandidate(
            candidate_id="tb-00",
            text=(plan.brief or first.section or "开场标题")[:16],
            target_scene_id=first.scene_id,
            start=first.start,
            end=first.start + min(1.5, first.duration),
            font_size="large", color="#FFFFFF", background_color="#14181F",
            position="top", rationale="开场点题",
        ))
    stickers: list[StickerCandidate] = []
    if plan.main_track:
        last = plan.main_track[-1]
        cta_text = (plan.settings.cta or "点这里").strip()[:8] or "点这里"
        stickers.append(StickerCandidate(
            candidate_id="st-00",
            text=cta_text,
            target_scene_id=last.scene_id,
            start=last.start + max(0.0, last.duration - 1.0),
            end=last.start + last.duration,
            color="#FFE600", background_color="#000000",
            position="bottom-center",
            rationale="收尾 CTA",
        ))
    bundles: list[TransitionCandidateBundle] = []
    pairs = _section_pairs(plan.main_track)
    primary = prefs.allowed_transition_styles[0]
    for idx, (a, b) in enumerate(pairs):
        rule_pick: TransitionStyle = _RULE_TRANSITION.get((a.section, b.section), primary)
        if rule_pick not in prefs.allowed_transition_styles:
            rule_pick = primary
        opts = [TransitionSuggestion(
            item_id=f"pkg-tr-{idx:02d}-0",
            at_seconds=float(b.start),
            from_section=a.section,
            to_section=b.section,
            style=rule_pick,
            duration=min(0.4, prefs.max_transition_duration),
            reason=f"规则推荐 {rule_pick}",
        )]
        if len(prefs.allowed_transition_styles) >= 2:
            alt = prefs.allowed_transition_styles[1]
            if alt != rule_pick:
                opts.append(TransitionSuggestion(
                    item_id=f"pkg-tr-{idx:02d}-1",
                    at_seconds=float(b.start),
                    from_section=a.section,
                    to_section=b.section,
                    style=alt,
                    duration=min(0.4, prefs.max_transition_duration),
                    reason=f"备选 {alt}",
                ))
        bundles.append(TransitionCandidateBundle(
            candidate_id=f"tb-tr-{idx:02d}",
            at_seconds=float(b.start),
            from_section=a.section,
            to_section=b.section,
            options=opts,
            rationale=f"{a.section}→{b.section} 切换",
        ))
    covers = [
        CoverCandidate(
            candidate_id="cv-00",
            title=(plan.brief or plan.video_goal or "短视频封面").strip()[:12] or "短视频封面",
            subtitle=None,
            palette=["#FFE600", "#1F2937", "#FFFFFF"],
            layout="center",
            style_note="黑底黄字大标题居中",
            rationale="高对比、视线焦点",
        ),
        CoverCandidate(
            candidate_id="cv-01",
            title=(plan.video_goal or plan.brief or "今天聊点干货").strip()[:12] or "今天聊点干货",
            subtitle="3 秒抓住你",
            palette=["#FFFFFF", "#0EA5E9", "#FACC15"],
            layout="split",
            style_note="撞色分屏，干净专业",
            rationale="信息流风、不浮躁",
        ),
    ]
    return {
        "subtitle_styles": subs,
        "title_bars": title_bars,
        "stickers": stickers,
        "transition_bundles": bundles,
        "covers": covers,
    }


async def recommend_packaging_v2(
    plan: Plan,
    *,
    preferences: Optional[PackagingPreferences] = None,
) -> PackagingRecommendationV2:
    """LLM 一次性出 5 维度独立候选。失败时按规则兜底。不 mutate plan。

    路由端在调用前已合并 prefs 并 expand_preset。
    """
    raw_prefs = preferences or plan.settings.packaging_prefs
    prefs = expand_preset(raw_prefs)
    frame = getattr(plan.settings, "frame_design", None)

    notes: list[str] = []
    system_prompt = _build_v2_system_prompt(prefs, frame=frame)
    user_prompt = _build_v2_user_prompt(plan)

    data: Any = None
    try:
        llm = get_llm_client()
        data = await llm.complete_json(
            system_prompt, user_prompt, temperature=prefs.llm_temperature,
        )
    except LLMError as exc:
        log.warning("[packaging-v2] LLM failed: %s; using rule fallback", exc)
        notes.append(f"LLM 失败，规则兜底：{exc}")
    except Exception as exc:  # noqa: BLE001
        log.warning("[packaging-v2] LLM unexpected: %s; using rule fallback", exc)
        notes.append(f"LLM 异常，规则兜底：{exc}")

    subtitle_styles: list[SubtitleStyleCandidate] = []
    title_bars: list[TitleBarCandidate] = []
    stickers: list[StickerCandidate] = []
    transition_bundles: list[TransitionCandidateBundle] = []
    covers: list[CoverCandidate] = []

    if isinstance(data, dict):
        for idx, raw in enumerate((data.get("subtitle_styles") or [])[:4]):
            c = _coerce_subtitle_style(raw, idx)
            if c is not None:
                subtitle_styles.append(c)
        for idx, raw in enumerate((data.get("title_bars") or [])[:6]):
            c = _coerce_title_bar(raw, idx, plan)
            if c is not None:
                title_bars.append(c)
        for idx, raw in enumerate((data.get("stickers") or [])[:6]):
            c = _coerce_sticker(raw, idx, plan)
            if c is not None:
                stickers.append(c)
        for idx, raw in enumerate((data.get("transition_bundles") or [])[:8]):
            c = _coerce_transition_bundle(raw, idx, prefs)
            if c is not None:
                transition_bundles.append(c)
        for idx, raw in enumerate((data.get("covers") or [])[:4]):
            c = _coerce_cover_candidate(raw, idx, plan)
            if c is not None:
                covers.append(c)

    fallback = _rule_based_v2_candidates(plan, prefs)
    if not subtitle_styles:
        subtitle_styles = fallback["subtitle_styles"]
        notes.append("subtitle_styles 走规则兜底")
    if not title_bars:
        title_bars = fallback["title_bars"]
        notes.append("title_bars 走规则兜底")
    if not stickers:
        stickers = fallback["stickers"]
        notes.append("stickers 走规则兜底")
    if not transition_bundles:
        transition_bundles = fallback["transition_bundles"]
        notes.append("transition_bundles 走规则兜底")
    if not covers:
        covers = fallback["covers"]
        notes.append("covers 走规则兜底")

    # 对齐 transition 时间到真实段落切换点（防止 LLM 凭空写 at_seconds）
    real_pairs_by_kind: dict[tuple[str, str], float] = {
        (a.section, b.section): float(b.start) for a, b in _section_pairs(plan.main_track)
    }
    aligned_bundles: list[TransitionCandidateBundle] = []
    for bundle in transition_bundles:
        anchor = real_pairs_by_kind.get((bundle.from_section, bundle.to_section))
        if anchor is not None and abs(bundle.at_seconds - anchor) > 0.5:
            new_opts = [opt.model_copy(update={"at_seconds": anchor}) for opt in bundle.options]
            aligned_bundles.append(bundle.model_copy(update={"at_seconds": anchor, "options": new_opts}))
        else:
            aligned_bundles.append(bundle)
    transition_bundles = aligned_bundles

    return PackagingRecommendationV2(
        plan_id=plan.plan_id,
        subtitle_styles=subtitle_styles,
        title_bars=title_bars,
        stickers=stickers,
        transition_bundles=transition_bundles,
        covers=covers,
        notes=notes,
    )


# ---------------------------------------------------------------------------
# 场景级推荐：单 scene + 自然语言 hint → 单个 PackagingItem
# ---------------------------------------------------------------------------

def _build_scene_scoped_system_prompt(
    kind: str,
    prefs: PackagingPreferences,
    frame: Optional[FrameDesignSystem],
) -> str:
    """单 scene + 单 kind 的系统提示，只要一条候选。"""
    common_head = (
        "你是短视频包装设计师。创作者已经选定了**一个分镜片段**，并给出了自然语言诉求。\n"
        "请只为这一个分镜生成 1 条最贴合的包装组件，类型固定为「{kind}」。\n"
        f"{_frame_design_block(frame)}"
        f"{_catalog_hint_block()}\n"
        "**严禁** mutate 别的 scene；**严禁**给多个候选；**严禁**返回 markdown 包裹。\n"
        "**严禁**忽略创作者诉求——若诉求与 frame 冲突，以创作者诉求为先；若诉求与画面/口播无关也要尊重。\n"
    ).format(kind=kind)
    if kind == "title_bar":
        body = (
            "返回 JSON：\n"
            "{\n"
            "  \"text\": str(≤16字, 强卖点/概念词，禁口播原文),\n"
            "  \"font_size\": one of [small,medium,large],\n"
            "  \"color\": hex, \"background_color\": hex,\n"
            "  \"position\": one of [top,middle],\n"
            "  \"rationale\": str(≤30字，告诉创作者为什么这样设计)\n"
            "}\n"
            "时长由系统自动取本镜全段（创作者会在落轨后手动拉短）。颜色应贴合 frame 主色板。"
        )
    elif kind == "sticker":
        body = (
            "返回 JSON：\n"
            "{\n"
            "  \"text\": str(≤8字, CTA/强调短语, 如『立即购买』『颜值在线』),\n"
            "  \"color\": hex, \"background_color\": hex,\n"
            "  \"position\": one of [bottom-center,top-right,bottom-right,middle],\n"
            "  \"rationale\": str(≤30字)\n"
            "}\n"
            "时长由系统自动取本镜全段。颜色突出但不喧宾夺主。"
        )
    elif kind == "cover":
        body = (
            "返回 JSON：\n"
            "{\n"
            "  \"title\": str(≤12字, 钩子标题),\n"
            "  \"subtitle\": str(≤18字, 可空),\n"
            "  \"palette\": [hex, hex, hex],\n"
            "  \"layout\": one of [center,left,split,stacked],\n"
            "  \"catalog_block\": str|null,\n"
            "  \"style_note\": str(≤30字, 设计描述),\n"
            "  \"rationale\": str(≤30字)\n"
            "}\n"
            f"封面只占片头 0~{prefs.cover_duration:.2f}s。palette 优先来自 frame 主色板。"
        )
    else:
        body = "返回 JSON 对象，字段语义参见前文规则。"
    return common_head + body


def _build_scene_scoped_user_prompt(
    plan: Plan,
    scene: Scene,
    hint: str,
) -> str:
    brief = plan.brief or "(创作者未提供主题文本)"
    goal = plan.video_goal or "(创作者未提供 video_goal)"
    scene_window = (
        f"scene_id={scene.scene_id} role={scene.section} "
        f"窗口 {scene.start:.2f}-{scene.start + scene.duration:.2f}s "
        f"(时长 {scene.duration:.2f}s)"
    )
    narration = (scene.narration or "(无口播)").strip()
    subject = (scene.shot_subject or "").strip()
    user_hint = hint.strip() or "(创作者未给出额外诉求，按场景与 frame 自由发挥)"
    return (
        f"创作者主题：{brief}\n"
        f"video_goal：{goal}\n"
        f"plan_id：{plan.plan_id}\n"
        f"目标分镜：{scene_window}\n"
        f"分镜口播：{narration}\n"
        f"分镜主体（subject 锚点，不可同义化）：{subject or '(未指定)'}\n"
        f"创作者自然语言诉求：{user_hint}\n"
    )


def _next_unique_item_id(plan: Plan, kind: str) -> str:
    prefix = {"title_bar": "pkg-tb", "sticker": "pkg-st", "cover": "pkg-cv"}.get(kind, f"pkg-{kind}")
    used = {it.item_id for it in plan.packaging_track}
    i = 1
    while f"{prefix}-r{i}" in used:
        i += 1
    return f"{prefix}-r{i}"


def _scene_scoped_dict_to_item(
    plan: Plan,
    scene: Scene,
    kind: str,
    raw: Any,
    prefs: PackagingPreferences,
) -> Optional[tuple[PackagingItem, str]]:
    if not isinstance(raw, dict):
        return None
    rationale = str(raw.get("rationale", "") or "")[:60]
    if kind == "title_bar":
        text = str(raw.get("text", "")).strip()[:20]
        if not text:
            return None
        # 创作者明确要求：包装组件时长与本镜一致，覆盖整段；后续可点击组件块手动改时间。
        start = scene.start
        end = scene.start + scene.duration
        if end <= start:
            return None
        fs = str(raw.get("font_size", "medium")).lower()
        if fs not in _TITLE_BAR_FONT_SIZES:
            fs = "medium"
        pos = str(raw.get("position", "top")).lower()
        if pos not in _TITLE_BAR_POSITIONS:
            pos = "top"
        item = PackagingItem(
            item_id=_next_unique_item_id(plan, "title_bar"),
            kind="title_bar",
            start=start,
            end=end,
            text=text,
            style={
                "font_size": fs,
                "color": _valid_hex(raw.get("color"), "#FFFFFF"),
                "background_color": _valid_hex(raw.get("background_color"), "#14181F"),
                "position": pos,
            },
        )
        return item, rationale or "贴合分镜重点"
    if kind == "sticker":
        text = str(raw.get("text", "")).strip()[:10]
        if not text:
            return None
        # 同 title_bar：贴纸默认贴满本镜全段，让创作者后续自己拉短。
        start = scene.start
        end = scene.start + scene.duration
        if end <= start:
            return None
        pos = str(raw.get("position", "bottom-center")).lower()
        if pos not in ("bottom-center", "top-right", "bottom-right", "middle"):
            pos = "bottom-center"
        item = PackagingItem(
            item_id=_next_unique_item_id(plan, "sticker"),
            kind="sticker",
            start=start,
            end=end,
            text=text,
            style={
                "color": _valid_hex(raw.get("color"), "#FFE600"),
                "background_color": _valid_hex(raw.get("background_color"), "#000000"),
                "position": pos,
            },
        )
        return item, rationale or "强化片段卖点"
    if kind == "cover":
        title = str(raw.get("title", "")).strip()[:16]
        if not title:
            return None
        first_dur = plan.main_track[0].duration if plan.main_track else prefs.cover_duration
        cover_end = max(0.6, min(prefs.cover_duration, first_dur))
        layout = str(raw.get("layout", "center")).lower()
        if layout not in _ALLOWED_LAYOUTS:
            layout = "center"
        palette = raw.get("palette") or []
        if not isinstance(palette, list):
            palette = []
        clean_palette = [
            _valid_hex(p, "#000000") for p in palette[:5]
            if isinstance(p, str)
        ]
        item = PackagingItem(
            item_id=_next_unique_item_id(plan, "cover"),
            kind="cover",
            start=0.0,
            end=cover_end,
            text=title,
            style={
                "subtitle": str(raw.get("subtitle", "") or "")[:24] or None,
                "palette": clean_palette,
                "layout": layout,
                "style_note": str(raw.get("style_note", "") or "")[:60],
            },
        )
        return item, rationale or "片头钩子"
    return None


async def recommend_packaging_for_scene(
    plan: Plan,
    *,
    scene_id: str,
    kind: str,
    hint: str,
) -> tuple[PackagingItem, str]:
    """单 scene + 自然语言 hint → 单 PackagingItem（草稿，未写 plan）。

    kind 仅支持 title_bar / sticker / cover；前端调用 /packaging/items/place 落盘。
    """
    if kind not in ("title_bar", "sticker", "cover"):
        raise ValueError(f"unsupported kind: {kind}")
    scene = next((s for s in plan.main_track if s.scene_id == scene_id), None)
    if scene is None:
        raise ValueError(f"scene_id 不存在：{scene_id}")
    prefs = expand_preset(plan.settings.packaging_prefs)
    frame = getattr(plan.settings, "frame_design", None)
    system_prompt = _build_scene_scoped_system_prompt(kind, prefs, frame)
    user_prompt = _build_scene_scoped_user_prompt(plan, scene, hint)
    log.info(
        "[packaging-scene] scene=%s kind=%s hint_len=%d",
        scene_id, kind, len(hint or ""),
    )

    data: Any = None
    try:
        llm = get_llm_client()
        data = await llm.complete_json(
            system_prompt, user_prompt, temperature=prefs.llm_temperature,
        )
    except LLMError as exc:
        log.warning("[packaging-scene] LLM failed: %s", exc)
        raise
    out = _scene_scoped_dict_to_item(plan, scene, kind, data, prefs)
    if out is None:
        raise LLMError(f"LLM 返回不合法，无法生成 {kind}")
    return out


def apply_selection_to_plan(
    plan: Plan,
    selection: "PackagingSelectionLike",
) -> Plan:
    """把用户挑选的候选写到 plan.packaging_track + Scene.transition_in。

    selection 是 PackagingSelection（局部 import 避免循环依赖）。
    无状态：所有候选都来自 selection.recommendation 自带的快照。
    """
    from ...schemas import PackagingSelection  # local import

    sel: PackagingSelection = selection  # type: ignore[assignment]
    rec = sel.recommendation
    sub_lookup = {c.candidate_id: c for c in rec.subtitle_styles}
    tb_lookup = {c.candidate_id: c for c in rec.title_bars}
    st_lookup = {c.candidate_id: c for c in rec.stickers}
    cv_lookup = {c.candidate_id: c for c in rec.covers}
    bundle_lookup = {c.candidate_id: c for c in rec.transition_bundles}

    # 1. 清主轨 transition_in
    for sc in plan.main_track:
        sc.transition_in = None

    # 2. 应用转场选择
    scenes_by_start = sorted(plan.main_track, key=lambda s: s.start)
    for bundle_id, picked_style in sel.transition_selections.items():
        bundle = bundle_lookup.get(bundle_id)
        if bundle is None:
            continue
        opt = next((o for o in bundle.options if o.style == picked_style), None)
        if opt is None:
            continue
        target = None
        best_delta = 0.6
        for sc in scenes_by_start:
            if sc is plan.main_track[0]:
                continue
            delta = abs(sc.start - opt.at_seconds)
            if delta <= best_delta:
                best_delta = delta
                target = sc
        if target is None:
            continue
        target.transition_in = SceneTransition(style=opt.style, duration=opt.duration)

    # 3. 重建 packaging_track（保留口播字幕：start 时根据选中的 subtitle_style 重新生成 style）
    sub_style = sub_lookup.get(sel.subtitle_style_id) if sel.subtitle_style_id else None
    new_packaging: list[PackagingItem] = []

    # 3a. 字幕：每条 scene narration 一条 subtitle，沿用选中样式（无选则继承 prefs）。
    #     由 subtitle_enabled 单独控制（与 TTS 解耦：可以只上字幕不口播，反之亦然）。
    #     scene.text_card_spec 非空的段跳过——字卡画面本身已经显示主副标，再叠字幕会重复打架。
    prefs_eff = expand_preset(plan.settings.packaging_prefs)
    if plan.settings.subtitle_enabled:
        for idx, sc in enumerate(plan.main_track):
            if sc.text_card_spec is not None:
                continue
            sub_text = (sc.narration or "").strip()
            if not sub_text:
                continue
            if sub_style is not None:
                style_dict = {
                    "font_size": sub_style.font_size,
                    "position": sub_style.position,
                    "background": sub_style.background,
                    "bilingual": sub_style.bilingual,
                }
            else:
                style_dict = {
                    "font_size": prefs_eff.subtitle_font_size,
                    "position": prefs_eff.subtitle_position,
                    "background": prefs_eff.subtitle_background,
                    "bilingual": prefs_eff.subtitle_bilingual,
                }
            new_packaging.append(PackagingItem(
                item_id=f"pkg-sub-{idx}",
                kind="subtitle",
                start=sc.start,
                end=sc.start + sc.duration,
                text=sub_text,
                style=style_dict,
            ))

    # 3b. 标题条
    for tb_id in sel.title_bar_ids:
        c = tb_lookup.get(tb_id)
        if c is None:
            continue
        new_packaging.append(PackagingItem(
            item_id=f"pkg-tb-{c.candidate_id}",
            kind="title_bar",
            start=c.start,
            end=c.end,
            text=c.text,
            style={
                "font_size": c.font_size,
                "color": c.color,
                "background_color": c.background_color,
                "position": c.position,
            },
        ))

    # 3c. 贴纸
    for st_id in sel.sticker_ids:
        c = st_lookup.get(st_id)
        if c is None:
            continue
        new_packaging.append(PackagingItem(
            item_id=f"pkg-st-{c.candidate_id}",
            kind="sticker",
            start=c.start,
            end=c.end,
            text=c.text,
            style={
                "color": c.color,
                "background_color": c.background_color,
                "position": c.position,
            },
        ))

    # 3d. 封面
    cover_c = cv_lookup.get(sel.cover_id) if sel.cover_id else None
    if cover_c is not None and plan.main_track:
        first_dur = plan.main_track[0].duration
        cover_end = max(0.6, min(prefs_eff.cover_duration, first_dur))
        new_packaging.append(PackagingItem(
            item_id=f"pkg-cv-{cover_c.candidate_id}",
            kind="cover",
            start=0.0,
            end=cover_end,
            text=cover_c.title,
            style={
                "subtitle": cover_c.subtitle if prefs_eff.cover_with_subtitle else None,
                "palette": cover_c.palette,
                "layout": cover_c.layout,
                "style_note": cover_c.style_note,
            },
        ))

    plan.packaging_track = new_packaging
    plan_store.replace(plan)
    log.info(
        "[packaging-v2] plan=%s applied: subs=%d, title_bars=%d, stickers=%d, transitions=%d, cover=%s",
        plan.plan_id,
        sum(1 for it in new_packaging if it.kind == "subtitle"),
        len(sel.title_bar_ids),
        len(sel.sticker_ids),
        sum(1 for sc in plan.main_track if sc.transition_in),
        sel.cover_id or "none",
    )
    return plan


# typing forward-ref helper
PackagingSelectionLike = Any
