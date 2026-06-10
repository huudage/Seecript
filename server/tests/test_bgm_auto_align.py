"""BGM 高潮自动切片对齐内容高潮直测——routers.plan._auto_align_bgm_to_emotion。

覆盖：
1. happy path：BGM climax @20s + 内容 peak @7s → anchor = -13.0（切掉 BGM 前 13s）
2. climax-kind 优先级：同曲多个节点 climax > drop > release > build_start > break
3. analysis 没 climaxes → 回落 peak_seconds
4. 既无 climaxes 也无 peak_seconds → 不动 anchor（保持默认 0）
5. emotion_curve.peaks 空 → 不动 anchor
6. clamp 下限：BGM 25s + 视频 60s + bgm_peak=2 + content_peak=50 → raw=48 但 upper=59 → 取 48
7. clamp 上限：BGM 25s + 视频 5s + bgm_peak=20 + content_peak=3 → raw=-17 但 lower=-24 → 取 -17，再被 upper=4 限制 → -17 仍在 [-24, 4] 内
8. 没 BGM（track_url 为 None）→ 不动
"""
from __future__ import annotations

from app.services.plans.bgm_align import auto_align_bgm_to_emotion as _auto_align_bgm_to_emotion
from app.schemas import (
    BGMAnalysis,
    BGMConfig,
    BGMHighlight,
    EmotionCurve,
    EmotionPeak,
    Plan,
    ReferenceVersion,
)


def _make_plan(
    *,
    bgm: BGMConfig | None,
    emotion_curve: EmotionCurve | None,
    duration: float = 30.0,
) -> Plan:
    """构造最小可用 Plan——只填本测试用到的字段，其余取 schema 默认。"""
    return Plan(
        plan_id="plan-test-bgm-align",
        brief="测试 brief",
        video_goal="测试目标",
        reference_versions=[ReferenceVersion(sample_id="sample-x", slot_id="slot-x")],
        adapted_sections=[],
        variant="A",
        duration_seconds=duration,
        main_track=[],
        packaging_track=[],
        bgm=bgm or BGMConfig(),
        emotion_curve=emotion_curve,
    )


def _bgm_with_climaxes(
    climaxes: list[BGMHighlight],
    *,
    duration_seconds: float = 60.0,
    peak_seconds: float | None = None,
) -> BGMConfig:
    return BGMConfig(
        bgm_asset_id="bgm-test",
        track_url="/assets/proj-x/bgm/test.mp3",
        duration_seconds=duration_seconds,
        peak_seconds=peak_seconds,
        analysis=BGMAnalysis(
            title_guess="钢琴抒情",
            mood_tags=["平静"],
            energy_shape="build_up",
            energy_shape_reason="慢起后段拉高",
            theme_fit_score=0.7,
            theme_fit_reason="契合",
            climaxes=climaxes,
            calm_segments=[],
            overall_advice="后段对齐高潮",
            backend="mock",
        ) if climaxes else None,
    )


def _emotion_with_peaks(peaks: list[EmotionPeak]) -> EmotionCurve:
    return EmotionCurve(points=[], anchors=[], peaks=peaks, valleys=[], backend="llm")


def test_align_happy_path():
    """BGM climax @20s + 内容 peak @7s → anchor = -13.0 切掉 BGM 前 13s。"""
    bgm = _bgm_with_climaxes(
        [BGMHighlight(at_seconds=20.0, kind="climax", label="副歌入", fit_with_video="对齐卖点")],
    )
    emo = _emotion_with_peaks(
        [EmotionPeak(t=7.0, intensity=0.92, reason="BGM 鼓点 + 关键句叠加")],
    )
    plan = _make_plan(bgm=bgm, emotion_curve=emo, duration=30.0)
    _auto_align_bgm_to_emotion(plan)
    assert plan.bgm.video_anchor_seconds == -13.0


def test_climax_kind_priority():
    """同曲多节点：climax 优先级高于 drop / release / build_start / break，同优先级取最早。"""
    bgm = _bgm_with_climaxes([
        BGMHighlight(at_seconds=10.0, kind="build_start", label="蓄势", fit_with_video="x"),
        BGMHighlight(at_seconds=5.0, kind="break", label="留白", fit_with_video="x"),
        BGMHighlight(at_seconds=25.0, kind="climax", label="副歌", fit_with_video="x"),
        BGMHighlight(at_seconds=18.0, kind="drop", label="骤降", fit_with_video="x"),
    ])
    emo = _emotion_with_peaks([EmotionPeak(t=15.0, intensity=0.9)])
    plan = _make_plan(bgm=bgm, emotion_curve=emo, duration=30.0)
    _auto_align_bgm_to_emotion(plan)
    # bgm_peak = 25s（climax 优先），video_peak = 15s → anchor = -10.0
    assert plan.bgm.video_anchor_seconds == -10.0


def test_fallback_to_peak_seconds_when_no_climaxes():
    """analysis 没 climaxes → 回落 librosa peak_seconds 做对齐。"""
    bgm = BGMConfig(
        bgm_asset_id="bgm-test",
        track_url="/assets/proj-x/bgm/test.mp3",
        duration_seconds=60.0,
        peak_seconds=15.0,
        analysis=None,
    )
    emo = _emotion_with_peaks([EmotionPeak(t=5.0, intensity=0.88)])
    plan = _make_plan(bgm=bgm, emotion_curve=emo, duration=20.0)
    _auto_align_bgm_to_emotion(plan)
    # bgm_peak=15, content_peak=5 → anchor=-10.0
    assert plan.bgm.video_anchor_seconds == -10.0


def test_no_signal_keeps_default():
    """既无 climaxes 也无 peak_seconds → anchor 保持默认 0.0。"""
    bgm = BGMConfig(
        bgm_asset_id="bgm-test",
        track_url="/assets/proj-x/bgm/test.mp3",
        duration_seconds=30.0,
        peak_seconds=None,
        analysis=None,
    )
    emo = _emotion_with_peaks([EmotionPeak(t=5.0, intensity=0.9)])
    plan = _make_plan(bgm=bgm, emotion_curve=emo, duration=20.0)
    _auto_align_bgm_to_emotion(plan)
    assert plan.bgm.video_anchor_seconds == 0.0


def test_no_content_peak_keeps_default():
    """emotion_curve.peaks 为空 → anchor 保持默认 0.0。"""
    bgm = _bgm_with_climaxes(
        [BGMHighlight(at_seconds=20.0, kind="climax", label="x", fit_with_video="x")],
    )
    emo = _emotion_with_peaks([])  # 平稳曲线没标 peaks
    plan = _make_plan(bgm=bgm, emotion_curve=emo, duration=30.0)
    _auto_align_bgm_to_emotion(plan)
    assert plan.bgm.video_anchor_seconds == 0.0


def test_no_emotion_curve_keeps_default():
    """plan.emotion_curve=None（LLM 全挂）→ anchor 不动。"""
    bgm = _bgm_with_climaxes(
        [BGMHighlight(at_seconds=20.0, kind="climax", label="x", fit_with_video="x")],
    )
    plan = _make_plan(bgm=bgm, emotion_curve=None, duration=30.0)
    _auto_align_bgm_to_emotion(plan)
    assert plan.bgm.video_anchor_seconds == 0.0


def test_clamp_positive_anchor_to_video_duration():
    """raw_anchor 超过 (video_dur - 1) → 上限钳制。

    BGM dur=120s，BGM climax @2s；视频 dur=10s，内容 peak @9s
    → raw=9-2=7；upper=10-1=9，lower=-(120-1)=-119 → 仍 7
    （此例不触发钳制，验证 anchor 落在区间内）
    """
    bgm = _bgm_with_climaxes(
        [BGMHighlight(at_seconds=2.0, kind="climax", label="x", fit_with_video="x")],
        duration_seconds=120.0,
    )
    emo = _emotion_with_peaks([EmotionPeak(t=9.0, intensity=0.95)])
    plan = _make_plan(bgm=bgm, emotion_curve=emo, duration=10.0)
    _auto_align_bgm_to_emotion(plan)
    assert plan.bgm.video_anchor_seconds == 7.0


def test_clamp_negative_anchor_to_bgm_duration():
    """BGM 极短 → 负 anchor 触发下限钳制：bgm_dur=8s，BGM climax @5s，内容 peak @100s
    → raw=95；upper=200-1=199 → 仍 95（不触发上限），但视频长 200s 不是问题。
    换个用例验证下限：bgm_dur=5s，climax @4s，内容 peak @0.5s
    → raw=0.5-4=-3.5；lower=-(5-1)=-4 → 仍 -3.5（区间内）
    再换：bgm_dur=2s，climax @1.8s，内容 peak @0s
    → raw=-1.8；lower=-(2-1)=-1 → clamp 到 -1.0
    """
    bgm = _bgm_with_climaxes(
        [BGMHighlight(at_seconds=1.8, kind="climax", label="x", fit_with_video="x")],
        duration_seconds=2.0,
    )
    emo = _emotion_with_peaks([EmotionPeak(t=0.0, intensity=0.9)])
    plan = _make_plan(bgm=bgm, emotion_curve=emo, duration=10.0)
    _auto_align_bgm_to_emotion(plan)
    assert plan.bgm.video_anchor_seconds == -1.0


def test_no_bgm_track_url_is_noop():
    """plan.bgm.track_url=None（用户没绑 BGM）→ 不动也不抛。"""
    bgm = BGMConfig()  # 空配置
    emo = _emotion_with_peaks([EmotionPeak(t=5.0, intensity=0.9)])
    plan = _make_plan(bgm=bgm, emotion_curve=emo, duration=20.0)
    _auto_align_bgm_to_emotion(plan)
    assert plan.bgm.video_anchor_seconds == 0.0


def test_picks_highest_intensity_content_peak():
    """多个 peaks 时取 intensity 最高的，不是时间最早的。"""
    bgm = _bgm_with_climaxes(
        [BGMHighlight(at_seconds=20.0, kind="climax", label="x", fit_with_video="x")],
    )
    emo = _emotion_with_peaks([
        EmotionPeak(t=5.0, intensity=0.6),    # 较低
        EmotionPeak(t=12.0, intensity=0.95),  # 最强 → 应选这个
    ])
    plan = _make_plan(bgm=bgm, emotion_curve=emo, duration=30.0)
    _auto_align_bgm_to_emotion(plan)
    # bgm_peak=20, content_peak=12（intensity 0.95 > 0.6）→ anchor=-8.0
    assert plan.bgm.video_anchor_seconds == -8.0
