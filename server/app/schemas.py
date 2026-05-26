"""Pydantic v2 schemas — 爆款结构迁移引擎.

模块映射（与 docs/ARCHITECTURE.md §5.3 一致）：
- Library      : LibraryItem, SampleManifest, Shot, RhythmCurve, Section, PackagingProfile
- Material     : Material, MaterialUploadResponse
- Gap          : Gap, GapDetectRequest, GapFillRequest, FillResult
- Plan         : Plan, Scene, PackagingItem, BGMConfig, PlanBuildRequest
- Decompose    : DecomposeRequest, DecomposeSubmitResponse
- Render       : RenderSubmitRequest, RenderSubmitResponse
- Edit         : EditApplyRequest, EditMark
- Jobs / SSE   : Job, ProgressEvent
- Health / Err : HealthResponse, ErrorResponse, ASRResponse

字段保留 snake_case；前端 TS 镜像参见 web/src/types/schemas.ts。
"""
from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

# =========================================================================
# Common
# =========================================================================

GapStatus = Literal["ok", "warn", "miss"]
"""槽位匹配状态：✅ 完全命中 / ⚠️ 勉强命中 / ❌ 缺口需补全"""

FillAction = Literal["rerank", "copy", "aigc"]
"""缺口补全动作：结构重排 / 文案补全 / Seedance T2V 短片生成"""

Variant = Literal["A", "B"]
"""AB 双版本渲染标识"""

JobStatus = Literal["pending", "running", "succeeded", "failed", "cancelled"]


VideoType = Literal["marketing", "editing", "motion_graph"]
"""视频类型——上传/选样例时用户必选；驱动 LLM 段落 prompt 三选一。

- marketing      营销/带货：hook → body → cta（痛点钩子→产品演示→行动引导）
- editing        剪辑/Vlog：opening → climax → closing（氛围铺垫→情绪高潮→余韵收尾）
- motion_graph   Motion Graph：intro → build → drop → outro（标题→铺陈→爆点→落版）
"""


SectionKind = Literal[
    # marketing
    "hook", "body", "cta",
    # editing
    "opening", "climax", "closing",
    # motion_graph
    "intro", "build", "drop", "outro",
]
"""段落 kind 9 元枚举——按 video_type 在三组里取一组。"""


_MARKETING_KINDS: tuple[str, ...] = ("hook", "body", "cta")
_EDITING_KINDS: tuple[str, ...] = ("opening", "climax", "closing")
_MOTION_GRAPH_KINDS: tuple[str, ...] = ("intro", "build", "drop", "outro")


def kinds_for_video_type(video_type: VideoType) -> tuple[str, ...]:
    """给定视频类型，返回该类型对应的段落 kind 序列。"""
    if video_type == "editing":
        return _EDITING_KINDS
    if video_type == "motion_graph":
        return _MOTION_GRAPH_KINDS
    return _MARKETING_KINDS  # marketing 兜底


class HealthResponse(BaseModel):
    status: Literal["healthy", "degraded"]
    version: str
    llm_provider: str
    t2v_provider: str = "mock"
    asr_provider: str


class ErrorResponse(BaseModel):
    detail: str
    code: Optional[str] = None
    trace_id: Optional[str] = None


# =========================================================================
# Module 1 — 素材库 (Library)
# =========================================================================

LibrarySource = Literal["system", "user"]
"""样例库来源：
- system  内置爆款样例（server/samples/*，所有用户共享）
- user    用户上传到自己样例库的样例（按 session 隔离；本期 MVP 只占位，下期接持久化）
"""


class LibraryItem(BaseModel):
    """`GET /api/library` 列表项。"""

    id: str
    title: str
    video_type: VideoType = Field(..., description="样例视频类型，决定段落 schema")
    scene: str = Field(..., description="样例所属类型的中文标签，如『营销/剪辑/Motion Graph』")
    duration_seconds: float
    shot_count: int
    cover_url: str
    source: LibrarySource = Field(
        default="system",
        description="system=内置样例（所有用户共享），user=用户上传到样例库的样例。",
    )


class Shot(BaseModel):
    """PySceneDetect 输出的镜头切片。"""

    index: int
    start: float
    end: float
    duration: float
    thumbnail_url: Optional[str] = None
    transcript: Optional[str] = Field(default=None, description="本镜头对应的 ASR 口播片段")
    tags: list[str] = Field(default_factory=list, description="VLM 帧打标（封面风格/转场/字幕样式等）")


class RhythmCurve(BaseModel):
    """节奏曲线 = 镜头切换频次 + BGM 能量。前端拿来画双线图。"""

    times: list[float] = Field(..., description="采样时间点（秒）")
    cut_density: list[float] = Field(..., description="单位时间镜头切换密度")
    bgm_energy: list[float] = Field(..., description="librosa RMS 能量曲线，归一到 [0,1]")
    tempo_bpm: Optional[float] = None


class Section(BaseModel):
    """LLM 段落结构。具体 kind 序列按 video_type 三选一（见 kinds_for_video_type）。"""

    kind: SectionKind
    start: float
    end: float
    summary: str
    shot_indices: list[int] = Field(default_factory=list, description="本段覆盖的镜头 index")


class PackagingProfile(BaseModel):
    """画面包装统计（字幕样式 / 标题条 / 转场 / 封面风格）。"""

    subtitle_style: str = Field(..., description="主导字幕样式名，如『大字加描边』")
    has_title_bar: bool = False
    transition_types: list[str] = Field(default_factory=list)
    cover_style: Optional[str] = None
    sticker_density: float = Field(default=0.0, ge=0.0, le=1.0, description="贴纸/icon 出现密度")


class SampleManifest(BaseModel):
    """`GET /api/sample/{id}/manifest` —— 一个样例的完整预解析包。"""

    sample_id: str
    title: str
    video_type: VideoType = Field(default="marketing", description="样例视频类型，决定段落 schema")
    duration_seconds: float
    video_url: str
    has_voice: bool = Field(default=True, description="VAD 探测：是否有口播；纯 BGM 视频跳过 ASR/逐句字幕")
    shots: list[Shot]
    rhythm: RhythmCurve
    sections: list[Section]
    packaging: PackagingProfile


# =========================================================================
# Module 2 — 拆解 (Decompose)
# =========================================================================

class DecomposeRequest(BaseModel):
    sample_id: str = Field(..., description="样例 ID；命中内置样例则走缓存，新视频走完整链路")
    video_type: VideoType = Field(default="marketing", description="视频类型，驱动段落 prompt")


class DecomposeSubmitResponse(BaseModel):
    job_id: str


# =========================================================================
# Module 3 — 新素材上传 (Material)
# =========================================================================

class Material(BaseModel):
    """用户上传的素材分析结果（含 多模态 LLM 标签 + 段落推荐）。"""

    material_id: str
    filename: str
    media_type: Literal["video", "image", "audio"]
    duration_seconds: Optional[float] = None
    thumbnail_url: Optional[str] = None
    tags: list[str] = Field(default_factory=list, description="多模态 LLM 打标：物体/场景/风格")
    recommended_section: Optional[SectionKind] = Field(
        default=None, description="LLM 推荐它适合放在样例哪一段（按 video_type 解读 kind）"
    )
    sort_order: int = Field(
        default=0,
        description="前端拖拽排序产物，plan/build 时按它排 selected_materials；越小越靠前。",
    )


class MaterialUploadResponse(BaseModel):
    session_id: str
    materials: list[Material]


# =========================================================================
# Module 4 — 缺口识别与补全 (Gap)
# =========================================================================

class Gap(BaseModel):
    """槽位匹配产物。一个 Section 对应若干 Gap，每个 Gap 反映「样例需要 vs 用户素材」的差距。"""

    gap_id: str
    section: SectionKind
    slot_index: int = Field(..., description="该 section 下的第几个分镜槽位")
    requirement: str = Field(..., description="样例对该槽位的描述（如『3 秒痛点提问近景』）")
    status: GapStatus
    impact: Literal["high", "medium", "low"] = "medium"
    matched_material_id: Optional[str] = None
    note: Optional[str] = Field(default=None, description="状态原因，如『时长不足』『风格不符』")
    sample_thumbnail_url: Optional[str] = Field(
        default=None,
        description="样例中该槽对应镜头的缩略图——前端点槽位时弹出『样例长这样』。",
    )


class GapDetectRequest(BaseModel):
    plan_id: str
    session_id: Optional[str] = Field(
        default=None,
        description="上传素材的 session 隔离 ID；为空走 mock 素材（兼容旧调用）。",
    )
    allow_mock: bool = Field(
        default=True,
        description="True：session 为空时回退到内置 mock 素材（默认，方便 demo）；"
                    "False：纯文本流程，缺素材时所有 gap 都标 miss，引导用户走 copy/aigc 补全。",
    )


class GapFillRequest(BaseModel):
    gap_id: str
    action: FillAction
    params: dict[str, Any] = Field(
        default_factory=dict,
        description="动作参数：rerank={target_slot} / copy={prompt_hint} / aigc={prompt, first_frame_url?, duration_seconds?}",
    )


class FillResult(BaseModel):
    gap_id: str
    action: FillAction
    new_material_id: Optional[str] = Field(default=None, description="aigc 生成产物或 rerank 选中的素材")
    narration: Optional[str] = Field(default=None, description="copy 动作的补全文案")
    alternatives: list[str] = Field(
        default_factory=list,
        description="copy 动作 LLM 返回的备选文案，给前端三选一（普通采纳 / 删改 / 换一个）。",
    )
    note: Optional[str] = None
    status: GapStatus = "ok"


# =========================================================================
# Module 5 — 方案 Plan
# =========================================================================

class Scene(BaseModel):
    """主轨分镜：素材切片 + 字幕。FFmpeg concat 的最小单位。"""

    scene_id: str
    section: SectionKind
    source: Literal["sample", "user_material", "aigc_t2v"]
    source_ref: str = Field(..., description="样例镜头 id / material_id / Seedance 任务返回的 media_id")
    start: float = Field(..., description="时间线上的起点（秒）")
    duration: float
    in_point: float = Field(default=0.0, description="源素材内的入点（秒）")
    out_point: Optional[float] = Field(default=None, description="源素材内的出点；None 表示用到结尾")
    narration: Optional[str] = Field(default=None, description="本场口播文字（drawtext 基础字幕）")


class PackagingItem(BaseModel):
    """包装轨元素：交给 Remotion 渲染成透明 WebM。"""

    item_id: str
    kind: Literal["subtitle", "title_bar", "sticker", "transition", "cover"]
    start: float
    end: float
    text: Optional[str] = None
    style: dict[str, Any] = Field(default_factory=dict, description="字体/颜色/位置/动画参数")


class BGMConfig(BaseModel):
    track_url: Optional[str] = None
    volume: float = Field(default=0.6, ge=0.0, le=1.0)
    fade_in: float = 0.0
    fade_out: float = 0.0


class Plan(BaseModel):
    """`POST /api/plan/build` 产物 / 后续渲染与编辑的核心数据结构。"""

    plan_id: str
    sample_id: str
    session_id: Optional[str] = Field(
        default=None,
        description="生成本 Plan 时的素材 session 隔离 ID，渲染时用来反查上传文件。",
    )
    brief: Optional[str] = Field(
        default=None,
        description="构建 Plan 时用户给的主题/卖点；供 /api/edit 阶段二次理解上下文。",
    )
    variant: Variant = "A"
    duration_seconds: float
    main_track: list[Scene]
    packaging_track: list[PackagingItem] = Field(default_factory=list)
    bgm: BGMConfig = Field(default_factory=BGMConfig)


class PlanBuildRequest(BaseModel):
    sample_id: str
    session_id: str = Field(..., description="上传素材的 session 隔离 ID")
    brief: Optional[str] = Field(
        default=None,
        max_length=500,
        description="用户输入的主题/卖点文本，驱动 LLM 段落 prompt + 缺口需求生成。",
    )
    selected_materials: list[str] = Field(default_factory=list, description="用户挑中的 material_id 列表")
    fills: list[FillResult] = Field(default_factory=list, description="已确认的缺口补全结果")
    variant: Variant = "A"


# =========================================================================
# Module 6 — 渲染 (Render)
# =========================================================================

class RenderSubmitRequest(BaseModel):
    plan_id: str
    variant: Variant = "A"


class RenderSubmitResponse(BaseModel):
    job_id: str


# =========================================================================
# Module 7 — 自然语言编辑 (Edit)
# =========================================================================

class EditMark(BaseModel):
    """用户在双轨编辑器上的选中标注，告诉 LLM『要改哪段』。"""

    track: Literal["main", "packaging"]
    start: float
    end: float
    target_id: Optional[str] = Field(default=None, description="scene_id 或 packaging_item.item_id")


class EditApplyRequest(BaseModel):
    plan_id: str
    instruction: str = Field(..., min_length=1, max_length=1000, description="自然语言改片指令")
    marks: list[EditMark] = Field(default_factory=list, description="选中区间；空表示对整段生效")


# =========================================================================
# Jobs & SSE
# =========================================================================

class Job(BaseModel):
    job_id: str
    kind: Literal["decompose", "render"]
    status: JobStatus
    percent: float = Field(default=0.0, ge=0.0, le=100.0)
    created_at: float
    updated_at: float
    payload: dict[str, Any] = Field(default_factory=dict, description="终态结果（如 video_url / manifest）")
    error: Optional[str] = None


class ProgressEvent(BaseModel):
    """SSE `event: progress` 的 data 字段；done/error 复用同结构。"""

    step: str = Field(..., description="当前阶段标识，如『scene_detect』『vlm_tag』『ffmpeg_concat』")
    percent: float = Field(..., ge=0.0, le=100.0)
    payload: dict[str, Any] = Field(default_factory=dict, description="阶段产物或中间日志")


# =========================================================================
# ASR — 单独端点，被 /api/asr/transcribe 复用
# =========================================================================

class ASRResponse(BaseModel):
    transcript: str
    duration_seconds: float
    provider: str
    elapsed_ms: int
