"""拆解 Agent —— 把样例视频拆成 SampleManifest。

数据流（与 docs/ARCHITECTURE.md §3 对齐）：
1. PySceneDetect 切镜头 → Shot[]
2. librosa 算 BGM 能量曲线 + tempo
3. librosa VAD 探测人声占比 → 命中口播再走 ASR；纯 BGM 跳过 ASR
4. 多模态 LLM（doubao-seed-2.0-lite）拿镜头缩略图打标 → 标签 + 主导字幕样式
5. 多模态 LLM 按 video_type 三选一 prompt 给段落结构

每一步失败都降级（mock 数据补齐），不让流水线挂掉——比赛 demo 优先保完整性。

`video_type` 三类型对应不同段落 schema：
- marketing      → hook/body/cta
- editing        → opening/climax/closing
- motion_graph   → intro/build/drop/outro
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from ..jobs import job_store
from ..llm_client import get_llm_client, _extract_json
from ..asr_client import get_asr_client, ASRError
from ..video import scene_detect, audio_analysis, voice_detect
from ...schemas import (
    PackagingProfile,
    RhythmCurve,
    SampleManifest,
    Section,
    Shot,
    VideoType,
    kinds_for_video_type,
)

log = logging.getLogger("seecript.agent.decompose")


# server/samples/<sample_id>/shot-NN.jpg 实际存在范围——agent 拿到 shot 索引后
# 先查盘上有没有对应的 jpg，没有就置 None，避免前端 404。
_SAMPLES_ROOT = Path(__file__).resolve().parents[3] / "samples"


def _shot_thumbnail_url(sample_id: str, index: int) -> Optional[str]:
    if not sample_id:
        return None
    candidate = _SAMPLES_ROOT / sample_id / f"shot-{index:02d}.jpg"
    if candidate.is_file():
        return f"/samples/{sample_id}/shot-{index:02d}.jpg"
    return None


# ----------------------------- Prompt 模板 -----------------------------------

_SECTIONS_TASK_HINT = (
    "返回 JSON：{\"sections\": [{\"kind\": <见允许枚举>, \"start\": number, "
    "\"end\": number, \"summary\": str, \"shot_indices\": [int]}]}。"
)

_SECTIONS_PROMPTS: dict[str, str] = {
    "marketing": (
        "你是营销/带货短视频结构分析师。视频按时间排序的镜头列表配口播。"
        "请把视频切成 hook（≤5s 痛点钩子）→ body（产品演示/对比主体）→ cta（≤5s 行动引导）三段。"
        + _SECTIONS_TASK_HINT
        + " 允许 kind 枚举：hook, body, cta。"
    ),
    "editing": (
        "你是剪辑/Vlog 视频结构分析师。视频按时间排序的镜头列表配口播（可能为空）。"
        "请按情绪曲线切成 opening（环境/氛围铺垫）→ climax（情绪/动作高潮）→ closing（余韵收尾）三段。"
        + _SECTIONS_TASK_HINT
        + " 允许 kind 枚举：opening, climax, closing。"
    ),
    "motion_graph": (
        "你是 Motion Graph 视频结构分析师。镜头多为合成动画。"
        "请按四段切：intro（标题/Logo 入场）→ build（信息铺陈）→ drop（视觉爆点）→ outro（落版收尾）。"
        + _SECTIONS_TASK_HINT
        + " 允许 kind 枚举：intro, build, drop, outro。"
    ),
}


_FRAME_TAG_SYSTEM = (
    "你是短视频画面打标助手。输入是一组按时间排序的关键帧。"
    "请按封面风格 / 转场类型 / 字幕样式 / 物体场景 四个维度，"
    "为每帧给 3-5 个标签，并判定字幕样式（大字加描边 / 小字白底 / 无字幕）。"
    "返回 JSON：{\"frame_tags\": [{\"frame_id\": str, \"tags\": [str], \"subtitle_style\": str}]}"
)


# ----------------------------- 主流水线 --------------------------------------

async def decompose(
    sample_id: str,
    *,
    job_id: Optional[str] = None,
    video_path: Optional[str | Path] = None,
    title: str = "",
    video_type: VideoType = "marketing",
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
            thumbnail_url=_shot_thumbnail_url(sample_id, s.index),
        )
        for s in raw_shots
    ]

    # ---- 2. 音频分析 ----
    push("audio_analysis", 28, {"note": "librosa BGM 能量曲线"})
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

    # ---- 3. 人声 VAD + 条件 ASR ----
    push("voice_detect", 42, {"note": "librosa 人声 VAD 探测"})
    has_voice = True
    if video_path:
        try:
            vad = voice_detect.detect_voice(video_path)
            has_voice = vad.has_voice
            push("voice_detect", 45, {
                "has_voice": vad.has_voice,
                "voice_ratio": round(vad.voice_ratio, 3),
                "backend": vad.backend,
            })
        except Exception as exc:
            log.warning("voice_detect failed, defaulting has_voice=True: %s", exc)
            has_voice = True

    if has_voice:
        push("asr_transcribe", 55, {"note": "豆包 turbo ASR 口播"})
        await _attach_transcripts(shots, video_path)
    else:
        push("asr_transcribe", 55, {
            "note": "纯 BGM/无口播——跳过 ASR",
            "skipped": True,
        })
        # 纯 BGM 视频：transcript 全置空，让段落 prompt 走"靠画面+节奏"分支
        for sh in shots:
            sh.transcript = None

    # ---- 4. 多模态 LLM 关键帧打标 ----
    push("vlm_tag", 75, {"note": "多模态 LLM 帧打标 (seed-2.0-lite)"})
    await _attach_frame_tags(shots)

    # ---- 5. 多模态 LLM 段落结构（按 video_type 三选一）----
    push("llm_section", 92, {"note": f"LLM 段落分析 · {video_type}"})
    sections = await _llm_sections(shots, total_duration, video_type, has_voice)

    # ---- 6. 打包 PackagingProfile ----
    subtitle_styles = [s for sh in shots for s in (sh.tags or []) if isinstance(s, str) and "字幕" in s]
    if subtitle_styles:
        dominant_subtitle = max(set(subtitle_styles), key=subtitle_styles.count)
    else:
        dominant_subtitle = "大字加描边" if has_voice else "无字幕"
    packaging = PackagingProfile(
        subtitle_style=dominant_subtitle,
        has_title_bar=True,
        transition_types=["cut", "fade"],
        cover_style="实拍画面 + 标题条" if video_type != "motion_graph" else "合成画面 + 大字标题",
        sticker_density=0.3,
    )

    manifest = SampleManifest(
        sample_id=sample_id,
        title=title or f"sample {sample_id}",
        video_type=video_type,
        duration_seconds=total_duration,
        video_url=f"/samples/{sample_id}/video.mp4",
        has_voice=has_voice,
        shots=shots,
        rhythm=rhythm,
        sections=sections,
        packaging=packaging,
    )

    if job_id:
        job_store.complete(job_id, payload={"sample_id": sample_id, "manifest": manifest.model_dump()})

    return manifest


# ----------------------------- ASR 子例程 ------------------------------------

async def _attach_transcripts(shots: list[Shot], video_path: Optional[str | Path]) -> None:
    """有口播时调 ASR 拿整段 transcript，按"按 shot 时长比例分摊"近似挂到每个 Shot。

    ASR 极速版当前只回单字符串（无逐句时间戳）；我们没有更精确的时间轴信息，
    所以用最朴素的"按时长占比切字符"来给每个 Shot 配字幕。
    后续若切到带 utterances 的版本，把这段换成 utterance 区间匹配即可。
    """
    if not shots:
        return

    # mock 模式 / 没有真文件 → 占位文本
    if not video_path or not Path(str(video_path)).exists():
        for sh in shots:
            sh.transcript = sh.transcript or f"[mock] 镜头 {sh.index + 1} 口播片段。"
        return

    try:
        with open(str(video_path), "rb") as f:
            blob = f.read()
        asr = get_asr_client()
        suffix = Path(str(video_path)).suffix.lstrip(".") or "mp4"
        transcript = await asr.transcribe_bytes(blob, audio_format=suffix)
    except (ASRError, OSError) as exc:
        log.warning("ASR failed, using mock per-shot transcripts: %s", exc)
        for sh in shots:
            sh.transcript = f"[mock] 镜头 {sh.index + 1} 口播片段。"
        return

    transcript = (transcript or "").strip()
    if not transcript:
        for sh in shots:
            sh.transcript = None
        return

    total_duration = sum(max(0.1, sh.duration) for sh in shots)
    cursor = 0
    for sh in shots:
        ratio = max(0.1, sh.duration) / total_duration
        chars = max(1, int(len(transcript) * ratio))
        sh.transcript = transcript[cursor: cursor + chars].strip()
        cursor += chars
    # 余数补给最后一个 shot
    if cursor < len(transcript) and shots:
        shots[-1].transcript = (shots[-1].transcript or "") + transcript[cursor:]


# ----------------------------- 多模态打标子例程 -------------------------------

async def _attach_frame_tags(shots: list[Shot]) -> None:
    """对每个 Shot 取一张缩略图，打包成多模态 LLM 请求。

    失败/无图时降级为空标签——让流水线推进，但段落分析仍可工作。
    """
    if not shots:
        return
    llm = get_llm_client()
    images: list[str] = []
    for sh in shots:
        # 缩略图 URL 现阶段都是 /samples/.../shot-NN.jpg 的虚拟路径——
        # 文件不存在时 _image_ref_to_url 会回落到 1×1 占位 PNG，模型仍能拿到结构合法的 user content。
        images.append(sh.thumbnail_url or "")

    user_text = (
        "请为以下 " + str(len(shots)) + " 张关键帧打标。frame_id 用 'f-001'.. 这种 0 填充三位的格式。"
    )
    try:
        text = await llm.complete_multimodal(_FRAME_TAG_SYSTEM, user_text, images)
        data = _extract_json(text)
        items = data.get("frame_tags", []) if isinstance(data, dict) else []
    except Exception as exc:
        log.warning("multimodal frame tagging failed, using empty tags: %s", exc)
        items = []

    # 按顺序映射到 shots；缺失项补空标签
    for i, sh in enumerate(shots):
        if i < len(items):
            sh.tags = list(items[i].get("tags", []))
        else:
            sh.tags = []


# ----------------------------- 段落分析子例程 ---------------------------------

async def _llm_sections(
    shots: list[Shot],
    total: float,
    video_type: VideoType,
    has_voice: bool,
) -> list[Section]:
    """让 LLM 按 video_type 给段落结构。失败时按等比兜底。"""
    llm = get_llm_client()
    payload_lines: list[str] = []
    for s in shots:
        speech = s.transcript or "(无口播)"
        tag_str = "/".join(s.tags) if s.tags else ""
        line = f"{s.index}: {s.start:.1f}-{s.end:.1f}s | {speech}"
        if tag_str:
            line += f" | tags: {tag_str}"
        payload_lines.append(line)

    voice_hint = "本视频有口播，可结合字面信息推断段落语义。" if has_voice else (
        "本视频是纯 BGM / 环境音，没有口播文字。请仅根据画面标签和镜头节奏切段落。"
    )
    user = (
        f"视频类型：{video_type}\n"
        f"总时长：{total:.1f} 秒\n"
        f"{voice_hint}\n\n"
        "镜头列表：\n" + "\n".join(payload_lines)
    )
    system = _SECTIONS_PROMPTS.get(video_type, _SECTIONS_PROMPTS["marketing"])

    # 优先走多模态：把每个 Shot 的缩略图作为 image 参数传给 LLM。
    images = [sh.thumbnail_url or "" for sh in shots]
    try:
        text = await llm.complete_multimodal(system, user, images)
        data = _extract_json(text)
        raw = data.get("sections", []) if isinstance(data, dict) else []
        allowed_kinds = set(kinds_for_video_type(video_type))
        sections: list[Section] = []
        for s in raw:
            kind = s.get("kind", "")
            if kind not in allowed_kinds:
                # 模型偶尔串了别类型的 kind，按位置兜底改回当前类型的对应位
                kind = next(iter(allowed_kinds))
            sections.append(
                Section(
                    kind=kind,
                    start=float(s.get("start", 0.0)),
                    end=float(s.get("end", 0.0)),
                    summary=str(s.get("summary", "")),
                    shot_indices=[int(i) for i in s.get("shot_indices", [])],
                )
            )
        if sections:
            return sections
    except Exception as exc:
        log.warning("llm sections failed, using even split: %s", exc)

    return _even_split(shots, total, video_type)


def _even_split(shots: list[Shot], total: float, video_type: VideoType) -> list[Section]:
    """按 video_type 的段落数量均分时长兜底。"""
    kinds = kinds_for_video_type(video_type)
    n_seg = len(kinds)
    if n_seg == 3:
        # 营销/剪辑类：开场短、收尾短、主体长（15/70/15）
        boundaries = [0.0, total * 0.15, total * 0.85, total]
    else:
        # motion_graph 4 段：等分
        step = total / n_seg
        boundaries = [step * i for i in range(n_seg + 1)]
        boundaries[-1] = total

    sections: list[Section] = []
    for i, kind in enumerate(kinds):
        start = boundaries[i]
        end = boundaries[i + 1]
        shot_idx = [s.index for s in shots if start <= s.start < end]
        sections.append(
            Section(
                kind=kind,
                start=start,
                end=end,
                summary=f"{kind} 段（兜底等分）",
                shot_indices=shot_idx,
            )
        )
    return sections
