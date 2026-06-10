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

import asyncio
import logging
from pathlib import Path
from typing import Optional

from ..jobs import job_store
from ..library import manifest_store
from ..llm_client import get_llm_client, _extract_json
from ..asr_client import get_asr_client, ASRError, ASRTranscript, Utterance as ASRUtterance
from ..assets import resolve_reference_image_urls
from ..video import scene_detect, audio_analysis, voice_detect
from ..video import ffmpeg as ffmpeg_svc
from ..video.scene_detect import DetectedShot
from ...config import get_settings
from ...schemas import (
    HighlightItem,
    ImprovementItem,
    PackagingProfile,
    RhythmCurve,
    SampleAnalysis,
    SampleManifest,
    Section,
    SectionRole,
    Shot,
    ShotTarget,
    Utterance,
    VideoUnderstanding,
    VideoType,
    role_is_closing,
    role_is_opening,
    role_is_peak,
)

log = logging.getLogger("seecript.agent.decompose")


# server/samples/<sample_id>/shot-NN.jpg 实际存在范围——agent 拿到 shot 索引后
# 先查盘上有没有对应的 jpg，没有就置 None，避免前端 404。
_SAMPLES_ROOT = Path(__file__).resolve().parents[3] / "samples"


def _user_decompose_root() -> Path:
    """用户上传待拆解视频的目录，与 routers/decompose.py:_user_uploads_root 等价。"""
    return get_settings().log_dir.parent / "var" / "uploads" / "decompose"


def _resolve_sample_dir(sample_id: str) -> tuple[Optional[Path], Optional[str]]:
    """根据 sample_id 找物理目录与 URL 前缀；找不到返回 (None, None)。

    - 内置/sys-* 样例 → server/samples/<sample_id>/，URL 前缀 /samples/<id>
    - user-* 上传样例 → server/var/uploads/decompose/<sample_id>/，URL 前缀 /uploads/decompose/<id>
    用于：① 抽 shot-NN.jpg 时确定写盘位置 ② 生成 shot.thumbnail_url。
    """
    if not sample_id:
        return None, None
    sys_dir = _SAMPLES_ROOT / sample_id
    if sys_dir.is_dir():
        return sys_dir, f"/samples/{sample_id}"
    user_dir = _user_decompose_root() / sample_id
    if user_dir.is_dir():
        return user_dir, f"/uploads/decompose/{sample_id}"
    return None, None


# 长视频（≤3min）适配的两个上限：
# - _MAX_SHOTS：PySceneDetect 在 3min 高密度剪辑下可能给 100+ shots，下游 _attach_frame_tags
#   的批数、_segment_with_roles 的文本 payload 都会跟着膨胀；先把最短相邻镜头合并到 80 个以内
# - _SEGMENT_LLM_MAX_IMAGES：seed-2.0-lite 单请求图像有事实上限（30 张左右就开始截断或丢图），
#   _segment_with_roles 之前是把全部缩略图都喂进去；超过这个数就均匀采样代表帧，
#   文字 shot 列表仍是全集——LLM 按 shot_index 输出标注即可。
_MAX_SHOTS = 80
_SEGMENT_LLM_MAX_IMAGES = 20


def _compact_shots(raw_shots: list[DetectedShot]) -> list[DetectedShot]:
    """超过 _MAX_SHOTS 时反复挑出"最短的那一段"和它较短的邻居合并，直到 ≤ _MAX_SHOTS。

    合并后保留前一段的 index/start 与后一段的 end，duration 重算。
    index 不重排（thumbnail 文件 shot-NN.jpg 用原 index 命名），合并掉的后段
    缩略图就此被丢弃——3min 视频下牺牲少数过短镜头的封面是可接受的。
    """
    if len(raw_shots) <= _MAX_SHOTS:
        return list(raw_shots)
    shots = list(raw_shots)
    while len(shots) > _MAX_SHOTS:
        # 最短一段 + 它较短的邻居
        min_i = min(range(len(shots)), key=lambda i: shots[i].duration)
        if min_i == 0:
            merge_j = 1
        elif min_i == len(shots) - 1:
            merge_j = len(shots) - 2
        else:
            merge_j = (
                min_i - 1
                if shots[min_i - 1].duration <= shots[min_i + 1].duration
                else min_i + 1
            )
        a, b = sorted((min_i, merge_j))
        merged = DetectedShot(
            index=shots[a].index,
            start=shots[a].start,
            end=shots[b].end,
            duration=shots[b].end - shots[a].start,
        )
        shots = shots[:a] + [merged] + shots[b + 1:]
    return shots


# 语义相似合并的阈值（stage-23）。
# 目标：物理切镜后同机位连续表达合一，让"分镜"颗粒接近用户主观感知。
# 保守：tags 集合 Jaccard 高 + 时长合计够短 + 总数最多压到原 50%。
_MERGE_JACCARD = 0.6
_MERGE_MAX_DURATION = 8.0
_MERGE_MIN_RATIO = 0.5  # 最多压到 50%（防过度合并）


def _shot_thumbnail_url(sample_id: str, index: int) -> Optional[str]:
    """返回 shot 缩略图 URL（如果文件已存在）。

    sys-* 走 /samples/<id>/shot-NN.jpg，user-* 走 /uploads/decompose/<id>/shot-NN.jpg。
    用户上传的 sample 没预生成缩略图——下游 _extract_shot_thumbnails 抽完后这里才会有结果。
    """
    if not sample_id:
        return None
    sample_dir, url_prefix = _resolve_sample_dir(sample_id)
    if sample_dir is None or url_prefix is None:
        return None
    candidate = sample_dir / f"shot-{index:02d}.jpg"
    if candidate.is_file():
        return f"{url_prefix}/shot-{index:02d}.jpg"
    return None


async def _extract_shot_thumbnails(
    sample_id: str,
    video_path: Optional[str | Path],
    raw_shots: list[DetectedShot],
) -> int:
    """给每个 shot 抽中点关键帧到 sample 目录的 shot-NN.jpg。

    没有 video_path、ffmpeg 不可用、目录不存在时静默跳过（后续 _shot_thumbnail_url 返回 None，
    前端"无图"占位）。返回成功抽出的帧数，供 caller 打日志。

    必须在 detect_shots 之后、_attach_frame_tags 之前调——否则多模态 LLM 拿不到图，
    会幻觉"纯色灰色背景/无内容"那种烂标签（这是用户上线后实际反馈的痛点）。
    """
    if not video_path or not raw_shots:
        return 0
    p = Path(str(video_path))
    if not p.is_file():
        return 0
    sample_dir, _ = _resolve_sample_dir(sample_id)
    if sample_dir is None:
        log.warning("[decompose] sample dir 未找到 sample_id=%s,跳过抽帧", sample_id)
        return 0
    sample_dir.mkdir(parents=True, exist_ok=True)

    async def _one(s: DetectedShot) -> bool:
        # 中点抽帧——避开切镜头瞬间的过渡帧/黑帧。
        mid = max(0.05, (s.start + s.end) / 2.0)
        dst = sample_dir / f"shot-{s.index:02d}.jpg"
        try:
            await asyncio.to_thread(ffmpeg_svc.extract_frame, p, mid, dst)
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("[decompose] extract_frame 失败 idx=%d t=%.2fs: %s", s.index, mid, exc)
            return False

    results = await asyncio.gather(*[_one(s) for s in raw_shots])
    ok = sum(1 for r in results if r)
    log.info("[decompose] 抽帧完成 sample=%s 成功=%d/%d → %s",
             sample_id, ok, len(raw_shots), sample_dir)
    return ok


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
    "\"archetype\": str(≤20字, 一句话定性这视频的原型；例：『艺术展宣传』『带货种草』『城市Vlog』『信息可视化解释』『教程演示』『盘点合集』), "
    "\"narrative_summary\": str(≤80字, 一段话讲清整支视频在说什么、怎么说), "
    "\"structural_pattern\": one of [dramatic/stepwise/listicle/atmospheric/info_dense/vlog]（"
    "dramatic 戏剧弧（钩子→发展→高潮→收尾）；"
    "stepwise 步骤教程（intro→step_1→step_2→…→recap）；"
    "listicle 盘点合集（hook→item_1→item_2→…→closer）；"
    "atmospheric 氛围/纪录有峰值（establish→flow→peak→resolve）；"
    "info_dense 信息密集（title_card→info_block→info_block→payoff）；"
    "vlog 日常 Vlog 无明确高潮（intro_scene→daily_1→daily_2→…→wrap_up）——"
    "如果整支视频情绪/视觉始终平稳没有明显爆点，宁可选 vlog 也别硬塞 climax/peak"
    "), "
    "\"tempo\": one of [slow/medium/fast/peak/deceleration]，可选, "
    "\"estimated_segments\": int(2-8, 你估计该切成几段；listicle 上限 8 其它一般 3-6), "
    "\"tone\": str(≤15字, 基调；例：『冷静克制』『高燃热血』『诙谐自嘲』『庄重正式』)"
    "}。\n"
    "注意：先判 structural_pattern——按视频实际叙事方式选，别套 dramatic 模板。"
    "教程类一定是 stepwise，盘点类一定是 listicle，"
    "明显有情绪/视觉爆点的氛围片是 atmospheric，没有爆点的日常 Vlog 落 vlog，"
    "信息可视化/纯数据是 info_dense，常规带货/故事广告才是 dramatic。"
)


_SHOT_ROLE_SYSTEM = (
    "你是短视频结构分析师。给定视频画像（含 structural_pattern）和按时间排序的镜头列表，"
    "为**每个镜头**标注它在叙事中的角色和主题。\n\n"
    "角色（role）必须严格按 structural_pattern 来选：\n"
    "- dramatic：opening / development / climax / closing\n"
    "- stepwise：intro / step_1 / step_2 / step_3 / ... / recap（不超过 step_8）\n"
    "- listicle：hook / item_1 / item_2 / ... / closer（不超过 item_8）\n"
    "- atmospheric：establish / flow / peak / resolve\n"
    "- info_dense：title_card / info_block / payoff\n"
    "- vlog：intro_scene / daily_1 / daily_2 / ... / wrap_up（不超过 daily_8）\n\n"
    "硬约束：\n"
    "1. 第一个镜头必须是开场类（dramatic→opening；stepwise→intro；listicle→hook；atmospheric→establish；info_dense→title_card；vlog→intro_scene）\n"
    "2. 最后一个镜头必须是收尾类（dramatic→closing；stepwise→recap；listicle→closer；atmospheric→resolve；info_dense→payoff；vlog→wrap_up）\n"
    "3. 中间镜头不能是开场/收尾类\n"
    "4. dramatic/atmospheric 整支视频最多 1 个峰值类镜头（climax/peak），其余模式无峰值类（vlog 显式无峰值——不允许出 climax/peak）\n"
    "5. stepwise 的 step_N / listicle 的 item_N / vlog 的 daily_N 必须按 1/2/3… 顺序递增，N 不能跳号\n"
    "6. 相邻同 role 镜头会被合并为一个段落——所以最终段落数 ≤ 镜头数\n\n"
    "theme: 中文短标签（≤10 字），反映这个镜头真实在讲什么——"
    "不要照抄 role，要从画面/口播内容里提炼（例如 step_1 的 theme 可以是『准备食材』，daily_2 可以是『街角咖啡』）。\n\n"
    "返回 JSON：{\"shot_roles\": [{\"shot_index\": int, \"role\": str, \"theme\": str}]}\n"
    "数组长度必须等于镜头数，按 shot_index 升序排列。"
)


_FRAME_TAG_SYSTEM = (
    "你是短视频画面打标助手。输入是一组按时间排序的关键帧。\n"
    "请按以下三件事处理每一帧：\n"
    "1) 给 3-5 个标签（封面风格 / 转场类型 / 物体场景 / 主体动作 任选维度,中文短词）。\n"
    "2) 如果画面里有任何文字——烧录字幕、标题、口播字卡、商品/数据 HUD、横幅、水印外的有效文字——"
    "**逐字摘录**到 tags 里,前缀 '字幕:' 或 '标题:'(短一点的文字也要抓,不要只标『有字幕』);"
    "无文字才允许标 '无字幕'。\n"
    "3) 判定字幕样式 subtitle_style:大字加描边 / 小字白底 / 综艺花字 / 角标小字 / 无字幕,任一即可。\n\n"
    "硬约束:\n"
    "- 不准用『纯色背景』『占位帧』『无内容』『空白』『静态过渡』之类标签——"
    "如果你看到的是这种,几乎一定是图加载失败,请只回 'frame_id' 而 tags 留空数组,subtitle_style 标 '未识别'。\n"
    "- tags 必须从画面真实内容来,禁止套模板。\n"
    "返回 JSON:{\"frame_tags\": [{\"frame_id\": str, \"tags\": [str], \"subtitle_style\": str}]}"
)


# ----------------------------- 主流水线 --------------------------------------

async def decompose(
    sample_id: str,
    *,
    job_id: Optional[str] = None,
    video_path: Optional[str | Path] = None,
    title: str = "",
    video_type: VideoType = "marketing",
    reference_asset_ids: Optional[list[str]] = None,
    nl_prompt: Optional[str] = None,
    replace_slot: Optional[str] = None,
    persist: bool = False,
) -> SampleManifest:
    """完整拆解流水线。每一步失败都降级为 mock 数据但不中断。

    job_id 提供时通过 JobStore 推进度；不提供时纯函数式跑通。
    reference_asset_ids 是用户素材库参考素材（图/视频抽帧），在视频理解阶段作为额外视觉上下文。
    nl_prompt 是用户对本次拆解的自由文本指引（『更看重开场』『压短结尾』之类），
    注入到视频理解 + 段落切分的 LLM user message 末尾。
    replace_slot 给版本槽满时用——告诉 manifest_store.create_version 替换哪个旧槽。
    persist (stage-15)：False（默认）= 草稿模式，跑完 SSE done 直接推 manifest 给前端 zustand，
        不写盘；用户要保存时再走 POST /sample/{id}/manifest/save。
        True = 老行为，跑完直接 create_version 入版本槽。
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
    # 长视频降密：超过 _MAX_SHOTS 把最短相邻镜头合并到上限内，控制下游 LLM payload
    raw_count = len(raw_shots)
    raw_shots = _compact_shots(raw_shots)
    if len(raw_shots) < raw_count:
        log.info("[decompose] compact shots %d → %d (max=%d)",
                 raw_count, len(raw_shots), _MAX_SHOTS)
        push("scene_detect", 10, {
            "note": f"镜头过多，合并最短相邻 {raw_count} → {len(raw_shots)}",
            "raw_count": raw_count,
            "compacted_count": len(raw_shots),
        })

    # ---- 1.5 抽 shot-NN.jpg 缩略图（用户上传 sample 必走，sys-* 已预生成的会直接覆盖更新）----
    # 下游多模态打标 / 段落切分 / 视频理解都要喂图,缺图会幻觉"纯色灰色背景/无内容"。
    push("scene_detect", 14, {"note": "抽镜头中点关键帧"})
    extracted = await _extract_shot_thumbnails(sample_id, video_path, raw_shots)
    if extracted > 0:
        push("scene_detect", 18, {
            "note": f"抽帧 {extracted}/{len(raw_shots)} 张",
            "extracted": extracted,
        })

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
        cut_density=[],  # R1 改版后弃用,前端不再消费
        bgm_energy=audio.rms_energy,
        tempo_bpm=None,  # R1 改版后弃用,前端不再消费
    )
    total_duration = audio.duration_seconds or (raw_shots[-1].end if raw_shots else 30.0)

    # ---- 2.5 LLM 音频理解（v3 新增）----
    # 把样例视频音轨抽到 samples/<sid>/audio.mp3，借 /samples 静态挂载给 doubao 拉，
    # 再走 analyze_bgm_with_llm 出 energy_shape / climaxes / calm_segments / overall_advice。
    # 任何异常都降级为 None（前端兜底 BPM + 单点 peak），不阻断后续流水线。
    audio_understanding = await _llm_audio_understand(
        sample_id=sample_id,
        video_path=video_path,
        total_duration=total_duration,
        title=title,
        nl_prompt=nl_prompt,
        push=push,
    )

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

    # ---- 4b. 语义相似合并（stage-23）：把"同机位连续表达"合一 ----
    pre_merge = len(shots)
    shots = _merge_similar_shots(shots)
    if len(shots) < pre_merge:
        log.info("[decompose] semantic merge %d → %d", pre_merge, len(shots))
        push("vlm_tag", 70, {
            "note": f"语义合并相似分镜 {pre_merge} → {len(shots)}",
            "pre_merge": pre_merge,
            "post_merge": len(shots),
        })

    # ---- 5. 视频理解（v2 新增）----
    push("video_understand", 80, {"note": "LLM 视频画像：archetype / narrative / segments / tone"})
    understanding = await _video_understand(
        shots, video_type, has_voice, total_duration,
        reference_asset_ids=reference_asset_ids,
        nl_prompt=nl_prompt,
    )
    push("video_understand", 84, {
        "archetype": understanding.archetype,
        "tone": understanding.tone,
        "structural_pattern": understanding.structural_pattern,
        "estimated_segments": understanding.estimated_segments,
    })

    # ---- 5b. stage-23：每镜画面描述 + 脚本（含清洗 / 代字幕）----
    push("visual_script", 88, {"note": "LLM 给每镜写 visual + script"})
    try:
        await _attach_visual_and_script(shots, has_voice, tone_hint=understanding.tone or "")
    except Exception as exc:  # noqa: BLE001
        log.warning("[decompose] _attach_visual_and_script outer failure: %s", exc)
        for sh in shots:
            if not sh.visual_summary:
                sh.visual_summary = ("/".join((sh.tags or [])[:3]))[:120]
            if not sh.script:
                sh.script = (sh.transcript or "")[:200]
            if not sh.subject and sh.tags:
                sh.subject = "·".join(sh.tags[:2])[:40]

    # ---- 6. LLM 角色+主题切段（v2 重构）----
    push("llm_section", 93, {"note": f"LLM 段落分析 · 基于画像（{understanding.structural_pattern}）切 {understanding.estimated_segments} 段"})
    sections = await _segment_with_roles(
        shots, total_duration, understanding, has_voice,
        nl_prompt=nl_prompt,
    )

    # ---- 6b. R1：基于段落结构计算情绪走势 + BGM 契合度 ----
    mood_curve = _build_mood_curve(rhythm.times, sections)
    fit_score, fit_note = _bgm_fit(rhythm.bgm_energy, mood_curve)
    rhythm = rhythm.model_copy(update={
        "mood_curve": mood_curve,
        "bgm_fit_score": fit_score,
        "bgm_fit_note": fit_note,
    })

    # ---- 6c. stage-23 全片复盘：亮点 + 改进 + 总评 ----
    push("video_analysis", 96, {"note": "LLM 全片复盘：亮点 / 改进建议 / 总评"})
    analysis = await _video_analysis(understanding, sections, shots, audio_understanding)
    push("video_analysis", 97, {
        "highlights": len(analysis.highlights),
        "improvements": len(analysis.improvements),
        "overall_score": analysis.overall_score,
    })

    # ---- 6d. stage-28 LLM 多信号情绪曲线：综合段落 + 镜头 + BGM + 全片复盘 ----
    push("emotion_curve", 98, {"note": "LLM 综合打分情绪曲线"})
    try:
        from .emotion_agent import score_emotion as _score_emotion
        emotion = await _score_emotion(
            sections=sections,
            shots=shots,
            total_duration=total_duration,
            bgm_analysis=audio_understanding,
            bgm_energy=rhythm.bgm_energy,
            bgm_times=rhythm.times,
            understanding=understanding,
            sample_analysis=analysis,
            intent=None,  # 拆解阶段无用户意图
        )
        rhythm = rhythm.model_copy(update={"emotion": emotion})
        push("emotion_curve", 99, {
            "backend": emotion.backend,
            "anchors": len(emotion.anchors),
            "peaks": len(emotion.peaks),
            "valleys": len(emotion.valleys),
        })
    except Exception as exc:  # noqa: BLE001
        log.warning("[decompose] emotion scoring outer failure: %s", exc)

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
        audio_understanding=audio_understanding,
        analysis=analysis,
    )

    # stage-15: persist=False 是默认草稿模式——SSE done 把 manifest 推到前端 zustand,
    # 用户在 Decompose 页点「保存到资产库」时才走 POST /sample/{id}/manifest/save 落槽。
    # persist=True 走老行为(直接 create_version),供需要无人值守自动入库的内部场景使用。
    # video_url 改写在两种模式下都做——草稿态前端要立即播放,URL 必须正确。
    new_slot_id: Optional[str] = None
    if video_path:
        vp = Path(str(video_path)).resolve()
        if "uploads" in vp.parts and "decompose" in vp.parts:
            manifest = manifest.model_copy(update={
                "video_url": f"/uploads/decompose/{sample_id}/video.mp4",
            })
        if persist:
            try:
                new_slot_id = manifest_store.create_version(
                    sample_id, manifest, replace_slot=replace_slot, activate=True,
                )
            except manifest_store.SlotsFullError as exc:
                # 不应该到这里——路由层应当在 kickoff 前就拦下让用户选；走到这里说明并发或调用方忘了传 replace_slot。
                log.warning("[decompose] %s slots full at write time: %s", sample_id, exc)
            except (FileNotFoundError, OSError, ValueError) as exc:
                log.warning("[decompose] 写版本槽失败 %s: %s", sample_id, exc)

    if job_id:
        job_store.complete(job_id, payload={
            "sample_id": sample_id,
            "manifest": manifest.model_dump(),
            "slot_id": new_slot_id,
        })

    return manifest


# ----------------------------- LLM 音频理解子例程 -----------------------------

async def _llm_audio_understand(
    *,
    sample_id: str,
    video_path: Optional[str | Path],
    total_duration: float,
    title: Optional[str],
    nl_prompt: Optional[str],
    push,
):
    """抽样例视频音轨为 mp3 → doubao 多模态音频理解 → 返回 BGMAnalysis 或 None。

    任一环节失败都安静降级返 None：抽轨失败、配置缺失、LLM 异常都不能阻断主流水线，
    前端在没有 audio_understanding 时回落到 librosa 的 BPM + 单点 peak。
    """
    from ..video import ffmpeg as ffmpeg_svc
    from ..video.bgm_analysis import analyze_sample_audio_with_llm
    from ...schemas import BGMAnalysis

    push("audio_understand", 28, {"note": "音轨送多模态模型听整曲"})

    if not video_path:
        log.info("[decompose.audio_understand] 无 video_path，跳过 LLM 音频分析")
        return None

    audio_dst = _SAMPLES_ROOT / sample_id / "audio.mp3"
    try:
        audio_dst.parent.mkdir(parents=True, exist_ok=True)
        ffmpeg_svc.extract_audio_mp3(video_path, audio_dst)
    except Exception as exc:
        log.warning("[decompose.audio_understand] 抽音轨失败 sample=%s: %s", sample_id, exc)
        return None

    public_file_url = f"/samples/{sample_id}/audio.mp3"
    try:
        result_dict = await analyze_sample_audio_with_llm(
            file_url=public_file_url,
            duration_seconds=total_duration,
            sample_title=(title or sample_id),
            nl_prompt=(nl_prompt or "").strip(),
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("[decompose.audio_understand] LLM 调用异常 sample=%s: %s", sample_id, exc)
        return None

    if not result_dict:
        log.info("[decompose.audio_understand] LLM 未返回结果（mock/缺 key/失败），sample=%s", sample_id)
        return None

    try:
        analysis = BGMAnalysis(**result_dict)
    except Exception as exc:  # noqa: BLE001
        log.warning("[decompose.audio_understand] BGMAnalysis 反序列化失败 sample=%s: %s", sample_id, exc)
        return None

    push("audio_understand", 32, {
        "energy_shape": analysis.energy_shape,
        "climaxes": len(analysis.climaxes or []),
        "calm_segments": len(analysis.calm_segments or []),
    })
    log.info(
        "[decompose.audio_understand] ok sample=%s shape=%s climax=%d calm=%d",
        sample_id, analysis.energy_shape, len(analysis.climaxes or []), len(analysis.calm_segments or []),
    )
    return analysis


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


def _merge_similar_shots(shots: list[Shot]) -> list[Shot]:
    """stage-23：物理切镜后做一遍**语义相似合并**。

    相邻两 shot 同时满足：
    - tags 集合 Jaccard ≥ _MERGE_JACCARD
    - 时长合计 ≤ _MERGE_MAX_DURATION
    则合一；合并后 tags 取并集，transcript 拼接（中间补"。"），
    merged_from 累计被并入的原 indices（含原本镜的 index）。

    硬限：最多压到原数 50%（_MERGE_MIN_RATIO）；防止 LLM tag 抽风把全片合一锅。
    不调 LLM，纯标签+时长规则，省一次大模型调用、可解释、可手测。
    """
    if len(shots) <= 2:
        return shots
    floor = max(1, int(len(shots) * _MERGE_MIN_RATIO))
    out: list[Shot] = []
    for sh in shots:
        if not out:
            # 第一个：复制并初始化 merged_from
            out.append(sh.model_copy(update={"merged_from": list(sh.merged_from) or [sh.index]}))
            continue
        if len(out) + (len(shots) - shots.index(sh) - 1) < floor + 1:
            # 已经接近 floor，停止合并
            out.append(sh.model_copy(update={"merged_from": list(sh.merged_from) or [sh.index]}))
            continue
        prev = out[-1]
        # 计算 Jaccard
        s1 = set(prev.tags or [])
        s2 = set(sh.tags or [])
        if s1 and s2:
            jaccard = len(s1 & s2) / max(1, len(s1 | s2))
        else:
            jaccard = 0.0
        combined_dur = (sh.end - prev.start)
        if jaccard >= _MERGE_JACCARD and combined_dur <= _MERGE_MAX_DURATION:
            merged_tags = list(dict.fromkeys((prev.tags or []) + (sh.tags or [])))
            transcripts = [t for t in (prev.transcript, sh.transcript) if t]
            merged_transcript = "。".join(transcripts) if transcripts else None
            merged_from = list(prev.merged_from) + ([sh.index] if sh.index not in prev.merged_from else [])
            out[-1] = prev.model_copy(update={
                "end": sh.end,
                "duration": combined_dur,
                "tags": merged_tags,
                "transcript": merged_transcript,
                "merged_from": merged_from,
            })
        else:
            out.append(sh.model_copy(update={"merged_from": list(sh.merged_from) or [sh.index]}))

    if len(out) >= floor:
        return out
    # 兜底：合得过狠就回退
    return shots


# stage-23 prompts: 画面描述 + 脚本清洗
_VISUAL_SCRIPT_SYSTEM = (
    "你是短视频拆解助手。给定一组按时间排序的镜头（含缩略图、tags、可能的口播片段），"
    "为**每个镜头**生成四个字段：\n"
    "- subject：本镜画面**主体对象**——必须是具象名词（人/物/场景），≤14 中文字。\n"
    "    · ✅ 写法：『青铜器残片特写』『主播正脸』『展厅长廊』『红色运动鞋』\n"
    "    · ❌ 严禁比喻 / 上位词 / 营销修饰：『国宝碎片』(改→『青铜器残片特写』)、"
    "『潮品』(改→『红色运动鞋』)、『颜值担当』(改→『主播正脸』)\n"
    "    · 这个字段会**原样**喂给下游 AI 生图 prompt 作为锚点，**不允许使用任何会被同义化、误读的词**\n"
    "- visual：≤60 中文字，描述这一镜的画面主体/动作/构图；不要照抄 tags，要写画面在演什么\n"
    "    · 同样要用具象表达，subject 出现的词要原样保留，不要换成同义词\n"
    "- script：≤80 中文字，本镜的口播或代字幕文案。\n"
    "    · 有 transcript 时：清洗（去 'emm/啊/呃' 这类口语停顿、修标点）后填进去\n"
    "    · 无 transcript 时（纯 BGM 视频）：根据画面 + 整片调性写一句『代字幕』参考文案，不要瞎编台词\n"
    "- targets：本镜的目标分布（数组，0-4 个；可空数组）。每个目标 = {\n"
    "    kind: person/object/scene/text/graphic/other,\n"
    "    name: 简短名（≤12 中文字，如『主播』『青铜鼎』『展厅全景』『品牌字』『莫比乌斯环』），\n"
    "    role: primary/secondary/background（可空，主体留 primary）,\n"
    "    visual_hint: 视觉特征（≤40 字，可空）}\n"
    "  · 多目标场景必须分开列：带货镜常含 `[人物-主播, 物品-商品]`；文物展常含 `[物品-文物, 文字-展名]`\n"
    "  · graphic 类用于纯动效图形（如莫比乌斯环、几何形状），下游 plan_agent 会**重写**为目标域\n"
    "  · 单纯空镜 / 转场 / 抽象图形的镜头可以返 [] 空数组\n"
    "返回 JSON：{\"items\": [{\"shot_index\": int, \"subject\": str, \"visual\": str, \"script\": str, \"targets\": [...]}, ...]}\n"
    "items 长度等于镜头数，按 shot_index 升序。"
)


async def _attach_visual_and_script(shots: list[Shot], has_voice: bool, tone_hint: str = "") -> None:
    """stage-23：一次 LLM 调用给所有 shot 填 visual_summary + script。

    任意失败都降级 per-shot：visual = (tags[:3] 拼接)；script = (transcript or "")。
    """
    if not shots:
        return
    llm = get_llm_client()

    lines: list[str] = []
    for sh in shots:
        speech = sh.transcript or ""
        tags = "/".join(sh.tags) if sh.tags else ""
        line = f"#{sh.index} {sh.start:.1f}-{sh.end:.1f}s ({sh.duration:.1f}s)"
        if tags:
            line += f" | tags: {tags}"
        if speech:
            line += f" | transcript: {speech[:60]}"
        lines.append(line)

    voice_hint = "有口播（用 transcript 清洗为 script）" if has_voice else "纯 BGM 无口播（script 为代字幕参考）"
    user = (
        f"整体调性：{tone_hint or '通用'}；声音情况：{voice_hint}\n\n"
        f"镜头列表（{len(shots)} 个）：\n" + "\n".join(lines)
    )

    # ≤20 张缩略图给多模态模型；超出走采样代表帧
    if len(shots) <= 20:
        sampled_indices = list(range(len(shots)))
    else:
        step = len(shots) / 20
        sampled_indices = [int(i * step) for i in range(20)]
    images = [shots[i].thumbnail_url or "" for i in sampled_indices]

    items: list[dict] = []
    try:
        text = await llm.complete_multimodal(_VISUAL_SCRIPT_SYSTEM, user, images)
        data = _extract_json(text)
        if isinstance(data, dict):
            raw = data.get("items") or []
            if isinstance(raw, list):
                items = [it for it in raw if isinstance(it, dict)]
    except Exception as exc:  # noqa: BLE001
        log.warning("[decompose] visual_script LLM failed, falling back: %s", exc)

    by_index = {int(it.get("shot_index", -1)): it for it in items if "shot_index" in it}

    for sh in shots:
        item = by_index.get(sh.index, {})
        subject = str(item.get("subject", "") or "").strip()[:40]
        visual = str(item.get("visual", "") or "").strip()[:120]
        script = str(item.get("script", "") or "").strip()[:200]
        if not visual:
            visual = ("/".join((sh.tags or [])[:3]))[:120]
        if not script:
            script = (sh.transcript or "")[:200]
        sh.subject = subject  # 失败时落空，下游 AIGC prompt 会回退到 visual + tags 拼接
        sh.visual_summary = visual
        sh.script = script
        # stage-25：解析 targets 字段。失败 / 缺字段 → 留空 list（"可以没有"）
        raw_targets = item.get("targets") or []
        parsed: list[ShotTarget] = []
        if isinstance(raw_targets, list):
            for t in raw_targets[:4]:
                if not isinstance(t, dict):
                    continue
                name = str(t.get("name") or "").strip()[:24]
                if not name:
                    continue
                kind = str(t.get("kind") or "object").strip().lower()
                if kind not in ("person", "object", "scene", "text", "graphic", "other"):
                    kind = "other"
                role_val = t.get("role")
                role: Optional[str] = None
                if isinstance(role_val, str) and role_val in ("primary", "secondary", "background"):
                    role = role_val
                hint = t.get("visual_hint")
                hint = str(hint).strip()[:80] if isinstance(hint, str) and hint.strip() else None
                try:
                    parsed.append(ShotTarget(kind=kind, name=name, role=role, visual_hint=hint))  # type: ignore[arg-type]
                except Exception:  # noqa: BLE001
                    continue
        sh.targets = parsed
        # subject fallback：解析失败时从 targets[primary] / tags 兜底
        if not sh.subject:
            primary = next((t for t in sh.targets if t.role == "primary"), None) or (sh.targets[0] if sh.targets else None)
            if primary is not None:
                sh.subject = primary.name[:40]
            elif sh.tags:
                sh.subject = "·".join(sh.tags[:2])[:40]


# stage-23 全片复盘 prompt
_VIDEO_ANALYSIS_SYSTEM = (
    "你是短视频复盘专家。给定视频画像 + 段落结构 + 各分镜画面与脚本 + BGM 走势，"
    "输出全片亮点与改进建议。\n\n"
    "亮点（highlights）≤6 条；改进建议（improvements）≤6 条；不足可少。\n"
    "每条 aspect 取自：hook（钩子）/ narrative（叙事）/ visual（视觉）/ audio（声音/BGM）/ "
    "rhythm（节奏）/ copy（文案）/ cta（行动呼吁），improvements 多一个 'structure' 选项（结构问题）。\n"
    "text ≤40 中文字描述这条是什么；improvements 还要 suggestion ≤60 中文字写「具体怎么改」。\n"
    "可选 shot_indices（数组，相关分镜 index），不知道就给空数组。\n\n"
    "返回 JSON：{"
    "\"highlights\": [{aspect, text, shot_indices}], "
    "\"improvements\": [{aspect, text, suggestion, shot_indices}], "
    "\"overall_score\": int 0-100（综合质量，60-75 是常见区间）, "
    "\"one_line_verdict\": str ≤30 中文字一句话总评"
    "}"
)


_VALID_HIGHLIGHT_ASPECTS = {"hook", "narrative", "visual", "audio", "rhythm", "copy", "cta"}
_VALID_IMPROVEMENT_ASPECTS = _VALID_HIGHLIGHT_ASPECTS | {"structure"}


async def _video_analysis(
    understanding: VideoUnderstanding,
    sections: list[Section],
    shots: list[Shot],
    audio_understanding,
) -> SampleAnalysis:
    """stage-23：跑完段落后做一次全片复盘，给亮点 + 改进 + 总评。失败安静返默认空。"""
    llm = get_llm_client()

    # 构建 user content：理解 + 段落 + 镜头摘要 + BGM
    section_lines: list[str] = []
    for i, sec in enumerate(sections):
        section_lines.append(
            f"段{i + 1} [{sec.role}] {sec.theme or ''} {sec.start:.1f}-{sec.end:.1f}s | {sec.summary[:60]}"
        )

    shot_lines: list[str] = []
    for sh in shots[:60]:  # 防 prompt 爆炸；60 镜以上截断
        line = f"#{sh.index} {sh.start:.1f}-{sh.end:.1f}s | {sh.visual_summary[:50]}"
        if sh.script:
            line += f" | 词: {sh.script[:40]}"
        shot_lines.append(line)
    if len(shots) > 60:
        shot_lines.append(f"... (后续 {len(shots) - 60} 镜略)")

    bgm_hint = ""
    if audio_understanding is not None:
        bgm_hint = (
            f"BGM 走向：energy_shape={getattr(audio_understanding, 'energy_shape', '?')}; "
            f"climaxes={len(getattr(audio_understanding, 'climaxes', None) or [])}; "
            f"calm_segments={len(getattr(audio_understanding, 'calm_segments', None) or [])}"
        )

    user = (
        f"视频原型：{understanding.archetype} | 调性：{understanding.tone}\n"
        f"叙事模式：{understanding.structural_pattern} | 估计段数：{understanding.estimated_segments}\n"
        f"narrative：{understanding.narrative_summary}\n"
        f"{bgm_hint}\n\n"
        f"段落（{len(sections)}）：\n" + "\n".join(section_lines) + "\n\n"
        f"分镜（{len(shots)}）：\n" + "\n".join(shot_lines)
    )

    try:
        text = await llm.complete(_VIDEO_ANALYSIS_SYSTEM, user)
        data = _extract_json(text)
        if not isinstance(data, dict):
            return SampleAnalysis()

        highlights: list[HighlightItem] = []
        for it in (data.get("highlights") or [])[:6]:
            if not isinstance(it, dict):
                continue
            aspect = str(it.get("aspect", "") or "").strip().lower()
            if aspect not in _VALID_HIGHLIGHT_ASPECTS:
                continue
            text_v = str(it.get("text", "") or "").strip()[:80]
            if not text_v:
                continue
            indices_raw = it.get("shot_indices") or []
            indices = [int(x) for x in indices_raw if isinstance(x, (int, float))] if isinstance(indices_raw, list) else []
            highlights.append(HighlightItem(aspect=aspect, text=text_v, shot_indices=indices))  # type: ignore[arg-type]

        improvements: list[ImprovementItem] = []
        for it in (data.get("improvements") or [])[:6]:
            if not isinstance(it, dict):
                continue
            aspect = str(it.get("aspect", "") or "").strip().lower()
            if aspect not in _VALID_IMPROVEMENT_ASPECTS:
                continue
            text_v = str(it.get("text", "") or "").strip()[:80]
            sugg = str(it.get("suggestion", "") or "").strip()[:120]
            if not text_v or not sugg:
                continue
            indices_raw = it.get("shot_indices") or []
            indices = [int(x) for x in indices_raw if isinstance(x, (int, float))] if isinstance(indices_raw, list) else []
            improvements.append(ImprovementItem(  # type: ignore[arg-type]
                aspect=aspect, text=text_v, suggestion=sugg, shot_indices=indices,
            ))

        try:
            score = int(data.get("overall_score", 70))
        except (TypeError, ValueError):
            score = 70
        score = max(0, min(100, score))
        verdict = str(data.get("one_line_verdict", "") or "").strip()[:60]

        return SampleAnalysis(
            highlights=highlights,
            improvements=improvements,
            overall_score=score,
            one_line_verdict=verdict,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("[decompose] _video_analysis failed: %s", exc)
        return SampleAnalysis()


# ----------------------------- 视频理解子例程 ---------------------------------

async def _video_understand(
    shots: list[Shot],
    video_type: VideoType,
    has_voice: bool,
    total_duration: float,
    *,
    reference_asset_ids: Optional[list[str]] = None,
    nl_prompt: Optional[str] = None,
) -> VideoUnderstanding:
    """LLM 给整支视频的语义画像。失败时按 video_type 兜底。

    sample 一组（不超过 12 张）关键帧给 LLM——太多会撑爆 max_tokens；
    采样策略：均匀采样代表性帧，覆盖首中尾。
    reference_asset_ids 给的参考图/参考视频抽帧会拼到 images 末尾，作为
    『用户希望对齐的视觉气质』提示——但样例本身的镜头才是分析对象。
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

    # 拼参考素材到末尾（如果用户提供了）。参考帧不是样例本身的内容，
    # 仅供 LLM 感知"用户期望的对齐气质"。控制单次 ≤6 张避免 token 爆。
    ref_urls = resolve_reference_image_urls(reference_asset_ids or [], max_total=6)
    if ref_urls:
        images = images + ref_urls
        user += (
            f"\n\n附带 {len(ref_urls)} 张『用户参考画面』——来自用户素材库，"
            f"不属于样例视频，仅作为用户希望对齐的视觉气质提示。请仍以样例镜头为主体做分析。"
        )

    if nl_prompt:
        # 用户的自由文本指引（来自"重新生成"对话框）。注入到 user message 末尾——
        # 让 LLM 在做画像 / 切段时偏向用户期望，但不替换硬约束。
        user += f"\n\n【用户额外要求】{nl_prompt.strip()[:300]}"
    try:
        text = await llm.complete_multimodal(_UNDERSTAND_SYSTEM, user, images)
        data = _extract_json(text)
        if isinstance(data, dict):
            archetype = str(data.get("archetype", "") or "").strip()[:40]
            narrative = str(data.get("narrative_summary", "") or "").strip()[:200]
            try:
                seg_count = int(data.get("estimated_segments", data.get("suggested_segments", 4)))
            except (TypeError, ValueError):
                seg_count = 4
            seg_count = max(2, min(8, seg_count))
            tone = str(data.get("tone", "") or "").strip()[:30]
            pattern_raw = str(data.get("structural_pattern", "") or "").strip().lower()
            valid_patterns = {"dramatic", "stepwise", "listicle", "atmospheric", "info_dense", "vlog"}
            pattern = pattern_raw if pattern_raw in valid_patterns else "dramatic"
            tempo_raw = str(data.get("tempo", "") or "").strip().lower()
            valid_tempos = {"slow", "medium", "fast", "peak", "deceleration"}
            tempo = tempo_raw if tempo_raw in valid_tempos else None
            if archetype and narrative:
                return VideoUnderstanding(
                    archetype=archetype,
                    narrative_summary=narrative,
                    structural_pattern=pattern,  # type: ignore[arg-type]
                    tempo=tempo,  # type: ignore[arg-type]
                    estimated_segments=seg_count,
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
        structural_pattern="dramatic",
        estimated_segments=seg,
        tone=tone,
    )


# ----------------------------- 段落分析子例程 ---------------------------------

def _default_role_for(pattern: str, role_class: str, *, ordinal: int = 1) -> tuple[str, str]:
    """按 pattern 返回某一角色类的默认 role 字符串 + 中文 theme。

    `ordinal` 仅 stepwise/listicle 的 main 类用——step_N / item_N 顺序编号；其他模式忽略。

    Stage-16 起 _assign_shot_roles / _fallback_shot_roles 走这条路而不是硬编码 dramatic。
    """
    table: dict[str, dict[str, tuple[str, str]]] = {
        "dramatic":    {"opening": ("opening", "开场"), "main": ("development", "铺陈"), "peak": ("climax", "高潮"), "closing": ("closing", "收尾")},
        "stepwise":    {"opening": ("intro", "引入"),    "main": (f"step_{ordinal}", f"步骤 {ordinal}"), "peak": ("step_1", "步骤"), "closing": ("recap", "总结")},
        "listicle":    {"opening": ("hook", "钩子"),     "main": (f"item_{ordinal}", f"第 {ordinal} 项"), "peak": ("item_1", "重点项"), "closing": ("closer", "收束")},
        "atmospheric": {"opening": ("establish", "起势"), "main": ("flow", "流转"),       "peak": ("peak", "顶点"),   "closing": ("resolve", "余韵")},
        "info_dense":  {"opening": ("title_card", "标题"), "main": ("info_block", "信息块"), "peak": ("payoff", "落版"), "closing": ("payoff", "落版")},
        "vlog":        {"opening": ("intro_scene", "开场"), "main": (f"daily_{ordinal}", f"日常 {ordinal}"), "peak": (f"daily_{ordinal}", "日常"), "closing": ("wrap_up", "收尾")},
    }
    return table.get(pattern, table["dramatic"]).get(role_class, table["dramatic"]["main"])



async def _segment_with_roles(
    shots: list[Shot],
    total: float,
    understanding: VideoUnderstanding,
    has_voice: bool,
    *,
    nl_prompt: Optional[str] = None,
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
        f"structural_pattern：{understanding.structural_pattern}\n"
        f"tempo：{understanding.tempo or '(未指定)'}\n"
        f"镜头总数：{len(shots)}\n"
        f"总时长：{total:.1f} 秒\n"
        f"{voice_hint}\n\n"
        "镜头列表（请为每一个镜头给出 role + theme，role 必须符合上面声明的 structural_pattern）：\n" + "\n".join(payload_lines)
    )

    # 长视频降图：seed-2.0-lite 单请求图像过多会被截断；镜头数 > 上限时均匀采样代表帧，
    # 文字镜头列表仍是全集——让 LLM 按 shot_index 输出每一个镜头的标注。
    if len(shots) <= _SEGMENT_LLM_MAX_IMAGES:
        images = [sh.thumbnail_url or "" for sh in shots]
    else:
        step = len(shots) / _SEGMENT_LLM_MAX_IMAGES
        sampled_idx = [int(i * step) for i in range(_SEGMENT_LLM_MAX_IMAGES)]
        # 确保覆盖首末镜头
        if sampled_idx[0] != 0:
            sampled_idx[0] = 0
        if sampled_idx[-1] != len(shots) - 1:
            sampled_idx[-1] = len(shots) - 1
        images = [shots[i].thumbnail_url or "" for i in sampled_idx]
        sampled_shot_indices = [shots[i].index for i in sampled_idx]
        user += (
            f"\n\n注意：由于镜头较多，附带的 {len(images)} 张缩略图是均匀采样的代表帧"
            f"（对应 shot_index = {sampled_shot_indices}），不是每个镜头都有图。"
            f"请仍按上方文字列表里全部 {len(shots)} 个 shot_index 输出标注。"
        )

    if nl_prompt:
        # 用户的自由文本指引——告诉 LLM 在 role/theme 选择上向哪边偏。
        # 注入位置在硬约束之后，避免覆盖"首=opening 尾=closing"的结构稳定性。
        user += f"\n\n【用户额外要求】{nl_prompt.strip()[:300]}"

    pattern = understanding.structural_pattern

    try:
        text = await llm.complete_multimodal(_SHOT_ROLE_SYSTEM, user, images)
        data = _extract_json(text)
        raw = data.get("shot_roles", []) if isinstance(data, dict) else []

        # 用 index → (role, theme) 映射，方便按镜头顺序回填
        # Stage-16 起 role 是自由字符串（要支持 step_N/item_N），仅做长度/空校验
        roles_by_index: dict[int, tuple[SectionRole, str]] = {}
        for item in raw:
            if not isinstance(item, dict):
                continue
            try:
                idx = int(item.get("shot_index"))
            except (TypeError, ValueError):
                continue
            role = str(item.get("role", "") or "").strip()
            if not role or len(role) > 30:
                continue
            theme = str(item.get("theme", "") or "").strip()[:20]
            roles_by_index[idx] = (role, theme)

        if roles_by_index:
            shot_roles = _assign_shot_roles(shots, roles_by_index, pattern)
            return _merge_shot_roles_into_sections(shots, shot_roles)
    except Exception as exc:
        log.warning("segment_with_roles failed, using shot-first fallback: %s", exc)

    return _fallback_shot_roles(shots, pattern)


def _assign_shot_roles(
    shots: list[Shot],
    roles_by_index: dict[int, tuple[SectionRole, str]],
    pattern: str = "dramatic",
) -> list[tuple[SectionRole, str]]:
    """把 LLM 给的 {shot_index: (role, theme)} 映射成 [(role, theme)]（按 shots 顺序）。

    Stage-16 起 pattern-aware：用 STRUCTURAL_PATTERNS 判定首=开场类、尾=收尾类、
    中间不得为开场/收尾类、峰值类至多 1 个；越界镜头降级为该 pattern 的 main 角色。
    """
    out: list[tuple[SectionRole, str]] = []
    n = len(shots)
    open_role, open_theme = _default_role_for(pattern, "opening")
    close_role, close_theme = _default_role_for(pattern, "closing")

    # stepwise/listicle 的 main 是 step_N/item_N 顺序编号——用 step_counter 给中间镜头编号
    step_counter = 0
    for i, sh in enumerate(shots):
        if sh.index in roles_by_index:
            role, theme = roles_by_index[sh.index]
        else:
            if i == 0:
                role, theme = open_role, open_theme
            elif i == n - 1:
                role, theme = close_role, close_theme
            else:
                step_counter += 1
                role, theme = _default_role_for(pattern, "main", ordinal=step_counter)
        out.append((role, theme))

    # 强约束修正：首 = 开场类、尾 = 收尾类
    if n >= 1:
        first_role, first_theme = out[0]
        if not role_is_opening(first_role, pattern):
            out[0] = (open_role, first_theme or open_theme)
    if n >= 2:
        last_role, last_theme = out[-1]
        if not role_is_closing(last_role, pattern):
            out[-1] = (close_role, last_theme or close_theme)

    # 中间镜头：不允许开场/收尾类；峰值类至多 1 个
    peak_seen = 0
    mid_step = 0
    for i in range(1, n - 1):
        role, theme = out[i]
        if role_is_opening(role, pattern) or role_is_closing(role, pattern):
            mid_step += 1
            new_role, new_theme = _default_role_for(pattern, "main", ordinal=mid_step)
            out[i] = (new_role, theme or new_theme)
        elif role_is_peak(role, pattern):
            peak_seen += 1
            if peak_seen > 1:
                mid_step += 1
                new_role, new_theme = _default_role_for(pattern, "main", ordinal=mid_step)
                out[i] = (new_role, theme or new_theme)

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
    """老调用方兼容：根据 dramatic 角色名给中文短标签。

    Stage-16 起新代码应用 _default_role_for(pattern, ...) 取 (role, theme) 对。
    """
    return {
        "opening": "开场",
        "development": "铺陈",
        "climax": "高潮",
        "closing": "收尾",
    }.get(role, role)


def _fallback_shot_roles(shots: list[Shot], pattern: str = "dramatic") -> list[Section]:
    """无 LLM 时的 shot-first 兜底，按 pattern 给首/尾/中间角色。

    镜头总数 ≥ 4 且 pattern 有峰值类（dramatic/atmospheric）时，挑中间偏后的镜头当峰值；
    其他模式（stepwise/listicle/info_dense）中间镜头按 main 类编号铺开。
    """
    n = len(shots)
    if n == 0:
        return []
    open_role, open_theme = _default_role_for(pattern, "opening")
    close_role, close_theme = _default_role_for(pattern, "closing")
    if n == 1:
        sh = shots[0]
        return [Section(
            role=open_role, theme=open_theme, start=sh.start, end=sh.end,
            summary="单镜头视频", shot_indices=[sh.index],
        )]

    # 仅 dramatic/atmospheric 有峰值类
    peak_role, peak_theme = _default_role_for(pattern, "peak")
    has_peak = pattern in ("dramatic", "atmospheric")

    shot_roles: list[tuple[SectionRole, str]] = []
    peak_idx: Optional[int] = None
    if has_peak and n >= 4:
        peak_idx = int(n * 0.6)
        if peak_idx <= 0 or peak_idx >= n - 1:
            peak_idx = None

    step_counter = 0
    for i in range(n):
        if i == 0:
            shot_roles.append((open_role, open_theme))
        elif i == n - 1:
            shot_roles.append((close_role, close_theme))
        elif i == peak_idx:
            shot_roles.append((peak_role, peak_theme))
        else:
            step_counter += 1
            r, t = _default_role_for(pattern, "main", ordinal=step_counter)
            shot_roles.append((r, t))

    return _merge_shot_roles_into_sections(shots, shot_roles)


# ----------------------------- 高潮时间估算 ------------------------------------

def _compute_climax(
    sections: list[Section],
    rhythm: RhythmCurve,
    total_duration: float,
) -> Optional[float]:
    """估算高潮时间点（秒），用于前端节奏图上的 ReferenceLine。

    优先级：
    1. role=climax/peak 段的中点
    2. BGM 能量曲线峰值
    3. 总时长 60% 处（短视频经典高潮位）
    4. pattern 无峰值类（stepwise/listicle/info_dense/vlog）→ 返回 None,前端不画 ReferenceLine
    """
    if total_duration <= 0:
        return None

    for sec in sections:
        if sec.role in ("climax", "peak"):
            return float((sec.start + sec.end) / 2)

    # 没有显式峰值段——若 BGM 有能量峰值,沿用兜底（旧 dramatic plan 兼容）
    if rhythm.bgm_energy and rhythm.times:
        n = min(len(rhythm.bgm_energy), len(rhythm.times))
        if n > 0:
            peak_idx = max(range(n), key=lambda i: rhythm.bgm_energy[i])
            return float(rhythm.times[peak_idx])

    return float(total_duration * 0.6)


# ----------------------------- 情绪走势曲线 + BGM 契合度（R1） ---------------------

# 角色 → 情绪基准（0..1）。无峰值类模式（stepwise/listicle/info_dense/vlog）整片只在 0.3-0.5 间起伏。
_ROLE_MOOD_BASE: dict[str, float] = {
    # dramatic
    "opening": 0.35, "development": 0.40, "climax": 0.85, "closing": 0.30,
    # stepwise
    "intro": 0.35, "recap": 0.30,
    # listicle
    "hook": 0.40, "closer": 0.30,
    # atmospheric
    "establish": 0.35, "flow": 0.40, "peak": 0.80, "resolve": 0.30,
    # info_dense
    "title_card": 0.40, "info_block": 0.40, "payoff": 0.50,
    # vlog
    "intro_scene": 0.35, "wrap_up": 0.30,
}


def _role_mood_value(role: str) -> float:
    """role → mood 基准。step_N / item_N / daily_N 走 main 类默认。"""
    if role in _ROLE_MOOD_BASE:
        return _ROLE_MOOD_BASE[role]
    if role.startswith("step_"):
        return 0.40
    if role.startswith("item_"):
        return 0.42
    if role.startswith("daily_"):
        return 0.45
    return 0.40  # 未知 role 当 main 类


def _smooth(values: list[float], window: int) -> list[float]:
    """简单滑动平均（无 numpy 依赖）。window<2 时返回原值。"""
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


def _pearson(xs: list[float], ys: list[float]) -> Optional[float]:
    """两个等长序列的 Pearson 相关系数。退化情况返回 None。"""
    n = len(xs)
    if n < 3 or n != len(ys):
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    dx = sum((x - mx) ** 2 for x in xs) ** 0.5
    dy = sum((y - my) ** 2 for y in ys) ** 0.5
    if dx == 0 or dy == 0:
        return None
    return num / (dx * dy)


def _build_mood_curve(times: list[float], sections: list[Section]) -> list[float]:
    """按段落基准 + 滑动平均生成情绪走势曲线。

    - 每个采样时间点落到包含它的 section（按 start ≤ t < end），取该 role 的 mood_base
    - 全曲线滑动平均一遍（窗口 ~10% 采样数,至少 3）做低频平滑——避免段落跳变出现台阶
    """
    if not times or not sections:
        return []
    sorted_sec = sorted(sections, key=lambda s: s.start)

    def _mood_at(t: float) -> float:
        for sec in sorted_sec:
            if sec.start <= t < sec.end:
                return _role_mood_value(sec.role)
        # 边界：超出最后一段（浮点尾零）落到最后一段 mood
        return _role_mood_value(sorted_sec[-1].role)

    raw = [_mood_at(t) for t in times]
    window = max(3, len(times) // 10)
    return [round(v, 3) for v in _smooth(raw, window)]


def _bgm_fit(bgm_energy: list[float], mood_curve: list[float]) -> tuple[Optional[float], Optional[str]]:
    """计算 BGM 与 mood_curve 的契合度评分 + 一句话评注。

    Pearson 相关系数 → 0..1 评分（负相关也映到 0..1,但 note 会指出"反向"）。
    """
    if not bgm_energy or not mood_curve:
        return None, "本样例没有可分析的 BGM 信号"
    n = min(len(bgm_energy), len(mood_curve))
    bgm = bgm_energy[:n]
    mood = mood_curve[:n]
    # bgm 归一化（mood 已是 0..1）
    bmin, bmax = min(bgm), max(bgm)
    span = bmax - bmin
    if span < 1e-6:
        return 0.5, "BGM 能量整体平稳,与视频结构高低无明显关联"
    bgm_norm = [(b - bmin) / span for b in bgm]
    corr = _pearson(bgm_norm, mood)
    if corr is None:
        return None, "BGM 数据样本不足以判断契合度"
    score = round(max(0.0, min(1.0, (corr + 1.0) / 2.0)), 3)
    if corr >= 0.55:
        note = "BGM 起伏与视频结构同步,峰值段也跟着抬升,情绪铺垫到位"
    elif corr >= 0.2:
        note = "BGM 整体起伏方向与结构一致,但局部细节匹配一般"
    elif corr > -0.2:
        note = "BGM 能量整体平稳,与视频结构高低无明显关联"
    else:
        note = "BGM 能量走向与视频结构相反,情绪铺垫可能错位"
    return score, note


