"""Module 7 — 自然语言编辑（三轨分流版）。

`POST /api/edit/apply`  按 `track ∈ {main, packaging, voice}` 拆 LLM tools，
模型只看自己轨道的工具集，意图识别更准；用户也能用"我现在只想改字幕，
别动内容"这种话表达边界。

渲染态锁：`track=="main"` 且对应 Project.current_step=="render" → 409。
        产品约束："进了渲染流程，内容轨不可改；要改请回 Compose"。

口播轨 (track=="voice") 改完 narration 后自动重合成 TTS，覆盖 voiceover_url。
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Callable

from fastapi import APIRouter, HTTPException

from ..schemas import EditApplyRequest, Plan, SceneTransition, TransitionStyle
from ..services.llm_client import get_llm_client
from ..services.plans import plan_store
from ..services.projects import project_store
from ..services.tts import TTSError, synthesize_scene_voice

log = logging.getLogger("seecript.edit")
router = APIRouter()


_SYSTEM_MAIN = (
    "你是视频剪辑助手，本次只能修改【内容轨】（main_track）。"
    "可选 tool：调整 scene 时长 / 替换 scene 素材 / 设置 scene 入场转场。"
    "禁止改字幕、BGM、口播 —— 那些是其他轨道的工具。"
    "『时长 / 更长 / 更短 / 缩短 / 拉长 / N秒』→ edit_scene_duration；"
    "『替换 / 换成 / 改成 / 用素材』→ replace_scene_material；"
    "『转场 / 过渡 / 切换 / dissolve / 渐变 / 推拉 / 缩放 / 擦除』→ set_scene_transition；"
    "set_scene_transition 的 style 必须取自 {hard_cut, dissolve, slide, zoom, whip, wipe}，"
    "其他词都先归到 dissolve；duration 不填默认 0.4 秒（范围 0.1-1.5）。"
)


_SYSTEM_PACKAGING = (
    "你是视频剪辑助手，本次只能修改【包装轨】（packaging_track / BGM）。"
    "可选 tool：改字幕/标题/贴纸文字 / 调 BGM 音量。"
    "禁止改 scene 时长、口播、素材 —— 那些是其他轨道的工具。"
    "『字幕 / 标题 / 文字 / 改成 / 写成』→ update_packaging_text；"
    "『BGM / 背景音乐 / 音量 / 大声 / 小声 / 调到』→ update_bgm_volume。"
)


_SYSTEM_VOICE = (
    "你是视频剪辑助手，本次只能修改【口播轨】（main_track[i].narration），"
    "也就是 TTS 朗读用的文字稿。修改后系统会自动重新合成 wav。"
    "可选 tool：仅 edit_scene_narration。"
    "禁止改时长、字幕、素材、BGM。"
    "『口播 / 旁白 / 念白 / 朗读 / 口语化 / 改得更…』→ edit_scene_narration。"
)


_TOOLS_MAIN = [
    {
        "type": "function",
        "function": {
            "name": "edit_scene_duration",
            "description": "调整 scene 时长（秒）。下限 0.5 秒。",
            "parameters": {
                "type": "object",
                "properties": {
                    "scene_id": {"type": "string"},
                    "duration": {"type": "number"},
                },
                "required": ["scene_id", "duration"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "replace_scene_material",
            "description": "把 scene 的来源素材换成另一个 material_id 或样例 shot 引用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "scene_id": {"type": "string"},
                    "source_ref": {"type": "string"},
                },
                "required": ["scene_id", "source_ref"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_scene_transition",
            "description": "设置 scene 的入场转场（与上一段如何衔接）。sc-0 没有上一段，调它会被忽略。",
            "parameters": {
                "type": "object",
                "properties": {
                    "scene_id": {"type": "string"},
                    "style": {
                        "type": "string",
                        "enum": ["hard_cut", "dissolve", "slide", "zoom", "whip", "wipe"],
                    },
                    "duration": {"type": "number", "description": "0.1-1.5 秒，默认 0.4"},
                },
                "required": ["scene_id", "style"],
            },
        },
    },
]


_TOOLS_PACKAGING = [
    {
        "type": "function",
        "function": {
            "name": "update_packaging_text",
            "description": "修改包装轨某 item 的文字（字幕 / 标题条 / 贴纸）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "item_id": {"type": "string"},
                    "text": {"type": "string"},
                },
                "required": ["item_id", "text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_bgm_volume",
            "description": "调整 BGM 音量（0-1）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "volume": {"type": "number"},
                },
                "required": ["volume"],
            },
        },
    },
]


_TOOLS_VOICE = [
    {
        "type": "function",
        "function": {
            "name": "edit_scene_narration",
            "description": "改写指定 scene 的口播文字。系统会自动重新合成 wav。",
            "parameters": {
                "type": "object",
                "properties": {
                    "scene_id": {"type": "string"},
                    "narration": {"type": "string"},
                },
                "required": ["scene_id", "narration"],
            },
        },
    },
]


_VALID_STYLES: set[str] = {"hard_cut", "dissolve", "slide", "zoom", "whip", "wipe"}


def _dispatch_main(plan: Plan, name: str, args: dict) -> str:
    if name == "edit_scene_duration":
        sid = args.get("scene_id")
        dur = float(args.get("duration", 0))
        for sc in plan.main_track:
            if sc.scene_id == sid:
                sc.duration = max(0.5, dur)
                return f"duration({sid}={sc.duration:.1f}s)"
        return f"miss(duration {sid})"
    if name == "replace_scene_material":
        sid = args.get("scene_id")
        ref = args.get("source_ref", "")
        for sc in plan.main_track:
            if sc.scene_id == sid:
                sc.source_ref = ref
                return f"material({sid}→{ref})"
        return f"miss(material {sid})"
    if name == "set_scene_transition":
        sid = args.get("scene_id")
        raw_style = args.get("style", "dissolve")
        style: TransitionStyle = raw_style if raw_style in _VALID_STYLES else "dissolve"  # type: ignore[assignment]
        dur = float(args.get("duration", 0.4) or 0.4)
        dur = max(0.1, min(1.5, dur))
        for i, sc in enumerate(plan.main_track):
            if sc.scene_id == sid:
                if i == 0:
                    return f"skip(sc-0 没有上一段，转场被忽略 {sid})"
                sc.transition_in = SceneTransition(style=style, duration=dur)
                return f"transition({sid}={style}/{dur:.2f}s)"
        return f"miss(transition {sid})"
    return f"noop-main({name})"


def _dispatch_packaging(plan: Plan, name: str, args: dict) -> str:
    if name == "update_packaging_text":
        iid = args.get("item_id")
        txt = args.get("text", "")
        for it in plan.packaging_track:
            if it.item_id == iid:
                it.text = txt
                return f"packaging({iid})"
        # packaging fallback：找不到 item 时改第一个非 transition 项，保留原 fallback 行为
        for it in plan.packaging_track:
            if it.kind != "transition":
                it.text = txt
                return f"packaging(fallback {it.item_id})"
        return f"miss(packaging {iid})"
    if name == "update_bgm_volume":
        plan.bgm.volume = max(0.0, min(1.0, float(args.get("volume", plan.bgm.volume))))
        return f"bgm_volume={plan.bgm.volume:.2f}"
    return f"noop-packaging({name})"


def _dispatch_voice(plan: Plan, name: str, args: dict, touched: set[str]) -> str:
    if name == "edit_scene_narration":
        sid = args.get("scene_id")
        narr = args.get("narration", "")
        for sc in plan.main_track:
            if sc.scene_id == sid:
                sc.narration = narr
                if sid:
                    touched.add(sid)
                return f"narration({sid})"
        return f"miss(narration {sid})"
    return f"noop-voice({name})"


_TRACK_CONFIG: dict[str, tuple[list[dict], str]] = {
    "main": (_TOOLS_MAIN, _SYSTEM_MAIN),
    "packaging": (_TOOLS_PACKAGING, _SYSTEM_PACKAGING),
    "voice": (_TOOLS_VOICE, _SYSTEM_VOICE),
}


def _build_user_prompt(plan: Plan, instruction: str, marks: list, track: str) -> str:
    parts: list[str] = []
    if track in ("main", "voice"):
        parts.append("当前 Plan main_track：")
        for sc in plan.main_track:
            tr = sc.transition_in
            tr_s = f" trans={tr.style}/{tr.duration:.2f}s" if tr else ""
            parts.append(
                f"- {sc.scene_id} ({sc.section}) src={sc.source_ref} "
                f"dur={sc.duration:.1f}s narr={sc.narration!r}{tr_s}"
            )
    if track == "packaging":
        parts.append("当前 packaging_track：")
        for it in plan.packaging_track:
            if it.kind == "transition":
                continue  # 旧 transition 包装项已废弃，不喂给模型免得它学坏
            parts.append(f"- {it.item_id} kind={it.kind} text={it.text!r}")
        parts.append(f"BGM 当前音量={plan.bgm.volume:.2f}")
    if marks:
        parts.append("\n用户选中：")
        for m in marks:
            parts.append(f"[{m.track}] {m.start:.1f}-{m.end:.1f}s target={m.target_id or '-'}")
    parts.append(f"\n用户指令（轨道={track}）：{instruction}")
    parts.append("输出 edit_tool_calls。")
    return "\n".join(parts)


def _enforce_render_lock(plan: Plan, track: str) -> None:
    """track==main 且对应 project.current_step==render → 409"""
    if track != "main":
        return
    pid = plan.project_id
    if not pid:
        return
    project = project_store.get(pid)
    if project is not None and project.current_step == "render":
        raise HTTPException(
            status_code=409,
            detail="已进入渲染流程，内容轨（main）不可改；请返回 Compose 步骤后再编辑，"
                   "或选择包装轨 / 口播轨。",
        )


@router.post("/edit/apply", response_model=Plan)
async def apply_edit(req: EditApplyRequest) -> Plan:
    current = plan_store.get(req.plan_id)
    if current is None:
        raise HTTPException(status_code=404, detail=f"plan not found: {req.plan_id}")

    _enforce_render_lock(current, req.track)

    if req.track not in _TRACK_CONFIG:
        raise HTTPException(status_code=400, detail=f"未知轨道：{req.track}")
    tools, system = _TRACK_CONFIG[req.track]

    log.info("[edit] plan=%s track=%s instruction=%r marks=%d",
             req.plan_id, req.track, req.instruction, len(req.marks))

    user = _build_user_prompt(current, req.instruction, req.marks, req.track)

    llm = get_llm_client()
    try:
        result = await llm.complete_with_tools(system, user, tools)
    except Exception as exc:  # noqa: BLE001
        log.warning("[edit] LLM tool call failed track=%s: %s", req.track, exc)
        result = {"tool_calls": [], "content": str(exc)}

    new_plan = current.model_copy(deep=True)
    new_plan.plan_id = f"plan-{uuid.uuid4().hex[:10]}"

    applied: list[str] = []
    voice_touched: set[str] = set()
    dispatcher: Callable[[str, dict], str]
    if req.track == "main":
        dispatcher = lambda n, a: _dispatch_main(new_plan, n, a)  # noqa: E731
    elif req.track == "packaging":
        dispatcher = lambda n, a: _dispatch_packaging(new_plan, n, a)  # noqa: E731
    else:
        dispatcher = lambda n, a: _dispatch_voice(new_plan, n, a, voice_touched)  # noqa: E731

    for tc in result.get("tool_calls", []) or []:
        applied.append(dispatcher(tc.get("name", ""), tc.get("arguments") or {}))

    if not applied:
        # 兜底策略按 track 分：main 直接 409 要求用户改清楚；packaging/voice 沿用旧行为兜底改第一个
        if req.track == "main":
            raise HTTPException(
                status_code=409,
                detail="未能识别出可执行的内容轨编辑动作；请改用更明确的指令"
                       "（如『把 sc-1 改成 3 秒』或『sc-2 加 dissolve 转场』）。",
            )
        if req.track == "voice" and new_plan.main_track:
            sid = new_plan.main_track[0].scene_id
            new_plan.main_track[0].narration = (
                f"[{req.instruction[:60]}] {new_plan.main_track[0].narration or ''}".strip()
            )
            voice_touched.add(sid)
            applied.append(f"fallback-narration({sid})")
        if req.track == "packaging":
            non_trans = [it for it in new_plan.packaging_track if it.kind != "transition"]
            if non_trans:
                non_trans[0].text = f"[{req.instruction[:30]}] {non_trans[0].text or ''}".strip()
                applied.append(f"fallback-packaging({non_trans[0].item_id})")

    # voice 轨道：对被修改 narration 的 scene 重新合成 wav
    if voice_touched:
        for sid in voice_touched:
            try:
                ret = await asyncio.to_thread(
                    synthesize_scene_voice, new_plan, sid,
                    text=None, voice=None,
                )
                if ret is None:
                    log.info("[edit] voice resynth skip scene=%s (空文案 / 未找到)", sid)
                else:
                    url, truncated, chars = ret
                    log.info("[edit] voice resynth scene=%s chars=%d truncated=%s url=%s",
                             sid, chars, truncated, url)
            except TTSError as exc:
                log.warning("[edit] voice resynth failed scene=%s: %s", sid, exc)

    log.info("[edit] plan %s → %s, track=%s tool_calls=%s",
             current.plan_id, new_plan.plan_id, req.track, applied)
    plan_store.put(new_plan)
    return new_plan
