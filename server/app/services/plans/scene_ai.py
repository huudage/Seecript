"""v2 功能盘 AI 动作服务（PRD-v2 F6 · Epic-5 D3）。

三个函数对应盘上三个「AI 出建议 → 人消费」的动作，共同契约：**一律不写 plan**。
- scene_insight：AI 理解 → 贴纸数据（摘要 / 标签 / 亮点），前端贴块上可摘除
- suggest_trim：AI 裁剪 → 建议入出点 + 理由，人确认后走 swap-source 手动裁剪应用
- shot_brief：补拍清单 → 拍摄规格（拍什么 / 多长 / 什么情绪 / 参考哪段），给人看

LLM 失败一律规则兜底（用素材预处理已产出的 caption / tags / highlight 拼装），
不向路由抛错——贴纸与建议是增强，不是依赖；路由层只拦结构性 422（非实拍块等）。
"""
from __future__ import annotations

import logging
from typing import Optional

from ...schemas import AdaptedSection, Material, Plan, Scene, ShotPlan
from ..llm_client import get_llm_client, _extract_json

log = logging.getLogger("seecript.scene_ai")

_ROLE_EMOTION = {
    "opening": "期待感",
    "development": "专注 / 跟随",
    "climax": "高燃 / 情绪高点",
    "closing": "信任 / 行动冲动",
}


def _matched_shot(scene: Scene, material: Material) -> Optional[object]:
    """定位 scene 当前用的 MaterialShot：优先按 in_point 落在哪条窗口，兜底首镜。"""
    if not material.shots:
        return None
    for ms in material.shots:
        if ms.start <= scene.in_point < ms.end:
            return ms
    return material.shots[0]


def _resolve_shot_plan(section: Optional[AdaptedSection], scene: Scene) -> Optional[ShotPlan]:
    if section is None or not section.shots:
        return None
    return next((sh for sh in section.shots if sh.order == scene.shot_order), None)


def _context_lines(
    plan: Plan, scene: Scene, section: Optional[AdaptedSection], material: Optional[Material]
) -> list[str]:
    lines = [
        f"【本镜】scene_id={scene.scene_id} 时长={scene.duration:.1f}s 主体={scene.shot_subject or '—'}",
        f"口播={(scene.narration or '').strip() or '（空）'}",
    ]
    if section is not None:
        lines.append(
            f"【所属段】{section.section_id}（{section.role} · {section.theme or '—'}）"
            f" 内容={section.content_description}"
        )
    if material is not None:
        shot = _matched_shot(scene, material)
        lines.append(
            f"【源素材】{material.filename}（{material.duration_seconds or 0:.1f}s）"
            f" 标签={','.join(material.tags[:8]) or '—'}"
            f" 主体词={','.join(material.subjects[:8]) or '—'}"
        )
        if material.highlight_reason:
            lines.append(f"高光点评：{material.highlight_reason}（评分 {material.highlight_score:.2f}）")
        if shot is not None and shot.caption:
            lines.append(f"本镜画面描述：{shot.caption}")
    if plan.brief:
        lines.append(f"【全片主题】{plan.brief}")
    if plan.video_goal:
        lines.append(f"【视频要求】{plan.video_goal}")
    return lines


async def scene_insight(
    plan: Plan, scene: Scene, section: Optional[AdaptedSection], material: Optional[Material]
) -> dict:
    """AI 理解 → 贴纸数据 {summary, tags, highlights, source}。"""
    ctx = "\n".join(_context_lines(plan, scene, section, material))
    system = (
        "你是短视频素材理解助手。给定一个分镜的上下文（源素材标签 / 画面描述 / 段落角色 / 口播），"
        "把它蒸馏成一枚画布贴纸，帮创作者快速判断『这段素材是什么、好在哪』。\n"
        "输出 JSON：{\"summary\": \"≤40字摘要\", \"tags\": [\"3-6个短标签\"], "
        "\"highlights\": [\"1-3条亮点，每条≤20字\"]}\n"
        "要求：summary 说清画面内容与可用场景；tags 用名词短语；highlights 只挑真正值得说的。"
    )
    try:
        llm = get_llm_client()
        text = await llm.complete(system, ctx)
        data = _extract_json(text) if text else None
    except Exception as exc:  # noqa: BLE001
        log.warning("[scene_ai] insight LLM 失败 scene=%s: %s", scene.scene_id, exc)
        data = None

    if isinstance(data, dict) and data.get("summary"):
        return {
            "summary": str(data["summary"])[:80],
            "tags": [str(t)[:12] for t in (data.get("tags") or [])][:6],
            "highlights": [str(h)[:40] for h in (data.get("highlights") or [])][:3],
            "source": "llm",
        }

    # 规则兜底：素材预处理已有字段直接拼
    shot = _matched_shot(scene, material) if material else None
    summary = (
        (getattr(shot, "caption", None) if shot else None)
        or (material.highlight_reason if material else None)
        or scene.shot_subject
        or (section.theme if section else "")
        or "暂无理解数据"
    )
    tags = list((material.tags if material else [])[:6]) or ([scene.shot_subject] if scene.shot_subject else [])
    highlights = [material.highlight_reason] if material and material.highlight_reason else []
    return {
        "summary": str(summary)[:80],
        "tags": [str(t)[:12] for t in tags][:6],
        "highlights": [str(h)[:40] for h in highlights][:3],
        "source": "rule",
    }


async def suggest_trim(
    plan: Plan, scene: Scene, section: Optional[AdaptedSection], material: Material
) -> dict:
    """AI 裁剪 → {suggested_in, suggested_out, reason, source}（素材内秒数）。

    建议窗口约束：落在当前 MaterialShot 窗口内、时长 ≥0.5s；越界一律 clamp。
    """
    shot = _matched_shot(scene, material)
    win_start = float(shot.start) if shot else 0.0
    win_end = float(shot.end) if shot else float(material.duration_seconds or 0.0)
    if win_end <= win_start:
        win_end = win_start + scene.duration

    mat_dur = float(material.duration_seconds or 0.0)
    cur_out = scene.out_point if scene.out_point is not None else (mat_dur or win_end)
    ctx_lines = _context_lines(plan, scene, section, material) + [
        f"【当前取用】in={scene.in_point:.2f}s out={cur_out:.2f}s（{scene.duration:.1f}s）",
        f"【可选窗口】{win_start:.2f}s - {win_end:.2f}s（镜头全长 {win_end - win_start:.1f}s）",
    ]
    system = (
        "你是短视频剪辑顾问。用户已在一条素材上取了一段（in/out），"
        "请判断更优的入出点：让画面起止更干净（动作完整 / 情绪到位 / 避开废帧头尾）。\n"
        "输出 JSON：{\"in\": 秒数, \"out\": 秒数, \"reason\": \"≤50字理由\"}\n"
        f"硬约束：in/out 都必须在 {win_start:.2f}-{win_end:.2f}s 内；out-in ≥ 0.5s；"
        "时长与当前值相差不要超过一倍；如果当前取用已经合理，就原样返回并说明。"
    )
    try:
        llm = get_llm_client()
        text = await llm.complete(system, "\n".join(ctx_lines))
        data = _extract_json(text) if text else None
    except Exception as exc:  # noqa: BLE001
        log.warning("[scene_ai] trim LLM 失败 scene=%s: %s", scene.scene_id, exc)
        data = None

    def _clamp(in_pt: float, out_pt: float) -> tuple[float, float] | None:
        in_pt = max(win_start, min(in_pt, win_end))
        out_pt = max(win_start, min(out_pt, win_end))
        if out_pt - in_pt < 0.5:
            return None
        return round(in_pt, 3), round(out_pt, 3)

    if isinstance(data, dict):
        try:
            clamped = _clamp(float(data["in"]), float(data["out"]))
        except (KeyError, TypeError, ValueError):
            clamped = None
        if clamped is not None:
            return {
                "suggested_in": clamped[0],
                "suggested_out": clamped[1],
                "reason": str(data.get("reason") or "")[:60] or "AI 建议的更优入出点",
                "source": "llm",
            }

    # 规则兜底：当前时长在窗口正中取一段（当前已覆盖窗口大半则保持不动）
    span = min(scene.duration, win_end - win_start)
    if scene.in_point <= win_start + 0.01 and cur_out >= win_end - 0.01 and span >= scene.duration - 0.01:
        sug_in, sug_out = scene.in_point, cur_out
        reason = "已覆盖整个镜头窗口，无需调整"
    else:
        mid = (win_start + win_end) / 2
        sug_in = max(win_start, mid - span / 2)
        sug_out = min(win_end, sug_in + span)
        sug_in = max(win_start, sug_out - span)
        reason = f"规则建议：取镜头窗口中段 {span:.1f}s（AI 不可用时的兜底）"
    return {
        "suggested_in": round(sug_in, 3),
        "suggested_out": round(sug_out, 3),
        "reason": reason,
        "source": "rule",
    }


async def shot_brief(
    plan: Plan, scene: Scene, section: Optional[AdaptedSection]
) -> dict:
    """补拍清单 → {what_to_shoot, duration_seconds, emotion, reference, tips, source}。

    AI 出拍摄规格而非替拍（PRD F4/U4）：给「自己拍质量最高」的用户一份可执行的 brief。
    """
    shot_plan = _resolve_shot_plan(section, scene)
    subject = (shot_plan.subject if shot_plan else "") or scene.shot_subject or ""
    visual = (shot_plan.visual if shot_plan else "") or (scene.narration or "")
    ctx = "\n".join(
        [
            f"【空槽位置】scene_id={scene.scene_id} 目标时长={scene.duration:.1f}s",
            f"【画面主体】{subject or '—'}",
            f"【画面要求】{visual or '—'}",
            (
                f"【所属段】{section.section_id}（{section.role} · {section.theme or '—'}）"
                f" 内容={section.content_description}"
                if section
                else ""
            ),
            f"【全片主题】{plan.brief or '—'}",
            f"【视频要求】{plan.video_goal or '—'}",
        ]
    )
    system = (
        "你是短视频拍摄指导。画布上有一个待补的空槽，用户决定自己补拍——"
        "请给一份可当场执行的拍摄规格（shot brief），而不是替用户生成画面。\n"
        "输出 JSON：{\"what_to_shoot\": \"拍什么：主体+动作+构图，≤60字\", "
        "\"duration_seconds\": 数字(2-15), \"emotion\": \"镜头情绪≤10字\", "
        "\"reference\": \"参考哪段/什么感觉，≤30字\", \"tips\": [\"2-4条实操提示，每条≤25字\"]}\n"
        "要求：具象可执行（机位 / 光线 / 动作起点终点），不写空话。"
    )
    try:
        llm = get_llm_client()
        text = await llm.complete(system, ctx)
        data = _extract_json(text) if text else None
    except Exception as exc:  # noqa: BLE001
        log.warning("[scene_ai] brief LLM 失败 scene=%s: %s", scene.scene_id, exc)
        data = None

    if isinstance(data, dict) and data.get("what_to_shoot"):
        try:
            dur = float(data.get("duration_seconds") or scene.duration)
        except (TypeError, ValueError):
            dur = scene.duration
        return {
            "what_to_shoot": str(data["what_to_shoot"])[:120],
            "duration_seconds": round(dur if dur > 0 else scene.duration, 2),
            "emotion": str(data.get("emotion") or "")[:12],
            "reference": str(data.get("reference") or "")[:40],
            "tips": [str(t)[:30] for t in (data.get("tips") or [])][:4],
            "source": "llm",
        }

    # 规则兜底：ShotPlan 的 subject/visual 本来就是「该拍什么」的描述
    role = section.role if section else (scene.section or "development")
    tips_by_role = {
        "opening": ["前 0.5s 主体就入画", "光线充足，背景干净"],
        "development": ["一个镜头只讲一件事", "手持的话贴紧身体减抖"],
        "climax": ["动作放大幅度拉满", "可以拍两遍挑更有劲的一条"],
        "closing": ["收尾留 1s 静帧好接字幕", "情绪放松，像跟朋友说话"],
    }
    return {
        "what_to_shoot": (visual or subject or "按段落内容补拍一段画面")[:120],
        "duration_seconds": round(scene.duration if scene.duration > 0 else 4.0, 2),
        "emotion": _ROLE_EMOTION.get(role, "自然"),
        "reference": (section.theme if section else "") or "同段落其它镜头的调性",
        "tips": tips_by_role.get(role, ["横平竖直，主体居中"]),
        "source": "rule",
    }
