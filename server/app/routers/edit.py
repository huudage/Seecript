"""Module 7 — 自然语言编辑。

`POST /api/edit/apply`  LLM tool calling → 改 Plan → 返回新 Plan（前端推到 undo 栈）

阶段 3 实现：
  1. 从 PlanStore 取出当前 Plan
  2. 构造 tool 描述（5 个原子操作）+ 调 LLMClient.complete_with_tools
  3. 在 Plan 深拷贝上 dispatch tool_calls
  4. 新 plan_id 写回 PlanStore，前端 push 到 undo 栈

Tool 定义保持轻量、原子：用户的一句指令一般对应 1-2 个 tool call。
"""
from __future__ import annotations

import copy
import logging
import uuid

from fastapi import APIRouter, HTTPException

from ..schemas import EditApplyRequest, Plan
from ..services.llm_client import get_llm_client
from ..services.plans import plan_store

log = logging.getLogger("seecript.edit")
router = APIRouter()


_EDIT_SYSTEM = (
    "你是视频剪辑助手。用户给一段自然语言指令；"
    "请输出 edit_tool_calls，从给定 tools 中调用 1-3 个最匹配的工具。"
    "可选 tools 一定要严格按 JSON schema 给参数。"
    "如果指令含『时长 / 更长 / 更短』优先 edit_scene_duration；"
    "『口语 / 字幕 / 口播』优先 edit_scene_narration；"
    "『替换 / 换成 / 改成』优先 replace_scene_material；"
    "『BGM / 音量』优先 update_bgm_volume。"
)


_EDIT_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "edit_scene_narration",
            "description": "改写指定 scene 的口播文字。",
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
    {
        "type": "function",
        "function": {
            "name": "edit_scene_duration",
            "description": "调整 scene 时长（秒）。",
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


def _dispatch(plan: Plan, name: str, args: dict) -> str:
    """对 plan 原地应用 tool。返回简短描述。"""
    if name == "edit_scene_narration":
        sid = args.get("scene_id")
        narr = args.get("narration", "")
        for sc in plan.main_track:
            if sc.scene_id == sid:
                sc.narration = narr
                return f"narration({sid})"
        # 没找到时改第一个非空
        if plan.main_track:
            plan.main_track[0].narration = narr
            return f"narration(fallback {plan.main_track[0].scene_id})"
    elif name == "edit_scene_duration":
        sid = args.get("scene_id")
        dur = float(args.get("duration", 0))
        for sc in plan.main_track:
            if sc.scene_id == sid:
                sc.duration = max(0.5, dur)
                return f"duration({sid})"
    elif name == "replace_scene_material":
        sid = args.get("scene_id")
        ref = args.get("source_ref", "")
        for sc in plan.main_track:
            if sc.scene_id == sid:
                sc.source_ref = ref
                return f"material({sid}→{ref})"
    elif name == "update_packaging_text":
        iid = args.get("item_id")
        txt = args.get("text", "")
        for it in plan.packaging_track:
            if it.item_id == iid:
                it.text = txt
                return f"packaging({iid})"
        if plan.packaging_track:
            plan.packaging_track[0].text = txt
            return f"packaging(fallback {plan.packaging_track[0].item_id})"
    elif name == "update_bgm_volume":
        plan.bgm.volume = max(0.0, min(1.0, float(args.get("volume", plan.bgm.volume))))
        return f"bgm_volume={plan.bgm.volume:.2f}"
    return f"noop({name})"


@router.post("/edit/apply", response_model=Plan)
async def apply_edit(req: EditApplyRequest) -> Plan:
    current = plan_store.get(req.plan_id)
    if current is None:
        raise HTTPException(status_code=404, detail=f"plan not found: {req.plan_id}")

    log.info("[edit] plan=%s instruction=%r marks=%d", req.plan_id, req.instruction, len(req.marks))

    # 拼 marks 给模型一个区段提示
    mark_lines: list[str] = []
    for m in req.marks:
        mark_lines.append(f"[{m.track}] {m.start:.1f}-{m.end:.1f}s target={m.target_id or '-'}")
    user = (
        f"当前 Plan main_track:\n"
        + "\n".join(
            f"- {sc.scene_id} ({sc.section}) src={sc.source_ref} dur={sc.duration:.1f}s narr={sc.narration!r}"
            for sc in current.main_track
        )
        + "\n\n当前 packaging_track:\n"
        + "\n".join(
            f"- {it.item_id} kind={it.kind} text={it.text!r}" for it in current.packaging_track
        )
        + (("\n\n用户选中：\n" + "\n".join(mark_lines)) if mark_lines else "")
        + f"\n\n用户指令：{req.instruction}\n输出 edit_tool_calls。"
    )

    llm = get_llm_client()
    try:
        result = await llm.complete_with_tools(_EDIT_SYSTEM, user, _EDIT_TOOLS)
    except Exception as exc:  # noqa: BLE001
        log.warning("[edit] LLM tool call failed: %s — append narration fallback", exc)
        result = {"tool_calls": [], "content": str(exc)}

    new_plan = current.model_copy(deep=True)
    new_plan.plan_id = f"plan-{uuid.uuid4().hex[:10]}"

    applied: list[str] = []
    for tc in result.get("tool_calls", []) or []:
        applied.append(_dispatch(new_plan, tc.get("name", ""), tc.get("arguments") or {}))

    # 兜底：模型没给 tool_calls 时，把指令直接写进第一个 scene 的 narration
    if not applied and new_plan.main_track:
        new_plan.main_track[0].narration = f"[{req.instruction[:60]}] {new_plan.main_track[0].narration or ''}".strip()
        applied.append(f"fallback-narration({new_plan.main_track[0].scene_id})")

    log.info("[edit] plan %s → %s, tool_calls=%s", current.plan_id, new_plan.plan_id, applied)
    plan_store.put(new_plan)
    return new_plan
