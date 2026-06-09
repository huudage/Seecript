"""Copy Outline Agent —— 字卡画面的"分析阶段"：先帮用户出一份字卡 spec 推荐，再让用户调参后真渲染。

stage-19 起 copy 动作的语义从『写口播一句』改成『生成个性字卡画面』——
LLM 不再写一句话给后置 TTS，而是直接策划一份 TextCardSpec：主标 / 副标 /
字体 / 布局 / 配色 / 动画 / emoji。前端在 CSS 实时预览里改参，最终发回后端
让 ffmpeg 真渲染 mp4 落到内容轨。

调用入口：`server/app/routers/gap.py:POST /api/gap/copy-outline`。
失败兜底：本地按段落角色 + 全局关键词合成默认 spec，前端永远能拿到数据。
"""
from __future__ import annotations

import logging
import re
from typing import Optional

from ...schemas import (
    AdaptedSection,
    CopyOutline,
    FrameDesignSystem,
    Gap,
    Plan,
    TextCardSpec,
)
from ..llm_client import LLMError, _extract_json, get_llm_client
from .preference import preference_hint

log = logging.getLogger("seecript.agent.copy_outline")


# 系统 prompt 同时是 mock 路由指纹：必须含 "copy_outline"。
_COPY_OUTLINE_SYSTEM = (
    "你是短视频字卡画面策划。给定一段视频的角色 / 主题 / 内容 / 整体调性 / 平台 / 关键词 / "
    "已选 frame 设计系统（色板/字体/动效），你要直接策划一张『字卡画面』——这一段没有视频素材时，"
    "靠纯文字大字 + 配色 + 动画撑起视觉。你产出一份 copy_outline，由前端面板加载、用户微调，"
    "最后让 ffmpeg 渲染。\n\n"
    "—— 字卡画面的本质 ——\n"
    "字卡 = 主标语 + 副标语 + 背景 + 字体 + 动画 + 装饰 emoji。\n"
    "它替代『当下没合适视频素材的那一段』，所以必须自带视觉冲击与信息密度，**且要和这一片的"
    "整体画面调性（frame 设计系统）保持一致**——否则插进来就像贴片广告。\n"
    "比口播更挑核心：句子要短、要醒目、不能注释化。\n\n"
    "—— 输出字段 ——\n"
    "• main_text：主标语（≤20 字最佳，最多 24 字）。占满画面 60% 视觉权重。\n"
    "• sub_text：副标语（可空，≤30 字）。注释 / 反问 / 数据补充。\n"
    "• core_message：你认为本段最该传达的信息（≤30 字），用于回放给用户校对。\n"
    "• emotional_hook：anxiety / wow / anticipation / twist / resonance（决定字体 + 动画推荐；"
    "**配色不再由 hook 单独决定**，详见下条 frame 契约）\n"
    "• must_include_keywords：从全局关键词里挑 1-2 个本段必须承载的，要求已嵌入 main_text 或 sub_text。\n"
    "• recommended_spec：推荐 TextCardSpec，字段：\n"
    "    - font_family: bold_sans | serif_classic | handwriting | tech_mono\n"
    "    - layout: center | top | bottom | split_top_bottom\n"
    "    - bg_mode: solid | gradient | image_blur | dark_overlay\n"
    "    - bg_color: '#RRGGBB' 6 位 hex —— 详见 frame 契约\n"
    "    - text_color: '#RRGGBB'\n"
    "    - accent_color: '#RRGGBB'（副标 / emoji 装饰用）\n"
    "    - animation: fade_in | typewriter | bounce_word | zoom_pop\n"
    "    - emoji_decor: ['✨'] 0-3 个 emoji 字符；不需要时给空数组\n"
    "    - duration_seconds: 字卡时长，跟段落时长走\n"
    "    - font_size_pct: 0.8-1.4，正文短/单句给 1.0-1.1，标语长就调到 0.85；强情绪段可拉到 1.2+\n"
    "• tone_lean：在全局调性基础上的微调，≤20 字（『开场再紧』『收尾留余韵』）\n\n"
    "—— **frame 契约（颜色优先级最高，强约束）** ——\n"
    "用户上传/选择的 frame 设计系统是整片视觉锚点，**字卡颜色必须从 frame 派生**：\n"
    "1. 当 user prompt 给出 `frame.palette = [c0, c1, c2, ...]` 时：\n"
    "   • bg_color = frame.background_color（若给）；否则取 palette 中明度最低/最高的那一色（与文字色形成≥4.5对比）\n"
    "   • text_color = palette[0]（primary）；若与 bg_color 对比不足，挑 palette 中对比最高的一色\n"
    "   • accent_color = palette[1]（accent）若有；否则 palette[2]，否则按 hook 预设\n"
    "2. 当 frame 没有 palette 时，才回退到 hook 预设配色：\n"
    "   • anxiety       暗背景 + 警示色（深红/橘红 #DC2626 / #F97316）\n"
    "   • wow           亮背景 + 高对比（亮黄 #FACC15 / 电光蓝 #38BDF8）\n"
    "   • anticipation  渐变背景（深紫到亮橙）\n"
    "   • twist         双色对撞（黑底亮金 / 白底深红）\n"
    "   • resonance     暖色 / 米白底（#FFF7ED / #FEF3C7）\n"
    "3. 段落内容情绪可微调饱和度/亮度，但**不可改变 frame 主色相**——比如 frame 是冷蓝色板，不能给暖橙背景；\n"
    "   段落 happy/wow 时把 palette 提亮 10-15%，段落 anxiety/twist 时压暗 10-15%、加大对比。\n"
    "4. font_family 也优先看 frame.typography_display：含 serif/明朝/Times 字样 → serif_classic；含 mono/code → tech_mono；\n"
    "   含 hand/script → handwriting；其余/无 → bold_sans。无 frame 字体提示时再用 hook 预设。\n"
    "5. animation 看 frame.motion_density：minimal → fade_in / typewriter；balanced → 按 hook 推荐；"
    "kinetic → 优先 zoom_pop / bounce_word。\n\n"
    "—— hook → 字体 / 动画 默认（仅当 frame 无字体/动效约束时使用） ——\n"
    "• anxiety  bold_sans + zoom_pop\n"
    "• wow      tech_mono + zoom_pop\n"
    "• anticipation bold_sans + typewriter\n"
    "• twist    serif_classic + bounce_word\n"
    "• resonance handwriting / serif_classic + fade_in\n"
    "若段落是 opening / hook → 动画偏 zoom_pop / typewriter；climax/peak → bounce_word / zoom_pop；"
    "closing/closer → fade_in。\n\n"
    "—— 决策原则 ——\n"
    "1. main_text 优先承载『本段内容要求 content_description』+ 全局关键词\n"
    "2. 颜色一定要是合法的 6 位 hex（#RRGGBB），不要写成 'red' 这种名字\n"
    "3. emoji_decor 只在情绪强烈时才用（wow / anxiety），平淡段落给空数组\n"
    "4. 不许在文案里出现段落角色名（hook/opening/climax 等）与『本段』『第 X 段』元数据自指\n"
    "5. duration_seconds 紧跟段落时长（用户给定），不要自己加长\n"
    "6. **rationale**：在 thinking 里至少有一条说明你怎么从 frame.palette / 段落内容 推到这套配色\n\n"
    "—— 输出 JSON ——\n"
    "{\"outline\": {\"main_text\": \"...\", \"sub_text\": \"...\", \"core_message\": \"...\", "
    "\"emotional_hook\": \"wow\", \"must_include_keywords\": [\"...\"], "
    "\"recommended_spec\": {\"font_family\":\"bold_sans\",\"layout\":\"center\","
    "\"bg_mode\":\"solid\",\"bg_color\":\"#0F172A\",\"text_color\":\"#FFFFFF\","
    "\"accent_color\":\"#22D3EE\",\"animation\":\"zoom_pop\",\"emoji_decor\":[\"✨\"],"
    "\"duration_seconds\":4.0,\"font_size_pct\":1.0}, \"tone_lean\": \"...\"}, "
    "\"thinking\": [\"识别本段核心...\", \"决定情绪钩子...\", \"配色与字体（说明从 frame 还是 hook 派生）...\"]}\n"
    "thinking 是 2-4 条短句（每条 ≤30 字），讲清你怎么从段落上下文 + frame 设计系统推到这份字卡 spec。"
)


_VALID_HOOKS = {"anxiety", "wow", "anticipation", "twist", "resonance"}
_VALID_FONTS = {"bold_sans", "serif_classic", "handwriting", "tech_mono"}
_VALID_LAYOUTS = {"center", "top", "bottom", "split_top_bottom"}
_VALID_BG_MODES = {"solid", "gradient", "image_blur", "dark_overlay"}
_VALID_ANIMATIONS = {"fade_in", "typewriter", "bounce_word", "zoom_pop"}

_HEX_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")


# 情绪钩子 → 默认配色 / 字体 / 动画
_HOOK_PRESET: dict[str, dict] = {
    "anxiety": dict(
        font_family="bold_sans",
        bg_mode="dark_overlay",
        bg_color="#0F172A",
        text_color="#F97316",
        accent_color="#FCA5A5",
        animation="zoom_pop",
    ),
    "wow": dict(
        font_family="tech_mono",
        bg_mode="solid",
        bg_color="#0B1220",
        text_color="#FACC15",
        accent_color="#38BDF8",
        animation="zoom_pop",
    ),
    "anticipation": dict(
        font_family="bold_sans",
        bg_mode="gradient",
        bg_color="#1E1B4B",
        text_color="#FBBF24",
        accent_color="#A78BFA",
        animation="typewriter",
    ),
    "twist": dict(
        font_family="serif_classic",
        bg_mode="solid",
        bg_color="#111111",
        text_color="#F5D547",
        accent_color="#EF4444",
        animation="bounce_word",
    ),
    "resonance": dict(
        font_family="handwriting",
        bg_mode="solid",
        bg_color="#FFF7ED",
        text_color="#1F2937",
        accent_color="#DB7C5C",
        animation="fade_in",
    ),
}


async def generate_copy_outline(
    gap: Gap,
    plan: Optional[Plan],
    section: Optional[AdaptedSection],
    *,
    user_hint: str = "",
) -> tuple[CopyOutline, list[str]]:
    """根据 gap + section + plan.settings 让 LLM 给出字卡画面大纲。

    返回 `(outline, thinking)`。失败时回落本地合成 outline + 兜底思考说明。
    """
    hint = (user_hint or "").strip()[:200]
    role = section.role if section else gap.section
    theme = (section.theme if section else "") or "（无主题）"
    content_desc = (section.content_description if section else "").strip()
    duration = float(section.duration_seconds) if section else 4.0

    brief = (plan.brief or "").strip() if plan else ""
    goal = (plan.video_goal or "").strip() if plan else ""
    settings = plan.settings if plan else None
    tone = settings.tone if settings else ""
    platform = settings.target_platform if settings else ""
    cta = (settings.cta or "").strip() if settings else ""
    keywords = list(settings.keywords) if settings else []

    user_lines: list[str] = [
        f"段落角色：{role}",
        f"段落主题：{theme}",
        f"段落时长：约 {duration:.1f}s",
        f"段落内容要求：{content_desc or '（无）'}",
        f"原始槽位需求：{gap.requirement}",
    ]
    if brief:
        user_lines.append(f"视频整体主题：{brief}")
    if goal:
        user_lines.append(f"视频要求与目的：{goal}")
    if tone:
        user_lines.append(f"全局调性：{tone}")
    if platform:
        user_lines.append(f"目标平台：{platform}")
    if cta:
        user_lines.append(f"结尾 CTA：{cta}")
    user_lines.append(f"全局关键词：{', '.join(keywords) if keywords else '（无）'}")
    if settings is not None:
        user_lines.append(preference_hint(settings.migration_preference))
    frame = getattr(settings, "frame_design", None) if settings else None
    if frame is not None:
        # 把 frame 拆开喂——LLM 才好按"contract"派色，不是糊一行
        fd_palette_block = _frame_palette_block(frame)
        if fd_palette_block:
            user_lines.append(fd_palette_block)
        if frame.background_color:
            user_lines.append(f"frame.background_color：{frame.background_color}（如设置则字卡 bg_color 优先取此值）")
        type_parts: list[str] = []
        if frame.typography_display:
            type_parts.append(f"标题={frame.typography_display}")
        if frame.typography_body:
            type_parts.append(f"正文={frame.typography_body}")
        if frame.typography_mono:
            type_parts.append(f"等宽={frame.typography_mono}")
        if type_parts:
            user_lines.append("frame.typography：" + " | ".join(type_parts) + "（决定 font_family；含 serif→serif_classic，含 mono→tech_mono，含 hand/script→handwriting，否则 bold_sans）")
        misc_parts: list[str] = []
        if frame.preset and frame.preset != "custom":
            misc_parts.append(f"预设={frame.preset}")
        if frame.motion_density and frame.motion_density != "balanced":
            misc_parts.append(f"动效密度={frame.motion_density}")
        if frame.grain_overlay:
            misc_parts.append("颗粒纹理=on")
        if frame.vignette:
            misc_parts.append("暗角=on")
        if frame.notes:
            misc_parts.append(f"备注={frame.notes}")
        if misc_parts:
            user_lines.append("frame 其他：" + " | ".join(misc_parts))
        user_lines.append(
            "**约束**：bg_color/text_color/accent_color 必须从 frame.palette + frame.background_color 派生；"
            "段落情绪只能微调饱和度/亮度（±10-15%），不能改色相。"
        )
    else:
        user_lines.append("frame.md 未设置：可按 hook 预设挑色，但仍要保证 bg/text 对比 ≥4.5。")
    if hint:
        user_lines.append(f"创作者额外提示：{hint}")
    user_lines.append("请输出 outline + thinking 的 JSON（含 recommended_spec 完整字段）。")
    user = "\n".join(user_lines)

    llm = get_llm_client()
    try:
        # outline JSON 实际 < 600 token；显式 max_tokens=900 比默认 2048 快 ~25%（Doubao Lite）
        text = await llm.complete(_COPY_OUTLINE_SYSTEM, user, max_tokens=900)
        data = _extract_json(text) if text else None
        if isinstance(data, dict):
            outline = _parse_outline(data.get("outline"), duration, keywords, frame)
            thinking_raw = data.get("thinking")
            thinking: list[str] = []
            if isinstance(thinking_raw, list):
                thinking = [str(x).strip()[:60] for x in thinking_raw if str(x).strip()][:4]
            if outline.main_text:
                log.info(
                    "[copy-outline] gap=%s role=%s ok hook=%s font=%s anim=%s",
                    gap.gap_id, role, outline.emotional_hook,
                    outline.recommended_spec.font_family, outline.recommended_spec.animation,
                )
                return outline, thinking
        log.warning("[copy-outline] gap=%s LLM 返回不合法 → fallback", gap.gap_id)
    except (LLMError, ValueError, Exception) as exc:  # noqa: BLE001
        log.warning("[copy-outline] gap=%s LLM 失败 → fallback：%s", gap.gap_id, exc)

    return _fallback_outline(gap, section, theme, duration, keywords, frame), [
        "LLM 暂时不可用，使用本地兜底字卡 spec",
        "按段落角色匹配预设配色" + ("，并贴合 frame 主色板" if frame and frame.palette else ""),
    ]


def _frame_palette_block(frame: FrameDesignSystem) -> str:
    """把 frame.palette 拆成 primary/accent/supporting，让 LLM 一眼对照规则取色。"""
    pal = [c for c in (frame.palette or []) if isinstance(c, str) and _HEX_RE.match(c)]
    if not pal:
        return ""
    parts = [f"primary={pal[0]}"]
    if len(pal) >= 2:
        parts.append(f"accent={pal[1]}")
    if len(pal) >= 3:
        parts.append("supporting=" + ",".join(pal[2:6]))
    return "frame.palette：" + " | ".join(parts) + "（**优先用作字卡 bg/text/accent，按 frame 契约派生**）"


def _hex_luminance(hex_color: str) -> float:
    """简易亮度估算，用于挑选"明度最低/最高"的色。"""
    s = hex_color.lstrip("#")
    if len(s) != 6:
        return 0.5
    try:
        r = int(s[0:2], 16) / 255
        g = int(s[2:4], 16) / 255
        b = int(s[4:6], 16) / 255
    except ValueError:
        return 0.5
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _pick_frame_colors(frame: Optional[FrameDesignSystem], hook_preset: dict) -> dict:
    """从 frame 派生 (bg_color, text_color, accent_color, font_family, animation)；缺什么用 hook_preset 兜底。"""
    out = {
        "bg_color": hook_preset["bg_color"],
        "text_color": hook_preset["text_color"],
        "accent_color": hook_preset["accent_color"],
        "font_family": hook_preset["font_family"],
        "animation": hook_preset["animation"],
        "bg_mode": hook_preset["bg_mode"],
    }
    if frame is None:
        return out
    pal = [c for c in (frame.palette or []) if isinstance(c, str) and _HEX_RE.match(c)]
    bg = frame.background_color if (frame.background_color and _HEX_RE.match(frame.background_color)) else ""
    if pal:
        # text_color = primary
        out["text_color"] = pal[0]
        # accent_color = accent (palette[1]) or palette[2]
        if len(pal) >= 2:
            out["accent_color"] = pal[1]
        elif len(pal) >= 3:
            out["accent_color"] = pal[2]
        # bg_color: 优先 frame.background_color；否则取与 text_color 亮度差最大的一色
        if bg:
            out["bg_color"] = bg
        else:
            text_lum = _hex_luminance(out["text_color"])
            best = pal[0]
            best_diff = 0.0
            for c in pal:
                diff = abs(_hex_luminance(c) - text_lum)
                if diff > best_diff:
                    best_diff = diff
                    best = c
            # 文字色和背景色相同 → 反色
            if best == out["text_color"] and len(pal) >= 2:
                best = pal[1]
            out["bg_color"] = best
    elif bg:
        out["bg_color"] = bg
    # font_family from frame.typography_display
    disp = (frame.typography_display or "").lower()
    if disp:
        if any(k in disp for k in ("serif", "明朝", "times", "songti", "宋体")):
            out["font_family"] = "serif_classic"
        elif any(k in disp for k in ("mono", "code", "consolas")):
            out["font_family"] = "tech_mono"
        elif any(k in disp for k in ("hand", "script", "kai", "楷")):
            out["font_family"] = "handwriting"
        else:
            out["font_family"] = "bold_sans"
    # animation from motion_density
    if frame.motion_density == "minimal":
        out["animation"] = "fade_in"
    elif frame.motion_density == "kinetic":
        if hook_preset["animation"] == "fade_in":
            out["animation"] = "zoom_pop"
        else:
            out["animation"] = hook_preset["animation"]
    return out


def _safe_hex(value: object, fallback: str) -> str:
    s = str(value or "").strip()
    if _HEX_RE.match(s):
        return s if s.startswith("#") else f"#{s}"
    return fallback


def _safe_enum(value: object, valid: set[str], fallback: str) -> str:
    s = str(value or "").strip().lower()
    return s if s in valid else fallback


def _parse_outline(
    raw: object,
    duration: float,
    global_keywords: list[str],
    frame: Optional[FrameDesignSystem] = None,
) -> CopyOutline:
    """把 LLM 返回的任意字典字段裁剪到合法 CopyOutline。frame 给出时，缺失字段用 frame 派生兜底。"""
    if not isinstance(raw, dict):
        raw = {}
    main_text = str(raw.get("main_text") or "").strip()[:24]
    sub_text = str(raw.get("sub_text") or "").strip()[:40]
    core_message = str(raw.get("core_message") or "").strip()[:80]
    if not core_message:
        core_message = main_text
    hook = _safe_enum(raw.get("emotional_hook"), _VALID_HOOKS, "resonance")

    kws_raw = raw.get("must_include_keywords") or []
    kws: list[str] = []
    if isinstance(kws_raw, list):
        global_set = {k for k in global_keywords}
        for k in kws_raw:
            s = str(k).strip()[:24]
            if s and (not global_set or s in global_set) and s not in kws:
                kws.append(s)
            if len(kws) >= 2:
                break

    spec_raw = raw.get("recommended_spec") if isinstance(raw.get("recommended_spec"), dict) else {}
    preset = _HOOK_PRESET.get(hook, _HOOK_PRESET["resonance"])
    # frame 派生 → 作为缺省值；LLM 主动给的合法字段仍优先
    derived = _pick_frame_colors(frame, preset)

    try:
        font_size_pct = float(spec_raw.get("font_size_pct") or 1.0)
    except (TypeError, ValueError):
        font_size_pct = 1.0
    font_size_pct = max(0.6, min(1.6, font_size_pct))

    spec = TextCardSpec(
        main_text=main_text,
        sub_text=sub_text,
        font_family=_safe_enum(spec_raw.get("font_family"), _VALID_FONTS, derived["font_family"]),  # type: ignore[arg-type]
        layout=_safe_enum(spec_raw.get("layout"), _VALID_LAYOUTS, "center"),  # type: ignore[arg-type]
        bg_mode=_safe_enum(spec_raw.get("bg_mode"), _VALID_BG_MODES, derived["bg_mode"]),  # type: ignore[arg-type]
        bg_color=_safe_hex(spec_raw.get("bg_color"), derived["bg_color"]),
        text_color=_safe_hex(spec_raw.get("text_color"), derived["text_color"]),
        accent_color=_safe_hex(spec_raw.get("accent_color"), derived["accent_color"]),
        animation=_safe_enum(spec_raw.get("animation"), _VALID_ANIMATIONS, derived["animation"]),  # type: ignore[arg-type]
        emoji_decor=_clean_emoji(spec_raw.get("emoji_decor")),
        duration_seconds=_safe_duration(spec_raw.get("duration_seconds"), duration),
        font_size_pct=font_size_pct,
    )

    lean = str(raw.get("tone_lean") or "").strip()[:40]
    return CopyOutline(
        main_text=main_text,
        sub_text=sub_text,
        core_message=core_message,
        emotional_hook=hook,  # type: ignore[arg-type]
        must_include_keywords=kws,
        recommended_spec=spec,
        tone_lean=lean,
    )


def _clean_emoji(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for it in value:
        s = str(it).strip()
        if s and len(s) <= 8:  # 单 emoji 通常 1-2 字符；超过 8 ascii 长度的视为非法
            out.append(s)
        if len(out) >= 3:
            break
    return out


def _safe_duration(value: object, fallback: float) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        v = fallback
    return max(1.5, min(15.0, v))


def _fallback_outline(
    gap: Gap,
    section: Optional[AdaptedSection],
    theme: str,
    duration: float,
    keywords: list[str],
    frame: Optional[FrameDesignSystem] = None,
) -> CopyOutline:
    """本地兜底字卡 spec：按段落角色映射 hook，再按 frame.palette + hook 预设取配色。"""
    role = section.role if section else gap.section
    role_lc = role.lower()
    if any(k in role_lc for k in ("opening", "hook", "intro", "establish", "title_card")):
        hook = "wow"
    elif any(k in role_lc for k in ("climax", "peak", "payoff")):
        hook = "twist"
    elif any(k in role_lc for k in ("closing", "closer", "resolve", "recap")):
        hook = "resonance"
    else:
        hook = "resonance"

    base_theme = (theme or gap.requirement or "本段")[:18]
    main_text = base_theme[:18]
    sub_text = (keywords[0] if keywords else "").strip()[:30]
    preset = _HOOK_PRESET[hook]
    derived = _pick_frame_colors(frame, preset)
    spec = TextCardSpec(
        main_text=main_text,
        sub_text=sub_text,
        font_family=derived["font_family"],  # type: ignore[arg-type]
        layout="split_top_bottom" if sub_text else "center",
        bg_mode=derived["bg_mode"],  # type: ignore[arg-type]
        bg_color=derived["bg_color"],
        text_color=derived["text_color"],
        accent_color=derived["accent_color"],
        animation=derived["animation"],  # type: ignore[arg-type]
        emoji_decor=[],
        duration_seconds=max(1.5, min(15.0, duration)),
    )
    return CopyOutline(
        main_text=main_text,
        sub_text=sub_text,
        core_message=base_theme,
        emotional_hook=hook,  # type: ignore[arg-type]
        must_include_keywords=keywords[:1],
        recommended_spec=spec,
        tone_lean="",
    )
