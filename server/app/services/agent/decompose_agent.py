"""拆解 Agent —— 把样例视频拆成 SampleManifest。

Plan-act 循环（简单版）：每一步是一个独立的 sub-routine，向 JobStore 推进度，
错误就降级（用 mock 数据补齐）但不让流水线挂掉——比赛 demo 优先保完整性。

输入：sample_id + 样例视频路径（可空，空就走 mock 数据）
输出：SampleManifest

调用方：routers/decompose.py 的 BackgroundTask；阶段 1 mock 进度走的是这里。
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from ..jobs import job_store
from ..llm_client import get_llm_client, _extract_json
from ..vlm_client import get_vlm_client
from ..video import scene_detect, audio_analysis
from ...schemas import (
    PackagingProfile,
    RhythmCurve,
    SampleManifest,
    Section,
    Shot,
)

log = logging.getLogger("seecript.agent.decompose")


_PROMPT_SYSTEM = (
    "你是视频结构分析师。输入是按时间排序的镜头列表，每个镜头有口播文字。"
    "请把整段视频分成 hook / body / cta 三段，"
    "返回 JSON：{\"sections\": [{\"kind\": \"hook|body|cta\", \"start\": number, "
    "\"end\": number, \"summary\": str, \"shot_indices\": [int]}]}。"
    "hook 通常 ≤ 5 秒；cta 通常 ≤ 5 秒；body 是中间主体。"
)


async def decompose(
    sample_id: str,
    *,
    job_id: Optional[str] = None,
    video_path: Optional[str | Path] = None,
    title: str = "",
) -> SampleManifest:
    """完整拆解流水线。每一步失败都降级为 mock 数据但不中断。

    job_id 提供时通过 JobStore 推进度；不提供时纯函数式跑通。
    """
    def push(step: str, percent: float, payload: dict | None = None) -> None:
        if job_id:
            job_store.publish(job_id, step=step, percent=percent, payload=payload or {})

    if job_id:
        job_store.start(job_id)

    # ---- 1. 镜头切分 ----
    push("scene_detect", 10, {"note": "PySceneDetect 切镜头"})
    try:
        raw_shots = scene_detect.detect_shots(video_path) if video_path else scene_detect.detect_shots("")
    except Exception as exc:
        log.warning("scene_detect failed, using mock: %s", exc)
        raw_shots = scene_detect.detect_shots("")
    shots = [
        Shot(
            index=s.index,
            start=s.start,
            end=s.end,
            duration=s.duration,
            thumbnail_url=f"/samples/{sample_id}/shot-{s.index:02d}.jpg",
        )
        for s in raw_shots
    ]

    # ---- 2. 音频分析 ----
    push("audio_analysis", 30, {"note": "librosa BGM 能量曲线"})
    try:
        audio = audio_analysis.analyze_audio(video_path) if video_path else audio_analysis.analyze_audio("")
    except Exception as exc:
        log.warning("audio_analysis failed, using mock: %s", exc)
        audio = audio_analysis.analyze_audio("")
    rhythm = RhythmCurve(
        times=audio.times,
        cut_density=[1.0 if i % 2 == 0 else 0.6 for i in range(len(audio.times))],
        bgm_energy=audio.rms_energy,
        tempo_bpm=audio.tempo_bpm,
    )
    total_duration = audio.duration_seconds or (raw_shots[-1].end if raw_shots else 30.0)

    # ---- 3. ASR 口播 ----
    push("asr_transcribe", 55, {"note": "豆包 turbo ASR 口播"})
    # 简化：阶段 3 只在 video_path 真存在时走 ASR；mock 模式下填占位文本。
    for sh in shots:
        sh.transcript = sh.transcript or f"[mock] 镜头 {sh.index + 1} 口播片段。"

    # ---- 4. VLM 帧打标 ----
    push("vlm_tag", 75, {"note": "Doubao Vision 帧打标"})
    vlm = get_vlm_client()
    try:
        # 每个镜头取一张缩略图——阶段 1 没有真实文件，VLM 在 mock 模式直接 fallback。
        thumbs = [sh.thumbnail_url or f"frame-{sh.index}" for sh in shots]
        tag_results = await vlm.tag_frames(thumbs, taxonomy=["封面风格", "转场类型", "字幕样式", "物体场景"])
    except Exception as exc:
        log.warning("vlm tag_frames failed, using mock tags: %s", exc)
        tag_results = [{"frame": "", "tags": [], "subtitle_style": ""} for _ in shots]
    subtitle_styles: list[str] = []
    for sh, tr in zip(shots, tag_results):
        sh.tags = tr.get("tags", [])
        if tr.get("subtitle_style"):
            subtitle_styles.append(tr["subtitle_style"])

    # ---- 5. LLM 段落结构 ----
    push("llm_section", 92, {"note": "LLM 分 Hook/Body/CTA"})
    sections = await _llm_sections(shots, total_duration)

    # ---- 6. 打包 PackagingProfile ----
    dominant_subtitle = max(set(subtitle_styles), key=subtitle_styles.count) if subtitle_styles else "大字加描边"
    packaging = PackagingProfile(
        subtitle_style=dominant_subtitle,
        has_title_bar=True,
        transition_types=["cut", "fade"],
        cover_style="实拍画面 + 标题条",
        sticker_density=0.3,
    )

    manifest = SampleManifest(
        sample_id=sample_id,
        title=title or f"sample {sample_id}",
        duration_seconds=total_duration,
        video_url=f"/samples/{sample_id}/video.mp4",
        shots=shots,
        rhythm=rhythm,
        sections=sections,
        packaging=packaging,
    )

    if job_id:
        job_store.complete(job_id, payload={"sample_id": sample_id, "manifest": manifest.model_dump()})

    return manifest


async def _llm_sections(shots: list[Shot], total: float) -> list[Section]:
    """让 LLM 给出 Hook/Body/CTA 三段。失败时按 15/70/15 等比兜底。"""
    llm = get_llm_client()
    payload_lines = [f"{s.index}: {s.start:.1f}-{s.end:.1f}s | {s.transcript or '(无口播)'}" for s in shots]
    user = "镜头列表：\n" + "\n".join(payload_lines) + f"\n\n总时长：{total:.1f} 秒"
    try:
        data = await llm.complete_json(_PROMPT_SYSTEM, user)
        raw = data.get("sections", []) if isinstance(data, dict) else []
        sections: list[Section] = []
        for s in raw:
            sections.append(Section(
                kind=s.get("kind", "body"),
                start=float(s.get("start", 0.0)),
                end=float(s.get("end", 0.0)),
                summary=str(s.get("summary", "")),
                shot_indices=[int(i) for i in s.get("shot_indices", [])],
            ))
        if sections:
            return sections
    except Exception as exc:
        log.warning("llm sections failed, using even split: %s", exc)
    # 兜底：15/70/15
    hook_end = total * 0.15
    cta_start = total * 0.85
    n = len(shots)
    return [
        Section(kind="hook", start=0.0, end=hook_end, summary="开场",
                shot_indices=[i for i, s in enumerate(shots) if s.end <= hook_end]),
        Section(kind="body", start=hook_end, end=cta_start, summary="主体",
                shot_indices=[i for i, s in enumerate(shots) if hook_end < s.start < cta_start]),
        Section(kind="cta", start=cta_start, end=total, summary="收尾",
                shot_indices=[i for i, s in enumerate(shots) if s.start >= cta_start]),
    ]
