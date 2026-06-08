"""Emotion Agent —— LLM 多信号情绪曲线打分（stage-28）。

为什么独立：
- decompose_agent 原 `_build_mood_curve` 只看 section.role，单信号，不能综合 BGM 鼓点 / 镜头节奏 /
  口播紧张度 / 用户意图等多维度。
- Plan 阶段又有自己的输入（用户 brief、video_goal、migration_preference、改编后的段落）。
- 把"情绪打分"独立出来，拆解阶段和 Plan 阶段都能复用同一份 score_emotion，输入不同结果不同。

设计要点：
- LLM 只输出 anchors（每段一条强度）+ peaks/valleys（≤2+2 个时刻）；规则层做线性插值 + 凸包 + 平滑。
  比让 LLM 直接输出 60 点曲线 token 省、确定性强、不抖动。
- LLM 失败回落 `_build_mood_curve` 规则版（只看 role + 平滑），backend="rule_fallback"。
- migration_preference="amp_emotion" 时 prompt 里写"anchor 平均抬高 15-25%、peaks 抬高 20-30%"。

主入口：`score_emotion(...) -> EmotionCurve`。
"""
from __future__ import annotations

import json
import logging
import math
import time
from dataclasses import dataclass
from typing import Any, Optional, Sequence

from ..llm_client import LLMError, get_llm_client
from ...schemas import (
    AdaptedSection,
    BGMAnalysis,
    EmotionAnchor,
    EmotionCurve,
    EmotionPeak,
    EmotionPoint,
    SampleAnalysis,
    Scene,
    Section,
    Shot,
    VideoUnderstanding,
)

log = logging.getLogger("seecript.agent.emotion")


# --------------------------------------------------------------------------
# Public dataclass
# --------------------------------------------------------------------------


@dataclass
class PlanIntent:
    """Plan 阶段才有的用户意图——传给 LLM 让它倾斜情绪基调。

    拆解阶段无 intent（intent=None），LLM 只看样例本身。
    """

    brief: Optional[str] = None
    video_goal: Optional[str] = None
    migration_preference: str = "mirror"  # mirror / amp_emotion / amp_pace


# --------------------------------------------------------------------------
# 段落/分镜 duck typing helpers
# --------------------------------------------------------------------------


def _section_bounds(section: Any) -> tuple[float, float]:
    """支持 Section（含 start/end）和 AdaptedSection（无 start/end，按 order × duration 推算）。

    AdaptedSection 不带绝对时间——上层调用 score_emotion 时若传 AdaptedSection，
    需先用 `_assign_adapted_section_times` 把绝对时间补齐再喂进来。
    """
    start = float(getattr(section, "start", 0.0) or 0.0)
    end = float(getattr(section, "end", 0.0) or 0.0)
    if end <= start:
        # AdaptedSection fallback：duration_seconds × order
        dur = float(getattr(section, "duration_seconds", 0.0) or 0.0)
        if dur > 0:
            end = start + dur
    return start, end


def _section_role(section: Any) -> str:
    return str(getattr(section, "role", "development") or "development")


def _section_theme(section: Any) -> str:
    return str(getattr(section, "theme", "") or "")


def _section_summary(section: Any) -> str:
    """Section.summary / AdaptedSection.content_description 都映射到 summary。"""
    s = getattr(section, "summary", None)
    if s:
        return str(s)
    cd = getattr(section, "content_description", None)
    if cd:
        return str(cd)
    return ""


def _shot_in_section(shot: Any, start: float, end: float) -> bool:
    sh_start = float(getattr(shot, "start", 0.0) or 0.0)
    sh_end = float(getattr(shot, "end", 0.0) or 0.0) or sh_start + float(getattr(shot, "duration", 0.0) or 0.0)
    # 与段落有重叠即认为属于该段
    return sh_start < end and sh_end > start


def _shot_script(shot: Any) -> str:
    s = getattr(shot, "script", None) or getattr(shot, "narration", None) or ""
    return str(s)


def _shot_subject(shot: Any) -> str:
    return str(getattr(shot, "shot_subject", None) or getattr(shot, "subject", "") or "")


def _shot_tags(shot: Any) -> list[str]:
    tags = getattr(shot, "tags", None) or []
    return [str(t) for t in tags[:4]]


def _shot_duration(shot: Any) -> float:
    d = getattr(shot, "duration", None)
    if d is not None:
        return float(d)
    s = float(getattr(shot, "start", 0.0) or 0.0)
    e = float(getattr(shot, "end", 0.0) or 0.0)
    return max(0.0, e - s)


# --------------------------------------------------------------------------
# bgm_energy 形态摘要
# --------------------------------------------------------------------------


def _summarize_bgm_energy(bgm_energy: Optional[Sequence[float]], times: Optional[Sequence[float]] = None) -> Optional[dict[str, float]]:
    if not bgm_energy:
        return None
    vals = [float(v) for v in bgm_energy if v is not None]
    if not vals:
        return None
    n = len(vals)
    mean = sum(vals) / n
    mx = max(vals)
    var = sum((v - mean) ** 2 for v in vals) / n
    std = math.sqrt(var)
    peak_idx = vals.index(mx)
    if times and len(times) == n:
        peak_t = float(times[peak_idx])
    else:
        # 没传 times 时按 idx/n 比例 × 假设全片 1.0 给出比例位置（不准也无妨,prompt 用百分比）
        peak_t = peak_idx / max(1, n - 1)
    return {"mean": round(mean, 3), "max": round(mx, 3), "std": round(std, 3), "peak_t": round(peak_t, 2)}


# --------------------------------------------------------------------------
# Prompt builders
# --------------------------------------------------------------------------


def _build_system_prompt() -> str:
    return (
        "你是视频情绪曲线打分器。给你一支视频的段落结构、镜头列表、BGM 画像、整片画像和（可选）用户意图，"
        "你要输出每段情绪强度（0..1）+ 全片高潮/低谷时刻 + 一句话总结。\n\n"
        "打分原则：\n"
        "1. 整片基调先看 understanding.tone 和 BGM.mood_tags；migration_preference=amp_emotion 时整体抬高 15-25%、peaks 抬高 20-30%。\n"
        "2. 段落 anchor：role=climax/peak 段普遍 0.7-0.9；opening/closing/establish/resolve 普遍 0.25-0.45；development/flow/info_block 普遍 0.4-0.6。\n"
        "   有 BGM.climax 命中本段、或 sample_highlight 命中本段 → 在基线上 +0.1~0.2。\n"
        "3. peaks/valleys 时刻：从 BGM.climaxes 的 at_seconds 与 sample_highlight.shot_indices 对应时间窗的交集挑；最多各 2 个，必须落在 [0, total_duration] 内。\n"
        "4. reason 一句话不超过 30 字，要具体（提某一个 climax / 某段 highlight / 某段 role），不要空泛。\n"
        "5. summary 一段话讲清整片情绪走势：哪段平稳、哪段拐点、收尾怎么落。≤80 字。\n\n"
        "输出严格 JSON，无 markdown 代码块、无前后说明文字：\n"
        "{\n"
        '  "anchors": [{"section_idx": 0, "intensity": 0.32, "reason": "..."}],\n'
        '  "peaks": [{"t": 18.2, "intensity": 0.92, "reason": "..."}],\n'
        '  "valleys": [{"t": 42.0, "intensity": 0.20, "reason": "..."}],\n'
        '  "summary": "..."\n'
        "}\n"
        "约束：anchors 必须每段一条且按 section_idx 顺序；peaks ≤2、valleys ≤2；intensity ∈ [0.05, 0.98]；reason ≤30 字。"
    )


def _build_user_prompt(
    *,
    sections: Sequence[Any],
    shots: Sequence[Any],
    total_duration: float,
    bgm_analysis: Optional[BGMAnalysis] = None,
    bgm_energy_summary: Optional[dict[str, float]] = None,
    understanding: Optional[VideoUnderstanding] = None,
    sample_analysis: Optional[SampleAnalysis] = None,
    intent: Optional[PlanIntent] = None,
) -> str:
    lines: list[str] = []
    lines.append(f"# 视频总时长：{total_duration:.1f}s\n")

    # 整片画像
    if understanding is not None:
        lines.append("## 整片画像")
        lines.append(f"- 原型：{understanding.archetype}")
        lines.append(f"- 结构模式：{understanding.structural_pattern}")
        if understanding.tone:
            lines.append(f"- 基调：{understanding.tone}")
        lines.append(f"- 叙事概要：{understanding.narrative_summary}")
        lines.append("")

    # 段落结构
    lines.append("## 段落结构（按 section_idx 顺序）")
    for idx, sec in enumerate(sections):
        s, e = _section_bounds(sec)
        dur = max(0.0, e - s)
        ratio = dur / total_duration if total_duration > 0 else 0.0
        sec_shots = [sh for sh in shots if _shot_in_section(sh, s, e)]
        avg_dur = (sum(_shot_duration(sh) for sh in sec_shots) / len(sec_shots)) if sec_shots else 0.0
        lines.append(
            f"### sec[{idx}] role={_section_role(sec)} theme={_section_theme(sec) or '-'} "
            f"[{s:.1f}-{e:.1f}s] {dur:.1f}s ({ratio*100:.0f}%) "
            f"shots={len(sec_shots)} avg_shot={avg_dur:.1f}s"
        )
        if _section_summary(sec):
            lines.append(f"  summary: {_section_summary(sec)}")
        # 抽 2-3 个代表镜头：首/中/末
        if sec_shots:
            picks: list[Any] = []
            picks.append(sec_shots[0])
            if len(sec_shots) >= 3:
                picks.append(sec_shots[len(sec_shots) // 2])
            if len(sec_shots) >= 2:
                picks.append(sec_shots[-1])
            seen_ids: set[int] = set()
            for sh in picks:
                sid = id(sh)
                if sid in seen_ids:
                    continue
                seen_ids.add(sid)
                script = _shot_script(sh).replace("\n", " ").strip()
                if len(script) > 80:
                    script = script[:80] + "…"
                tags = ",".join(_shot_tags(sh)) or "-"
                subj = _shot_subject(sh) or "-"
                lines.append(
                    f"  - shot dur={_shot_duration(sh):.1f}s subject={subj} tags=[{tags}] script={script or '-'}"
                )
        lines.append("")

    # BGM 画像
    if bgm_analysis is not None:
        lines.append("## BGM 画像")
        lines.append(f"- 曲风：{bgm_analysis.title_guess} 情绪={','.join(bgm_analysis.mood_tags)}")
        lines.append(f"- 能量形态：{bgm_analysis.energy_shape}（{bgm_analysis.energy_shape_reason}）")
        if bgm_analysis.climaxes:
            cm = "; ".join(f"{c.at_seconds:.1f}s {c.kind} {c.label}（{c.fit_with_video}）" for c in bgm_analysis.climaxes)
            lines.append(f"- 高潮节点：{cm}")
        if bgm_analysis.calm_segments:
            cs = "; ".join(f"{c.start:.1f}-{c.end:.1f}s {c.note}" for c in bgm_analysis.calm_segments)
            lines.append(f"- 平稳段：{cs}")
        lines.append("")

    if bgm_energy_summary is not None:
        lines.append(
            f"## BGM 能量摘要：mean={bgm_energy_summary['mean']:.2f} "
            f"max={bgm_energy_summary['max']:.2f} std={bgm_energy_summary['std']:.2f} "
            f"peak_t={bgm_energy_summary['peak_t']:.2f}s\n"
        )

    # SampleAnalysis
    if sample_analysis is not None:
        lines.append("## 全片亮点 / 改进")
        for h in sample_analysis.highlights[:6]:
            shots_str = f" shots={h.shot_indices}" if h.shot_indices else ""
            lines.append(f"- ✨ [{h.aspect}] {h.text}{shots_str}")
        for imp in sample_analysis.improvements[:6]:
            shots_str = f" shots={imp.shot_indices}" if imp.shot_indices else ""
            lines.append(f"- ⚠️ [{imp.aspect}] {imp.text} → {imp.suggestion}{shots_str}")
        lines.append("")

    # 用户意图
    if intent is not None:
        lines.append("## 用户意图（Plan 阶段）")
        if intent.brief:
            lines.append(f"- brief：{intent.brief}")
        if intent.video_goal:
            lines.append(f"- video_goal：{intent.video_goal}")
        lines.append(f"- migration_preference：{intent.migration_preference}")
        if intent.migration_preference == "amp_emotion":
            lines.append("  → 情绪放大模式：anchors 平均抬高 15-25%，peaks 抬高 20-30%。")
        elif intent.migration_preference == "amp_pace":
            lines.append("  → 节奏加快模式：anchors 整体小幅抬升 5-10%，但保持原峰谷形状。")
        lines.append("")

    return "\n".join(lines)


# --------------------------------------------------------------------------
# LLM 输出解析 + clamp
# --------------------------------------------------------------------------


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _parse_anchors(raw: Any, n_sections: int) -> list[EmotionAnchor]:
    if not isinstance(raw, list):
        return []
    out: list[EmotionAnchor] = []
    seen_idx: set[int] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            idx = int(item.get("section_idx", -1))
            intensity = float(item.get("intensity", 0.4))
        except (TypeError, ValueError):
            continue
        if idx < 0 or idx >= n_sections or idx in seen_idx:
            continue
        seen_idx.add(idx)
        intensity = _clamp(intensity, 0.05, 0.98)
        reason = str(item.get("reason", ""))[:80]
        out.append(EmotionAnchor(section_idx=idx, intensity=intensity, reason=reason))
    out.sort(key=lambda a: a.section_idx)
    return out


def _parse_peaks(raw: Any, total_duration: float, max_count: int = 2) -> list[EmotionPeak]:
    if not isinstance(raw, list):
        return []
    out: list[EmotionPeak] = []
    for item in raw[:max_count]:
        if not isinstance(item, dict):
            continue
        try:
            t = float(item.get("t", -1))
            intensity = float(item.get("intensity", 0.5))
        except (TypeError, ValueError):
            continue
        if t < 0 or t > total_duration:
            # clamp 进 [0, total]，超界丢弃过远的
            if total_duration <= 0:
                continue
            if t < 0 or t > total_duration * 1.05:
                continue
            t = _clamp(t, 0.0, total_duration)
        intensity = _clamp(intensity, 0.05, 0.98)
        reason = str(item.get("reason", ""))[:80]
        out.append(EmotionPeak(t=t, intensity=intensity, reason=reason))
    return out


# --------------------------------------------------------------------------
# 规则插值
# --------------------------------------------------------------------------


def _section_role_base(role: str) -> float:
    """复用 decompose_agent._role_mood_value 的基线（不直接 import 避免循环：在 fallback 里用）。"""
    base = {
        "opening": 0.35, "development": 0.40, "climax": 0.85, "closing": 0.30,
        "intro": 0.35, "recap": 0.30,
        "hook": 0.40, "closer": 0.30,
        "establish": 0.35, "flow": 0.40, "peak": 0.80, "resolve": 0.30,
        "title_card": 0.40, "info_block": 0.40, "payoff": 0.50,
        "intro_scene": 0.35, "wrap_up": 0.30,
    }.get(role)
    if base is not None:
        return base
    if role.startswith("step_"):
        return 0.40
    if role.startswith("item_"):
        return 0.42
    if role.startswith("daily_"):
        return 0.45
    return 0.40


def _smooth_window(values: list[float], window: int) -> list[float]:
    if window < 2 or len(values) < 2:
        return list(values)
    n = len(values)
    out: list[float] = []
    half = window // 2
    for i in range(n):
        lo = max(0, i - half)
        hi = min(n, i + half + 1)
        seg = values[lo:hi]
        out.append(sum(seg) / len(seg))
    return out


def _interpolate_curve(
    *,
    sections: Sequence[Any],
    anchors: list[EmotionAnchor],
    peaks: list[EmotionPeak],
    valleys: list[EmotionPeak],
    total_duration: float,
    n_points: int = 60,
) -> list[EmotionPoint]:
    """规则插值：anchor 段中点 → 线性 → peaks/valleys 凸包加 bump → 滑动平均。"""
    if total_duration <= 0 or n_points <= 0:
        return []

    # 1) 段中点 → intensity 表
    mid_intensity: dict[float, float] = {}
    by_idx = {a.section_idx: a for a in anchors}
    for idx, sec in enumerate(sections):
        s, e = _section_bounds(sec)
        if e <= s:
            continue
        mid = (s + e) / 2.0
        a = by_idx.get(idx)
        if a is not None:
            mid_intensity[mid] = float(a.intensity)
        else:
            # 缺 anchor 走 role base 兜底
            mid_intensity[mid] = _section_role_base(_section_role(sec))

    if not mid_intensity:
        # 没段落，整体给 0.4
        step = total_duration / max(1, n_points - 1)
        return [EmotionPoint(t=round(i * step, 3), intensity=0.4) for i in range(n_points)]

    sorted_mids = sorted(mid_intensity.items(), key=lambda x: x[0])

    def _intensity_at(t: float) -> float:
        if t <= sorted_mids[0][0]:
            return sorted_mids[0][1]
        if t >= sorted_mids[-1][0]:
            return sorted_mids[-1][1]
        for i in range(len(sorted_mids) - 1):
            t0, v0 = sorted_mids[i]
            t1, v1 = sorted_mids[i + 1]
            if t0 <= t <= t1:
                if t1 == t0:
                    return v0
                ratio = (t - t0) / (t1 - t0)
                return v0 + (v1 - v0) * ratio
        return sorted_mids[-1][1]

    # 2) 60 点等距采样
    step = total_duration / max(1, n_points - 1)
    times = [round(i * step, 3) for i in range(n_points)]
    values = [_intensity_at(t) for t in times]

    # 3) peaks / valleys 凸包加 bump（半窗 ±2.0s）
    half_win = 2.0

    def _apply_bump(t_target: float, intensity_target: float, is_peak: bool) -> None:
        for i, t in enumerate(times):
            dt = abs(t - t_target)
            if dt > half_win:
                continue
            falloff = 1.0 - dt / half_win  # 1.0 at center → 0.0 at edge
            if is_peak:
                if intensity_target > values[i]:
                    values[i] = values[i] + (intensity_target - values[i]) * falloff
            else:
                if intensity_target < values[i]:
                    values[i] = values[i] + (intensity_target - values[i]) * falloff

    for p in peaks:
        _apply_bump(p.t, p.intensity, is_peak=True)
    for v in valleys:
        _apply_bump(v.t, v.intensity, is_peak=False)

    # 4) 滑动平均：window = max(3, n//12)
    window = max(3, n_points // 12)
    values = _smooth_window(values, window)

    # 5) clamp + 组装
    return [EmotionPoint(t=t, intensity=round(_clamp(v, 0.0, 1.0), 4)) for t, v in zip(times, values)]


# --------------------------------------------------------------------------
# 规则 fallback（LLM 挂时用）
# --------------------------------------------------------------------------


def _rule_fallback_curve(
    sections: Sequence[Any],
    total_duration: float,
    n_points: int = 60,
) -> EmotionCurve:
    """LLM 挂时回落：每段中点取 role_base + 平滑。signals_used=['role']。"""
    fake_anchors: list[EmotionAnchor] = []
    for idx, sec in enumerate(sections):
        s, e = _section_bounds(sec)
        if e <= s:
            continue
        fake_anchors.append(
            EmotionAnchor(
                section_idx=idx,
                intensity=_section_role_base(_section_role(sec)),
                reason="LLM 不可达，按段落角色基线生成",
            )
        )
    points = _interpolate_curve(
        sections=sections, anchors=fake_anchors, peaks=[], valleys=[],
        total_duration=total_duration, n_points=n_points,
    )
    return EmotionCurve(
        points=points,
        anchors=fake_anchors,
        peaks=[], valleys=[],
        summary="LLM 不可达，按段落角色基线生成",
        backend="rule_fallback",
        signals_used=["role"],
        computed_at=time.time(),
    )


# --------------------------------------------------------------------------
# Main entry
# --------------------------------------------------------------------------


async def score_emotion(
    *,
    sections: Sequence[Any],
    shots: Sequence[Any],
    total_duration: float,
    bgm_analysis: Optional[BGMAnalysis] = None,
    bgm_energy: Optional[Sequence[float]] = None,
    bgm_times: Optional[Sequence[float]] = None,
    understanding: Optional[VideoUnderstanding] = None,
    sample_analysis: Optional[SampleAnalysis] = None,
    intent: Optional[PlanIntent] = None,
    n_points: int = 60,
    timeout_s: float = 8.0,
) -> EmotionCurve:
    """打分入口。LLM 失败回落 _rule_fallback_curve。

    timeout_s 控制 LLM 单次调用上限，超时直接 fallback。
    """
    if total_duration <= 0 or not sections:
        return EmotionCurve(
            points=[], anchors=[], peaks=[], valleys=[],
            summary="无段落或时长无效，跳过情绪曲线",
            backend="rule_fallback",
            signals_used=[],
            computed_at=time.time(),
        )

    energy_sum = _summarize_bgm_energy(bgm_energy, bgm_times)

    signals_used: list[str] = ["role"]
    if shots:
        signals_used.append("script")
        signals_used.append("cut")
    if bgm_analysis is not None:
        signals_used.append("bgm")
        if bgm_analysis.climaxes:
            signals_used.append("climax")
    if energy_sum is not None:
        signals_used.append("bgm_energy")
    if understanding is not None:
        signals_used.append("tone")
    if sample_analysis is not None and sample_analysis.highlights:
        signals_used.append("highlight")
    if intent is not None:
        signals_used.append("intent")

    system = _build_system_prompt()
    user = _build_user_prompt(
        sections=sections, shots=shots, total_duration=total_duration,
        bgm_analysis=bgm_analysis, bgm_energy_summary=energy_sum,
        understanding=understanding, sample_analysis=sample_analysis, intent=intent,
    )

    import asyncio

    try:
        llm = get_llm_client()
        data = await asyncio.wait_for(
            llm.complete_json(system, user, temperature=0.4),
            timeout=timeout_s,
        )
    except (LLMError, asyncio.TimeoutError) as exc:
        log.warning("[emotion] LLM failed (%s); using rule fallback", exc)
        return _rule_fallback_curve(sections, total_duration, n_points=n_points)
    except Exception as exc:  # noqa: BLE001
        log.warning("[emotion] LLM unexpected (%s); using rule fallback", exc)
        return _rule_fallback_curve(sections, total_duration, n_points=n_points)

    if not isinstance(data, dict):
        log.warning("[emotion] LLM returned non-dict %r; using rule fallback", type(data).__name__)
        return _rule_fallback_curve(sections, total_duration, n_points=n_points)

    n_sections = len(sections)
    anchors = _parse_anchors(data.get("anchors"), n_sections)
    if len(anchors) < n_sections:
        # 缺的段补 role_base 兜底，但保留 LLM 给到的部分
        existing_idx = {a.section_idx for a in anchors}
        for idx, sec in enumerate(sections):
            if idx not in existing_idx:
                anchors.append(
                    EmotionAnchor(
                        section_idx=idx,
                        intensity=_section_role_base(_section_role(sec)),
                        reason="LLM 漏给该段，按角色基线兜底",
                    )
                )
        anchors.sort(key=lambda a: a.section_idx)

    peaks = _parse_peaks(data.get("peaks"), total_duration, max_count=2)
    valleys = _parse_peaks(data.get("valleys"), total_duration, max_count=2)
    summary = str(data.get("summary", ""))[:200]

    points = _interpolate_curve(
        sections=sections, anchors=anchors, peaks=peaks, valleys=valleys,
        total_duration=total_duration, n_points=n_points,
    )

    return EmotionCurve(
        points=points,
        anchors=anchors,
        peaks=peaks,
        valleys=valleys,
        summary=summary or "LLM 已生成情绪曲线",
        backend="llm",
        signals_used=signals_used,
        computed_at=time.time(),
    )
