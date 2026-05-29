"""AIGC Prompt Agent —— 把段落上下文转写为 Seedance T2V 友好的完备 prompt。

为什么需要：`gap.requirement` 是给创作者看的中文段落描述（『开场：黄金面具特写...』），
直接送给 Seedance 缺少镜头/景别/机位/光线/质感/动作等 T2V 关键要素 → 出片质量与预期偏差大。

本 agent 喂给 LLM 的上下文：AdaptedSection.theme + content_description + role + 时长
+ Plan.brief / video_goal + 用户在面板里追加的 hint，让 LLM 输出一句简洁但要素完备的
T2V prompt（≤120 字中文，包含主体/景别/机位/光线/质感/动作/情绪）。

调用入口在 `server/app/routers/gap.py:POST /api/gap/aigc-prompt`。
失败时回落 `f"短视频画面：{gap.requirement}"`，保证前端 textarea 始终有内容。
"""
from __future__ import annotations

import logging
from typing import Optional

from ..llm_client import LLMError, get_llm_client, _extract_json
from ...schemas import AdaptedSection, Gap, Plan

log = logging.getLogger("seecript.agent.aigc_prompt")


# 系统 prompt 同时是 mock 路由指纹：必须含 "t2v_prompt"，且在 plan_agent 的
# "adapted_sections" 之前优先匹配（mock 已按序号决定路由顺序）。
_PROMPT_SYSTEM = (
    "你是 Seedance 文生视频（T2V）的提示词工程师。给定一个短视频段落的角色、主题、"
    "内容说明、时长，以及视频的整体主题与目的，请输出一句**完备的中文 t2v_prompt**——"
    "Seedance 直接拿这一句去生成画面。\n\n"
    "要素必须覆盖（缺一不可）：\n"
    "1. 主体：画面里的人/物，正在做什么\n"
    "2. 景别：特写 / 中景 / 远景 / 航拍 / 大全景，至少一个\n"
    "3. 机位运动：固定 / 推进 / 拉远 / 跟随 / 摇移 / 手持，选一个最合本段叙事的\n"
    "4. 光线与色调：黄昏暖光 / 冷调高对比 / 自然光 / 棚拍硬光 等\n"
    "5. 质感：电影感 / 纪实 / 产品级 / 杂志感 等\n"
    "6. 情绪/氛围：紧张 / 庄重 / 轻快 / 神秘 等\n\n"
    "硬约束：\n"
    "- 总长 60-120 字中文，一句话或两个短句\n"
    "- 不出现段落角色名（opening/development/climax/closing）\n"
    "- 不出现『本段』『第 X 段』『片段』等元数据词\n"
    "- 不要 ASCII 引号、不要 markdown\n"
    "- 不要把『时长 Ns』直接写进 prompt 文案（duration_seconds 由后端单独传给 Seedance）\n\n"
    "返回 JSON：{\"prompt\": \"...一句完备的 t2v_prompt...\"}"
)


async def generate_aigc_prompt(
    gap: Gap,
    plan: Optional[Plan],
    section: Optional[AdaptedSection],
    *,
    user_hint: str = "",
) -> str:
    """根据 gap + 所属 section + plan 上下文 + 用户 hint 生成 T2V prompt。

    失败兜底：回落到 `短视频画面：{gap.requirement}`，保证 caller 一定拿得到字符串。
    """
    hint = (user_hint or "").strip()[:200]
    role = section.role if section else gap.section
    theme = (section.theme if section else "") or "（无主题）"
    content_desc = (section.content_description if section else "").strip()
    duration = float(section.duration_seconds) if section else 4.0

    brief = (plan.brief or "").strip() if plan else ""
    goal = (plan.video_goal or "").strip() if plan else ""

    user_lines: list[str] = [
        f"段落角色：{role}",
        f"段落主题：{theme}",
        f"段落时长：约 {duration:.1f}s",
        f"段落内容说明：{content_desc or '（无）'}",
        f"原始槽位需求：{gap.requirement}",
    ]
    if brief:
        user_lines.append(f"视频整体主题：{brief}")
    if goal:
        user_lines.append(f"视频要求与目的：{goal}")
    if hint:
        user_lines.append(f"创作者额外提示：{hint}")
    user_lines.append("请输出一句完备的 t2v_prompt，覆盖主体/景别/机位/光线/质感/情绪。")

    user = "\n".join(user_lines)

    llm = get_llm_client()
    try:
        text = await llm.complete(_PROMPT_SYSTEM, user)
        data = _extract_json(text) if text else None
        prompt = ""
        if isinstance(data, dict):
            prompt = str(data.get("prompt") or "").strip()
        prompt = _sanitize(prompt)
        if prompt:
            log.info(
                "[aigc-prompt] gap=%s role=%s ok len=%d",
                gap.gap_id, role, len(prompt),
            )
            return prompt
        log.warning("[aigc-prompt] gap=%s LLM 返回空 prompt → fallback", gap.gap_id)
    except (LLMError, ValueError, Exception) as exc:  # noqa: BLE001
        log.warning("[aigc-prompt] gap=%s LLM 失败 → fallback：%s", gap.gap_id, exc)

    return _fallback_prompt(gap, section, hint)


def _sanitize(prompt: str) -> str:
    """裁剪长度 + 去掉 prompt 里偶发的 markdown 残留与角色元数据词。"""
    if not prompt:
        return ""
    s = prompt.replace("```", "").replace("`", "").strip()
    # 不允许角色名直接出现
    for bad in ("opening", "development", "climax", "closing"):
        s = s.replace(bad, "")
    # 截断到 200 字（system prompt 要求 60-120，但留余地处理 LLM 越界）
    if len(s) > 200:
        s = s[:200].rstrip("，。；,;.") + "…"
    return s.strip()


def _fallback_prompt(gap: Gap, section: Optional[AdaptedSection], hint: str) -> str:
    """LLM 失败时的本地合成：把 content_description / requirement / hint 拼成一句保底 prompt。"""
    parts: list[str] = []
    if section and section.content_description:
        parts.append(section.content_description.strip())
    if gap.requirement:
        parts.append(gap.requirement.strip())
    if hint:
        parts.append(hint)
    base = "；".join(p for p in parts if p) or f"短视频画面：{gap.section} 段"
    # 兜底加点拍摄要素，让 Seedance 不至于完全失焦
    return _sanitize(
        f"{base}。镜头建议：中景跟随，自然光，电影感色调，节奏与情绪贴合段落主题。"
    )
