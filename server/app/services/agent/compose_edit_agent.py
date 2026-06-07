"""Compose 自然语言编辑 agent —— ⌘K 讨论小助手的后端实装。

与 render-时 `routers/edit.py` 的三轨分流编辑互补：
- 那一组是渲染态的"改片"，作用对象是 Plan.main_track / packaging_track / 口播 wav
- 本模块是 Compose 态的"改稿"

作用域（v2，按用户最新需求收窄/扩张）：
- step2 (拆解-改编态)：**只改内容轨** — 段落文案 / 段落时长 / 删段 / 重排顺序
- step3 (包装-渲染态)：**禁内容轨**，其余全开 — 字卡 / 包装项 / BGM 偏移 / BGM 音量 / compose 设置

设计：
1. LLM tool-call 提取意图 → 调度到本地 mutator
2. 每个 mutator 返回 `ComposeEditDiff(op, target_id, before, after, summary)`
3. apply=False 时只算 diff（dry-run），apply=True 时落盘新 plan
"""
from __future__ import annotations

import logging
import uuid
from typing import Any, Callable

from ...schemas import (
    AdaptedSection,
    ComposeEditDiff,
    ComposeEditStep,
    Plan,
    SectionRole,
    TextCardSpec,
)
from ..llm_client import get_llm_client

log = logging.getLogger("seecript.compose_edit")


# ---- 系统 prompt --------------------------------------------------------------

# 三角色：项目讲解（基于上下文回答）+ 编辑执行（tool_calls 落 diff）+ 追问澄清（信息不全时）。
# 严守『非必要不编造』：上下文里没有的信息一律回"超出我对本项目的了解范围"。
# 严守『信息不全 → 追问而非脑补』：不允许把『调整一下第二段』脑补成『改文案+改时长』。

_QA_RULES = (
    "你有三类回应方式：\n"
    "A) **编辑**（tool_calls）：用户**明确说了要改什么 + 改成什么**才用。例如：『把 sec-1 改成 5 秒』『删除 sec-2』。\n"
    "B) **讲解**（不要 tool_calls，直接 1-3 句中文）：用户在问或聊本项目。答案必须建立在系统消息提供的『当前 Plan 概览』之上。\n"
    "C) **追问**（不要 tool_calls，直接 1 句反问）：用户的编辑意图**模糊或不全**时——\n"
    "   - 指了对象但没说怎么改：『调整一下第二段』『改一下 BGM』 → 反问『你想调时长 / 改文案 / 删段？要的话告诉我目标值』。\n"
    "   - 说了动作但没说目标：『删掉那段』『重排』 → 反问『指的是哪个 section_id？现在有 sec-0/1/2…』。\n"
    "   - 给了模糊量词没数值：『稍微短一点 sec-1』 → **允许执行**（按 ×0.85 惯例），但只能落『一项』时长变更，不要额外编『改文案』。\n"
    "讲解能覆盖：项目主题/目标、段落结构（每段角色/主题/时长/描述）、结构空缺（没有 scene 或时长占比异常的段）、"
    "素材选择建议（基于段角色 + 全局调性 / 平台 / 关键词推断要找什么样素材）、"
    "BGM / 包装 / 调性 / 比例当前是什么、本 step 能改什么、为什么改不了。\n"
    "**禁止编造**：上下文里没有的信息一律答『这超出我对本项目的了解范围，没法编造，建议你直接告诉我你想改的是什么』。\n"
    "**禁止脑补**：不要把单一指令拆成多个 diff。一句『调整第二段』决不能产生『改文案 + 改时长』两个 tool_call——这是幻觉。\n"
    "**禁止在讲解 / 追问里夹带 tool_calls**。"
)

_SYSTEM_STEP2 = (
    "你是 Compose 拆解-改编态的对话编辑小助手。当前作用域 step2，**只能改内容轨**。"
    "可调用编辑工具："
    "update_section_narration（改某段文案）、"
    "update_section_duration（调某段时长，2-30 秒）、"
    "delete_section（删除某段）、"
    "reorder_sections（按 section_id 列表重排）、"
    "update_shot_visual（改某段下第 N 个分镜的画面描述）、"
    "update_shot_narration（改某分镜的口播/字幕，同步主轨 scene）、"
    "update_shot_duration（改某分镜的时长 1-12 秒，自动缩放段总时长与对应 scene）、"
    "regenerate_fill（重新生成某段已有 fill，仅 rerank/copy/aigc_image；aigc 视频不允许通过对话重生成）。"
    "用户表达模糊时按惯例：『稍短=×0.85 / 更短=×0.7 / 更长=×1.25 / 长很多=×1.5』，"
    "时长统一钳制 [2, 30] 秒。"
    "**分镜级编辑**：用户说『sec-1 第 2 镜画面改成…』『sec-2 第 1 镜口播改成…』『sec-0 第 3 镜短一点』时，"
    "用 update_shot_visual / update_shot_narration / update_shot_duration；shot_order 从 0 起（用户说『第 1 镜』即 shot_order=0）。"
    "若用户说『换一张图 / 重新生图 sec-1』『换文案』『重新挑素材』→ regenerate_fill。"
    "若用户说『重新生成视频』『重做 AI 视频』→ **不要 tool_calls**，直接讲解："
    "『AI 视频成本高，请在 AIGC 面板手动改提示词后点重新生成。』"
    "用户若说『改字卡』『改 BGM』『改调性/比例』等非内容轨指令，"
    "**不要 tool_calls**，直接讲解：『这些要到 step3 包装编辑再调，先把结构定下来』。\n\n"
    + _QA_RULES
)

_SYSTEM_STEP3 = (
    "你是 Compose 包装态的对话编辑小助手。当前作用域 step3，**禁止改内容轨**。"
    "可调用编辑工具："
    "update_text_card_spec（改字卡文案/字号）、"
    "update_packaging_text（改包装项 item 的文字）、"
    "update_bgm_offset（BGM 起点对齐到视频第几秒，可负）、"
    "update_bgm_volume（BGM 音量 0-1.5）、"
    "update_compose_setting（改 tone/cta/keywords/target_platform/aspect_ratio/target_duration_seconds）。"
    "用户若说『改第 2 段文案』『把这段删掉』『重排段落』等内容轨指令，"
    "**不要 tool_calls**，直接讲解：『step3 不可改内容轨，请回 step2 调整结构』。\n\n"
    + _QA_RULES
)


# ---- 工具集 -------------------------------------------------------------------

_TOOL_UPDATE_NARRATION = {
    "type": "function",
    "function": {
        "name": "update_section_narration",
        "description": "改写某段的 content_description（拆解阶段叙事 description）。",
        "parameters": {
            "type": "object",
            "properties": {
                "section_id": {"type": "string", "description": "AdaptedSection.section_id，如 sec-0"},
                "content_description": {"type": "string", "description": "新的内容说明（≤300 字）"},
            },
            "required": ["section_id", "content_description"],
        },
    },
}

_TOOL_UPDATE_DURATION = {
    "type": "function",
    "function": {
        "name": "update_section_duration",
        "description": "改某段目标时长（秒）；自动钳制 [2, 30]。",
        "parameters": {
            "type": "object",
            "properties": {
                "section_id": {"type": "string"},
                "duration_seconds": {"type": "number"},
            },
            "required": ["section_id", "duration_seconds"],
        },
    },
}

_TOOL_DELETE_SECTION = {
    "type": "function",
    "function": {
        "name": "delete_section",
        "description": "删除某段（连带其 main_track 的 scenes）。",
        "parameters": {
            "type": "object",
            "properties": {"section_id": {"type": "string"}},
            "required": ["section_id"],
        },
    },
}

_TOOL_REORDER_SECTIONS = {
    "type": "function",
    "function": {
        "name": "reorder_sections",
        "description": "按 section_id 顺序重新排列所有段落；列表必须包含且仅包含现有的全部 section_id。",
        "parameters": {
            "type": "object",
            "properties": {
                "section_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "新顺序，例如 ['sec-1','sec-0','sec-2']",
                },
            },
            "required": ["section_ids"],
        },
    },
}

_TOOL_UPDATE_TEXT_CARD = {
    "type": "function",
    "function": {
        "name": "update_text_card_spec",
        "description": "改某 scene 的字卡 spec：text（首行 main_text 主标≤24，余下 sub_text 副标≤40）。",
        "parameters": {
            "type": "object",
            "properties": {
                "scene_id": {"type": "string"},
                "text": {"type": "string", "description": "字卡文字。多行：第一行=主标，剩余=副标。"},
            },
            "required": ["scene_id"],
        },
    },
}

_TOOL_UPDATE_PACKAGING_TEXT = {
    "type": "function",
    "function": {
        "name": "update_packaging_text",
        "description": "改包装轨某 item 的文字（字幕/标题/贴纸）。",
        "parameters": {
            "type": "object",
            "properties": {
                "item_id": {"type": "string"},
                "text": {"type": "string"},
            },
            "required": ["item_id", "text"],
        },
    },
}

_TOOL_UPDATE_BGM_OFFSET = {
    "type": "function",
    "function": {
        "name": "update_bgm_offset",
        "description": "BGM 起点对齐到视频第几秒（video_anchor_seconds）。可为负数。",
        "parameters": {
            "type": "object",
            "properties": {"video_anchor_seconds": {"type": "number"}},
            "required": ["video_anchor_seconds"],
        },
    },
}

_TOOL_UPDATE_BGM_VOLUME = {
    "type": "function",
    "function": {
        "name": "update_bgm_volume",
        "description": "调整 BGM 音量（0.0-1.5）。",
        "parameters": {
            "type": "object",
            "properties": {"volume": {"type": "number"}},
            "required": ["volume"],
        },
    },
}

_TOOL_UPDATE_COMPOSE_SETTING = {
    "type": "function",
    "function": {
        "name": "update_compose_setting",
        "description": "改 ComposeSettings 的常用字段；只填要改的字段，其他保持。",
        "parameters": {
            "type": "object",
            "properties": {
                "tone": {
                    "type": "string",
                    "enum": ["tight_hype", "calm_narrative", "casual_daily", "professional_cool"],
                },
                "target_platform": {
                    "type": "string",
                    "enum": ["douyin", "wechat", "xiaohongshu", "bilibili"],
                },
                "aspect_ratio": {"type": "string", "enum": ["9:16", "16:9", "1:1"]},
                "cta": {"type": "string"},
                "keywords": {"type": "array", "items": {"type": "string"}},
                "target_duration_seconds": {"type": "number"},
            },
        },
    },
}


_TOOL_UPDATE_SHOT_VISUAL = {
    "type": "function",
    "function": {
        "name": "update_shot_visual",
        "description": "改某段（section_id）下第 shot_order 个分镜的画面描述（visual，≤120 字）。",
        "parameters": {
            "type": "object",
            "properties": {
                "section_id": {"type": "string", "description": "AdaptedSection.section_id，如 sec-1"},
                "shot_order": {"type": "integer", "description": "分镜序号（0 起，对应 ShotPlan.order）"},
                "visual": {"type": "string", "description": "新的画面描述（≤120 字，主体+动作+构图+氛围）"},
            },
            "required": ["section_id", "shot_order", "visual"],
        },
    },
}

_TOOL_UPDATE_SHOT_NARRATION = {
    "type": "function",
    "function": {
        "name": "update_shot_narration",
        "description": "改某段下第 shot_order 个分镜的口播/字幕（narration，≤200 字）；同步更新主轨对应 scene.narration。",
        "parameters": {
            "type": "object",
            "properties": {
                "section_id": {"type": "string"},
                "shot_order": {"type": "integer"},
                "narration": {"type": "string"},
            },
            "required": ["section_id", "shot_order", "narration"],
        },
    },
}

_TOOL_UPDATE_SHOT_DURATION = {
    "type": "function",
    "function": {
        "name": "update_shot_duration",
        "description": "改某段下第 shot_order 个分镜的时长（秒，钳制 [1, 12]）；自动按比例缩放 section.duration_seconds 与对应 scene.duration。",
        "parameters": {
            "type": "object",
            "properties": {
                "section_id": {"type": "string"},
                "shot_order": {"type": "integer"},
                "duration_seconds": {"type": "number"},
            },
            "required": ["section_id", "shot_order", "duration_seconds"],
        },
    },
}


_TOOL_REGENERATE_FILL = {
    "type": "function",
    "function": {
        "name": "regenerate_fill",
        "description": (
            "重新生成某段（section）已有的缺口补全（fill）。仅支持 rerank/copy/aigc_image 三种；"
            "aigc 视频生成成本高、耗时长，不允许通过对话重生成（用户应在 AIGC 面板手动改提示词后再点重新生成）。"
            "hint 是用户给本次重生成的额外指引（可空），会作为 prompt_hint 透传给 fill_gap。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "section_id": {"type": "string", "description": "AdaptedSection.section_id，如 sec-0"},
                "action": {
                    "type": "string",
                    "enum": ["rerank", "copy", "aigc_image"],
                    "description": "重生成的 fill 动作。rerank=结构重排；copy=字卡 LLM 文案；aigc_image=Seedream 静图。",
                },
                "hint": {
                    "type": "string",
                    "description": "可选：用户对本次重生成的要求（例如『更紧凑些』『换成竖向构图』『字卡用反问句』）。",
                },
            },
            "required": ["section_id", "action"],
        },
    },
}


_TOOLS_STEP2: list[dict] = [
    _TOOL_UPDATE_NARRATION,
    _TOOL_UPDATE_DURATION,
    _TOOL_DELETE_SECTION,
    _TOOL_REORDER_SECTIONS,
    _TOOL_UPDATE_SHOT_VISUAL,
    _TOOL_UPDATE_SHOT_NARRATION,
    _TOOL_UPDATE_SHOT_DURATION,
    _TOOL_REGENERATE_FILL,
]

_TOOLS_STEP3: list[dict] = [
    _TOOL_UPDATE_TEXT_CARD,
    _TOOL_UPDATE_PACKAGING_TEXT,
    _TOOL_UPDATE_BGM_OFFSET,
    _TOOL_UPDATE_BGM_VOLUME,
    _TOOL_UPDATE_COMPOSE_SETTING,
]


_STEP_TOOLS: dict[ComposeEditStep, list[dict]] = {
    "step2": _TOOLS_STEP2,
    "step3": _TOOLS_STEP3,
}

_STEP_SYSTEM: dict[ComposeEditStep, str] = {
    "step2": _SYSTEM_STEP2,
    "step3": _SYSTEM_STEP3,
}


_VALID_TONES = {"tight_hype", "calm_narrative", "casual_daily", "professional_cool"}
_VALID_PLATFORMS = {"douyin", "wechat", "xiaohongshu", "bilibili"}
_VALID_RATIOS = {"9:16", "16:9", "1:1"}


# ---- timeline 重建 -----------------------------------------------------------

def _rebuild_timeline(plan: Plan) -> dict:
    """结构变更后（删段 / 重排 / 改时长）统一重铺时间轴。

    - 按 adapted_sections 当前顺序串起 main_track 中同 role 的 scene；段内保留旧的相对顺序
    - 重设每个 scene 的 start，避免删段后留出空洞
    - subtitle 文本本与具体段落绑定，结构变更后语义会错位 → 一律清空（提示用户回 step3 重生成）
    - 其它 packaging 项（title_bar / sticker / cover / transition）超出新总长的截断或丢弃
    - 同步 settings.target_duration_seconds 当作目标参考（避免 UI 显示不一致）

    返回 {"scenes_moved": int, "subtitles_cleared": int, "packaging_trimmed": int, "total": float}。
    """
    role_to_scenes: dict[str, list] = {}
    for sc in plan.main_track:
        role_to_scenes.setdefault(sc.section, []).append(sc)
    for scenes in role_to_scenes.values():
        scenes.sort(key=lambda s: s.start)

    new_track: list = []
    t = 0.0
    moved = 0
    for sec in plan.adapted_sections:
        scenes = role_to_scenes.pop(sec.role, [])
        for sc in scenes:
            if abs(sc.start - t) > 0.01:
                moved += 1
            sc.start = round(t, 3)
            t += sc.duration
            new_track.append(sc)
    # 不属于任何 section 的孤儿 scene（老数据兼容）—— 追到末尾
    for scenes in role_to_scenes.values():
        for sc in scenes:
            sc.start = round(t, 3)
            t += sc.duration
            new_track.append(sc)
    plan.main_track = new_track

    total = round(t, 3)

    subtitles_cleared = 0
    trimmed = 0
    new_pkg = []
    for it in plan.packaging_track:
        if it.kind == "subtitle":
            subtitles_cleared += 1
            continue
        if it.start >= total + 0.01:
            trimmed += 1
            continue
        if it.end > total + 0.01:
            it.end = total
            trimmed += 1
        new_pkg.append(it)
    plan.packaging_track = new_pkg

    if plan.settings is not None and total > 0:
        plan.settings.target_duration_seconds = max(10.0, min(300.0, total))

    return {
        "scenes_moved": moved,
        "subtitles_cleared": subtitles_cleared,
        "packaging_trimmed": trimmed,
        "total": total,
    }


def _summary_with_rebuild(base: str, info: dict) -> str:
    """把 timeline 重建带来的影响附在 diff summary 末尾（合到 120 字内）。"""
    extras: list[str] = []
    if info["scenes_moved"]:
        extras.append(f"{info['scenes_moved']} 个 scene 重排")
    if info["subtitles_cleared"]:
        extras.append(f"清空 {info['subtitles_cleared']} 条字幕（请回 step3 重生成）")
    if info["packaging_trimmed"]:
        extras.append(f"裁剪 {info['packaging_trimmed']} 个包装项")
    if not extras:
        return base
    suffix = "；" + "、".join(extras)
    if len(base) + len(suffix) > 120:
        suffix = suffix[: 120 - len(base) - 1] + "…"
    return base + suffix


# ---- mutator 一组 ------------------------------------------------------------

def _find_section(plan: Plan, section_id: str) -> AdaptedSection | None:
    for sec in plan.adapted_sections:
        if sec.section_id == section_id:
            return sec
    return None


def _mut_update_narration(plan: Plan, args: dict) -> ComposeEditDiff | None:
    sid = args.get("section_id") or ""
    new_text = (args.get("content_description") or "").strip()
    if not sid or not new_text:
        return None
    sec = _find_section(plan, sid)
    if sec is None:
        return None
    before = sec.content_description
    sec.content_description = new_text[:300]
    return ComposeEditDiff(
        op="update_section_narration",
        target_id=sid,
        before=before,
        after=sec.content_description,
        summary=f"段 {sid} 文案改写（{len(before)}→{len(sec.content_description)} 字）",
    )


def _mut_update_duration(plan: Plan, args: dict) -> ComposeEditDiff | None:
    sid = args.get("section_id") or ""
    try:
        new_dur = float(args.get("duration_seconds", 0) or 0)
    except (TypeError, ValueError):
        return None
    if not sid or new_dur <= 0:
        return None
    sec = _find_section(plan, sid)
    if sec is None:
        return None
    before = sec.duration_seconds
    sec.duration_seconds = max(2.0, min(30.0, new_dur))
    role = sec.role
    matched_scenes = [sc for sc in plan.main_track if sc.section == role]
    if matched_scenes and before > 0:
        ratio = sec.duration_seconds / before
        for sc in matched_scenes:
            sc.duration = max(0.5, sc.duration * ratio)
    info = _rebuild_timeline(plan)
    base = f"段 {sid} 时长 {before:.1f}s → {sec.duration_seconds:.1f}s（总时长 {info['total']:.1f}s）"
    return ComposeEditDiff(
        op="update_section_duration",
        target_id=sid,
        before=round(before, 2),
        after=round(sec.duration_seconds, 2),
        summary=_summary_with_rebuild(base, info),
    )


def _mut_delete_section(plan: Plan, args: dict) -> ComposeEditDiff | None:
    sid = args.get("section_id") or ""
    if not sid:
        return None
    sec = _find_section(plan, sid)
    if sec is None:
        return None
    role: SectionRole = sec.role
    before = {"section_id": sid, "role": role, "content_description": sec.content_description}
    plan.adapted_sections = [s for s in plan.adapted_sections if s.section_id != sid]
    for i, s in enumerate(plan.adapted_sections):
        s.order = i
    removed_scenes = [sc.scene_id for sc in plan.main_track if sc.section == role]
    plan.main_track = [sc for sc in plan.main_track if sc.section != role]
    info = _rebuild_timeline(plan)
    base = f"删除段 {sid}（role={role}，连带 {len(removed_scenes)} 个 scene；剩 {len(plan.adapted_sections)} 段 / {info['total']:.1f}s）"
    return ComposeEditDiff(
        op="delete_section",
        target_id=sid,
        before=before,
        after=None,
        summary=_summary_with_rebuild(base, info),
    )


def _mut_reorder_sections(plan: Plan, args: dict) -> ComposeEditDiff | None:
    new_order = args.get("section_ids") or []
    if not isinstance(new_order, list) or not new_order:
        return None
    existing_ids = [s.section_id for s in plan.adapted_sections]
    if set(new_order) != set(existing_ids) or len(new_order) != len(existing_ids):
        return None
    if new_order == existing_ids:
        return None
    id_to_sec = {s.section_id: s for s in plan.adapted_sections}
    plan.adapted_sections = [id_to_sec[i] for i in new_order]
    for i, s in enumerate(plan.adapted_sections):
        s.order = i
    info = _rebuild_timeline(plan)
    base = f"段落重排：{' / '.join(existing_ids)} → {' / '.join(new_order)}"
    return ComposeEditDiff(
        op="reorder_sections",
        target_id=None,
        before=existing_ids,
        after=new_order,
        summary=_summary_with_rebuild(base, info),
    )


def _find_shot(sec: AdaptedSection, shot_order: int):
    """按 ShotPlan.order 在 section.shots 中精确匹配；不存在返回 None。"""
    for sh in (sec.shots or []):
        if sh.order == shot_order:
            return sh
    return None


def _matching_scene(plan: Plan, sec: AdaptedSection, shot_order: int):
    """匹配 stage-24 Scene：parent_section_id == sec.section_id 且 shot_order 相等。"""
    for sc in plan.main_track:
        if getattr(sc, "parent_section_id", None) == sec.section_id and getattr(sc, "shot_order", -1) == shot_order:
            return sc
    return None


def _mut_update_shot_visual(plan: Plan, args: dict) -> ComposeEditDiff | None:
    sid = args.get("section_id") or ""
    try:
        shot_order = int(args.get("shot_order"))
    except (TypeError, ValueError):
        return None
    new_visual = (args.get("visual") or "").strip()
    if not sid or not new_visual:
        return None
    sec = _find_section(plan, sid)
    if sec is None:
        return None
    shot = _find_shot(sec, shot_order)
    if shot is None:
        return ComposeEditDiff(
            op="update_shot_visual", target_id=f"{sid}#{shot_order}",
            before=None, after=None,
            summary=f"段 {sid} 没有第 {shot_order+1} 镜（共 {len(sec.shots or [])} 镜）",
        )
    before = shot.visual
    shot.visual = new_visual[:120]
    return ComposeEditDiff(
        op="update_shot_visual",
        target_id=f"{sid}#{shot_order}",
        before=before,
        after=shot.visual,
        summary=f"段 {sid} 第 {shot_order+1} 镜画面改写（{len(before)}→{len(shot.visual)} 字）",
    )


def _mut_update_shot_narration(plan: Plan, args: dict) -> ComposeEditDiff | None:
    sid = args.get("section_id") or ""
    try:
        shot_order = int(args.get("shot_order"))
    except (TypeError, ValueError):
        return None
    new_narration = (args.get("narration") or "").strip()
    if not sid:
        return None
    sec = _find_section(plan, sid)
    if sec is None:
        return None
    shot = _find_shot(sec, shot_order)
    if shot is None:
        return ComposeEditDiff(
            op="update_shot_narration", target_id=f"{sid}#{shot_order}",
            before=None, after=None,
            summary=f"段 {sid} 没有第 {shot_order+1} 镜（共 {len(sec.shots or [])} 镜）",
        )
    before = shot.narration
    shot.narration = new_narration[:200]
    scene_synced = False
    sc = _matching_scene(plan, sec, shot_order)
    if sc is not None:
        sc.narration = shot.narration
        scene_synced = True
    base = f"段 {sid} 第 {shot_order+1} 镜口播改写（{len(before)}→{len(shot.narration)} 字）"
    if scene_synced:
        base += "；已同步主轨 scene"
    return ComposeEditDiff(
        op="update_shot_narration",
        target_id=f"{sid}#{shot_order}",
        before=before,
        after=shot.narration,
        summary=base,
    )


def _mut_update_shot_duration(plan: Plan, args: dict) -> ComposeEditDiff | None:
    sid = args.get("section_id") or ""
    try:
        shot_order = int(args.get("shot_order"))
        new_dur = float(args.get("duration_seconds", 0) or 0)
    except (TypeError, ValueError):
        return None
    if not sid or new_dur <= 0:
        return None
    sec = _find_section(plan, sid)
    if sec is None:
        return None
    shot = _find_shot(sec, shot_order)
    if shot is None:
        return ComposeEditDiff(
            op="update_shot_duration", target_id=f"{sid}#{shot_order}",
            before=None, after=None,
            summary=f"段 {sid} 没有第 {shot_order+1} 镜（共 {len(sec.shots or [])} 镜）",
        )
    new_dur = max(1.0, min(12.0, new_dur))
    before = shot.duration_seconds
    if abs(new_dur - before) < 0.05:
        return None
    delta = new_dur - before
    shot.duration_seconds = new_dur
    sec_before = sec.duration_seconds
    sec.duration_seconds = max(2.0, min(120.0, sec_before + delta))
    scene_synced = False
    sc = _matching_scene(plan, sec, shot_order)
    if sc is not None:
        sc.duration = max(0.5, sc.duration + delta)
        scene_synced = True
    info = _rebuild_timeline(plan)
    base = (
        f"段 {sid} 第 {shot_order+1} 镜时长 {before:.1f}s → {new_dur:.1f}s"
        f"（段总时长 {sec_before:.1f}s → {sec.duration_seconds:.1f}s；总时长 {info['total']:.1f}s）"
    )
    if scene_synced:
        base += "；主轨 scene 已同步"
    return ComposeEditDiff(
        op="update_shot_duration",
        target_id=f"{sid}#{shot_order}",
        before=round(before, 2),
        after=round(new_dur, 2),
        summary=_summary_with_rebuild(base, info),
    )


def _mut_update_text_card(plan: Plan, args: dict) -> ComposeEditDiff | None:
    sid = args.get("scene_id") or ""
    if not sid:
        return None
    new_text = args.get("text")
    new_size = args.get("font_size_pct")
    for sc in plan.main_track:
        if sc.scene_id != sid:
            continue
        if sc.source != "text_card":
            return ComposeEditDiff(
                op="update_text_card_spec",
                target_id=sid,
                before=None,
                after=None,
                summary=f"忽略：scene {sid} 不是字卡（source={sc.source}）",
            )
        spec = sc.text_card_spec or TextCardSpec()
        before = spec.model_dump()
        changed: list[str] = []
        if isinstance(new_text, str) and new_text:
            # LLM 给一段文字 → 第一行作为 main_text（≤24），余下作为 sub_text（≤40）
            lines = [ln.strip() for ln in new_text.splitlines() if ln.strip()]
            if not lines:
                lines = [new_text.strip()]
            spec.main_text = lines[0][:24]
            changed.append("main_text")
            if len(lines) > 1:
                spec.sub_text = " ".join(lines[1:])[:40]
                changed.append("sub_text")
        if new_size is not None:
            # font_size_pct 已不在 TextCardSpec schema 上——本期忽略，只 log
            log.info("update_text_card_spec ignored font_size_pct=%s (not in schema)", new_size)
        if not changed:
            return None
        sc.text_card_spec = spec
        return ComposeEditDiff(
            op="update_text_card_spec",
            target_id=sid,
            before=before,
            after=spec.model_dump(),
            summary=f"字卡 {sid} 改 {','.join(changed)}",
        )
    return None


def _mut_update_packaging_text(plan: Plan, args: dict) -> ComposeEditDiff | None:
    iid = args.get("item_id") or ""
    new_text = (args.get("text") or "").strip()
    if not iid or not new_text:
        return None
    for it in plan.packaging_track:
        if it.item_id == iid:
            before = it.text or ""
            it.text = new_text
            return ComposeEditDiff(
                op="update_packaging_text",
                target_id=iid,
                before=before,
                after=it.text,
                summary=f"包装项 {iid} 文字改写",
            )
    return None


def _mut_move_packaging_item(plan: Plan, args: dict) -> ComposeEditDiff | None:
    """沿时间轴平移包装项（preserve duration）。来源：包装轨手动拖动。"""
    iid = args.get("item_id") or ""
    if not iid:
        return None
    try:
        new_start = float(args.get("start_seconds", -1))
    except (TypeError, ValueError):
        return None
    if new_start < 0:
        return None
    for it in plan.packaging_track:
        if it.item_id != iid:
            continue
        dur = max(0.0, float(it.end - it.start))
        max_start = max(0.0, float(plan.duration_seconds) - dur)
        clamped = min(new_start, max_start)
        before = (round(it.start, 2), round(it.end, 2))
        if abs(clamped - it.start) < 1e-3:
            return None
        it.start = round(clamped, 2)
        it.end = round(clamped + dur, 2)
        return ComposeEditDiff(
            op="move_packaging_item",
            target_id=iid,
            before=before,
            after=(it.start, it.end),
            summary=f"包装项 {iid} {before[0]:.1f}s → {it.start:.1f}s",
        )
    return None


def _mut_update_bgm_offset(plan: Plan, args: dict) -> ComposeEditDiff | None:
    try:
        val = float(args.get("video_anchor_seconds", 0))
    except (TypeError, ValueError):
        return None
    before = plan.bgm.video_anchor_seconds
    plan.bgm.video_anchor_seconds = val
    return ComposeEditDiff(
        op="update_bgm_offset",
        target_id=None,
        before=round(before, 2),
        after=round(val, 2),
        summary=f"BGM 起点 {before:+.1f}s → {val:+.1f}s",
    )


def _mut_update_bgm_volume(plan: Plan, args: dict) -> ComposeEditDiff | None:
    try:
        val = float(args.get("volume", 0))
    except (TypeError, ValueError):
        return None
    val = max(0.0, min(1.5, val))
    before = plan.bgm.volume
    if abs(val - before) < 1e-3:
        return None
    plan.bgm.volume = val
    return ComposeEditDiff(
        op="update_bgm_volume",
        target_id=None,
        before=round(before, 2),
        after=round(val, 2),
        summary=f"BGM 音量 {before:.2f} → {val:.2f}",
    )


def _mut_update_compose_setting(plan: Plan, args: dict) -> ComposeEditDiff | None:
    cs = plan.settings
    if cs is None:
        return None
    before = cs.model_dump()
    changes: list[str] = []
    if "tone" in args and args["tone"] in _VALID_TONES:
        cs.tone = args["tone"]
        changes.append(f"tone={cs.tone}")
    if "target_platform" in args and args["target_platform"] in _VALID_PLATFORMS:
        cs.target_platform = args["target_platform"]
        changes.append(f"platform={cs.target_platform}")
    if "aspect_ratio" in args and args["aspect_ratio"] in _VALID_RATIOS:
        cs.aspect_ratio = args["aspect_ratio"]
        changes.append(f"ratio={cs.aspect_ratio}")
    if "cta" in args and isinstance(args["cta"], str):
        cs.cta = args["cta"][:20]
        changes.append("cta")
    if "keywords" in args and isinstance(args["keywords"], list):
        kws = [str(k)[:20] for k in args["keywords"] if str(k).strip()][:5]
        cs.keywords = kws
        changes.append(f"keywords({len(kws)})")
    if "target_duration_seconds" in args:
        try:
            d = float(args["target_duration_seconds"])
            cs.target_duration_seconds = max(10.0, min(120.0, d))
            changes.append(f"duration={cs.target_duration_seconds:.0f}s")
        except (TypeError, ValueError):
            pass
    if not changes:
        return None
    return ComposeEditDiff(
        op="update_compose_setting",
        target_id=None,
        before=before,
        after=cs.model_dump(),
        summary="Compose 设置改写：" + " / ".join(changes),
    )


_MUTATORS: dict[str, Callable[[Plan, dict], ComposeEditDiff | None]] = {
    "update_section_narration": _mut_update_narration,
    "update_section_duration": _mut_update_duration,
    "delete_section": _mut_delete_section,
    "reorder_sections": _mut_reorder_sections,
    "update_shot_visual": _mut_update_shot_visual,
    "update_shot_narration": _mut_update_shot_narration,
    "update_shot_duration": _mut_update_shot_duration,
    "update_text_card_spec": _mut_update_text_card,
    "update_packaging_text": _mut_update_packaging_text,
    "move_packaging_item": _mut_move_packaging_item,
    "update_bgm_offset": _mut_update_bgm_offset,
    "update_bgm_volume": _mut_update_bgm_volume,
    "update_compose_setting": _mut_update_compose_setting,
}


async def _mut_regenerate_fill(plan: Plan, args: dict) -> ComposeEditDiff | None:
    """异步 mutator：找到 section_id 对应的 gap，调 fill_gap，再把结果 patch 回 plan 的 scene。

    设计取舍：
    - 不支持 action=aigc（视频生成）：成本 / 耗时太高，要求用户走 AIGC 面板手动改提示词后重生成
    - 旋转 fill_gap 时不持有 plan_store 锁——fill_gap 里有 LLM/Seedream IO 网络调用，分钟级
    - patch 主轨 scene 时：
        aigc_image：scene.source='aigc_image'，scene.aigc_image_url=新 URL；
                    多图（path B）会扩展成 N 个等长子 scene 替换原 scene
        copy      ：scene.source='text_card'，scene.text_card_spec=新规格，narration=主+副拼接
        rerank    ：scene.source='user_material'，scene.source_ref=新 material_id（暂不存全量 material 校验）
    """
    sid = (args.get("section_id") or "").strip()
    action = (args.get("action") or "").strip()
    hint = (args.get("hint") or "").strip()
    if not sid or action not in _REGEN_ALLOWED_ACTIONS:
        return None
    sec = _find_section(plan, sid)
    if sec is None:
        return ComposeEditDiff(
            op="regenerate_fill", target_id=sid, before=None, after=None,
            summary=f"未找到段落 {sid}，无法重生成。",
        )

    # 找 gap：先按 plan_id 列出全部，再按 section_id 过滤
    from ..materials.store import gap_store  # 延迟导入避免循环
    plan_gaps = gap_store.list_by_plan(plan.plan_id)
    gap = next((g for g in plan_gaps if g.section_id == sid), None)
    if gap is None:
        return ComposeEditDiff(
            op="regenerate_fill", target_id=sid, before=None, after=None,
            summary=f"段 {sid} 没有可重生成的缺口（请先在 Compose 触发一次 fill）。",
        )

    # 调 fill_gap：复用与 /gap/fill 一致的入口
    from .gap_agent import fill_gap  # 延迟导入避免循环
    params: dict[str, Any] = {}
    if hint:
        # 三个 action 都把 hint 当 prompt_hint：copy 走文案补充，aigc_image 走画面要求，rerank 暂时无视
        params["prompt_hint"] = hint
        if action == "aigc_image":
            params["prompt"] = hint  # aigc_image 用 prompt 字段
    if action == "aigc_image":
        ratio = None
        if plan.settings is not None and plan.settings.aspect_ratio:
            ratio = plan.settings.aspect_ratio
        if ratio:
            params["ratio"] = ratio
        if sec.duration_seconds > 0:
            params["duration_seconds"] = float(sec.duration_seconds)

    try:
        new_fill = await fill_gap(gap, action, params)
    except Exception as exc:  # noqa: BLE001
        log.warning("[compose_edit.regenerate_fill] fill_gap 失败 sid=%s action=%s: %s", sid, action, exc)
        return ComposeEditDiff(
            op="regenerate_fill", target_id=sid, before=None, after=None,
            summary=f"重生成失败：{str(exc)[:80]}",
        )

    # patch 主轨：先收集本 section 现有 scenes（按 section role + scene_id 前缀 sc-{order} 双锚定）
    sec_role: SectionRole = sec.role
    sec_order = sec.order
    prefix = f"sc-{sec_order}"
    old_scenes = [
        sc for sc in plan.main_track
        if sc.section == sec_role and sc.scene_id.startswith(prefix)
    ]
    if not old_scenes:
        return ComposeEditDiff(
            op="regenerate_fill", target_id=sid, before=None, after=None,
            summary=f"段 {sid} 在主轨上没有 scene，跳过 patch。fill 已写入 gap_store，下次 build 会生效。",
        )
    old_total_dur = sum(sc.duration for sc in old_scenes)
    if old_total_dur <= 0:
        old_total_dur = max(2.0, float(sec.duration_seconds) or 4.0)
    first_old = old_scenes[0]

    # 构造新 scenes 列表（替换 old_scenes 那段）
    from ...schemas import Scene  # 延迟导入

    new_segment: list = []
    if action == "aigc_image":
        urls = list(new_fill.aigc_image_urls or [])
        if not urls and new_fill.aigc_image_url:
            urls = [new_fill.aigc_image_url]
        if not urls:
            return ComposeEditDiff(
                op="regenerate_fill", target_id=sid, before=None, after=None,
                summary=f"段 {sid} aigc_image 重生成无图：{(new_fill.note or '')[:60]}",
            )
        per = old_total_dur / max(1, len(urls))
        for i, u in enumerate(urls):
            sid_suffix = "" if (len(urls) == 1 and i == 0) else f"-shot{i+1}"
            new_segment.append(Scene(
                scene_id=f"{prefix}{sid_suffix}",
                section=sec_role,
                source="aigc_image",
                source_ref=(new_fill.new_material_id or f"aigc-image-{sid}") + sid_suffix,
                start=first_old.start,  # 占位；后面 _rebuild_timeline 会重铺
                duration=per,
                in_point=0.0,
                out_point=None,
                narration=first_old.narration if i == 0 else "",
                voiceover_url=first_old.voiceover_url if i == 0 else None,
                aigc_video_urls=[],
                aigc_image_url=u,
                text_card_spec=None,
            ))
    elif action == "copy":
        spec = new_fill.text_card_spec
        narration = new_fill.narration or ""
        new_segment.append(Scene(
            scene_id=prefix,
            section=sec_role,
            source="text_card",
            source_ref=f"text-card-fill-{sid}",
            start=first_old.start,
            duration=old_total_dur,
            in_point=0.0,
            out_point=None,
            narration=narration,
            voiceover_url=(new_fill.voiceover_url or "").strip() or None,
            aigc_video_urls=[],
            aigc_image_url=None,
            text_card_spec=spec,
        ))
    elif action == "rerank":
        new_segment.append(Scene(
            scene_id=prefix,
            section=sec_role,
            source="user_material",
            source_ref=new_fill.new_material_id or first_old.source_ref,
            start=first_old.start,
            duration=old_total_dur,
            in_point=0.0,
            out_point=old_total_dur,
            narration=first_old.narration,
            voiceover_url=first_old.voiceover_url,
            aigc_video_urls=[],
            aigc_image_url=None,
            text_card_spec=None,
        ))
    else:
        return None  # _REGEN_ALLOWED_ACTIONS 已限定，理论上不会到这里

    # 整体替换：把 old_scenes 全删，按位插入 new_segment
    insert_idx = plan.main_track.index(first_old)
    remaining = [sc for sc in plan.main_track if sc not in old_scenes]
    plan.main_track = remaining[:insert_idx] + new_segment + remaining[insert_idx:]

    info = _rebuild_timeline(plan)
    n_new = len(new_segment)
    base = (
        f"段 {sid} {action} 重生成 → {n_new} 个 scene"
        + (f"（hint={hint[:20]!r}）" if hint else "")
        + f"；总时长 {info['total']:.1f}s"
    )
    return ComposeEditDiff(
        op="regenerate_fill",
        target_id=sid,
        before={"scene_count": len(old_scenes), "first_source": first_old.source},
        after={"scene_count": n_new, "first_source": new_segment[0].source if new_segment else None},
        summary=_summary_with_rebuild(base, info),
    )


_ASYNC_MUTATORS: dict[str, Any] = {
    "regenerate_fill": _mut_regenerate_fill,
}


# step → 允许 mutator 集合（外部越界检测用）
_STEP_ALLOWED_OPS: dict[ComposeEditStep, set[str]] = {
    "step2": {
        "update_section_narration",
        "update_section_duration",
        "delete_section",
        "reorder_sections",
        "update_shot_visual",
        "update_shot_narration",
        "update_shot_duration",
        "regenerate_fill",
    },
    "step3": {
        "update_text_card_spec",
        "update_packaging_text",
        "move_packaging_item",
        "update_bgm_offset",
        "update_bgm_volume",
        "update_compose_setting",
    },
}

# 内容轨 ops（用于在 step3 提示用户回 step2）
_CONTENT_TRACK_OPS = {
    "update_section_narration",
    "update_section_duration",
    "delete_section",
    "reorder_sections",
    "update_shot_visual",
    "update_shot_narration",
    "update_shot_duration",
    "regenerate_fill",
}

# 异步 mutator 集合：调外部 LLM / Seedream / fill_gap 链路；run_compose_edit 单独 await。
_ASYNC_OPS = {"regenerate_fill"}

# regenerate_fill 允许的 fill action（aigc 视频成本高、耗时长，禁止从对话端重生成）
_REGEN_ALLOWED_ACTIONS = {"rerank", "copy", "aigc_image"}


# ---- 主入口 -------------------------------------------------------------------

def _build_user_prompt(plan: Plan, instruction: str, step: ComposeEditStep) -> str:
    parts: list[str] = []
    parts.append("【当前 Plan 概览（讲解类问题只能用本块信息，禁止外推）】")
    parts.append(f"plan_id={plan.plan_id} variant={plan.variant} 总时长={plan.duration_seconds:.1f}s")
    if plan.brief:
        parts.append(f"主题/卖点 brief：{plan.brief[:200]}")
    if plan.video_goal:
        parts.append(f"目标 goal：{plan.video_goal[:200]}")
    cs = plan.settings
    if cs:
        parts.append(
            f"创作设置：tone={cs.tone} platform={cs.target_platform} "
            f"ratio={cs.aspect_ratio} target_duration={cs.target_duration_seconds:.0f}s "
            f"cta={cs.cta!r} keywords={cs.keywords}"
        )
    parts.append(f"BGM：video_anchor={plan.bgm.video_anchor_seconds:+.1f}s volume={plan.bgm.volume:.2f}")

    # 段落 + scene 密度（讲解结构空缺要用）
    scene_count_by_role: dict[str, int] = {}
    for sc in plan.main_track:
        scene_count_by_role[sc.section] = scene_count_by_role.get(sc.section, 0) + 1
    parts.append(f"段落结构（共 {len(plan.adapted_sections)} 段）：")
    for sec in plan.adapted_sections:
        n = scene_count_by_role.get(sec.role, 0)
        gap_flag = "（⚠ 无 scene）" if n == 0 else ""
        parts.append(
            f"- {sec.section_id} role={sec.role} 时长={sec.duration_seconds:.1f}s "
            f"scene 数={n}{gap_flag} 主题={sec.theme[:30]!r} 描述={sec.content_description[:60]!r}"
        )
        # stage-24：分镜级摘要——shot_order 从 0 起，前端"第 N 镜"= shot_order=N-1
        shots = getattr(sec, "shots", None) or []
        if shots:
            for sh in shots:
                parts.append(
                    f"    · 第 {sh.order+1} 镜 (shot_order={sh.order}) "
                    f"{sh.duration_seconds:.1f}s 主体={sh.subject[:20]!r} 画面={sh.visual[:40]!r} 口播={sh.narration[:40]!r}"
                )

    # 包装态额外信息
    if step == "step3":
        tc_count = sum(1 for sc in plan.main_track if sc.source == "text_card")
        pkg_by_kind: dict[str, int] = {}
        for it in plan.packaging_track:
            pkg_by_kind[it.kind] = pkg_by_kind.get(it.kind, 0) + 1
        parts.append(f"包装现状：字卡 scene={tc_count} 个；packaging_track 共 {len(plan.packaging_track)} 个 "
                     f"分布={pkg_by_kind}")
        # 字卡 / 包装项明细（编辑要用 id）
        for sc in plan.main_track:
            if sc.source == "text_card":
                spec = sc.text_card_spec
                if spec:
                    main_t = spec.main_text or ""
                    sub_t = spec.sub_text or ""
                    txt = f"{main_t} | {sub_t}" if sub_t else main_t
                else:
                    txt = ""
                parts.append(f"[text_card] {sc.scene_id} text={txt!r}")
        for it in plan.packaging_track:
            if it.kind == "transition":
                continue
            parts.append(f"[packaging:{it.kind}] {it.item_id} text={it.text!r}")

    parts.append(
        f"\n【本 step={step} 能改什么】"
        + ("段落文案 / 段落时长 / 删段 / 重排顺序 / 分镜画面 / 分镜口播 / 分镜时长" if step == "step2"
           else "字卡文案与字号 / 包装项文字 / BGM 偏移 / BGM 音量 / Compose 设置（tone/cta/keywords/platform/ratio/duration）")
    )
    parts.append(f"\n【用户消息】{instruction}")
    parts.append("【你要做什么】若用户在下指令 → tool_calls；若用户在问/聊本项目 → 用上面的概览答 1-3 句，不要 tool_calls；超出概览范围 → 直说『这超出我对本项目的了解范围』。")
    return "\n".join(parts)


def _mock_intent(instruction: str, step: ComposeEditStep) -> list[dict[str, Any]]:
    """mock 模式兜底意图识别：关键词匹配。"""
    import re
    txt = instruction.strip()
    if not txt:
        return []
    txt_lower = txt.lower()

    if step == "step3":
        # BGM 音量
        if any(k in txt for k in ["音量", "大声", "小声"]):
            m = re.search(r"(\d+(?:\.\d+)?)\s*%", txt)
            if m:
                return [{"name": "update_bgm_volume", "arguments": {"volume": float(m.group(1)) / 100}}]
            if "大" in txt:
                return [{"name": "update_bgm_volume", "arguments": {"volume": 1.0}}]
            if "小" in txt:
                return [{"name": "update_bgm_volume", "arguments": {"volume": 0.4}}]
        # BGM 偏移
        if "bgm" in txt_lower or "背景音乐" in txt or "节拍" in txt:
            m = re.search(r"([+-]?\d+(?:\.\d+)?)\s*秒?", txt)
            if m:
                return [{"name": "update_bgm_offset", "arguments": {"video_anchor_seconds": float(m.group(1))}}]
        # 调性
        for tone_kw, tone in [
            ("紧凑", "tight_hype"), ("高燃", "tight_hype"),
            ("沉稳", "calm_narrative"), ("叙事", "calm_narrative"),
            ("日常", "casual_daily"), ("专业", "professional_cool"),
        ]:
            if tone_kw in txt:
                return [{"name": "update_compose_setting", "arguments": {"tone": tone}}]
        # 比例
        for ratio_kw, ratio in [("竖屏", "9:16"), ("9:16", "9:16"), ("横屏", "16:9"), ("16:9", "16:9"), ("方版", "1:1"), ("1:1", "1:1")]:
            if ratio_kw in txt:
                return [{"name": "update_compose_setting", "arguments": {"aspect_ratio": ratio}}]

    if step == "step2":
        # stage-24 分镜级编辑兜底：『sec-1 第 2 镜短一点 / 改成 3 秒』『sec-0 第 1 镜画面改成 ...』『sec-2 第 3 镜口播改成 ...』
        m_shot_dur = re.search(r"(sec-\d+)[^第]{0,15}?第\s*(\d+)\s*镜.{0,15}?(\d+(?:\.\d+)?)\s*秒", txt)
        if m_shot_dur:
            return [{"name": "update_shot_duration", "arguments": {
                "section_id": m_shot_dur.group(1),
                "shot_order": int(m_shot_dur.group(2)) - 1,
                "duration_seconds": float(m_shot_dur.group(3)),
            }}]
        m_shot_visual = re.search(r"(sec-\d+)[^第]{0,15}?第\s*(\d+)\s*镜.{0,8}?画面.{0,8}?(?:改成|改为|是)?\s*[:：]?\s*(.+)", txt)
        if m_shot_visual:
            visual = m_shot_visual.group(3).strip().strip("『』""\"'")
            if visual:
                return [{"name": "update_shot_visual", "arguments": {
                    "section_id": m_shot_visual.group(1),
                    "shot_order": int(m_shot_visual.group(2)) - 1,
                    "visual": visual[:120],
                }}]
        m_shot_narr = re.search(r"(sec-\d+)[^第]{0,15}?第\s*(\d+)\s*镜.{0,8}?(?:口播|字幕|文案).{0,8}?(?:改成|改为|是)?\s*[:：]?\s*(.+)", txt)
        if m_shot_narr:
            narr = m_shot_narr.group(3).strip().strip("『』""\"'")
            if narr:
                return [{"name": "update_shot_narration", "arguments": {
                    "section_id": m_shot_narr.group(1),
                    "shot_order": int(m_shot_narr.group(2)) - 1,
                    "narration": narr[:200],
                }}]
        # 时长
        m_dur = re.search(r"(sec-\d+|第\s*\d+\s*段).{0,15}?(\d+(?:\.\d+)?)\s*秒", txt)
        if m_dur:
            sid_raw = m_dur.group(1)
            if sid_raw.startswith("sec-"):
                sid = sid_raw
            else:
                idx_m = re.search(r"\d+", sid_raw)
                sid = f"sec-{int(idx_m.group()) - 1}" if idx_m else "sec-0"
            return [{"name": "update_section_duration", "arguments": {"section_id": sid, "duration_seconds": float(m_dur.group(2))}}]
        # 删段
        m_del = re.search(r"删除?\s*(sec-\d+|第\s*\d+\s*段)", txt)
        if m_del:
            sid_raw = m_del.group(1)
            if sid_raw.startswith("sec-"):
                sid = sid_raw
            else:
                idx_m = re.search(r"\d+", sid_raw)
                sid = f"sec-{int(idx_m.group()) - 1}" if idx_m else "sec-0"
            return [{"name": "delete_section", "arguments": {"section_id": sid}}]
    return []


async def run_compose_edit(
    plan: Plan,
    instruction: str,
    step: ComposeEditStep,
) -> tuple[Plan, list[ComposeEditDiff], str | None]:
    """核心调度：plan 是 deep-copy 后传进来的副本，本函数会就地 mutate。

    返回 (mutated_plan, diffs, note)。note=None 时表示正常；非空时是兜底说明。
    """
    tools = _STEP_TOOLS[step]
    system = _STEP_SYSTEM[step]
    user = _build_user_prompt(plan, instruction, step)

    llm = get_llm_client()
    try:
        result = await llm.complete_with_tools(system, user, tools)
    except Exception as exc:  # noqa: BLE001
        log.warning("[compose_edit] LLM 失败 step=%s: %s", step, exc)
        result = {"tool_calls": [], "content": str(exc)}

    tool_calls = result.get("tool_calls") or []
    llm_text = (result.get("content") or "").strip()
    allowed_names = _STEP_ALLOWED_OPS[step]
    cleaned: list[dict[str, Any]] = []
    out_of_scope_hits: list[str] = []
    for tc in tool_calls:
        name = tc.get("name", "")
        if name in allowed_names:
            cleaned.append({"name": name, "arguments": tc.get("arguments") or {}})
        elif name in _MUTATORS:
            out_of_scope_hits.append(name)
    if not cleaned:
        cleaned = _mock_intent(instruction, step)

    diffs: list[ComposeEditDiff] = []
    for tc in cleaned:
        name = tc["name"]
        args = tc.get("arguments") or {}
        if name in _ASYNC_OPS:
            mut_async = _ASYNC_MUTATORS.get(name)
            if mut_async is None:
                continue
            diff = await mut_async(plan, args)
        else:
            mut = _MUTATORS.get(name)
            if mut is None:
                continue
            diff = mut(plan, args)
        if diff is not None:
            # 把 mutator 实参塞回 diff，apply 阶段原样回放，跳过 LLM 二次推理
            diff.args = {"op": name, **args}
            diffs.append(diff)

    note: str | None = None
    if not diffs:
        if step == "step3" and any(op in _CONTENT_TRACK_OPS for op in out_of_scope_hits):
            note = "step3 不可改内容轨（文案/段时长/删段/重排），请回 step2 调整结构。"
        elif step == "step2" and out_of_scope_hits:
            note = "step2 只改内容轨；字卡 / BGM / 调性 / 比例等请到 step3 包装编辑再调。"
        elif llm_text and not _looks_like_excuse(llm_text):
            # LLM 把指令理解成了讲解 / 问答 —— 把它说的话原样回给用户
            note = llm_text[:600]
        elif not cleaned:
            examples = {
                "step2": "如『把 sec-1 改成 5 秒』『删除 sec-2』『把段落顺序改成 sec-0, sec-2, sec-1』；也可以问『当前结构什么样？』『sec-1 这段时长够撑得起卖点吗？』",
                "step3": "如『BGM 推迟 2 秒』『调性改紧凑』『画面改方版』『把字卡 sc-3 文字改成…』；也可以问『当前调性是什么？』『现在的字卡密度合适吗？』",
            }[step]
            note = f"我没识别出可执行的编辑动作，请试更具体的指令——{examples}。"
        else:
            note = "工具识别成功但本地匹配失败（目标 id 不存在或参数无效）。"
    return plan, diffs, note


def _looks_like_excuse(text: str) -> bool:
    """LLM 偶尔会回『我没法执行』『请说具体点』这种空话——这种情况下落到默认引导更友好。"""
    if len(text) < 12:
        return True
    excuses = ("没法识别", "无法识别", "请说得更具体", "请提供更多", "无法执行", "没看明白")
    return any(k in text for k in excuses)


def replay_compose_ops(
    plan: Plan,
    confirmed_ops: list[dict[str, Any]],
    step: ComposeEditStep,
) -> list[ComposeEditDiff]:
    """确定性回放 dry-run 阶段已经定下来的 ops，**完全不走 LLM**，保证多 diff 一次 apply 全部落地。

    Args:
        plan: model_copy(deep=True) 出来的工作副本，本函数就地 mutate
        confirmed_ops: 形如 [{"op": "update_section_duration", "section_id": "sec-1", "duration_seconds": 5}, ...]
        step: 用作 step 边界检查

    返回新算出的 diffs（含最新 summary，因为顺序执行后 plan state 可能变了）。

    注意：异步 op（regenerate_fill）在 replay 时跳过——重生成会再调一遍 fill_gap，
    成本与 dry-run 一致；如果用户已经在 dry-run 里看到了重生成结果，再 replay 会
    产生新的网络 IO。前端在 confirmed_ops 时应过滤掉 regenerate_fill，或调
    `replay_compose_ops_async`（如果未来需要）。
    """
    allowed = _STEP_ALLOWED_OPS[step]
    out: list[ComposeEditDiff] = []
    for entry in confirmed_ops:
        if not isinstance(entry, dict):
            continue
        op = str(entry.get("op", ""))
        if op not in allowed:
            log.warning("[compose_edit.replay] 跳过越界 op=%s step=%s", op, step)
            continue
        if op in _ASYNC_OPS:
            log.info("[compose_edit.replay] 跳过异步 op=%s（regenerate_fill 在 dry-run 时已经写入 plan）", op)
            continue
        mut = _MUTATORS.get(op)
        if mut is None:
            continue
        args = {k: v for k, v in entry.items() if k != "op"}
        diff = mut(plan, args)
        if diff is not None:
            diff.args = {"op": op, **args}
            out.append(diff)
        else:
            log.warning("[compose_edit.replay] op=%s args=%s 返回 None（目标 id 已变？）", op, args)
    return out


def make_new_plan_id() -> str:
    return f"plan-{uuid.uuid4().hex[:10]}"
