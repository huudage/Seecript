"""Narration Agent —— 综合时长 + 内容直接输出每段口播；禁止复述凑时长。

为什么要单独抽出来：
- plan_agent 给出的 ShotPlan.narration 在 step1/step2 阶段还没确定最终段落时长，
  LLM 会按估时随便写。step3 入口前，所有段长已定稿，需要重新生成一份"严丝合缝"的口播。
- 旧版本 TTS 链 atempo 在 [0.75, 1.30] 之外会回落 1.0x，于是文本太短播完留空、太长被截。
  解法：在文本生成阶段就按 5 字/秒（汉语播报均速）估算段长目标字数，让 LLM 写得**刚好够**。
- 用户特别强调"禁止做复述靠拢时长"——本 agent system prompt 把这条放进硬约束里。

调用入口：`server/app/routers/plan.py:POST /api/plan/{plan_id}/regenerate-narrations`。
失败回落：保留每段原 narration（不抹掉用户已经手改过的内容）。
"""
from __future__ import annotations

import logging
from typing import Optional

from ..llm_client import LLMError, get_llm_client, _extract_json
from .preference import preference_hint
from ...schemas import Plan

log = logging.getLogger("seecript.agent.narration")


# 每秒可朗读的汉字字数（自然语速；TTS atempo 1.0x 约 4.5-5.5 字/秒）
_CHARS_PER_SECOND = 5.0


_NARRATION_SYSTEM = (
    "你是短视频口播脚本撰稿。给定全片 brief / 视频要求 / 段落清单（含每段角色 / 时长 / 内容描述 / 各分镜要表达的画面），"
    "为每个分镜（scene_id）输出一句口播。\n\n"
    "—— 绝对禁止 ——\n"
    "1. 不许为了凑够时长在文案里『复述、同义反复、把同一个意思换种说法说三次』。"
    "宁可短不许水。如果一段时长 6 秒、内容只够说 5 个字，那就只写 5 个字。\n"
    "2. 不许把『时长 Ns』『接下来 N 秒』之类元数据写进文案。\n"
    "3. 不许写 markdown / 引号 / 列表 / 分号清单 —— 必须是自然口语中文。\n"
    "4. 不许出现段落角色名（hook/opening/climax/closing/step_N/item_N）。\n"
    "5. 同一个意思只允许在全片中出现一次，不允许跨段重复同一句关键卖点（除非该卖点是结尾 CTA 的回扣）。\n\n"
    "—— 字数与节奏 ——\n"
    "• 普通话播报均速约 5 字/秒；每段口播字数 = 段时长 × 5（向下取整）。\n"
    "• 字数请控制在『目标字数 -30%』到『目标字数』之间——宁可短一点留白，也不要超时。\n"
    "• 钩子/开场段建议比目标字数再短 10-20%，让画面留出呼吸；高潮/收尾段尽量贴近目标。\n"
    "• **每个分镜都必须给一句不少于 3 个字的口播**——不允许返回空字符串。即使是纯画面/物品特写，"
    "也用一句简短的『画面解说 / 旁白点评 / 状态说明』来填，给观众一个停留点。\n\n"
    "—— 风格 ——\n"
    "• 口语化、有节奏感；动词优先；少用形容词堆砌。\n"
    "• 把『你 / 我们 / 来 / 看 / 试试』这类对话感词放在合适处（不是每段都用）。\n"
    "• 整片串起来念应当像一个人在讲故事，而不是一段段独立旁白。\n\n"
    "—— 输出 JSON ——\n"
    "{\"narrations\": [{\"scene_id\": \"sc-0\", \"text\": \"...\"}, {\"scene_id\": \"sc-1-shot-1\", \"text\": \"...\"}, ...]}\n"
    "scene_id 必须与输入的 scene_id 一一对应；**每条 text 都必须 ≥ 3 字，不允许空字符串**。\n"
    "text 严格遵守上面的字数与禁复述约束。"
)


async def regenerate_narrations(plan: Plan) -> dict[str, str]:
    """根据 plan.adapted_sections + plan.main_track + 全片 brief 重新生成每段口播。

    返回 `{scene_id: narration}`；调用方负责把它写回 scene.narration 并触发 TTS。
    LLM 失败时返回 `{}`（调用方应保留旧 narration）。
    """
    scenes = plan.main_track or []
    if not scenes:
        return {}

    settings = plan.settings
    brief = (plan.brief or "").strip()
    goal = (plan.video_goal or "").strip()

    # 段落上下文：role / theme / content_description / 时长 / shots 列表
    section_lines: list[str] = []
    sec_by_id = {sec.section_id: sec for sec in (plan.adapted_sections or [])}

    scene_table: list[str] = []
    for sc in scenes:
        target_chars = max(0, int(sc.duration * _CHARS_PER_SECOND))
        sec = sec_by_id.get(sc.parent_section_id or "")
        sec_role = sec.role if sec else sc.section
        sec_theme = (sec.theme if sec else "") or "—"
        scene_table.append(
            f"  · {sc.scene_id}（{sc.duration:.1f}s，目标≤{target_chars}字）"
            f" 段={sec_role}/{sec_theme}"
            f" | 主体={sc.shot_subject or '—'}"
            f" | 当前文案={(sc.narration or '').strip() or '（空）'}"
        )

    if plan.adapted_sections:
        section_lines.append("【段落总览（按时间序）】")
        for sec in plan.adapted_sections:
            section_lines.append(
                f"  · {sec.section_id}（{sec.role} · {sec.duration_seconds:.1f}s）"
                f" 主题={sec.theme or '—'}；内容={sec.content_description or '—'}"
            )

    user_lines: list[str] = []
    if brief:
        user_lines.append(f"【全片主题】{brief}")
    if goal:
        user_lines.append(f"【视频要求与目的】{goal}")
    user_lines.append(preference_hint(settings.migration_preference))
    if section_lines:
        user_lines.extend(section_lines)
    user_lines.append("【分镜清单（每条都要给一句 narration）】")
    user_lines.extend(scene_table)
    user_lines.append(
        "请输出 JSON：{\"narrations\":[{\"scene_id\":..., \"text\":...}]}\n"
        "严格执行字数上限与禁复述约束；**每个 scene 都必须给一句 ≥ 3 字的 text，不允许返回空**。"
    )

    user = "\n".join(user_lines)

    llm = get_llm_client()
    try:
        text = await llm.complete(_NARRATION_SYSTEM, user)
        data = _extract_json(text) if text else None
    except (LLMError, ValueError, Exception) as exc:  # noqa: BLE001
        log.warning("[narration] plan=%s LLM 失败：%s", plan.plan_id, exc)
        return {}

    if not isinstance(data, dict) or not isinstance(data.get("narrations"), list):
        log.warning("[narration] plan=%s LLM 返回不合法", plan.plan_id)
        return {}

    out: dict[str, str] = {}
    valid_scene_ids = {sc.scene_id for sc in scenes}
    for raw in data["narrations"]:
        if not isinstance(raw, dict):
            continue
        sid = str(raw.get("scene_id") or "").strip()
        if sid not in valid_scene_ids:
            continue
        txt = _sanitize(str(raw.get("text") or ""))
        # 字数硬截断：超长时按目标字数+20% 截断（避免 LLM 越界把口播写飞）
        sc = next(s for s in scenes if s.scene_id == sid)
        target = max(1, int(sc.duration * _CHARS_PER_SECOND))
        cap = int(target * 1.2)
        if len(txt) > cap:
            txt = _truncate_at_punct(txt, cap)
        # 空/过短回退：用 shot_subject + section theme 拼一句兜底，避免 voice/synthesize-all 跳过
        if len(txt) < 3:
            txt = _fallback_narration(sc, sec_by_id, target)
        out[sid] = txt

    # LLM 漏给某些 scene → 也用兜底填上（synthesize_all 不能再有空缺）
    for sc in scenes:
        if sc.scene_id not in out:
            target = max(1, int(sc.duration * _CHARS_PER_SECOND))
            out[sc.scene_id] = _fallback_narration(sc, sec_by_id, target)

    log.info(
        "[narration] plan=%s ok %d/%d scenes covered, %d non-empty",
        plan.plan_id, len(out), len(scenes), sum(1 for v in out.values() if v),
    )
    return out


def _fallback_narration(scene, sec_by_id: dict, target_chars: int) -> str:
    """LLM 没给/给了空 时的兜底文案。

    优先级：shot_subject → section.theme → section.content_description 截断 → "画面定格"。
    保证 ≥3 字、≤target_chars*1.2。
    """
    sec = sec_by_id.get(scene.parent_section_id or "") if scene.parent_section_id else None
    subject = (scene.shot_subject or "").strip()
    theme = (sec.theme if sec else "") or ""
    content = (sec.content_description if sec else "") or ""

    cap = max(6, int(target_chars * 1.2))
    if subject:
        text = f"{subject}定格" if len(subject) <= 6 else subject[:cap]
    elif theme:
        text = theme[:cap]
    elif content:
        text = content[:cap]
    else:
        text = "画面定格"
    return text[:cap] if len(text) >= 3 else (text + "登场")


def _sanitize(text: str) -> str:
    """剥掉 markdown / 引号 / 元数据词。"""
    if not text:
        return ""
    s = text.replace("```", "").replace("`", "").strip()
    s = s.strip("「」\"'“” ")
    # 元数据自指剥离（不区分大小写）
    import re as _re
    s = _re.sub(r"\b(opening|hook|climax|closing|step[_\s]?\d+|item[_\s]?\d+)\b",
                "", s, flags=_re.IGNORECASE)
    s = _re.sub(r"接下来.{0,3}\d+秒", "", s)
    return s.strip()


def _truncate_at_punct(text: str, cap: int) -> str:
    """在 cap 长度附近找最近的中文标点截断。找不到就硬切。"""
    if len(text) <= cap:
        return text
    head = text[:cap]
    for punct in "。！？；，、":
        idx = head.rfind(punct)
        if idx >= cap // 2:
            return head[: idx + 1]
    return head.rstrip("，。、；,;.") + "…"
