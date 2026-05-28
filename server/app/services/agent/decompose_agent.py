"""拆解 Agent —— 把样例视频拆成 SampleManifest（理解优先 · 角色+主题双层结构）。

数据流（与 docs/ARCHITECTURE.md §3 对齐）：
1. PySceneDetect 切镜头 → Shot[]
2. librosa 算 BGM 能量曲线 + tempo
3. librosa VAD 探测人声占比 → 命中口播再走 ASR；纯 BGM 跳过 ASR
4. 多模态 LLM（doubao-seed-2.0-lite）拿镜头缩略图打标 → 标签 + 主导字幕样式
5. **多模态 LLM 视频理解**（v2 新增）：先看完整片给出 archetype/narrative/segments/tone
6. **多模态 LLM 角色+主题切段**（v2 重构）：基于理解输出切成 opening/development/climax/closing
   各段附 free-text theme（『展品揭幕』『艺术家自述』『行动呼吁』）

每一步失败都降级（mock 数据补齐），不让流水线挂掉——比赛 demo 优先保完整性。

为什么改：旧版按 video_type 三选一塞 hook/body/cta、opening/climax/closing、intro/build/drop/outro
9 个固定 kind 给 LLM 让它切段，对真实样例僵硬——比如艺术展宣传片硬塞 hook/body/cta 语义不通。
新版让 LLM 先理解视频再切段，role 是抽象骨架（任何视频都适用），theme 是 LLM 给的具体语义。
video_type 降级为风格提示，只影响包装（字幕/转场/封面），不再决定段落结构。
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from ..jobs import job_store
from ..llm_client import get_llm_client, _extract_json
from ..asr_client import get_asr_client, ASRError, ASRTranscript, Utterance as ASRUtterance
from ..video import scene_detect, audio_analysis, voice_detect
from ...config import get_settings
from ...schemas import (
    PackagingProfile,
    RhythmCurve,
    SampleManifest,
    Section,
    SectionRole,
    Shot,
    Utterance,
    VideoUnderstanding,
    VideoType,
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

_VIDEO_TYPE_HINT: dict[str, str] = {
    "marketing":    "营销/带货/动态海报类——节奏紧凑、强字幕、强引导",
    "editing":      "剪辑/Vlog/纪录类——情绪曲线、空镜与高潮、长镜与余韵",
    "motion_graph": "Motion Graph/信息可视化——标题入场、爆点切换、落版收尾",
}


_UNDERSTAND_SYSTEM = (
    "你是短视频内容分析师。看一组按时间排序的关键帧（配可能为空的口播），"
    "请对整支视频做语义画像。\n"
    "返回 JSON：{"
    "\"archetype\": str(≤20字, 一句话定性这视频的原型；例：『艺术展宣传』『带货种草』『城市Vlog』『信息可视化解释』), "
    "\"narrative_summary\": str(≤80字, 一段话讲清整支视频在说什么、怎么说), "
    "\"suggested_segments\": int(3-6, 你建议把视频切成几个叙事段落), "
    "\"tone\": str(≤15字, 基调；例：『冷静克制』『高燃热血』『诙谐自嘲』『庄重正式』)"
    "}。\n"
    "注意：不要套用固定模板。视频拍什么样就说什么样——艺术展就是艺术展，不要硬说『钩子→主体→引导』。"
)


_SHOT_ROLE_SYSTEM = (
    "你是短视频结构分析师。给定视频画像和按时间排序的镜头列表，"
    "为**每个镜头**标注它在叙事中的角色和主题。\n\n"
    "角色（role）只能是以下 4 种之一：\n"
    "- opening: 开场（吸引注意/奠定基调）\n"
    "- development: 发展铺陈（信息展开/内容主体）\n"
    "- climax: 高潮（情绪/视觉/冲突顶点）\n"
    "- closing: 收尾（余韵/引导/落版）\n\n"
    "硬约束：\n"
    "1. 第一个镜头必须是 opening\n"
    "2. 最后一个镜头必须是 closing\n"
    "3. 中间镜头不能是 opening 或 closing\n"
    "4. 整支视频最多 1 个镜头标 climax（也可以没有）\n"
    "5. 相邻同 role 镜头会被合并为一个段落——所以最终段落数 ≤ 镜头数\n\n"
    "theme: 中文短标签（≤10 字），反映这个镜头真实在讲什么——"
    "不要照抄 role，要从画面/口播内容里提炼。\n\n"
    "返回 JSON：{\"shot_roles\": [{\"shot_index\": int, \"role\": str, \"theme\": str}]}\n"
    "数组长度必须等于镜头数，按 shot_index 升序排列。"
)


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
    push("scene_detect", 8, {"note": "PySceneDetect 切镜头"})
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
    push("audio_analysis", 22, {"note": "librosa BGM 能量曲线"})
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
    push("voice_detect", 36, {"note": "librosa 人声 VAD 探测"})
    has_voice = True
    if video_path:
        try:
            vad = voice_detect.detect_voice(video_path)
            has_voice = vad.has_voice
            push("voice_detect", 40, {
                "has_voice": vad.has_voice,
                "voice_ratio": round(vad.voice_ratio, 3),
                "backend": vad.backend,
            })
        except Exception as exc:
            log.warning("voice_detect failed, defaulting has_voice=True: %s", exc)
            has_voice = True

    if has_voice:
        push("asr_transcribe", 48, {"note": "豆包 ASR 2.0 转写口播"})
        asr_voice, asr_utterances = await _attach_transcripts(shots, video_path)
        # ASR 真模型对 VAD 的二次校正：模型回 20000003 = 确认无人声 → 把 has_voice 改回 False。
        if asr_voice is False:
            log.info("[decompose] ASR 校正：VAD has_voice=True 但模型确认无人声 → 改为 False")
            has_voice = False
            for sh in shots:
                sh.transcript = None
            asr_utterances = []
            push("asr_transcribe", 52, {
                "note": "ASR 模型确认无人声 → 修正为纯 BGM 分支",
                "vad_overruled": True,
            })
    else:
        push("asr_transcribe", 48, {
            "note": "纯 BGM/无口播——跳过 ASR",
            "skipped": True,
        })
        for sh in shots:
            sh.transcript = None
        asr_utterances = []

    # ---- 4. 多模态 LLM 关键帧打标 ----
    push("vlm_tag", 65, {"note": "多模态 LLM 帧打标 (seed-2.0-lite)"})
    await _attach_frame_tags(shots)

    # ---- 5. 视频理解（v2 新增）----
    push("video_understand", 80, {"note": "LLM 视频画像：archetype / narrative / segments / tone"})
    understanding = await _video_understand(shots, video_type, has_voice, total_duration)
    push("video_understand", 84, {
        "archetype": understanding.archetype,
        "tone": understanding.tone,
        "suggested_segments": understanding.suggested_segments,
    })

    # ---- 6. LLM 角色+主题切段（v2 重构）----
    push("llm_section", 93, {"note": f"LLM 段落分析 · 基于画像切 {understanding.suggested_segments} 段"})
    sections = await _segment_with_roles(shots, total_duration, understanding, has_voice)

    # ---- 7. 打包 PackagingProfile（video_type 仍驱动包装风格）----
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
        understanding=understanding,
        utterances=[
            Utterance(text=u.text, start=u.start, end=u.end) for u in asr_utterances
        ],
        climax_position=_compute_climax(sections, rhythm, total_duration),
    )

    if job_id:
        job_store.complete(job_id, payload={"sample_id": sample_id, "manifest": manifest.model_dump()})

    return manifest


# ----------------------------- ASR 子例程 ------------------------------------

async def _attach_transcripts(
    shots: list[Shot],
    video_path: Optional[str | Path],
) -> tuple[Optional[bool], list[ASRUtterance]]:
    """有口播时调 ASR 拿整段 transcript + 逐句时间戳，按时间窗映射到每个 Shot。"""
    if not shots:
        return None, []

    if not video_path or not Path(str(video_path)).exists():
        for sh in shots:
            sh.transcript = sh.transcript or f"[mock] 镜头 {sh.index + 1} 口播片段。"
        return None, []

    settings = get_settings()
    public_base = (settings.public_audio_base_url or "").rstrip("/")
    if not public_base:
        log.warning("PUBLIC_AUDIO_BASE_URL 未配置 → 豆包 2.0 拿不到音频，跳过 ASR 走 mock")
        for sh in shots:
            sh.transcript = f"[mock] 镜头 {sh.index + 1} 口播片段（未配 PUBLIC_AUDIO_BASE_URL）。"
        return None, []

    p = Path(str(video_path)).resolve()
    rel: str | None = None
    parts = p.parts
    for marker, mount in (("samples", "/samples"), ("uploads", "/uploads")):
        if marker in parts:
            idx = parts.index(marker)
            rel = mount + "/" + "/".join(parts[idx + 1:])
            break
    if rel is None:
        log.warning("视频路径 %s 不在 samples/uploads 下，跳过 ASR 走 mock", p)
        for sh in shots:
            sh.transcript = f"[mock] 镜头 {sh.index + 1} 口播片段（路径未挂载）。"
        return None, []
    audio_url = f"{public_base}{rel}"
    suffix = p.suffix.lstrip(".") or "mp4"

    try:
        asr = get_asr_client()
        result: ASRTranscript = await asr.transcribe_url(audio_url, audio_format=suffix)
    except ASRError as exc:
        if exc.upstream_status == 20000003:
            log.info("ASR 返回 20000003（静音/无人声）→ 该视频是纯 BGM，撤销 has_voice")
            for sh in shots:
                sh.transcript = None
            return False, []
        log.warning("ASR failed (url=%s), using mock per-shot transcripts: %s", audio_url, exc)
        for sh in shots:
            sh.transcript = f"[mock] 镜头 {sh.index + 1} 口播片段。"
        return None, []
    except OSError as exc:
        log.warning("ASR I/O failed (url=%s): %s", audio_url, exc)
        for sh in shots:
            sh.transcript = f"[mock] 镜头 {sh.index + 1} 口播片段。"
        return None, []

    text = (result.text or "").strip()
    utterances = result.utterances or []

    if not text and not utterances:
        for sh in shots:
            sh.transcript = None
        return False, []

    if utterances:
        log.info(
            "[asr] got %d utterances (text %d chars) → mapping by shot time-window",
            len(utterances), len(text),
        )
        _attach_by_time_window(shots, utterances)
    else:
        log.info("[asr] no utterances, fallback to char-ratio split (legacy)")
        _attach_by_char_ratio(shots, text)

    return True, utterances


def _attach_by_time_window(shots: list[Shot], utterances: list[ASRUtterance]) -> None:
    """按 shot 时间窗筛选 utterances → 拼成每个 shot 的 transcript。"""
    if not shots or not utterances:
        return
    for sh in shots:
        sh.transcript = None

    buckets: dict[int, list[str]] = {sh.index: [] for sh in shots}

    for utt in utterances:
        u_start, u_end = utt.start, max(utt.start, utt.end)
        best_idx = None
        best_overlap = 0.0
        for sh in shots:
            overlap = max(0.0, min(u_end, sh.end) - max(u_start, sh.start))
            if overlap > best_overlap:
                best_overlap = overlap
                best_idx = sh.index
        if best_idx is None:
            nearest = min(
                shots,
                key=lambda s: min(abs(s.start - u_start), abs(s.end - u_end)),
            )
            best_idx = nearest.index
        buckets[best_idx].append(utt.text.strip())

    for sh in shots:
        parts = [t for t in buckets.get(sh.index, []) if t]
        sh.transcript = " ".join(parts) if parts else None


def _attach_by_char_ratio(shots: list[Shot], transcript: str) -> None:
    """旧版兜底：按 shot 时长比例切字符。仅在 utterances 缺失时使用。"""
    total_duration = sum(max(0.1, sh.duration) for sh in shots)
    cursor = 0
    for sh in shots:
        ratio = max(0.1, sh.duration) / total_duration
        chars = max(1, int(len(transcript) * ratio))
        sh.transcript = transcript[cursor: cursor + chars].strip() or None
        cursor += chars
    if cursor < len(transcript) and shots:
        tail = transcript[cursor:].strip()
        if tail:
            shots[-1].transcript = ((shots[-1].transcript or "") + " " + tail).strip()


# ----------------------------- 多模态打标子例程 -------------------------------

async def _attach_frame_tags(shots: list[Shot]) -> None:
    """对每个 Shot 取一张缩略图，打包成多模态 LLM 请求（8 张一批）。"""
    if not shots:
        return
    llm = get_llm_client()
    BATCH = 8
    all_items: list[dict] = []
    for start in range(0, len(shots), BATCH):
        batch = shots[start:start + BATCH]
        images = [sh.thumbnail_url or "" for sh in batch]
        user_text = (
            f"请为以下 {len(batch)} 张关键帧打标。frame_id 用 'f-001'.. 这种 0 填充三位的格式。"
        )
        try:
            text = await llm.complete_multimodal(_FRAME_TAG_SYSTEM, user_text, images)
            data = _extract_json(text)
            items = data.get("frame_tags", []) if isinstance(data, dict) else []
        except Exception as exc:
            log.warning("multimodal frame tagging batch %d-%d failed: %s",
                        start, start + len(batch), exc)
            items = []
        while len(items) < len(batch):
            items.append({})
        all_items.extend(items[:len(batch)])

    for i, sh in enumerate(shots):
        item = all_items[i] if i < len(all_items) else {}
        sh.tags = list(item.get("tags", []))


# ----------------------------- 视频理解子例程 ---------------------------------

async def _video_understand(
    shots: list[Shot],
    video_type: VideoType,
    has_voice: bool,
    total_duration: float,
) -> VideoUnderstanding:
    """LLM 给整支视频的语义画像。失败时按 video_type 兜底。

    sample 一组（不超过 12 张）关键帧给 LLM——太多会撑爆 max_tokens；
    采样策略：均匀采样代表性帧，覆盖首中尾。
    """
    llm = get_llm_client()

    # 关键帧采样：≤12 帧，均匀分布
    if len(shots) <= 12:
        sampled = shots
    else:
        step = len(shots) / 12
        sampled = [shots[int(i * step)] for i in range(12)]

    # 把镜头标签 + 口播作为文本上下文
    lines: list[str] = []
    for sh in sampled:
        speech = sh.transcript or ("(无口播)" if not has_voice else "")
        tags = "/".join(sh.tags) if sh.tags else ""
        line = f"#{sh.index} {sh.start:.1f}-{sh.end:.1f}s"
        if speech:
            line += f" | {speech}"
        if tags:
            line += f" | tags: {tags}"
        lines.append(line)

    voice_hint = "有口播" if has_voice else "纯 BGM / 环境音，无口播"
    user = (
        f"视频风格类型（仅供参考）：{video_type}（{_VIDEO_TYPE_HINT.get(video_type, '通用短视频')}）\n"
        f"总时长：{total_duration:.1f} 秒\n"
        f"声音情况：{voice_hint}\n\n"
        f"代表性镜头（共 {len(shots)} 个，采样 {len(sampled)} 个）：\n"
        + "\n".join(lines)
    )

    images = [sh.thumbnail_url or "" for sh in sampled]
    try:
        text = await llm.complete_multimodal(_UNDERSTAND_SYSTEM, user, images)
        data = _extract_json(text)
        if isinstance(data, dict):
            archetype = str(data.get("archetype", "") or "").strip()[:40]
            narrative = str(data.get("narrative_summary", "") or "").strip()[:200]
            try:
                seg_count = int(data.get("suggested_segments", 4))
            except (TypeError, ValueError):
                seg_count = 4
            seg_count = max(3, min(6, seg_count))
            tone = str(data.get("tone", "") or "").strip()[:30]
            if archetype and narrative:
                return VideoUnderstanding(
                    archetype=archetype,
                    narrative_summary=narrative,
                    suggested_segments=seg_count,
                    tone=tone or "通用",
                )
    except Exception as exc:
        log.warning("video_understand failed, using fallback: %s", exc)

    # 兜底：按 video_type 给一份保守画像
    fallback = {
        "marketing":    ("营销/动态海报", "营销/带货向短视频，开场建立钩子，中段铺陈卖点，收尾引导行动。", 3, "活泼明快"),
        "editing":      ("剪辑/Vlog",   "情绪流向短视频，氛围铺垫→情绪/动作高潮→余韵收尾。",            4, "细腻温暖"),
        "motion_graph": ("Motion Graph", "信息/合成动画视频，标题入场→数据/概念铺陈→视觉爆点→落版。",   4, "干净利落"),
    }
    arche, narr, seg, tone = fallback.get(video_type, fallback["marketing"])
    return VideoUnderstanding(
        archetype=arche,
        narrative_summary=narr,
        suggested_segments=seg,
        tone=tone,
    )


# ----------------------------- 段落分析子例程 ---------------------------------

async def _segment_with_roles(
    shots: list[Shot],
    total: float,
    understanding: VideoUnderstanding,
    has_voice: bool,
) -> list[Section]:
    """Shot-first 分类：让 LLM 给**每个镜头**打 role + theme，再合并相邻同 role 镜头成段落。

    这样保证：
    1. 段落总时长 = 镜头总时长，绝不超出视频实际长度
    2. 段落数 ≤ 镜头数（一个镜头要么自成一段，要么和相邻同 role 镜头合并）
    3. 主题来源于真实镜头内容，不是套模板
    """
    if not shots:
        return []

    llm = get_llm_client()
    payload_lines: list[str] = []
    for s in shots:
        speech = s.transcript or "(无口播)"
        tag_str = "/".join(s.tags) if s.tags else ""
        line = f"{s.index}: {s.start:.1f}-{s.end:.1f}s | {speech}"
        if tag_str:
            line += f" | tags: {tag_str}"
        payload_lines.append(line)

    voice_hint = "本视频有口播。" if has_voice else "本视频纯 BGM / 环境音，无口播。请仅根据画面标签和镜头节奏判断。"
    user = (
        f"视频原型：{understanding.archetype}\n"
        f"叙事概览：{understanding.narrative_summary}\n"
        f"基调：{understanding.tone}\n"
        f"镜头总数：{len(shots)}\n"
        f"总时长：{total:.1f} 秒\n"
        f"{voice_hint}\n\n"
        "镜头列表（请为每一个镜头给出 role + theme）：\n" + "\n".join(payload_lines)
    )

    images = [sh.thumbnail_url or "" for sh in shots]
    try:
        text = await llm.complete_multimodal(_SHOT_ROLE_SYSTEM, user, images)
        data = _extract_json(text)
        raw = data.get("shot_roles", []) if isinstance(data, dict) else []
        allowed_roles: set[SectionRole] = {"opening", "development", "climax", "closing"}

        # 用 index → (role, theme) 映射，方便按镜头顺序回填
        roles_by_index: dict[int, tuple[SectionRole, str]] = {}
        for item in raw:
            if not isinstance(item, dict):
                continue
            try:
                idx = int(item.get("shot_index"))
            except (TypeError, ValueError):
                continue
            role = item.get("role", "")
            if role not in allowed_roles:
                continue
            theme = str(item.get("theme", "") or "").strip()[:20]
            roles_by_index[idx] = (role, theme)

        if roles_by_index:
            shot_roles = _assign_shot_roles(shots, roles_by_index)
            return _merge_shot_roles_into_sections(shots, shot_roles)
    except Exception as exc:
        log.warning("segment_with_roles failed, using shot-first fallback: %s", exc)

    return _fallback_shot_roles(shots)


def _assign_shot_roles(
    shots: list[Shot],
    roles_by_index: dict[int, tuple[SectionRole, str]],
) -> list[tuple[SectionRole, str]]:
    """把 LLM 给的 {shot_index: (role, theme)} 映射成 [(role, theme)]（按 shots 顺序）。

    缺漏的镜头按位置兜底：第一个镜头 opening、最后一个 closing、中间 development。
    然后做强约束修正：首=opening、尾=closing、中间至多 1 climax，多余 climax 降为 development。
    """
    out: list[tuple[SectionRole, str]] = []
    n = len(shots)
    for i, sh in enumerate(shots):
        if sh.index in roles_by_index:
            role, theme = roles_by_index[sh.index]
        else:
            if i == 0:
                role, theme = "opening", "开场"
            elif i == n - 1:
                role, theme = "closing", "收尾"
            else:
                role, theme = "development", "铺陈"
        out.append((role, theme))

    # 强约束修正
    if n >= 1:
        first_role, first_theme = out[0]
        out[0] = ("opening", first_theme or "开场")
    if n >= 2:
        last_role, last_theme = out[-1]
        out[-1] = ("closing", last_theme or "收尾")

    # 中间镜头：不允许 opening/closing；至多 1 个 climax
    climax_seen = 0
    for i in range(1, n - 1):
        role, theme = out[i]
        if role in ("opening", "closing"):
            out[i] = ("development", theme or "铺陈")
        elif role == "climax":
            climax_seen += 1
            if climax_seen > 1:
                out[i] = ("development", theme or "铺陈")

    return out


def _merge_shot_roles_into_sections(
    shots: list[Shot],
    shot_roles: list[tuple[SectionRole, str]],
) -> list[Section]:
    """按 shot 顺序走，相邻同 role 的镜头合并成一个 Section。

    section.start = 首个镜头的 start；section.end = 末个镜头的 end；
    section.theme 取段内**第一个非空** theme；section.shot_indices = 段内所有 shot index。
    """
    if not shots or not shot_roles:
        return []

    sections: list[Section] = []
    cur_role: Optional[SectionRole] = None
    cur_theme = ""
    cur_indices: list[int] = []
    cur_start = 0.0
    cur_end = 0.0

    def _flush() -> None:
        if cur_role is None or not cur_indices:
            return
        sections.append(
            Section(
                role=cur_role,
                theme=cur_theme or _default_theme(cur_role),
                start=cur_start,
                end=cur_end,
                summary=f"{cur_role} 段（{len(cur_indices)} 个镜头）",
                shot_indices=list(cur_indices),
            )
        )

    for sh, (role, theme) in zip(shots, shot_roles):
        if role != cur_role:
            _flush()
            cur_role = role
            cur_theme = theme
            cur_indices = [sh.index]
            cur_start = sh.start
            cur_end = sh.end
        else:
            cur_indices.append(sh.index)
            cur_end = sh.end
            if not cur_theme and theme:
                cur_theme = theme
    _flush()
    return sections


def _default_theme(role: SectionRole) -> str:
    return {
        "opening": "开场",
        "development": "铺陈",
        "climax": "高潮",
        "closing": "收尾",
    }[role]


def _fallback_shot_roles(shots: list[Shot]) -> list[Section]:
    """无 LLM 时的 shot-first 兜底：第一镜头 opening、最后 closing、中间 development。

    镜头总数 ≥ 4 时，挑中间偏后的镜头当 climax（粗略反映"高潮在后半段"的经验）。
    没有 LLM 也保证段落总时长 = 镜头总时长，绝不虚构超出视频长度。
    """
    n = len(shots)
    if n == 0:
        return []
    if n == 1:
        sh = shots[0]
        return [Section(
            role="opening", theme="开场", start=sh.start, end=sh.end,
            summary="单镜头视频", shot_indices=[sh.index],
        )]

    shot_roles: list[tuple[SectionRole, str]] = []
    climax_idx: Optional[int] = None
    if n >= 4:
        climax_idx = int(n * 0.6)
        if climax_idx <= 0 or climax_idx >= n - 1:
            climax_idx = None

    for i in range(n):
        if i == 0:
            shot_roles.append(("opening", "开场"))
        elif i == n - 1:
            shot_roles.append(("closing", "收尾"))
        elif i == climax_idx:
            shot_roles.append(("climax", "高潮"))
        else:
            shot_roles.append(("development", "铺陈"))

    return _merge_shot_roles_into_sections(shots, shot_roles)


# ----------------------------- 高潮时间估算 ------------------------------------

def _compute_climax(
    sections: list[Section],
    rhythm: RhythmCurve,
    total_duration: float,
) -> Optional[float]:
    """估算高潮时间点（秒），用于前端节奏图上的 ReferenceLine。

    优先级：
    1. role=climax 段的中点
    2. BGM 能量曲线峰值
    3. 总时长 60% 处（短视频经典高潮位）
    """
    if total_duration <= 0:
        return None

    for sec in sections:
        if sec.role == "climax":
            return float((sec.start + sec.end) / 2)

    if rhythm.bgm_energy and rhythm.times:
        n = min(len(rhythm.bgm_energy), len(rhythm.times))
        if n > 0:
            peak_idx = max(range(n), key=lambda i: rhythm.bgm_energy[i])
            return float(rhythm.times[peak_idx])

    return float(total_duration * 0.6)
