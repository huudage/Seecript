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
    "—— 绝对禁止（违反 = 重写整段） ——\n"
    "1. **严禁复述凑时长**——绝对不允许把同一个意思换种说法说两次／三次，不允许把同一短语连续重复（『来来来』『看看看』『一个一个又一个』全部违规），"
    "也不允许同义反复（『非常好』+『相当棒』+『十分赞』）。\n"
    "   口播字数应当严格由『内容能表达多少』决定，**与段时长无关**。"
    "   如果一段时长 8 秒、内容只够说 4 个字，那就只写 4 个字，留 7.x 秒静默，比堆词强一百倍。\n"
    "2. 不许把『时长 Ns』『接下来 N 秒』之类元数据写进文案。\n"
    "3. 不许写 markdown / 引号 / 列表 / 分号清单 —— 必须是自然口语中文。\n"
    "4. 不许出现段落角色名（hook/opening/climax/closing/step_N/item_N）。\n"
    "5. 同一关键卖点跨段只允许出现一次（除非结尾 CTA 的合理回扣）。\n\n"
    "—— 字数与节奏（只设上限，不设下限） ——\n"
    "• 普通话播报均速约 5 字/秒；**字数上限 = 段时长 × 5**（向下取整），超了会被截断。\n"
    "• 字数下限不设——一镜哪怕只需要 3 个字，就只写 3 个字，剩下让画面/字幕说话。\n"
    "• **每个分镜都必须给一句 ≥3 字的口播**——不允许返回空字符串。即使是纯画面/物品特写，"
    "用一句极简的『画面解说 / 旁白点评 / 状态说明』即可（如『咖啡冒热气』『齿轮咬合』），"
    "**禁止把这句拉长复述**。\n\n"
    "—— 风格 ——\n"
    "• 口语化、有节奏感；动词优先；少用形容词堆砌。\n"
    "• 把『你 / 我们 / 来 / 看 / 试试』这类对话感词放在合适处（不是每段都用）。\n"
    "• 整片串起来念应当像一个人在讲故事，而不是一段段独立旁白。\n\n"
    "—— 输出 JSON ——\n"
    "{\"narrations\": [{\"scene_id\": \"sc-0\", \"text\": \"...\"}, {\"scene_id\": \"sc-1-shot-1\", \"text\": \"...\"}, ...]}\n"
    "scene_id 必须与输入的 scene_id 一一对应；**每条 text 都必须 ≥ 3 字且 ≤ 段时长×5 字，不允许空字符串，不允许任何形式的重复凑数**。"
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
        # 反复述：先压掉 LLM 可能漏过去的重复（连续短语 / 同义反复短句）
        txt = _dedupe_repetition(txt)
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


def _dedupe_repetition(text: str) -> str:
    """压掉 LLM 偷塞的『复述凑时长』。

    分三层处理（够用且不误伤）：
    1. 连续单字重复 4 次以上 → 压成 2 次（『来来来来来』→『来来』）
    2. 连续 2-4 字短语完全重复 ≥2 次 → 只保留 1 次（『看看看看』→『看看』、『一个一个一个』→『一个』）
    3. 同一中文句子（按 。！？；分割）出现 ≥2 次 → 只保留首次出现位置

    保守起见，不动『不知道 不知道 不知道』这种有节奏感的修辞——只有完全连续重复才算违规。
    """
    if not text or len(text) < 4:
        return text
    import re as _re

    s = text
    # Layer 1: single-char 4+ runs → 2
    s = _re.sub(r"(.)\1{3,}", r"\1\1", s)

    # Layer 2: 2-4 char phrase repeated >= 2 times → keep one copy
    # 多次扫描覆盖嵌套场景
    for _ in range(3):
        prev = s
        s = _re.sub(r"(.{2,4}?)\1{2,}", r"\1", s)
        if s == prev:
            break

    # Layer 3: dedupe full sentences
    parts = _re.split(r"([。！？；])", s)
    sentences: list[str] = []
    buf = ""
    for token in parts:
        if token in "。！？；":
            sentences.append((buf + token).strip())
            buf = ""
        else:
            buf += token
    if buf.strip():
        sentences.append(buf.strip())
    seen: set[str] = set()
    dedup: list[str] = []
    for sent in sentences:
        key = _re.sub(r"\s+", "", sent)
        if not key:
            continue
        # 极短的 1-2 字句子（如『来。』）不去重——可能是节奏强调
        core = key.rstrip("。！？；，、,.;!?")
        if len(core) >= 3:
            if key in seen:
                continue
            seen.add(key)
        dedup.append(sent)
    s = "".join(dedup) if dedup else s

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
