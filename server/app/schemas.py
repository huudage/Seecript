"""Pydantic v2 schemas — 爆款结构迁移引擎.

模块映射（与 docs/ARCHITECTURE.md §5.3 一致）：
- Library      : LibraryItem, SampleManifest, Shot, RhythmCurve, Section, PackagingProfile, VideoUnderstanding
- Material     : Material, MaterialUploadResponse
- Gap          : Gap, GapDetectRequest, GapFillRequest, FillResult
- Plan         : Plan, Scene, PackagingItem, BGMConfig, PlanBuildRequest
- Decompose    : DecomposeRequest, DecomposeSubmitResponse
- Render       : RenderSubmitRequest, RenderSubmitResponse
- Edit         : EditApplyRequest, EditMark
- Jobs / SSE   : Job, ProgressEvent
- Health / Err : HealthResponse, ErrorResponse, ASRResponse

字段保留 snake_case；前端 TS 镜像参见 web/src/types/schemas.ts。

# 段落结构：角色 + 主题双层（v2）
旧版按 video_type 三选一写死 9 个 SectionKind（hook/body/cta/opening/climax/closing/...）
对真实样例僵硬——比如艺术展宣传视频没有 hook/body/cta 这种带货语义。

新版改用 `SectionRole` 四元枚举 + 自由文本 `theme`：
- role 是抽象骨架：opening / development / climax / closing （任何视频都适用）
- theme 是 LLM 看完视频后给的中文小标签（"展品揭幕"、"艺术家自述"、"行动呼吁"等）

video_type 仍保留，但降级为**风格提示**（驱动 BGM / 字幕 / 转场 / 封面），
不再决定段落结构本身。
"""
from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, model_validator

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
"""视频类型——风格提示。决定 BGM / 字幕 / 转场 / 封面，不再决定段落结构。

- marketing      营销/带货/动态海报：节奏紧凑、强字幕、大色块、行动引导
- editing        剪辑/Vlog/纪录：情绪曲线、空镜与高潮、长镜与余韵
- motion_graph   合成动画/信息可视化：标题入场、爆点切换、落版收尾
"""


SectionRole = Literal["opening", "development", "climax", "closing"]
"""段落角色——任何视频都适用的抽象骨架。

- opening      开场段：吸引注意、奠定基调（hook / 标题 / 氛围铺垫 都映射到这里）
- development  发展段：内容铺陈、信息展开（可以多段；body / build / 中段都映射到这里）
- climax       高潮段：情绪/视觉/冲突顶点（climax / drop / 卖点对比都映射到这里）
- closing      收尾段：余韵 / 行动引导 / 落版（cta / outro / closing 都映射到这里）

约束：一个 manifest 必须恰好 1 个 opening + 1 个 closing，最多 1 个 climax，其余皆 development。
"""


# ---- 旧 SectionKind → 新 SectionRole 的迁移映射 -------------------------------
# 历史 manifest.json (server/samples/<id>/manifest.json) 仍含 "kind": "hook"。
# Pydantic before-validator 用这张表把 kind 转成 role + 默认 theme。
_LEGACY_KIND_TO_ROLE: dict[str, tuple[str, str]] = {
    "hook":    ("opening",     "钩子开场"),
    "opening": ("opening",     "氛围铺垫"),
    "intro":   ("opening",     "标题入场"),
    "body":    ("development", "主体铺陈"),
    "build":   ("development", "信息铺陈"),
    "cta":     ("closing",     "行动引导"),
    "closing": ("closing",     "余韵收尾"),
    "outro":   ("closing",     "落版收尾"),
    "climax":  ("climax",      "情绪高潮"),
    "drop":    ("climax",      "视觉爆点"),
}


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
    video_type: VideoType = Field(..., description="样例视频类型（风格提示，决定 BGM/字幕/转场/封面）")
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


class Utterance(BaseModel):
    """ASR 逐句时间戳。时间单位均为秒（asr_client 已从毫秒换算）。

    模块 5 字幕烧录直接读这个列表；模块 2 decompose 用它做"按 shot 时间窗映射 transcript"，
    替代旧版按字符比例切分（会把英文单词从中间截断）。
    """

    text: str
    start: float
    end: float


class RhythmCurve(BaseModel):
    """节奏曲线 = 镜头切换频次 + BGM 能量。前端拿来画双线图。"""

    times: list[float] = Field(..., description="采样时间点（秒）")
    cut_density: list[float] = Field(..., description="单位时间镜头切换密度")
    bgm_energy: list[float] = Field(..., description="librosa RMS 能量曲线，归一到 [0,1]")
    tempo_bpm: Optional[float] = None


class Section(BaseModel):
    """LLM 段落结构。

    `role` 是抽象骨架（任何视频都有 opening/development/climax/closing 这 4 种角色），
    `theme` 是 LLM 看完视频后给的中文小标签——反映**这一段真实在讲什么**，比 role 信息量大。
    比如艺术展样例的 opening 段 theme 可能是『展品揭幕』，营销样例可能是『痛点钩子』。
    """

    role: SectionRole = Field(..., description="段落角色（4 元枚举，全视频类型通用）")
    theme: str = Field(default="", max_length=20, description="LLM 给出的本段中文主题标签（≤10 字）")
    start: float
    end: float
    summary: str
    shot_indices: list[int] = Field(default_factory=list, description="本段覆盖的镜头 index")

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy_kind(cls, data: Any) -> Any:
        """旧 manifest.json 用 `kind` 字段——映射成 role + 默认 theme。

        新写的数据应直接用 `role`/`theme`；这里只是兜底，防止预拆解缓存挂掉。
        """
        if not isinstance(data, dict):
            return data
        if "role" not in data and "kind" in data:
            legacy = data.pop("kind")
            role, default_theme = _LEGACY_KIND_TO_ROLE.get(
                legacy, ("development", legacy or "主体")
            )
            data["role"] = role
            data.setdefault("theme", default_theme)
        return data


class VideoUnderstanding(BaseModel):
    """多模态 LLM 对整支视频的语义画像。先理解再切段。

    decompose pipeline 的关键转折：以前直接按 video_type 三选一塞 prompt 给 LLM 让它切段，
    现在多走一步——先让 LLM 看完整片说"这是个什么样的视频"，再用这份画像驱动切段。
    这样艺术展宣传片不会被强切成 hook/body/cta，而是按它自己的叙事弧线切。
    """

    archetype: str = Field(..., max_length=40, description="视频原型，如『艺术展宣传』『带货种草』『城市 Vlog』")
    narrative_summary: str = Field(..., max_length=200, description="一段话讲清整支视频在说什么、怎么说")
    suggested_segments: int = Field(..., ge=3, le=6, description="LLM 建议切几段（3-6）")
    tone: str = Field(default="", max_length=30, description="基调描述：『冷静克制』『高燃热血』『诙谐自嘲』等")


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
    video_type: VideoType = Field(default="marketing", description="视频风格类型（决定包装样式，不决定段落结构）")
    duration_seconds: float
    video_url: str
    has_voice: bool = Field(default=True, description="VAD 探测：是否有口播；纯 BGM 视频跳过 ASR/逐句字幕")
    shots: list[Shot]
    rhythm: RhythmCurve
    sections: list[Section]
    packaging: PackagingProfile
    understanding: Optional[VideoUnderstanding] = Field(
        default=None,
        description="LLM 视频画像（archetype / narrative_summary / tone）。旧缓存无此字段为 None。",
    )
    utterances: list[Utterance] = Field(
        default_factory=list,
        description="ASR 逐句时间戳；纯 BGM 视频为空列表。供模块 5 字幕烧录与编辑器精对齐使用。",
    )
    climax_position: Optional[float] = Field(
        default=None,
        description="高潮时间点（秒）。优先取 role=climax 段中点；无 climax 时回落 BGM 能量峰值。前端节奏图叠 ReferenceLine。",
    )


# =========================================================================
# Module 2 — 拆解 (Decompose)
# =========================================================================

class DecomposeRequest(BaseModel):
    sample_id: str = Field(..., description="样例 ID；命中内置样例则走缓存，新视频走完整链路")
    video_type: VideoType = Field(default="marketing", description="视频类型（风格提示，影响包装；不影响段落结构）")
    reference_asset_ids: list[str] = Field(
        default_factory=list,
        max_length=6,
        description="用户素材库中的参考素材 id 列表（图/视频抽帧），喂给多模态 LLM 做风格/调性参考",
    )


class DecomposeSubmitResponse(BaseModel):
    job_id: str


# =========================================================================
# Module 3 — 新素材上传 (Material)
# =========================================================================

class Material(BaseModel):
    """用户上传的素材分析结果（含 多模态 LLM 标签 + 段落推荐 + 高光评分）。"""

    material_id: str
    filename: str
    media_type: Literal["video", "image", "audio"]
    duration_seconds: Optional[float] = None
    thumbnail_url: Optional[str] = None
    tags: list[str] = Field(default_factory=list, description="多模态 LLM 打标：物体/场景/风格")
    recommended_section: Optional[SectionRole] = Field(
        default=None, description="LLM 推荐它适合放在样例的哪种 role 段（opening/development/climax/closing）"
    )
    highlight_score: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="高光评分 0-1：0.7+ 适合做开头/高潮，0.4-0.7 适合中段铺陈，<0.4 仅作 B-roll。",
    )
    highlight_reason: Optional[str] = Field(
        default=None,
        description="LLM 给出高光评分的一句话理由（构图/动作/情绪等），前端 hover 卡片展示。",
    )
    sort_order: int = Field(
        default=0,
        description="前端拖拽排序产物，plan/build 时按它排 selected_materials；越小越靠前。",
    )

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy_recommended_section(cls, data: Any) -> Any:
        """旧素材 store 里的 recommended_section 可能还是 hook/body/cta 这套——映射到 role。"""
        if not isinstance(data, dict):
            return data
        rec = data.get("recommended_section")
        if isinstance(rec, str) and rec in _LEGACY_KIND_TO_ROLE:
            data["recommended_section"] = _LEGACY_KIND_TO_ROLE[rec][0]
        return data


class MaterialUploadResponse(BaseModel):
    session_id: str
    materials: list[Material]


# =========================================================================
# Module 4 — 缺口识别与补全 (Gap)
# =========================================================================

class Gap(BaseModel):
    """槽位匹配产物。一个 Section 对应若干 Gap，每个 Gap 反映「样例需要 vs 用户素材」的差距。"""

    gap_id: str
    section: SectionRole = Field(..., description="该 gap 所在段落的角色")
    section_id: Optional[str] = Field(
        default=None,
        description="所属 AdaptedSection.section_id，前端按段分组用；老 plan 为 None。",
    )
    project_id: Optional[str] = Field(
        default=None,
        description="所属项目 ID；由 /gap/detect 时回填，fill_gap 反查 plan/project 用。老 gap 为 None（落 __legacy 项目）。",
    )
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

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy_section(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        sec = data.get("section")
        if isinstance(sec, str) and sec in _LEGACY_KIND_TO_ROLE:
            data["section"] = _LEGACY_KIND_TO_ROLE[sec][0]
        return data


class GapDetectRequest(BaseModel):
    plan_id: str
    project_id: Optional[str] = Field(
        default=None,
        description="所属项目 ID（推荐显式传）；与 session_id 等价，二者都为空时回退 plan.project_id。",
    )
    session_id: Optional[str] = Field(
        default=None,
        description="兼容老前端：留作 project_id 的别名；为空走 mock 素材。",
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


class AigcPromptRequest(BaseModel):
    """`POST /api/gap/aigc-prompt` —— 把段落上下文交给 LLM，转写出一条完备的 Seedance T2V prompt。

    前端在用户打开 AIGC 面板（或切到不同 gap）时调一次，把返回的 prompt 填入 textarea
    供创作者编辑后再提交生成；解决"直接拿 gap.requirement 当 prompt 缺要素"的问题。
    """

    gap_id: str
    hint: Optional[str] = Field(
        default=None,
        max_length=200,
        description="创作者额外提示（可选）：风格倾向、必须出现的元素等",
    )


class AigcPromptResponse(BaseModel):
    gap_id: str
    prompt: str = Field(..., description="LLM 转写出的完备 T2V prompt")


class GapFillAllRequest(BaseModel):
    """`POST /api/gap/fill-all` —— 对 plan_id 下所有非 ok 缺口顺序触发补全。

    action:
      - "aigc"（默认，向后兼容）：每个缺口走 Seedance T2V 链式生成
      - "copy"：每个缺口走 LLM 文案补全（用 gap.requirement 作为 prompt_hint）
    """

    plan_id: str
    action: Literal["copy", "aigc"] = Field(
        default="aigc",
        description="批量补全使用的动作；rerank 不支持批量（依赖人工挑选）",
    )
    prompt_template: Optional[str] = Field(
        default=None,
        max_length=200,
        description="可选自定义 prompt 模板（仅 aigc），{requirement} 占位会被替换为 gap.requirement。",
    )


class GapFillAllResponse(BaseModel):
    plan_id: str
    fills: list["FillResult"] = Field(default_factory=list, description="成功生成的 fills，顺序与 gap 一致")
    failed_gap_id: Optional[str] = Field(
        default=None,
        description="部分失败时第一个失败的 gap_id；None 表示全部成功。",
    )
    stopped_reason: Optional[str] = None


class FillResult(BaseModel):
    gap_id: str
    action: FillAction
    new_material_id: Optional[str] = Field(default=None, description="aigc 最后一段 task_id 或 rerank 选中的素材")
    narration: Optional[str] = Field(default=None, description="copy 动作的补全文案")
    voiceover_url: Optional[str] = Field(
        default=None,
        description="copy 动作（且 voiceover_enabled=True）自动合成的 TTS 音频 URL；"
                    "rebuild plan 时回填到对应 Scene.voiceover_url。",
    )
    alternatives: list[str] = Field(
        default_factory=list,
        description="copy 动作 LLM 返回的备选文案，给前端三选一（普通采纳 / 删改 / 换一个）。",
    )
    video_urls: list[str] = Field(
        default_factory=list,
        description="aigc 链式生成产出的 N 段 CDN URL（按时序）。单段为 1 元素，>12s 走链式 N 段。",
    )
    cover_url: Optional[str] = Field(default=None, description="aigc 第一段封面 URL（前端预览缩略图）")
    chunks_count: int = Field(default=0, description="aigc chunks 数量；0 表示非 aigc 或失败")
    chunk_task_ids: list[str] = Field(
        default_factory=list,
        description="aigc 各 chunk 对应的 Seedance task_id；refresh 接口按此重试单段。",
    )
    note: Optional[str] = None
    status: GapStatus = "ok"
    section_id: Optional[str] = Field(
        default=None,
        description="所属 AdaptedSection.section_id，由后端在 fill_gap 时回填；plan 重建时直接用它路由，不再依赖 gap_store 进程内存。",
    )


# =========================================================================
# Module 5 — 方案 Plan
# =========================================================================

class AdaptedSection(BaseModel):
    """LLM 改编后的段落结构。Plan 的"叙事单位"层，位于 Scene"剪辑单位"层之上。

    流程：样例 manifest.sections（真模型拆出的样例骨架）+ 用户 brief + video_goal
    → LLM 改编 → AdaptedSection[]（含每段 content_description 内容说明）。

    Scene 负责"用哪个素材切片、时长多少"；AdaptedSection 负责"这一段叙事上要讲什么"。
    """

    section_id: str = Field(..., description="本 plan 内稳定 id，如 'sec-0'；Gap.section_id 反查")
    role: SectionRole = Field(..., description="段落角色（4 元枚举，全视频类型通用）")
    theme: str = Field(default="", max_length=20, description="紧贴用户主题的中文短标签（≤8 字）")
    content_description: str = Field(
        ...,
        max_length=300,
        description="内容说明：本段画面/口播应呈现什么；由 LLM 紧贴 brief+video_goal 生成",
    )
    source_section_indices: list[int] = Field(
        default_factory=list,
        description="改编自原 manifest.sections 的下标；纯新增段为空",
    )
    source_shot_indices: list[int] = Field(
        default_factory=list,
        description="改编自原样例哪些 shot index（用于 gap 缩略图反查）",
    )
    order: int = Field(..., description="段落顺序（从 0 开始）")
    duration_seconds: float = Field(
        default=4.0,
        ge=2.0,
        le=30.0,
        description="LLM 决定的本段目标时长（秒），驱动 Scene.duration 与 AIGC 链式分段。",
    )


TransitionStyle = Literal["hard_cut", "dissolve", "slide", "zoom", "whip", "wipe"]
"""转场风格 6 元枚举——和 ffmpeg xfade primitives 对齐。"""


class SceneTransition(BaseModel):
    """主轨场景的入场转场——与上一段如何衔接。

    渲染层 ffmpeg.concat_with_transitions 用 xfade 滤镜实装；
    duration 占用前后两段相邻 N 秒（前段尾部 + 后段头部 overlap）。
    """

    style: TransitionStyle = Field(default="dissolve")
    duration: float = Field(default=0.4, ge=0.1, le=1.5, description="转场持续秒数（与上一段 overlap 长度）")


class Scene(BaseModel):
    """主轨分镜：素材切片 + 字幕。FFmpeg concat 的最小单位。"""

    scene_id: str
    section: SectionRole = Field(..., description="本场所属段落角色（opening/development/climax/closing）")
    source: Literal["sample", "user_material", "aigc_t2v", "text_card"]
    source_ref: str = Field(..., description="样例镜头 id / material_id / Seedance 任务返回的 media_id / text_card 的标识")
    start: float = Field(..., description="时间线上的起点（秒）")
    duration: float
    in_point: float = Field(default=0.0, description="源素材内的入点（秒）")
    out_point: Optional[float] = Field(default=None, description="源素材内的出点；None 表示用到结尾")
    narration: Optional[str] = Field(default=None, description="本场口播文字（drawtext 基础字幕）")
    voiceover_url: Optional[str] = Field(
        default=None,
        description="本场口播 TTS 合成后的本地音频 URL（/voiceovers/<plan_id>/<scene_id>.wav）；"
                    "render pipeline 用 ffmpeg 按 scene.start 时间偏移混到主轨。",
    )
    aigc_video_urls: list[str] = Field(
        default_factory=list,
        description="source=aigc_t2v 时 Seedance 返回的 N 段 CDN URL；render pipeline 下载后 ffmpeg concat。",
    )
    transition_in: Optional[SceneTransition] = Field(
        default=None,
        description="与上一段衔接方式；sc-0 永远忽略此字段（无上一段）。"
                    "None 或 style=hard_cut 时走 concat demuxer 直拼；其他 style 走 xfade 滤镜。",
    )

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy_section(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        sec = data.get("section")
        if isinstance(sec, str) and sec in _LEGACY_KIND_TO_ROLE:
            data["section"] = _LEGACY_KIND_TO_ROLE[sec][0]
        return data


class PackagingItem(BaseModel):
    """包装轨元素：交给 Remotion 渲染成透明 WebM。"""

    item_id: str
    kind: Literal["subtitle", "title_bar", "sticker", "transition", "cover"]
    start: float
    end: float
    text: Optional[str] = None
    style: dict[str, Any] = Field(default_factory=dict, description="字体/颜色/位置/动画参数")


class BGMConfig(BaseModel):
    bgm_asset_id: Optional[str] = Field(
        default=None,
        description="素材库中用户选定的 BGM Asset id；None=本次无 BGM",
    )
    track_url: Optional[str] = Field(
        default=None,
        description="实际混音用的音频 URL（/assets/... 或 /uploads/...）；plan.py 由 asset 反填",
    )
    duration_seconds: Optional[float] = Field(
        default=None,
        ge=0.0,
        description="BGM 总时长（秒），上传分析时回填；前端绘制 BGM bar 宽度用。",
    )
    peak_seconds: Optional[float] = Field(
        default=None,
        ge=0.0,
        description="BGM 强音/能量峰值时间（秒），librosa onset+RMS 分析得到；前端在 BGM bar 上画一条参考线，"
                    "辅助用户把峰值对齐视频高潮。仅用于展示，不影响渲染本身。",
    )
    video_anchor_seconds: float = Field(
        default=0.0,
        description="BGM t=0 对齐到视频的哪一秒。正值=BGM 入场延迟（前面静音），负值=跳过 BGM 开头那段。"
                    "用户在 BGM 轨道上拖动 bar 即修改本字段。",
    )
    volume: float = Field(default=0.35, ge=0.0, le=1.0)
    fade_in: float = Field(default=1.5, ge=0.0, le=10.0)
    fade_out: float = Field(default=2.0, ge=0.0, le=10.0)
    duck_with_voice: bool = Field(
        default=True,
        description="有口播段时 BGM 自动闪避（sidechain compress）",
    )
    duck_attenuation_db: float = Field(default=-9.0, ge=-30.0, le=0.0)

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy_start_offset(cls, data: Any) -> Any:
        """旧 BGMConfig 用 start_offset（≥0，跳过 BGM 开头 N 秒），
        新模型用 video_anchor_seconds（BGM 0s 对齐到视频第几秒）。
        等价关系：video_anchor_seconds = -start_offset。
        """
        if not isinstance(data, dict):
            return data
        if "video_anchor_seconds" not in data and "start_offset" in data:
            try:
                legacy = float(data.pop("start_offset") or 0.0)
            except (TypeError, ValueError):
                legacy = 0.0
            data["video_anchor_seconds"] = -legacy
        else:
            data.pop("start_offset", None)
        return data


# ---- 创作设置 ----------------------------------------------------------------

TargetPlatform = Literal["douyin", "wechat", "xiaohongshu", "bilibili"]
"""目标平台 —— 决定画幅 + 节奏 + 字幕风格。

- douyin       抖音：9:16，强字幕，节奏紧凑
- wechat       视频号：9:16，温和节奏
- xiaohongshu  小红书：3:4 或 9:16，文艺克制
- bilibili     B 站：16:9，叙事感
"""


ToneStyle = Literal["tight_hype", "calm_narrative", "casual_daily", "professional_cool"]
"""整体调性 —— 影响 LLM 段落 prompt 倾向。

- tight_hype          紧凑高燃：快剪 + 强情绪 + 必有 climax
- calm_narrative      沉稳叙事：长镜头 + 余韵 + climax 可选
- casual_daily        轻松日常：口语化 + 节奏自然
- professional_cool   专业冷静：信息密度高 + 弱情绪 + 重数据
"""


TTSVoice = Literal[
    "zh_female_qingxin",
    "zh_male_jieshuo",
    "zh_female_meili",
    "zh_male_qingshuang",
    "zh_female_xinling",
]
"""ARK 火山方舟 TTS 音色（中文）。
- zh_female_qingxin     清新女声（默认）
- zh_male_jieshuo       磁性解说男声
- zh_female_meili       甜美治愈女声
- zh_male_qingshuang    清爽阳光男声
- zh_female_xinling     心灵叙事女声
"""


PackagingPreset = Literal["minimalist", "energetic", "info_feed", "dialogue", "custom"]
"""包装风格预设：
- minimalist  极简：只用 hard_cut + dissolve，小字号底部字幕无底色，封面文字来自 video_goal
- energetic   活力：全 6 种转场，大字号字幕带阴影，封面用 LLM 自动生成
- info_feed   信息流：dissolve + slide + wipe，中字号顶部字幕带渐变底色，封面 1.5s 停留
- dialogue    对话/口播：dissolve + hard_cut 为主，大字号底部字幕带阴影，双语字幕开启
- custom      自定义：UI 直接暴露所有字段，preset 不参与展开
"""


SubtitleFontSize = Literal["small", "medium", "large"]
"""字幕字号：small=36 / medium=48 / large=64（与 ffmpeg drawtext fontsize 对齐）。"""


SubtitlePosition = Literal["top", "middle", "bottom"]
"""字幕画面位置：top=画面上 1/8 / middle=正中 / bottom=底部上 8%（默认）。"""


SubtitleBackground = Literal["none", "shadow", "gradient"]
"""字幕底色：
- none      只画字（粗描边由字体本身提供），最干净
- shadow    黑底半透明 box（drawtext box=1，默认风格）
- gradient  渐变底（更厚的 box + 高斯模糊），观感最重
"""


CoverTextSource = Literal["auto", "video_goal", "custom"]
"""封面主标题文字来源：
- auto        LLM 自动生成（默认）
- video_goal  直接取 plan.video_goal 的前 12 字
- custom      取 PackagingPreferences.cover_custom_text
"""


class PackagingPreferences(BaseModel):
    """包装阶段用户配置 —— Compose/Render 都能改，驱动 PackagingAgent + 字幕烧录样式。

    存在 plan.settings.packaging_prefs 上，POST /packaging/recommend 时可被请求体覆盖；
    覆盖结果回写到 plan.settings.packaging_prefs，下次进入面板能反显上次配置。
    """

    preset: PackagingPreset = Field(
        default="custom",
        description="预设入口。非 custom 时 router 端会按预设展开覆盖具体字段；"
                    "用户在 UI 上动了任何具体字段就会回到 custom。",
    )
    allowed_transition_styles: list[TransitionStyle] = Field(
        default_factory=lambda: ["hard_cut", "dissolve", "slide", "zoom", "whip", "wipe"],
        min_length=1,
        max_length=6,
        description="允许的转场风格白名单。LLM 输出不在此列表内的会被替换成首项。",
    )
    max_transition_duration: float = Field(
        default=0.8,
        ge=0.2,
        le=1.5,
        description="转场持续秒数上限。LLM 输出超过此值会被 clamp。",
    )
    subtitle_font_size: SubtitleFontSize = Field(default="medium")
    subtitle_position: SubtitlePosition = Field(default="bottom")
    subtitle_background: SubtitleBackground = Field(default="shadow")
    subtitle_bilingual: bool = Field(
        default=False,
        description="开启后 LLM 给每段 narration 翻译一句英文，drawtext 两行展示。",
    )
    cover_text_source: CoverTextSource = Field(default="auto")
    cover_custom_text: Optional[str] = Field(
        default=None,
        max_length=20,
        description="cover_text_source=custom 时使用的主标题文字（≤20 字，渲染时截到 12 字）。",
    )
    cover_duration: float = Field(
        default=1.2,
        ge=0.6,
        le=2.0,
        description="封面停留秒数（叠在第 0 秒，结束后回到主轨第 1 段画面）。",
    )
    cover_with_subtitle: bool = Field(
        default=True,
        description="封面副标题是否同时显示；False 时即使 LLM 给了 subtitle 也不渲染。",
    )
    llm_temperature: float = Field(
        default=0.7,
        ge=0.3,
        le=0.9,
        description="PackagingAgent 调 LLM 时的温度。低=稳定，高=多样；默认 0.7 平衡。",
    )


class ComposeSettings(BaseModel):
    """Compose 页用户配置 —— 与 brief/video_goal 一起驱动结构改编。

    全部可选；前端折叠面板"高级设置"暴露。后端 plan_agent 把这些注入 prompt，
    驱动 LLM 决定段时长 / 调性 / CTA / 关键词命中。
    """

    target_duration_seconds: float = Field(
        default=30.0,
        ge=10.0,
        le=120.0,
        description="目标总时长（秒），驱动每段 duration_seconds 分配。",
    )
    target_platform: TargetPlatform = Field(
        default="douyin",
        description="目标平台。决定画幅 + 节奏 + 字幕风格。",
    )
    tone: ToneStyle = Field(
        default="tight_hype",
        description="整体调性。影响 LLM 段落结构与口播倾向。",
    )
    cta: str = Field(
        default="",
        max_length=20,
        description="核心 CTA 文案（≤20 字）。closing 段自动套用。",
    )
    keywords: list[str] = Field(
        default_factory=list,
        max_length=5,
        description="必须出现的关键词（最多 5 个）。每段 narration 至少出现 1 个。",
    )
    voiceover_enabled: bool = Field(
        default=True,
        description="是否需要口播。True=plan/build 自动生成逐句字幕 + copy fill 自动合成 TTS；"
                    "False=纯 BGM 视频,跳过字幕轨与 TTS（但保留 narration 文本作 LLM 上下文）。",
    )
    tts_voice: TTSVoice = Field(
        default="zh_female_qingxin",
        description="ARK TTS 音色。voiceover_enabled=False 时该字段无效。",
    )
    packaging_prefs: PackagingPreferences = Field(
        default_factory=PackagingPreferences,
        description="包装阶段用户配置（转场白名单/字幕样式/封面策略/LLM 温度）。"
                    "Compose 创建时落默认值，PackagingPanel 调推荐时可经请求体覆盖并回写。",
    )


class Plan(BaseModel):
    """`POST /api/plan/build` 产物 / 后续渲染与编辑的核心数据结构。"""

    plan_id: str
    sample_ids: list[str] = Field(
        ...,
        min_length=1,
        max_length=2,
        description="本 plan 改编自哪些参考样例（1-2 个）。多样例时 plan_agent 把段落结构合并为对等参考池。",
    )
    project_id: Optional[str] = Field(
        default=None,
        description="所属项目 ID；新建 plan 时由 /plan/build 写入。老 plan 为 None（落 __legacy 项目，后续可手动归并）。",
    )
    session_id: Optional[str] = Field(
        default=None,
        description="生成本 Plan 时的素材 session 隔离 ID；新模型下等价 project_id，渲染时用来反查上传文件。",
    )
    brief: Optional[str] = Field(
        default=None,
        description="构建 Plan 时用户给的主题/卖点；供 /api/edit 阶段二次理解上下文。",
    )
    video_goal: Optional[str] = Field(
        default=None,
        description="用户对新视频的要求与目的（受众、时长目标、调性等），驱动结构改编。",
    )
    adapted_sections: list[AdaptedSection] = Field(
        default_factory=list,
        description="LLM 基于样例骨架 + brief + video_goal 改编出的段落结构；空列表表示老 plan（兼容）。",
    )
    variant: Variant = "A"
    duration_seconds: float
    main_track: list[Scene]
    packaging_track: list[PackagingItem] = Field(default_factory=list)
    bgm: BGMConfig = Field(default_factory=BGMConfig)
    settings: ComposeSettings = Field(
        default_factory=ComposeSettings,
        description="创作设置回写。供 render/edit/packaging 复用。",
    )


class PlanBuildRequest(BaseModel):
    sample_ids: list[str] = Field(
        ...,
        min_length=1,
        max_length=2,
        description="参考样例 id 列表（1-2 个）。多选时两份段落结构会被合并成对等参考池喂给 plan_agent。",
    )
    project_id: str = Field(..., description="所属项目 ID（前端 currentProjectId）；后端按它路由素材/资产/落盘")
    session_id: Optional[str] = Field(
        default=None,
        description="兼容老前端：留作 project_id 的别名；为空时使用 project_id。",
    )
    brief: Optional[str] = Field(
        default=None,
        max_length=500,
        description="用户输入的主题/卖点文本，驱动 LLM 段落 prompt + 缺口需求生成。",
    )
    video_goal: Optional[str] = Field(
        default=None,
        max_length=500,
        description="视频要求与目的（受众/时长/调性等），与 brief 共同驱动结构改编。",
    )
    settings: ComposeSettings = Field(
        default_factory=ComposeSettings,
        description="创作设置（目标时长/平台/调性/CTA/关键词），驱动 plan_agent。",
    )
    selected_materials: list[str] = Field(default_factory=list, description="用户挑中的 material_id 列表")
    fills: list[FillResult] = Field(default_factory=list, description="已确认的缺口补全结果")
    bgm_asset_id: Optional[str] = Field(
        default=None,
        description="用户素材库 BGM Asset id；None=本次无 BGM（渲染阶段跳过混音）",
    )
    reference_asset_ids: list[str] = Field(
        default_factory=list,
        max_length=6,
        description="用户素材库中的参考素材 id 列表，喂给 plan_agent.adapt_structure 多模态 prompt",
    )
    variant: Variant = "A"


# =========================================================================
# Module 5b — 包装推荐 (Packaging Agent)
# =========================================================================
# TransitionStyle / SceneTransition 定义见 Scene 上方（被 Scene.transition_in 复用）。


class TransitionSuggestion(BaseModel):
    """段落切换处的转场推荐。一对相邻 Scene 给一条建议。"""

    item_id: str = Field(..., description="对应 packaging_track 里 kind=transition 的 item_id")
    at_seconds: float = Field(..., description="转场触发时间点（前一 scene 结束 = 后一 scene 起始）")
    from_section: SectionRole
    to_section: SectionRole
    style: TransitionStyle
    duration: float = Field(default=0.4, ge=0.1, le=1.5, description="转场持续秒数")
    reason: str = Field(..., description="LLM 给出此选择的一句话依据")

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy_sections(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        for fld in ("from_section", "to_section"):
            sec = data.get(fld)
            if isinstance(sec, str) and sec in _LEGACY_KIND_TO_ROLE:
                data[fld] = _LEGACY_KIND_TO_ROLE[sec][0]
        return data


class CoverDesign(BaseModel):
    """封面方案。Remotion 渲一帧透明 PNG，pipeline overlay 叠在第 0 秒。"""

    item_id: str = Field(default="pkg-cover", description="包装轨 item_id（kind=cover）")
    title: str = Field(..., description="主标题（≤ 12 字，强冲击）")
    subtitle: Optional[str] = Field(default=None, description="副标题/卖点提示（≤ 18 字）")
    palette: list[str] = Field(
        default_factory=list,
        description="主色 + 强调色 hex（2-3 个），如 ['#FFE600', '#1F2937']",
    )
    layout: Literal["center", "left", "split", "stacked"] = "center"
    style_note: str = Field(..., description="LLM 给出的一句话风格说明，比如『大字号 + 黑底白字 + 黄色高亮』")


class PackagingRecommendation(BaseModel):
    """`POST /api/packaging/recommend` 产物，回写到 PlanStore 的 packaging_track。"""

    plan_id: str
    transitions: list[TransitionSuggestion] = Field(default_factory=list)
    cover: Optional[CoverDesign] = None
    notes: list[str] = Field(default_factory=list, description="agent 调试日志（mock/失败原因等）")


class PackagingRecommendRequest(BaseModel):
    plan_id: str
    apply: bool = Field(
        default=True,
        description="True：落地为 PackagingItem 写回 plan.packaging_track；False：只返回建议不改 plan。",
    )
    preferences: Optional[PackagingPreferences] = Field(
        default=None,
        description="用户在 PackagingPanel 上配置的偏好（转场白名单/字幕样式/封面策略/温度）。"
                    "None 时直接复用 plan.settings.packaging_prefs；非空时与之合并（请求体优先），"
                    "结果回写到 plan.settings.packaging_prefs 持久化。",
    )


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
    track: Literal["main", "packaging", "voice"] = Field(
        ...,
        description="编辑意图轨道：main=内容轨（时长/素材/转场）/ packaging=包装轨（字幕/BGM）/ voice=口播轨（narration+TTS）。"
                    "渲染态（project.current_step=='render'）下 main 被拒 409。",
    )
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


# =========================================================================
# Asset Library — 用户长期素材库（BGM + 参考图 + 参考视频）
# =========================================================================
# 与 Material（一次 session 内的短期素材）严格分离：
# - Material 的生命周期跟 session 走，被剪进 main_track；
# - Asset 持久化在 var/assets/，承担 BGM 混音 / 多模态参考两类用途，**不一定进视频**。

AssetKind = Literal["bgm", "reference_image", "reference_video"]
"""资产类型：
- bgm              MP3/WAV/M4A 等，渲染阶段供 ffmpeg mix_bgm 使用
- reference_image  JPG/PNG/WEBP，作为多模态 LLM 的风格/调性/结构参考
- reference_video  MP4/MOV/WEBM，上传后抽 8-12 关键帧作为 reference_image 列表用
"""

AssetStatus = Literal["processing", "ready", "failed"]
"""上传后处理状态：processing → 后台抽元数据/缩略图；ready → 可用；failed → 看 error 字段。"""


class Asset(BaseModel):
    """用户素材库中的一项资产。"""

    asset_id: str = Field(..., description="ass-xxxxxxxx 格式")
    owner: str = Field(
        default="__legacy",
        description="资产所属项目 ID（v2 后含义改为 project_id）；老数据迁移期落 '__legacy'。",
    )
    kind: AssetKind

    # 文件
    file_name: str = Field(..., description="上传时的原文件名（仅展示）")
    file_url: str = Field(..., description="可访问 URL，例如 /assets/local/bgm/ass-xxx.mp3")
    file_size: int = Field(..., ge=0)
    content_hash: str = Field(..., description="sha256，上传去重")
    mime: str

    # 用户标注
    title: str = Field(default="", description="展示标题；默认用 file_name 截断")
    description: str = Field(default="", max_length=500)
    tags: list[str] = Field(default_factory=list, max_length=12)

    # 类型特定元数据（不强 schema 化，避免每加一个类型就动 Asset 主体）
    # bgm:             {duration_seconds, tempo_bpm?, peak_at_seconds?, sample_rate, channels}
    # reference_image: {width, height, thumbnail_url}
    # reference_video: {duration_seconds, width, height, fps, thumbnail_url, frame_urls: [...]}
    metadata: dict[str, Any] = Field(default_factory=dict)

    status: AssetStatus = "processing"
    error: Optional[str] = None
    created_at: float
    last_used_at: Optional[float] = None
    use_count: int = Field(default=0, ge=0)


class AssetUpdateRequest(BaseModel):
    """PATCH /api/asset/{id}：用户只能改这三个用户态字段。"""

    title: Optional[str] = Field(default=None, max_length=120)
    description: Optional[str] = Field(default=None, max_length=500)
    tags: Optional[list[str]] = Field(default=None, max_length=12)


class AssetListResponse(BaseModel):
    items: list[Asset]
    total: int


# =========================================================================
# Module · 项目 (Project) —— 用户工作流容器
# =========================================================================
# 一个 project 串起：选样例 → 上传素材 → 改编 plan → gap fill → render。
# project_id 是后端唯一隔离键；用户素材 / 资产库 / plans / gaps 都按它分组。
# 共享：server/samples/<sample_id>/manifest.json（VLM/ASR 预计算贵，不重跑）。

ProjectStatus = Literal["draft", "planned", "rendered"]
"""项目状态：
- draft     刚建项目，还没生成 plan
- planned   生成过 plan，可进 Compose / 缺口补全
- rendered  渲染过至少一次，有可看的成片
"""

StepName = Literal["library", "decompose", "compose", "render"]
"""线性工作流的四个步骤。Migrate 是 view-only 的，不在 commit 序列里。"""

StepStatus = Literal["pending", "in_progress", "saved", "dirty"]
"""单步状态：
- pending      尚未开始
- in_progress  当前步（用户正在编辑，未点『下一步』）
- saved        已提交快照
- dirty        上游被改过 → 本步快照可能过期，建议刷新；产物仍在盘上可看
"""


class StepSnapshot(BaseModel):
    """『下一步』提交时落盘的单步产物快照。

    payload 内容随 step 而异：
    - library:   {"sample_ids": list[str]}（1-2 个）
    - decompose: {"sample_ids": list[str]}（manifest 走样例共享区，不重存）
    - compose:   {"plan_id": str, "fill_ids": list[str]}
    - render:    {"job_id": str}
    """

    step: StepName
    saved_at: float
    payload: dict[str, Any] = Field(default_factory=dict)


class ProjectStepState(BaseModel):
    """Project.step_states 字段——顶部导航徽章数据源。"""

    library: StepStatus = "pending"
    decompose: StepStatus = "pending"
    compose: StepStatus = "pending"
    render: StepStatus = "pending"


class Project(BaseModel):
    """项目 = 一次完整的『样例 → 改编 → 补全 → 渲染』工作流容器。"""

    project_id: str = Field(..., description="UUID hex[:12]")
    name: str = Field(..., max_length=80, description="用户可见名称")
    sample_ids: list[str] = Field(
        ...,
        min_length=1,
        max_length=2,
        description="基于哪些样例（1-2 个，共享、跨项目复用）。多样例时 plan_agent 会把段落结构合并参考。",
    )
    brief: Optional[str] = Field(default=None, description="主题/卖点回写（Compose 用户输入）")
    video_goal: Optional[str] = Field(default=None, description="视频目的回写")
    settings: ComposeSettings = Field(default_factory=ComposeSettings)
    last_plan_id: Optional[str] = Field(default=None, description="最近一次 plan_id（前端 resume 用）")
    last_render_job_id: Optional[str] = Field(default=None, description="最近一次 render job_id（成片预览）")
    status: ProjectStatus = "draft"
    step_states: ProjectStepState = Field(default_factory=ProjectStepState, description="四步状态机")
    current_step: StepName = Field(default="library", description="用户最近停留的步骤")
    created_at: float
    updated_at: float


class ProjectCreateRequest(BaseModel):
    name: str = Field(..., max_length=80)
    sample_ids: list[str] = Field(
        ...,
        min_length=1,
        max_length=2,
        description="新建项目锚定的参考样例 id 列表（1-2 个）。",
    )


class ProjectUpdateRequest(BaseModel):
    """PATCH /api/project/{id}：所有字段可选，None 表示不动。"""

    name: Optional[str] = Field(default=None, max_length=80)
    brief: Optional[str] = Field(default=None, max_length=500)
    video_goal: Optional[str] = Field(default=None, max_length=500)
    settings: Optional[ComposeSettings] = None
    last_plan_id: Optional[str] = None
    last_render_job_id: Optional[str] = None
    status: Optional[ProjectStatus] = None
    step_states: Optional[ProjectStepState] = None
    current_step: Optional[StepName] = None


class ProjectListResponse(BaseModel):
    items: list[Project]
