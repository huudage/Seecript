"""emotion_agent 直测：mock LLM happy path / fallback / 缺信号。

校验：
1. mock LLM 返回合法 anchors+peaks+valleys → score_emotion 出
   60 个 points + backend="llm" + 包含相应 signals_used。
2. LLM 抛 LLMError → 走 _rule_fallback_curve，backend="rule_fallback"，points 仍 60 个。
3. shots/bgm/understanding 全空 → 仍能走 fallback 出曲线。
4. peak.t 超时长 → 入口 clamp 丢弃。
"""
from __future__ import annotations

import asyncio

from app.schemas import (
    BGMAnalysis,
    BGMHighlight,
    EmotionCurve,
    HighlightItem,
    SampleAnalysis,
    Section,
    Shot,
    VideoUnderstanding,
)
from app.services import llm_client as llm_client_module
from app.services.agent import emotion_agent
from app.services.agent.emotion_agent import PlanIntent, score_emotion


def _mini_sections() -> list[Section]:
    return [
        Section(role="opening",     theme="冷开场", start=0.0,  end=10.0, summary="信息密度低，打底"),
        Section(role="development", theme="积累",   start=10.0, end=22.0, summary="逐步推进，节奏起"),
        Section(role="climax",      theme="高潮",   start=22.0, end=28.0, summary="炸点"),
        Section(role="closing",     theme="收束",   start=28.0, end=32.0, summary="落幕"),
    ]


def _mini_shots() -> list[Shot]:
    return [
        Shot(index=0, start=0.0,  end=5.0,  duration=5.0, script="开场白",      tags=["特写"], subject="主角"),
        Shot(index=1, start=15.0, end=19.0, duration=4.0, script="转折点",      tags=["跟拍"], subject="场景"),
        Shot(index=2, start=24.0, end=27.0, duration=3.0, script="高潮台词！", tags=["快切"], subject="主角"),
        Shot(index=3, start=29.0, end=32.0, duration=3.0, script="收尾",        tags=["远景"], subject="环境"),
    ]


def _mini_bgm() -> BGMAnalysis:
    return BGMAnalysis(
        title_guess="励志钢琴",
        mood_tags=["昂扬", "鼓舞"],
        energy_shape="build_up",
        energy_shape_reason="鼓点 18s 后明显加重，副歌入",
        theme_fit_score=0.82,
        theme_fit_reason="情绪向 brief 与本曲走向贴合",
        climaxes=[
            BGMHighlight(at_seconds=24.0, kind="climax", label="副歌入", fit_with_video="对齐主角主台词"),
        ],
        calm_segments=[],
        overall_advice="开场吃留白，24s 处对齐情绪炸点",
        backend="mock",
    )


def _mini_understanding() -> VideoUnderstanding:
    return VideoUnderstanding(
        archetype="情绪短片",
        narrative_summary="平静开场，逐步累积，24s 处情绪炸裂，缓慢收束",
        structural_pattern="dramatic",
        tone="温暖→振奋→收束",
        estimated_segments=4,
    )


def _mini_sample_analysis() -> SampleAnalysis:
    return SampleAnalysis(
        highlights=[HighlightItem(aspect="rhythm", text="鼓点+句子叠加", shot_indices=[2])],
        improvements=[],
    )


class _FakeLLM:
    """直接 patch 到 emotion_agent.get_llm_client 上的桩。"""

    def __init__(self, mode: str = "json", payload: dict | None = None) -> None:
        self.mode = mode
        self.payload = payload or {
            "anchors": [
                {"section_idx": 0, "intensity": 0.30, "reason": "冷开场，BGM 未起"},
                {"section_idx": 1, "intensity": 0.55, "reason": "积累段"},
                {"section_idx": 2, "intensity": 0.92, "reason": "鼓点+主角嘶吼"},
                {"section_idx": 3, "intensity": 0.40, "reason": "收束"},
            ],
            "peaks": [{"t": 24.5, "intensity": 0.94, "reason": "鼓点炸裂"}],
            "valleys": [{"t": 4.0, "intensity": 0.18, "reason": "信息留白"}],
            "summary": "前段冷启动；24s 处情绪炸裂；尾段缓收。",
        }
        self.calls: list[tuple[str, str]] = []

    async def complete_json(self, system: str, user: str, *, temperature=None, max_tokens=None):
        self.calls.append((system, user))
        if self.mode == "raise":
            raise llm_client_module.LLMError("mock raise", code="LLM_TEST")
        if self.mode == "bad_dict":
            return ["not a dict"]
        return self.payload


def test_score_emotion_happy_path(monkeypatch):
    """mock LLM 出合法 JSON → 60 点 + backend='llm' + signals_used 涵盖各路输入。"""
    fake = _FakeLLM("json")
    monkeypatch.setattr(emotion_agent, "get_llm_client", lambda: fake)

    curve: EmotionCurve = asyncio.run(
        score_emotion(
            sections=_mini_sections(),
            shots=_mini_shots(),
            total_duration=32.0,
            bgm_analysis=_mini_bgm(),
            bgm_energy=[0.2, 0.3, 0.5, 0.8, 0.95, 0.6, 0.3],
            bgm_times=[0, 5, 10, 15, 24, 28, 31.5],
            understanding=_mini_understanding(),
            sample_analysis=_mini_sample_analysis(),
            intent=PlanIntent(brief="情绪向", video_goal="燃", migration_preference="amp_emotion"),
        )
    )

    assert curve.backend == "llm"
    assert len(curve.points) == 60
    assert len(curve.anchors) == 4
    assert len(curve.peaks) == 1 and abs(curve.peaks[0].t - 24.5) < 0.01
    assert len(curve.valleys) == 1
    assert curve.summary.startswith("前段冷启动")
    # 所有信号都喂了，signals_used 应该齐
    for sig in ("role", "script", "cut", "bgm", "climax", "bgm_energy", "tone", "highlight", "intent"):
        assert sig in curve.signals_used, f"missing signal: {sig} in {curve.signals_used}"
    # 时间序列单调递增
    ts = [p.t for p in curve.points]
    assert all(ts[i] <= ts[i + 1] for i in range(len(ts) - 1))
    # peak 邻域强度被推高（受 ±2s 凸包+平滑后仍应明显高于段落基线）
    near_peak = [p.intensity for p in curve.points if abs(p.t - 24.5) <= 1.5]
    assert near_peak and max(near_peak) >= 0.7
    assert all(0.0 <= p.intensity <= 1.0 for p in curve.points)


def test_score_emotion_fallback_when_llm_fails(monkeypatch):
    """LLM 抛错 → backend='rule_fallback'，points 仍 60 个，仍能画曲线。"""
    fake = _FakeLLM("raise")
    monkeypatch.setattr(emotion_agent, "get_llm_client", lambda: fake)

    curve = asyncio.run(
        score_emotion(
            sections=_mini_sections(),
            shots=_mini_shots(),
            total_duration=32.0,
        )
    )
    assert curve.backend == "rule_fallback"
    assert len(curve.points) == 60
    assert curve.summary
    # climax 段平均强度应明显高于 opening
    climax_pts = [p.intensity for p in curve.points if 22.0 <= p.t <= 28.0]
    opening_pts = [p.intensity for p in curve.points if p.t <= 10.0]
    assert climax_pts and opening_pts
    assert sum(climax_pts) / len(climax_pts) > sum(opening_pts) / len(opening_pts)


def test_score_emotion_fallback_when_llm_returns_non_dict(monkeypatch):
    """LLM 给非 dict → 走 fallback 路径。"""
    fake = _FakeLLM("bad_dict")
    monkeypatch.setattr(emotion_agent, "get_llm_client", lambda: fake)

    curve = asyncio.run(
        score_emotion(
            sections=_mini_sections(),
            shots=_mini_shots(),
            total_duration=32.0,
        )
    )
    assert curve.backend == "rule_fallback"
    assert len(curve.points) == 60


def test_score_emotion_minimal_inputs(monkeypatch):
    """只有 sections，没 shots / bgm / understanding → fallback 仍工作。"""
    fake = _FakeLLM("raise")
    monkeypatch.setattr(emotion_agent, "get_llm_client", lambda: fake)

    curve = asyncio.run(
        score_emotion(
            sections=_mini_sections(),
            shots=[],
            total_duration=32.0,
        )
    )
    assert curve.backend == "rule_fallback"
    assert len(curve.points) == 60
    assert isinstance(curve.signals_used, list)


def test_score_emotion_zero_duration_returns_empty():
    """total_duration <= 0 直接返回空曲线，不调 LLM。"""
    curve = asyncio.run(
        score_emotion(
            sections=_mini_sections(),
            shots=_mini_shots(),
            total_duration=0.0,
        )
    )
    assert curve.points == []
    assert curve.backend == "rule_fallback"


def test_score_emotion_clamps_out_of_range_peak(monkeypatch):
    """LLM 给的 peak.t 超出视频时长 → 应被丢弃；段落 anchor 还在。"""
    payload = {
        "anchors": [
            {"section_idx": 0, "intensity": 0.30, "reason": "x"},
            {"section_idx": 1, "intensity": 0.55, "reason": "x"},
            {"section_idx": 2, "intensity": 0.92, "reason": "x"},
            {"section_idx": 3, "intensity": 0.40, "reason": "x"},
        ],
        "peaks": [
            {"t": 999.0, "intensity": 0.99, "reason": "out of range"},
            {"t": 24.0, "intensity": 0.90, "reason": "ok"},
        ],
        "valleys": [],
        "summary": "x",
    }
    fake = _FakeLLM("json", payload=payload)
    monkeypatch.setattr(emotion_agent, "get_llm_client", lambda: fake)

    curve = asyncio.run(
        score_emotion(
            sections=_mini_sections(),
            shots=_mini_shots(),
            total_duration=32.0,
        )
    )
    assert curve.backend == "llm"
    assert len(curve.peaks) == 1 and abs(curve.peaks[0].t - 24.0) < 0.01
