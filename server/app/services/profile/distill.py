"""profile 蒸馏 worker —— Hermes 风格规则提炼。

输入：单 project 的所有 trace（A 一条 + B 多条）
输出：ProjectKB（summary + rules[]）

LLM prompt 走 complete_json，规则按 4 类 scope（structure/source/narration/pacing）。
失败时不抛——只 log warn，让上游 render 主链路继续。

调用入口：`distill_project_kb(user_id, project_id)`，render 完成后异步调（如 settings.realtime_distill_enabled=True）。
"""
from __future__ import annotations

import logging
import time
import uuid
from typing import Any, Optional

from .schemas import KBRule, ProjectKB, TraceA, TraceB
from .store import (
    append_trace_a,  # noqa: F401  re-export
    list_project_kbs,  # noqa: F401
    load_project_kb,
    read_traces_a,
    read_traces_b,
    save_project_kb,
)

log = logging.getLogger("seecript.profile.distill")


_DISTILL_SYSTEM = (
    "你是短视频结构迁移引擎的「个性知识沉淀助手」。"
    "我会给你一个项目里用户对 LLM 生成结果做出的两类修订记录：\n"
    "- TraceA：渲染最终视频前后内容轨结构 v0 与 v1 的对比 diff（段数变化 / 角色拆并 / 镜头来源类型变化 / 口播文本差）\n"
    "- TraceB：用户在场景编辑、缺口补全、或 ⌘K 对话编辑小助手里输入的自然语言（+ compose_edit_dismissed 表示用户预览后撤回的方向，是负信号）\n\n"
    "你的任务：从这些记录中蒸馏出**该用户在本项目里**反复体现的「偏好规则」，"
    "用最短的中文表达出来，便于下次新项目的 plan_agent / gap_agent / compose_edit_agent 直接套用。\n\n"
    "规则按 5 类 scope 归类：\n"
    "- structure：段落结构偏好（如『climax 段固定拆成两段』『closing 用单段不要余韵』）\n"
    "- source   ：镜头来源选择偏好（如『高潮段拒绝 aigc，必用真镜头』）\n"
    "- narration：口播/文案风格偏好（如『句末禁用感叹号』『不要英文混搭』）\n"
    "- pacing   ：节奏/时长偏好（如『开场不超过 3 秒』）\n"
    "- packaging：包装层偏好（调性 / 比例 / BGM 偏移 / 字卡风格——尤其参考用户在 ⌘K 对话编辑里反复改的项）\n\n"
    "输出严格 JSON：\n"
    "{\n"
    '  "summary": "≤80 字，整体偏好画像",\n'
    '  "rules": [\n'
    '    {"scope": "structure|source|narration|pacing|packaging", "text": "≤40 字的规则", "evidence_trace_ids": []}\n'
    "  ]\n"
    "}\n\n"
    "约束：\n"
    "- rules 数组 ≤ 8 条；只保留**真正有信号**的规则（出现 ≥2 次或单次但极强烈）。\n"
    "- 撤回方向（context=compose_edit_dismissed）是**负信号**——表达成『避免 X』而不是『偏好 X』。\n"
    "- 每条 rule.text 必须能直接拼到下游 prompt（不得用『建议』『可能』之类的虚词）。\n"
    "- 没有任何稳定信号时，rules 留空，summary 写「暂无足够信号」。\n"
    "- evidence_trace_ids 留空数组（v1 不强制做精细回溯）。"
)


def _trace_a_to_user_text(t: TraceA) -> str:
    parts = [f"## TraceA ({time.strftime('%Y-%m-%d', time.localtime(t.ts))})"]
    parts.append(f"- 段数变化 v0={len(t.v0.adapted_sections)} → v1={len(t.v1.adapted_sections)} (Δ={t.diff.section_count_delta})")
    if t.diff.role_changes:
        parts.append("- 角色变化:")
        for rc in t.diff.role_changes[:6]:
            parts.append(f"  · order={rc.section_order}: {rc.before or '(新增)'} → {rc.after or '(删除)'}")
    if t.diff.source_changes:
        parts.append("- 镜头来源变化:")
        for sc in t.diff.source_changes[:6]:
            parts.append(f"  · {sc.section_role} 段: {sc.before} → {sc.after}")
    if t.diff.narration_diffs:
        parts.append("- 口播文本变化:")
        for nd in t.diff.narration_diffs[:8]:
            b = (nd.before or "")[:40]
            a = (nd.after or "")[:40]
            parts.append(f"  · {nd.scene_id}: \"{b}\" → \"{a}\"")
    return "\n".join(parts)


def _trace_b_to_user_text(t: TraceB, idx: int) -> str:
    head = f"## TraceB-{idx} ({t.context}, scene={t.scene_id or t.gap_id or '?'}, role={t.section_role or '?'})"
    body = f"- 用户输入: {t.user_input[:200]}"
    extra = ""
    if t.context in {"compose_edit", "compose_edit_dismissed"}:
        ops = t.after.get("ops") if isinstance(t.after, dict) else None
        if isinstance(ops, list) and ops:
            op_summary = "、".join(
                f"{op.get('op', '?')}" + (f"({op.get('section_id') or op.get('scene_id') or op.get('item_id') or ''})"
                                          if isinstance(op, dict) else "")
                for op in ops[:5] if isinstance(op, dict)
            )
            extra = f"\n- 实际 ops（{'已落地' if t.context == 'compose_edit' else '被撤回'}）: {op_summary}"
    return f"{head}\n{body}{extra}"


def _build_user_payload(
    project_id: str,
    traces_a: list[TraceA],
    traces_b: list[TraceB],
) -> str:
    head = [f"项目 ID: {project_id}", f"TraceA 条数: {len(traces_a)}", f"TraceB 条数: {len(traces_b)}", ""]
    blocks: list[str] = list(head)
    for ta in traces_a:
        blocks.append(_trace_a_to_user_text(ta))
        blocks.append("")
    for i, tb in enumerate(traces_b, 1):
        blocks.append(_trace_b_to_user_text(tb, i))
        blocks.append("")
    return "\n".join(blocks)


def _extract_project_meta(traces_a: list[TraceA]) -> dict[str, Any]:
    """从最近一条 TraceA 反推项目元信息（标题用 brief 截断；video_type 暂不取，schema 上没字段）。"""
    if not traces_a:
        return {"project_title": "", "video_type": None}
    latest = traces_a[-1]
    title = (latest.v1.brief or latest.v0.brief or "")[:30]
    return {"project_title": title, "video_type": latest.v1.video_type}


async def distill_project_kb(user_id: str, project_id: str) -> Optional[ProjectKB]:
    """读取本 project 全部 trace → LLM 蒸馏 → 覆盖式落 ProjectKB。

    返回 None 表示蒸馏失败或无 trace；返回 ProjectKB 表示成功落盘。
    """
    traces_a = read_traces_a(user_id, project_id=project_id)
    traces_b = read_traces_b(user_id, project_id=project_id)
    if not traces_a and not traces_b:
        log.info("[profile.distill] project=%s 无 trace 数据，跳过蒸馏", project_id)
        return None

    user_payload = _build_user_payload(project_id, traces_a, traces_b)
    log.info("[profile.distill] project=%s traces_a=%d traces_b=%d payload_chars=%d",
             project_id, len(traces_a), len(traces_b), len(user_payload))

    # 延迟 import：profile 模块本身不该硬依赖 LLM client，单测可独立 monkeypatch
    from ..llm_client import get_llm_client, LLMError

    client = get_llm_client()
    try:
        raw = await client.complete_json(
            _DISTILL_SYSTEM, user_payload, temperature=0.4, max_tokens=1200,
        )
    except LLMError as exc:
        log.warning("[profile.distill] project=%s LLM 蒸馏失败: %s", project_id, exc)
        return None
    except Exception as exc:  # noqa: BLE001
        log.warning("[profile.distill] project=%s 未知异常: %s", project_id, exc)
        return None

    summary = str(raw.get("summary") or "").strip()[:200]
    rules_raw = raw.get("rules") or []
    rules: list[KBRule] = []
    for r in rules_raw[:8]:
        if not isinstance(r, dict):
            continue
        scope = str(r.get("scope") or "").strip().lower()
        if scope not in {"structure", "source", "narration", "pacing", "packaging"}:
            continue
        text = str(r.get("text") or "").strip()[:80]
        if not text:
            continue
        rules.append(KBRule(
            id=f"rule-{uuid.uuid4().hex[:6]}",
            scope=scope,
            text=text,
            evidence_trace_ids=list(r.get("evidence_trace_ids") or [])[:6],
        ))

    meta = _extract_project_meta(traces_a)
    kb = ProjectKB(
        project_id=project_id,
        project_title=meta["project_title"],
        video_type=meta["video_type"],
        render_committed_at=int(time.time()),
        summary=summary or "暂无足够信号",
        rules=rules,
    )
    save_project_kb(user_id, kb)
    log.info("[profile.distill] project=%s 蒸馏完成 summary_len=%d rules=%d",
             project_id, len(summary), len(rules))
    return kb
