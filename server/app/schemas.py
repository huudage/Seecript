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

from pydantic import BaseModel, Field, computed_field, model_validator

# =========================================================================
# Common
# =========================================================================

GapStatus = Literal["ok", "warn", "miss"]
"""槽位匹配状态：✅ 完全命中 / ⚠️ 勉强命中 / ❌ 缺口需补全"""

FillAction = Literal["rerank", "copy", "aigc", "aigc_image"]
"""缺口补全动作：
- rerank      结构重排（从素材库挑一个最匹配的）
- copy        字卡画面（LLM 写文案 + 设计字卡）
- aigc        Seedance T2V 短片生成（动态画面）
- aigc_image  Seedream 文生图（静态画面，按 scene.duration 定格成 mp4）

aigc_image 与 aigc 共享同一套准备链（参考图分析 → 提示词生成 → 用户调参），
只在最后一步把 Seedance 视频生成换成 Seedream 静图——成本与等待显著低于视频，
适合"展示型镜头不需要运动"的场景（人物/产品/场景静态特写）。
"""

Variant = Literal["A", "B"]
"""AB 双版本渲染标识"""

JobStatus = Literal["pending", "running", "succeeded", "failed", "cancelled"]


VideoType = Literal["marketing", "editing", "motion_graph"]
"""视频类型——风格提示。决定 BGM / 字幕 / 转场 / 封面，不再决定段落结构。

- marketing      营销/带货/动态海报：节奏紧凑、强字幕、大色块、行动引导
- editing        剪辑/Vlog/纪录：情绪曲线、空镜与高潮、长镜与余韵
- motion_graph   合成动画/信息可视化：标题入场、爆点切换、落版收尾
"""


SectionRole = str
"""段落角色——stage-16 起改为 free-string,支持 5 种结构模式 17 个角色名。

历史 4 角色（dramatic 模式）仍是 SectionRole 的合法值,新模式按 STRUCTURAL_PATTERNS 扩展。
合法性由 helper `allowed_roles_for(pattern)` 在 LLM 节点输出时按模式校验。
"""


StructuralPattern = Literal["dramatic", "stepwise", "listicle", "atmospheric", "info_dense", "vlog"]
"""视频结构模式——6 选 1,决定下游使用哪套角色体系。

- dramatic      戏剧四段式：起承转合(opening/development/climax/closing)
- stepwise      线性步骤式：教程/操作流程(intro/step_N/recap)
- listicle      并列盘点式：榜单/N 个理由(hook/item_N/closer)
- atmospheric   氛围推进式：Vlog/纪录(establish/flow/peak/resolve)
- info_dense    信息密集快切式：信息可视化(title_card/info_block/payoff)
- vlog          日常 Vlog 无高潮型：开场/日常×N/收尾(intro_scene/daily_N/wrap_up)
                没有强情绪峰值,允许 LLM 在「没有明显高潮」时落到这套结构,避免硬塞 climax。
"""


Tempo = Literal["slow", "medium", "fast", "peak", "deceleration"]
"""节奏标签——对单镜头/单段落的节奏感分类。"""


# ---- 5 种结构模式的角色分类表 -------------------------------------------------
# 每种模式按 4 类组织角色:opening 类(开场)/main 类(主体)/peak 类(峰值,可空)/closing 类(收尾)
# `step_*` / `item_*` 是通配符,匹配 step_1, step_2, ... 这种动态后缀。
STRUCTURAL_PATTERNS: dict[str, dict[str, list[str]]] = {
    "dramatic":    {"opening": ["opening"],     "main": ["development"], "peak": ["climax"], "closing": ["closing"]},
    "stepwise":    {"opening": ["intro"],       "main": ["step_*"],      "peak": [],         "closing": ["recap"]},
    "listicle":    {"opening": ["hook"],        "main": ["item_*"],      "peak": [],         "closing": ["closer"]},
    "atmospheric": {"opening": ["establish"],   "main": ["flow"],        "peak": ["peak"],   "closing": ["resolve"]},
    "info_dense":  {"opening": ["title_card"],  "main": ["info_block"],  "peak": [],         "closing": ["payoff"]},
    "vlog":        {"opening": ["intro_scene"], "main": ["daily_*"],     "peak": [],         "closing": ["wrap_up"]},
}


def _role_match(role: str, slot_specs: list[str]) -> bool:
    """把 role 字符串和 slot 规格列表(可能含 `step_*` 通配符)做匹配。"""
    role_l = (role or "").lower().strip()
    for spec in slot_specs:
        if spec.endswith("_*"):
            prefix = spec[:-1]  # 'step_'
            if role_l.startswith(prefix):
                rest = role_l[len(prefix):]
                if rest.isdigit() or rest == "":
                    return True
        elif role_l == spec.lower():
            return True
    return False


def role_is_opening(role: str, pattern: str) -> bool:
    """role 是否属于 pattern 的开场类。"""
    p = STRUCTURAL_PATTERNS.get(pattern, STRUCTURAL_PATTERNS["dramatic"])
    return _role_match(role, p["opening"])


def role_is_closing(role: str, pattern: str) -> bool:
    p = STRUCTURAL_PATTERNS.get(pattern, STRUCTURAL_PATTERNS["dramatic"])
    return _role_match(role, p["closing"])


def role_is_peak(role: str, pattern: str) -> bool:
    p = STRUCTURAL_PATTERNS.get(pattern, STRUCTURAL_PATTERNS["dramatic"])
    return _role_match(role, p["peak"])


def role_is_main(role: str, pattern: str) -> bool:
    p = STRUCTURAL_PATTERNS.get(pattern, STRUCTURAL_PATTERNS["dramatic"])
    return _role_match(role, p["main"])


def allowed_roles_for(pattern: str, *, max_dynamic: int = 8) -> list[str]:
    """枚举 pattern 下所有合法 role 名(`step_*` 展开为 step_1..step_max_dynamic)。"""
    p = STRUCTURAL_PATTERNS.get(pattern, STRUCTURAL_PATTERNS["dramatic"])
    out: list[str] = []
    for klass in ("opening", "main", "peak", "closing"):
        for spec in p[klass]:
            if spec.endswith("_*"):
                prefix = spec[:-2]  # 'step'
                out.extend([f"{prefix}_{i}" for i in range(1, max_dynamic + 1)])
            else:
                out.append(spec)
    return out


def all_role_names() -> list[str]:
    """所有 5 种模式下的全部静态 role 名(不含 step_N/item_N 动态序号),供 LLM blocklist 用。"""
    seen: set[str] = set()
    out: list[str] = []
    for p in STRUCTURAL_PATTERNS.values():
        for klass in ("opening", "main", "peak", "closing"):
            for spec in p[klass]:
                if spec.endswith("_*"):
                    base = spec[:-2]  # 'step' / 'item'
                    if base not in seen:
                        seen.add(base)
                        out.append(base)
                else:
                    if spec not in seen:
                        seen.add(spec)
                        out.append(spec)
    return out


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


ManifestStatus = Literal["none", "ready"]
"""样例的拆解状态：
- none   未拆解：只有 video.mp4 + meta.json，Compose 拿不到
- ready  已有 ≥1 个版本槽：当前 active 槽供 Compose / Library 列表用
"""


class SampleVersionInfo(BaseModel):
    """单个版本槽的列表元信息。前端按 updated_at 升序展示标签 v1/v2。"""

    slot_id: str = Field(..., description="后端内部 slot id（8 hex），前端不展示，调用 API 时回传")
    label: str = Field(..., description="展示用标签，如 v1/v2，由列表中的位置决定")
    updated_at: float = Field(..., description="manifest 文件 mtime（秒）")
    is_active: bool = Field(..., description="是否当前 active 槽（Compose / Library 用的就是它）")


class ReferenceVersion(BaseModel):
    """Compose 选用的「拆解版本」唯一指针：(sample_id, slot_id)。

    stage-15 起 Plan/Project 不再按 sample_id 默认拿 active 槽，而是显式按
    (sample_id, slot_id) pair 加载具体版本，让用户能拿同一样例的 v1/v2 做对比迁移。
    """

    sample_id: str = Field(..., description="所属样例 id")
    slot_id: str = Field(..., description="该样例下的 slot id（8 hex）")


class ReferenceListItem(BaseModel):
    """`GET /api/references` 列表项：拍平所有 sample × 所有槽。

    供 Compose 顶部 ReferencePicker 选 1-2 个版本作为结构参考用。
    """

    sample_id: str
    sample_title: str
    slot_id: str
    label: str = Field(..., description="该 sample 下的展示标签 v1/v2")
    video_type: VideoType
    scene: str
    duration_seconds: float
    shot_count: int
    cover_url: str
    source: "LibrarySource" = Field(default="system")
    updated_at: float
    is_active: bool


class ManifestSaveRequest(BaseModel):
    """`POST /api/sample/{id}/manifest/save` body：把前端草稿（zustand 内存里的 SampleManifest）
    落到资产库的版本槽。槽未满时 create_version；槽满 + replace_slot 时覆盖；槽满 + 无 replace_slot
    返 409 让前端弹「保存覆盖对话框」让用户挑要替换的 v1/v2。
    """

    manifest: "SampleManifest" = Field(..., description="完整的拆解结果（前端编辑后的版本）")
    replace_slot: Optional[str] = Field(
        default=None,
        description="槽满时显式指定要覆盖的 slot_id；槽未满时必须为空（路由层预校验）。",
    )


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
    manifest_status: ManifestStatus = Field(
        default="none",
        description="拆解状态。none=未拆；ready=至少 1 个版本槽可用。",
    )
    version_count: int = Field(
        default=0,
        ge=0,
        description="已存在的版本槽数量（0–MAX_VERSIONS=2）。",
    )
    active_slot: Optional[str] = Field(
        default=None,
        description="当前 active slot id；version_count=0 时为空。",
    )


class ShotTarget(BaseModel):
    """stage-25：分镜内的"目标"——一个分镜可能聚焦多个目标（人/物/场景/字）。

    设计动机：
    - 带货视频一镜常含 主播+商品 两个目标
    - 文物展一镜常含 文物+解说字幕 两个目标
    - 现状缺这个层级 → AIGC 补齐时只能"按整段画面描述"出图，丢失目标分布
    - decompose_agent 给样例 Shot 标 targets（用于"结构迁移参考"，不直接搬主体）
    - plan_agent 给 ShotPlan 写 targets（声明本镜要呈现哪些目标，gap_agent 据此多图合成）
    - aigc_prompt_agent 多目标拆 N 个 Seedream prompt → 各出 1 张 → 合成给 T2V
    """

    kind: Literal[
        "person",      # 人物（主播/客户/路人）
        "object",      # 物品（商品/道具/文物/食物）
        "scene",       # 场景（展厅/街景/室内/景观）
        "text",        # 字幕/标题/字卡（仅作为目标声明，不是画面元素）
        "graphic",     # 动效图形（仅样例参考，迁移时 plan_agent 必须替换为目标域）
        "other",
    ] = Field(default="object", description="目标类型，决定下游图像生成的 prompt 模板")
    name: str = Field(
        ...,
        max_length=24,
        description="目标的简短名（≤12 中文字），如『主播』『青铜鼎』『展厅全景』『品牌字』",
    )
    role: Optional[Literal["primary", "secondary", "background"]] = Field(
        default=None,
        description="目标在本镜中的位置：primary=主体 / secondary=陪体 / background=背景。None 等价 primary。",
    )
    visual_hint: Optional[str] = Field(
        default=None,
        max_length=80,
        description="可选：该目标的视觉特征/动作/构图（≤40 字），辅助 Seedream 出图",
    )


class Shot(BaseModel):
    """PySceneDetect 输出的镜头切片（stage-23 起作为「分镜」最小表达单元）。"""

    index: int
    start: float
    end: float
    duration: float
    thumbnail_url: Optional[str] = None
    transcript: Optional[str] = Field(default=None, description="本镜头对应的 ASR 口播片段（原始）")
    tags: list[str] = Field(default_factory=list, description="VLM 帧打标（封面风格/转场/字幕样式等)")
    visual_summary: str = Field(
        default="",
        max_length=120,
        description="画面内容描述：这一镜的视觉主体/动作/构图（≤60 中文字）",
    )
    script: str = Field(
        default="",
        max_length=200,
        description="本镜口播/字幕脚本——有 voice 时清洗自 transcript；无 voice 时由 LLM 看画面写代字幕",
    )
    merged_from: list[int] = Field(
        default_factory=list,
        description="语义合并保留：被并入的原 shot indices；len>1 表示「N 镜合 1」",
    )
    targets: list[ShotTarget] = Field(
        default_factory=list,
        max_length=4,
        description="stage-25：本镜的目标分布（可空，老 manifest 默认 [])。"
                    "样例 Shot 的 targets 仅作为 plan_agent 节奏参考——「graphic 类的莫比乌斯环」"
                    "等具体视觉物体绝不会被原样迁移到目标主题。",
    )


class Utterance(BaseModel):
    """ASR 逐句时间戳。时间单位均为秒（asr_client 已从毫秒换算）。

    模块 5 字幕烧录直接读这个列表；模块 2 decompose 用它做"按 shot 时间窗映射 transcript"，
    替代旧版按字符比例切分（会把英文单词从中间截断）。
    """

    text: str
    start: float
    end: float


class RhythmCurve(BaseModel):
    """节奏 / 情绪走势曲线——前端拿来画"BGM 与视频结构契合度"图。

    R1 改版（2026-06）：
    - mood_curve / bgm_fit_score / bgm_fit_note 是主用字段;前端只画 mood_curve + bgm_energy 两条平滑线
      + 一个契合度评分文案,不再展示 cut_density / tempo_bpm。
    - cut_density / tempo_bpm 保留为兼容字段(老 manifest 可能携带,前端忽略);新数据写空列表 / None。
    """

    times: list[float] = Field(..., description="采样时间点（秒）")
    bgm_energy: list[float] = Field(default_factory=list, description="librosa RMS 能量曲线,归一到 [0,1]")
    cut_density: list[float] = Field(default_factory=list, description="[已弃用] 单位时间镜头切换密度——保留兼容,前端不再读")
    tempo_bpm: Optional[float] = Field(default=None, description="[已弃用] 整体 BPM——保留兼容,前端不再读")
    mood_curve: list[float] = Field(
        default_factory=list,
        description="情绪走势 0..1。主体平稳,峰值段抬升,收尾下降——按段落结构低频平滑,不跟节拍跳动",
    )
    bgm_fit_score: Optional[float] = Field(
        default=None,
        description="BGM 能量与 mood_curve 的相关度 0..1;接近 1 说明 BGM 节奏与视频结构同步",
    )
    bgm_fit_note: Optional[str] = Field(
        default=None,
        description="一句话说明 BGM 是否服务于视频结构（命中 / 错位 / 平稳 / 过度起伏 等）",
    )


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

    Stage-16 起加 structural_pattern (5 选 1) 决定整片角色体系；tempo 给段落节奏锚定；
    estimated_segments 改名（原 suggested_segments，2-8 段，listicle 模式上限放宽到 8）。
    """

    archetype: str = Field(..., max_length=40, description="视频原型，如『艺术展宣传』『带货种草』『城市 Vlog』")
    narrative_summary: str = Field(..., max_length=200, description="一段话讲清整支视频在说什么、怎么说")
    structural_pattern: StructuralPattern = Field(
        default="dramatic",
        description="结构模式：dramatic 戏剧弧 / stepwise 步骤 / listicle 盘点 / atmospheric 氛围 / info_dense 信息密集",
    )
    tempo: Optional[Tempo] = Field(
        default=None,
        description="整体节奏：slow/medium/fast/peak/deceleration；可选，仅对 dramatic/info_dense 强相关",
    )
    estimated_segments: int = Field(..., ge=2, le=8, description="LLM 估计切几段（2-8，listicle 上限到 8）")
    tone: str = Field(default="", max_length=30, description="基调描述：『冷静克制』『高燃热血』『诙谐自嘲』等")

    @model_validator(mode="before")
    @classmethod
    def _migrate_suggested_segments(cls, data: Any) -> Any:
        """旧 manifest 用 `suggested_segments`——映射成 `estimated_segments`。

        新字段范围放宽到 2-8（旧字段 3-6），夹在新范围内即可，不丢数据。
        """
        if not isinstance(data, dict):
            return data
        if "estimated_segments" not in data and "suggested_segments" in data:
            data["estimated_segments"] = data.pop("suggested_segments")
        return data


HighlightAspect = Literal["hook", "narrative", "visual", "audio", "rhythm", "copy", "cta"]
ImprovementAspect = Literal[
    "hook", "narrative", "visual", "audio", "rhythm", "copy", "cta", "structure"
]


class HighlightItem(BaseModel):
    """全片亮点条目——LLM 复盘视频强点；plan_agent 拿来作为「迁移时必须保留这些表达」的硬约束。"""

    aspect: HighlightAspect = Field(..., description="亮点类型（钩子/叙事/视觉/音频/节奏/文案/CTA）")
    text: str = Field(..., max_length=80, description="≤40 字描述这条亮点是什么")
    shot_indices: list[int] = Field(
        default_factory=list,
        description="可选：相关 shot.index；用于前端高亮对应行",
    )


class ImprovementItem(BaseModel):
    """全片改进建议——LLM 指出弱点 + 写「怎么改」；plan_agent 当作迁移时主动规避的方向。"""

    aspect: ImprovementAspect = Field(..., description="改进维度（含 structure）")
    text: str = Field(..., max_length=80, description="≤40 字描述这个不足是什么")
    suggestion: str = Field(..., max_length=120, description="具体怎么改的建议（≤60 字）")
    shot_indices: list[int] = Field(default_factory=list, description="可选：相关 shot.index")


class SampleAnalysis(BaseModel):
    """全片复盘——给定 understanding + sections + shots + audio_understanding 后的 LLM 综合评估。

    用于 Decompose 页 AnalysisCard 展示（亮点 / 改进），并作为 plan_agent / copy_outline_agent
    的迁移引导：保留亮点，规避改进项。
    """

    highlights: list[HighlightItem] = Field(default_factory=list, max_length=6)
    improvements: list[ImprovementItem] = Field(default_factory=list, max_length=6)
    overall_score: int = Field(default=70, ge=0, le=100, description="主观打分（LLM 给出，0-100）")
    one_line_verdict: str = Field(
        default="",
        max_length=60,
        description="一句话总评（≤30 字），可作为前端大字标题",
    )


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
    analysis: Optional[SampleAnalysis] = Field(
        default=None,
        description=(
            "stage-23 起：全片亮点 + 改进建议 + 总评分。LLM 在 segment 之后跑一次综合评估。"
            "旧版本槽未跑过此步骤时为 None；plan_agent 会兜底处理。"
        ),
    )
    audio_understanding: Optional["BGMAnalysis"] = Field(
        default=None,
        description=(
            "LLM 多模态音频理解结果。decompose 跑完后异步算一遍：抽样例视频音轨到 samples/{sid}/audio.mp3，"
            "送 doubao-seed multimodal input_audio，输出 energy_shape / climaxes / calm_segments / overall_advice。"
            "复用 BGMAnalysis schema（theme_fit_* 当作『音频能量与视频题材的契合度』解读）。"
            "失败 / 未配 ARK / 老缓存 → None，前端兜底显示 librosa 的 BPM + 单点 peak。"
        ),
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
    nl_prompt: Optional[str] = Field(
        default=None,
        max_length=500,
        description="用户自由文本指引（『更看重开场』『压短结尾』之类）；注入到 LLM 视频理解+切段 prompt。",
    )
    replace_slot: Optional[str] = Field(
        default=None,
        description="版本槽已满时要覆盖的 slot_id；槽未满时必须为空。后端在路由层预校验。",
    )
    persist: bool = Field(
        default=False,
        description=(
            "是否在流水线跑完后直接落到版本槽。"
            "stage-15 起默认 False（草稿态，前端拿 SSE done 里的 manifest 自己存 zustand），"
            "用户点「保存到资产库」时再走 POST /sample/{id}/manifest/save 入库。"
            "True 走老行为（直接 create_version），仅供需要无人值守自动入库的内部场景使用。"
        ),
    )


class DecomposeSubmitResponse(BaseModel):
    job_id: str


# =========================================================================
# Module 3 — 新素材上传 (Material)
# =========================================================================


class MaterialShot(BaseModel):
    """视频素材切片：PySceneDetect 检测出的镜头边界 + 多模态 LLM 描述 + 角色推荐。

    用于 plan/build 阶段智能选片：从一段长视频里挑出对每个 section role 最匹配的镜头，
    而不是简单按 scene.duration 顺位 trim 前 N 秒。
    """

    index: int = Field(description="本镜头在素材里的序号，从 0 开始。")
    start: float = Field(ge=0.0, description="镜头起点（秒）。")
    end: float = Field(gt=0.0, description="镜头终点（秒）；end > start。")
    duration: float = Field(gt=0.0, description="镜头时长 = end - start。")
    thumbnail_url: Optional[str] = Field(
        default=None,
        description="代表帧（中间帧）的 URL，前端 hover 预览。/uploads/<sid>/shots/<material_id>-<i>.jpg",
    )
    caption: Optional[str] = Field(
        default=None,
        description="多模态 LLM 给的一句话画面描述。",
    )
    action_density: float = Field(
        default=0.0, ge=0.0, le=1.0,
        description="动作密度 0~1：1=全屏运动/快切，0=完全静态。决定能否当 hook/climax。",
    )
    recommended_role: Optional[SectionRole] = Field(
        default=None,
        description="LLM 推荐这个镜头适合放进哪个 role 段（opening/development/climax/closing）。",
    )


class Material(BaseModel):
    """用户上传的素材分析结果（含 多模态 LLM 标签 + 段落推荐 + 高光评分）。"""

    material_id: str
    filename: str
    media_type: Literal["video", "image", "audio"]
    duration_seconds: Optional[float] = None
    thumbnail_url: Optional[str] = None
    file_url: Optional[str] = Field(
        default=None,
        description=(
            "可访问的素材原文件 URL，例如 /uploads/<sid>/<material_id>_<filename>。"
            "前端 Remotion Player 实时预览时按此 URL 直接喂 <Video src> / <Audio src>。"
            "Optional 是为了兼容老 plan 持久化里没有该字段的 Material 记录。"
        ),
    )
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
    # ---- 视频预处理（Stage 20）：MaterialShot 切片 + 进度状态 ----
    preprocess_status: Literal["pending", "running", "ready", "failed", "skipped"] = Field(
        default="skipped",
        description=(
            "视频预处理阶段：pending=入队、running=切片+VLM 分析中、ready=完成可被 _pick 用、"
            "failed=失败但素材仍可用（fallback truncate）、skipped=非视频或未启用预处理。"
        ),
    )
    preprocess_error: Optional[str] = Field(
        default=None,
        description="failed 时一句话原因；前端进度条 hover 提示。",
    )
    shots: list[MaterialShot] = Field(
        default_factory=list,
        description="PySceneDetect 切片产物；空列表 = 未预处理或失败回退。",
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
        description="兼容老前端：留作 project_id 的别名。",
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
    thinking: list[str] = Field(
        default_factory=list,
        description="Agent 思考链——2-4 条短句，用于前端可视化『LLM 是怎么从段落上下文推到 prompt 的』",
    )


# --- AIGC 图片参考工作流（D2）：spec → seedream → tail-frame --------------

class ImageSpec(BaseModel):
    """一张"建议参考图"的元数据。AI 先看段落语境拍板需要几张图。"""

    slot_id: str = Field(..., description="同 gap 内唯一，前端按此 key 收集 imageSlots")
    caption: str = Field(..., max_length=80, description="人类语言描述（『展厅入口仰拍』）")
    prompt: str = Field(..., max_length=300, description="若用户选 Seedream 生成，默认 prompt")
    ratio: str = Field(default="16:9", description="豆包 Seedream 支持 16:9/9:16/1:1/4:3/3:4")


class AigcImageSpecRequest(BaseModel):
    """`POST /api/gap/aigc-image-spec` —— 让 LLM 判断本段需要几张参考图。"""

    gap_id: str
    hint: Optional[str] = Field(default=None, max_length=200)


class AigcImageSpecResponse(BaseModel):
    gap_id: str
    specs: list[ImageSpec] = Field(default_factory=list)
    thinking: list[str] = Field(
        default_factory=list,
        description="Agent 思考链——2-4 条短句，讲清 LLM 怎么判断本段需要哪几张参考图",
    )


class AigcSeedreamRequest(BaseModel):
    """`POST /api/gap/aigc-seedream` —— 调豆包 Seedream 出 1 张图。"""

    prompt: str = Field(..., min_length=2, max_length=1500)
    ratio: str = Field(default="16:9")
    n: int = Field(default=1, ge=1, le=4)


class SeedreamImage(BaseModel):
    url: str
    width: int
    height: int


class AigcSeedreamResponse(BaseModel):
    images: list[SeedreamImage] = Field(default_factory=list)


class AigcTailFrameRequest(BaseModel):
    """`POST /api/gap/aigc-tail-frame` —— 抽前一段视频的尾帧。"""

    plan_id: str
    scene_id: str = Field(..., description="本段 scene_id，会回查前一段 main_track")


class AigcTailFrameResponse(BaseModel):
    frame_data_url: str = Field(..., description="data:image/jpeg;base64,... 直接送 Seedance")


# --- Copy = Text Card Agent（stage-19）：字卡画面策划 ------------------------

EmotionalHook = Literal["anxiety", "wow", "anticipation", "twist", "resonance"]

TextCardFontFamily = Literal["bold_sans", "serif_classic", "handwriting", "tech_mono"]
"""字体族——pipeline 映射到 var/fonts/*.ttf。
- bold_sans       粗黑：信息密度高、宣告感强（默认）
- serif_classic   衬线：故事感、调性内敛
- handwriting     手写：温度、共鸣
- tech_mono       科技等宽：数据 / 反差 / 极客感
"""

TextCardLayout = Literal["center", "top", "bottom", "split_top_bottom"]
"""字卡布局——主标 / 副标 在画面上的位置编排。"""

TextCardBgMode = Literal["solid", "gradient", "image_blur", "dark_overlay"]
"""背景模式：纯色 / 渐变 / 模糊接上一段尾帧 / 暗罩。"""

TextCardAnimation = Literal["fade_in", "typewriter", "bounce_word", "zoom_pop"]
"""动画：淡入 / 打字机 / 词反弹 / 放大弹出。"""


class TextCardSpec(BaseModel):
    """字卡画面规格——驱动 ffmpeg 渲染纯文字短片；前端面板逐项可调，
    pipeline._render_text_card 按字段映射到 drawtext + bg + fade。"""

    main_text: str = Field(default="", max_length=24, description="主标语（≤24 字大字）")
    sub_text: str = Field(default="", max_length=40, description="副标语（≤40 字，可空）")
    font_family: TextCardFontFamily = Field(default="bold_sans")
    layout: TextCardLayout = Field(default="center")
    bg_mode: TextCardBgMode = Field(default="solid")
    bg_color: str = Field(default="#0F172A", pattern=r"^#[0-9A-Fa-f]{6}$", description="背景色 hex")
    text_color: str = Field(default="#FFFFFF", pattern=r"^#[0-9A-Fa-f]{6}$", description="主文字色 hex")
    accent_color: str = Field(default="#22D3EE", pattern=r"^#[0-9A-Fa-f]{6}$", description="副标 / 装饰色 hex")
    animation: TextCardAnimation = Field(default="fade_in")
    emoji_decor: list[str] = Field(default_factory=list, max_length=3, description="装饰 emoji，最多 3 个")
    duration_seconds: float = Field(default=4.0, ge=1.5, le=15.0, description="字卡时长")


class CopyOutline(BaseModel):
    """字卡画面大纲——给前端调参面板填默认值，再随 fill 请求回传 LLM 强化生成。

    stage-19 起 copy 动作不再生成口播一句话，而是生成"字卡画面"——
    所以 outline 字段也从『一句文案』变成『一份字卡推荐 spec』。
    保留 core_message/emotional_hook/forced_keywords 字段作为字卡策划锚点。
    """

    main_text: str = Field(
        default="",
        max_length=24,
        description="LLM 推荐的主标语（≤24 字）",
    )
    sub_text: str = Field(
        default="",
        max_length=40,
        description="LLM 推荐的副标语（≤40 字，可空）",
    )
    core_message: str = Field(
        default="",
        max_length=80,
        description="本段最该说的核心信息（≤20 字最佳，最大 80 字）；策划字卡时作为锚点",
    )
    emotional_hook: EmotionalHook = Field(
        default="resonance",
        description="情绪钩子：焦虑/惊艳/期待/反转/共鸣 五选一 —— 影响推荐配色与字体",
    )
    must_include_keywords: list[str] = Field(
        default_factory=list,
        description="从 compose_settings.keywords 中本段最该承载的 1-2 个，会落进 main_text/sub_text",
    )
    recommended_spec: TextCardSpec = Field(
        default_factory=TextCardSpec,
        description="LLM 推荐的字卡 spec —— 字体 / 布局 / 配色 / 动画 / emoji；用户在前端可改",
    )
    tone_lean: str = Field(
        default="",
        max_length=40,
        description="在全局 tone 基础上的微调（『开场加紧 / 收尾放缓』）",
    )


class CopyOutlineRequest(BaseModel):
    """`POST /api/gap/copy-outline` —— 让 LLM 先给文案写作大纲，再让用户调参后下单生成。"""

    gap_id: str
    hint: Optional[str] = Field(default=None, max_length=200)


class CopyOutlineResponse(BaseModel):
    gap_id: str
    outline: CopyOutline
    thinking: list[str] = Field(
        default_factory=list,
        description="Agent 思考链——2-4 条短句，给前端可视化 LLM 是怎么定下大纲的",
    )


class GapFillAllRequest(BaseModel):
    """`POST /api/gap/fill-all` —— 对 plan_id 下所有非 ok 缺口顺序触发补全。

    action:
      - "aigc"（默认，向后兼容）：每个缺口走 Seedance T2V 链式生成
      - "aigc_image"：每个缺口走 Seedream 文生图 + Remotion 动效渲染（成本远低于 T2V）
      - "copy"：每个缺口走 LLM 文案补全（用 gap.requirement 作为 prompt_hint）
    """

    plan_id: str
    action: Literal["copy", "aigc", "aigc_image"] = Field(
        default="aigc",
        description="批量补全使用的动作；rerank 不支持批量（依赖人工挑选）",
    )
    prompt_template: Optional[str] = Field(
        default=None,
        max_length=200,
        description="可选自定义 prompt 模板（仅 aigc），{requirement} 占位会被替换为 gap.requirement。",
    )
    skip_gap_ids: list[str] = Field(
        default_factory=list,
        description=(
            "前端已采纳/已完成的 gap_id 列表——后端在批量补全时跳过这些。"
            "因为 gap_store 的 status 不会随 fill 落地（fills 主要走前端 zustand），"
            "如果不传，已有字卡历史的镜头会被重新生成。"
        ),
    )
    existing_text_cards: list[TextCardSpec] = Field(
        default_factory=list,
        description=(
            "前端已采纳的字卡 spec 列表——作为风格样板透传给每个 batch fill。"
            "之所以由前端传：fill-all 调用时 plan_store 中的 plan_id 往往是『旧版』"
            "（runAnalyze 尚未跑完会签发新 plan_id），后端从 plan.main_track 取 text_card_spec 会拉到空。"
            "由前端直接传 fills 里已 ok 的 TextCardSpec 数组，绕过 plan 时序竞态。"
        ),
    )


class GapFillAllResponse(BaseModel):
    plan_id: str
    fills: list["FillResult"] = Field(default_factory=list, description="成功生成的 fills，顺序与 gap 一致")
    failed_gap_id: Optional[str] = Field(
        default=None,
        description="部分失败时第一个失败的 gap_id；None 表示全部成功。",
    )
    stopped_reason: Optional[str] = None


AnimationType = Literal["ken-burns", "parallax", "storyboard", "keyframe_morph", "static"]
"""单图 / 多图动效类型，与 remotion/src/AnimatedImage.tsx 镜像。"""

MotionDirection = Literal["in", "out", "pan-left", "pan-right", "pan-up", "pan-down"]


class AnimationSpec(BaseModel):
    """AI 生图动效规格——绑定在 FillResult.animation_spec / Scene.animation_spec。

    pipeline 渲染时优先看 engine：
    - 'remotion'：调 remotion_renderer 渲带动效 mp4（成本：node 渲 1s ≈ 1s CPU）
    - 'ffmpeg' / None：回落到 ffmpeg image_to_video 静帧（最快兜底）

    单图（image_urls 长度 = 1）：animation_type 推荐 'ken-burns' / 'parallax' / 'static'。
    多图（image_urls 长度 > 1）：推荐 'storyboard' / 'keyframe_morph'。

    与 Seedream multi-image 协同：n_shots > 1 时 plan.py 把 section 切成 N 子 Scene，
    每个子 Scene 持有一张图；Scene.animation_spec.image_urls 长度恒为 1（单图动效）。
    若用户选 keyframe_morph，则不切子 Scene，整个 section 作单 Scene，image_urls=[N urls]。
    """

    engine: Literal["remotion", "ffmpeg"] = Field(
        default="ffmpeg",
        description="渲染引擎：remotion 带动效 / ffmpeg 静帧。",
    )
    animation_type: AnimationType = Field(
        default="ken-burns",
        description="动效类型；单图选 ken-burns/parallax/static，多图选 storyboard/keyframe_morph。",
    )
    motion_direction: MotionDirection = Field(
        default="in",
        description="ken-burns 时缩放/平移方向；其它类型忽略。",
    )
    intensity: float = Field(
        default=0.3, ge=0.0, le=1.0,
        description="动效强度 0~1；0.3 温和、0.7 夸张。",
    )
    transition: Literal["cross-fade", "cut", "slide-left"] = Field(
        default="cross-fade",
        description="多图转场类型；单图忽略。",
    )
    transition_duration: float = Field(
        default=0.4, ge=0.0, le=2.0,
        description="多图转场时长（秒）。",
    )
    image_urls: list[str] = Field(
        default_factory=list,
        description="keyframe_morph 等多图动效用：保留 N 张图 URL 在同一 Scene 上，"
                    "由 remotion 渲染器一次性消费。单图动效不必填，pipeline 会从 Scene.aigc_image_url 取。",
    )


class FillResult(BaseModel):
    gap_id: str
    action: FillAction
    new_material_id: Optional[str] = Field(default=None, description="aigc 最后一段 task_id 或 rerank 选中的素材")
    narration: Optional[str] = Field(
        default=None,
        description="copy 动作字卡 main_text + sub_text 拼接（驱动可选 TTS）；stage-19 起非主输出",
    )
    text_card_spec: Optional["TextCardSpec"] = Field(
        default=None,
        description="copy 动作主输出：字卡画面 spec —— pipeline._render_text_card 按它真渲染 mp4",
    )
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
    aigc_image_url: Optional[str] = Field(
        default=None,
        description="aigc_image 动作产出：Seedream 文生图本地化路径（/aigc-images/<filename>）。"
                    "rebuild plan 时回填到对应 Scene.aigc_image_url。",
    )
    aigc_image_urls: list[str] = Field(
        default_factory=list,
        description="aigc_image 多镜头模式产出的 N 张图（同源 /aigc-images/...）。"
                    "n_shots > 1 时由 Seedream sequential 故事板生成，视觉一致；"
                    "plan.py 会把这段 AdaptedSection 展开成 N 个等长子 Scene，"
                    "每个子 Scene 取列表中一张图。单图模式留空，由 aigc_image_url 兜底。",
    )
    animation_spec: Optional["AnimationSpec"] = Field(
        default=None,
        description="aigc_image 动作时附带的 Remotion 动效 spec。"
                    "plan 重建时回填到对应 Scene.animation_spec，pipeline 渲染时用 remotion_renderer "
                    "渲成带动效的 mp4 而非 ffmpeg 静帧。None / engine=ffmpeg 时回落静帧。",
    )
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

class ShotPlan(BaseModel):
    """stage-24：分镜级拆解。AdaptedSection.shots 的最小单元。

    plan_agent 在改编 AdaptedSection 时会顺手把 content_description 拆成 1-3 个 ShotPlan
    （超过 5 个会被合并）。下游：
    - plan.py 把 N 个 ShotPlan 物化成 N 个 Scene（scene_id = sc-{sec.order}-sh-{shot.order}）
    - gap_agent / aigc_prompt_agent 按 shot 级拼提示词 → Seedance/Seedream 按 shot 各跑一次
    - 用户素材匹配（material_shot_matcher）按 shot.subject + visual_hint 选 MaterialShot
    - 字幕/口播按 shot.narration 切片，TTS 用 shot.duration 分配

    向后兼容：plan_agent 旧输出（无 shots[]）→ AdaptedSection.shots = [] →
    plan.py 在物化时若 shots 为空会自动生成 1 个 ShotPlan 包住整段（行为与旧版一致）。
    """

    order: int = Field(..., ge=0, description="本镜在 section 内的序号（从 0 开始）")
    subject: str = Field(
        default="",
        max_length=40,
        description="本镜画面主体（人物/物品/场景），如『主播口播』『青铜器特写』『展厅全景』",
    )
    visual: str = Field(
        ...,
        max_length=200,
        description="画面应呈现什么：主体 + 动作 + 构图 + 镜头语言（≤80 字）",
    )
    narration: str = Field(
        default="",
        max_length=200,
        description="本镜口播/字幕（可空——纯画面镜头允许无口播）",
    )
    duration_seconds: float = Field(
        default=2.5,
        ge=1.0,
        le=15.0,
        description="本镜目标时长，所有 shot 之和应等于 AdaptedSection.duration_seconds",
    )
    source_hint: Optional[Literal["sample", "user_material", "aigc_t2v", "aigc_image", "text_card"]] = Field(
        default=None,
        description="LLM 给的素材来源建议；None 时由 plan.py / gap_agent 按 fills 路由决定",
    )
    matched_material_id: Optional[str] = Field(
        default=None,
        description="匹配上的用户素材 material_id（shot_matcher 写入）",
    )
    matched_material_shot_index: Optional[int] = Field(
        default=None,
        description="匹配上的用户素材内分镜 index（MaterialShot.index）",
    )
    targets: list[ShotTarget] = Field(
        default_factory=list,
        max_length=4,
        description="stage-25：本镜要呈现的目标列表（0-4 个）。"
                    "plan_agent 在改编时按目标域（target_brief）声明，"
                    "下游 aigc_prompt_agent 按 N 个目标各拼 N 个 Seedream prompt → N 张图 → 合成给 T2V。"
                    "空列表 = 单目标（按 visual 整段出 1 张图，老路）。",
    )


class AdaptedSection(BaseModel):
    """LLM 改编后的段落结构。Plan 的"叙事单位"层，位于 Scene"剪辑单位"层之上。

    流程：样例 manifest.sections（真模型拆出的样例骨架）+ 用户 brief + video_goal
    → LLM 改编 → AdaptedSection[]（含每段 content_description 内容说明 + shots[] 分镜列表）。

    Scene 负责"用哪个素材切片、时长多少"；AdaptedSection 负责"这一段叙事上要讲什么"。
    stage-24：新增 shots[] 列表把段拆成 1-3 个 ShotPlan，下游补全/匹配/生成都按 shot 走。
    """

    section_id: str = Field(..., description="本 plan 内稳定 id，如 'sec-0'；Gap.section_id 反查")
    role: SectionRole = Field(..., description="段落角色（4 元枚举,全视频类型通用）")
    theme: str = Field(default="", max_length=20, description="紧贴用户主题的中文短标签（≤8 字）")
    content_description: str = Field(
        ...,
        max_length=300,
        description="内容说明：本段画面/口播应呈现什么；由 LLM 紧贴 brief+video_goal 生成",
    )
    shots: list[ShotPlan] = Field(
        default_factory=list,
        description=(
            "stage-24 分镜级拆解。1-3 个最佳，最多 5 个。"
            "为空时 plan.py 物化为 1 个 Scene 包整段（向后兼容）。"
        ),
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
    adaptation_note: str = Field(
        default="",
        max_length=60,
        description="改编说明：LLM 用一句话说本段相对样例如何变（如『压缩 20%，强化卖点』），≤60 字",
    )
    tempo: Optional[Tempo] = Field(
        default=None,
        description="本段节奏：slow/medium/fast/peak/deceleration；可选，仅 dramatic/info_dense 强用",
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
    parent_section_id: Optional[str] = Field(
        default=None,
        description=(
            "stage-24：本 Scene 所属 AdaptedSection.section_id，如『sec-0』。"
            "前端 FourTrackBoard 按 section 折叠展开 N 个 shot Scene 用。"
            "向后兼容：旧 plan 没有这个字段时前端按 scene.section（role）兜底分组。"
        ),
    )
    shot_order: int = Field(
        default=0,
        ge=0,
        description="stage-24：本 Scene 在所属 section 内的分镜序号（从 0 开始）；不切分时恒为 0。",
    )
    shot_subject: str = Field(
        default="",
        max_length=40,
        description="stage-24：本镜画面主体（人物/物品/场景），从 ShotPlan.subject 来；前端 chip 显示。",
    )
    source: Literal["sample", "user_material", "aigc_t2v", "aigc_image", "text_card"]
    source_ref: str = Field(..., description="样例镜头 id / material_id / Seedance/Seedream 任务返回的 media_id / text_card 的标识")
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
    aigc_image_url: Optional[str] = Field(
        default=None,
        description="source=aigc_image 时 Seedream 文生图本地化后的 URL（/aigc-images/<filename>）；"
                    "render pipeline 下载后 ffmpeg loop 成 scene.duration 长度的 mp4（静帧 + 静音）。",
    )
    animation_spec: Optional["AnimationSpec"] = Field(
        default=None,
        description="source=aigc_image 时的 Remotion 动效 spec；None / engine=ffmpeg 时走静帧回落。"
                    "由 fill_gap 阶段（FillResult.animation_spec）写入；plan rebuild 回填到 Scene。",
    )
    text_card_spec: Optional["TextCardSpec"] = Field(
        default=None,
        description="source=text_card 且来自 copy fill 时的字卡渲染 spec；为 None 时 _render_text_card 用默认 spec。",
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


EnergyShape = Literal["flat", "single_peak", "multi_peak", "build_up", "wave"]
"""BGM 能量形态——叙事性的整体走向，决定怎么和视频配合。

- flat         全程平稳：无明显峰值，适合科普/Vlog/治愈类视频做底色，不抢戏
- single_peak  单峰爆发：一条主高潮线（典型副歌），适合带 CTA / 卖点对比 / 反转视频
- multi_peak   多峰起伏：两次以上峰值，适合长剧情 / 多卖点串烧
- build_up     渐强推进：能量从低到高一直走，适合预告 / 蓄势 / 反差揭示
- wave         波浪起伏：高低反复，适合情绪 Vlog / 故事性叙事
"""


class BGMHighlight(BaseModel):
    """BGM 关键节点——高潮、转折、骤停等"值得用户对齐"的时间点。"""

    at_seconds: float = Field(ge=0.0, description="节点出现的时间（秒，相对 BGM t=0）")
    kind: Literal["climax", "drop", "build_start", "release", "break"] = Field(
        description="节点类型：climax 主高潮 / drop 骤降 / build_start 蓄势起点 / release 释放 / break 留白",
    )
    label: str = Field(max_length=24, description="节点小标，例『副歌入』『鼓点 drop』")
    fit_with_video: str = Field(
        max_length=80,
        description="建议把这个节点对齐到视频的什么动作（卖点出现 / 反转点 / CTA 起手）",
    )


class BGMCalmSegment(BaseModel):
    """BGM 平稳段——可以承载长口播 / 慢镜头的"安全区间"。"""

    start: float = Field(ge=0.0)
    end: float = Field(ge=0.0)
    note: str = Field(max_length=60, description="为什么这段适合做铺垫，例『纯钢琴留白，适合压口播』")


class BGMAnalysis(BaseModel):
    """LLM 音频理解结果：先定能量形态，再标关键节点 + 平稳段，最后给视频配合建议。

    设计取舍（v2）：
    - 旧版按 4-6 段强切色块罗列，对全程平稳的曲子很别扭、对真高潮迭起的曲子又割裂
    - 新版 energy_shape 一句话定调，climaxes 仅标真正值得对齐的"鼓点"（可空），calm_segments 标安全口播区间
    - 全程平稳的 flat 曲子可以 climaxes=[]，由 overall_advice 解释"为什么平稳反而合适"

    plan 绑定 BGM 时一次性算好放进 BGMConfig.analysis，渲染层不参与。
    """

    title_guess: str = Field(max_length=60, description="曲风/曲目猜测，无版权信息时给『钢琴抒情』『鼓点节拍』概括")
    mood_tags: list[str] = Field(default_factory=list, max_length=6, description="情绪标签 3-6 个")
    energy_shape: EnergyShape = Field(description="能量整体走向——决定视频该怎么用这首曲子")
    energy_shape_reason: str = Field(
        max_length=140,
        description="一句话讲为什么是这种形态（听到了什么），以及这种形态适合什么类型视频",
    )
    theme_fit_score: float = Field(ge=0.0, le=1.0, description="0-1：曲子与 brief 的契合度")
    theme_fit_reason: str = Field(max_length=140, description="一句话讲为什么契合或不契合")
    climaxes: list[BGMHighlight] = Field(
        default_factory=list,
        max_length=4,
        description="真正值得对齐的高潮/鼓点（0-3 个）；全程平稳时为空",
    )
    calm_segments: list[BGMCalmSegment] = Field(
        default_factory=list,
        max_length=4,
        description="平稳/留白区间，可以承载长口播 / 慢镜头",
    )
    overall_advice: str = Field(
        max_length=200,
        description="叙事性总建议：曲子和视频的配合策略（高潮放哪、平稳处怎么用、整体节奏怎么把）",
    )
    backend: str = Field(description="生成 backend：doubao_ark / mock")


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
    analysis: Optional[BGMAnalysis] = Field(
        default=None,
        description="LLM 音频理解结果：曲风/情绪/结构/视频匹配建议。绑定 BGM 时由 plan.py 异步填充，"
                    "失败/超时则保持 None，前端兜底用 librosa 的 peak/duration。",
    )

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


AspectRatio = Literal["9:16", "16:9", "1:1"]
"""画面比例 —— v2 起独立于 target_platform。

允许"B 站发竖屏"或"抖音发方版"等组合。aspect.py:aspect_for_settings 优先取此字段，
缺失时回落到 platform→ratio 老映射，老 plan 完全兼容。
"""


ToneStyle = Literal["tight_hype", "calm_narrative", "casual_daily", "professional_cool"]
"""整体调性 —— 影响 LLM 段落 prompt 倾向。

- tight_hype          紧凑高燃：快剪 + 强情绪 + 必有 climax
- calm_narrative      沉稳叙事：长镜头 + 余韵 + climax 可选
- casual_daily        轻松日常：口语化 + 节奏自然
- professional_cool   专业冷静：信息密度高 + 弱情绪 + 重数据
"""


MigrationPreference = Literal["mirror", "amp_emotion", "amp_pace"]
"""结构迁移倾向 (stage-23) —— 用户在 Compose Step1 选「我想要哪个版本」。

- mirror        平淡复刻：保持原片结构与调性，仅替换素材主题；不主动加强情绪 / 不加快节奏。
- amp_emotion   情绪增强（默认）：钩子更猛、收尾更有共鸣、CTA 更燃；情绪曲线抬高 20-30%。
- amp_pace      节奏紧凑：每段比原片缩短 10-25%，去掉缓冲过渡，让信息更密集。
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


FrameDesignPreset = Literal[
    "custom",
    "biennale-yellow",
    "blockframe",
    "blue-professional",
    "bold-poster",
    "broadside",
    "capsule",
    "cartesian",
    "cobalt-grid",
    "coral",
    "creative-mode",
]
"""frame.md 设计系统预设（参考 HyperFrames frame.md 模板）。

custom 表示完全由用户/LLM 在 FrameDesignSystem 字段里逐项填写；
其他值是预定义模板，packaging_agent 会按 preset 套对应 palette/typography/motion。
"""


MotionDensity = Literal["minimal", "balanced", "kinetic"]
"""画面动效密度：minimal=克制（适合品牌片）/ balanced=适中 / kinetic=高密度（适合社媒）。"""


class FrameDesignSystem(BaseModel):
    """frame.md —— 为相机重写的设计系统 token。

    HyperFrames 提出 frame.md 概念：把品牌 design.md 翻译成视频可消费的 token
    集合（atoms 神圣 / composition 自由 / numbers 来自脚本）。Seecript 用它
    统一全片包装风格——packaging/copy/aigc agent 都从这里读色板/字号/动效密度，
    避免 4 段视频视觉割裂。

    所有字段都可选；preset != custom 时空字段由 packaging_agent 按 preset 展开。
    """

    preset: FrameDesignPreset = Field(
        default="custom",
        description="设计系统预设。非 custom 时空字段会由 agent 按预设填充。",
    )
    palette: list[str] = Field(
        default_factory=list,
        max_length=6,
        description="主色板，HEX 字符串（最多 6 色）。第一色 = primary，第二 = accent，余下 = supporting。",
    )
    background_color: str = Field(
        default="",
        max_length=8,
        description="主背景色 HEX，例如 #03071e。空字符串=自动取 palette 反色。",
    )
    typography_display: str = Field(
        default="",
        max_length=40,
        description="标题字体族（display），例如 'Bebas Neue'。空=按 preset 默认。",
    )
    typography_body: str = Field(
        default="",
        max_length=40,
        description="正文字体族（body），例如 'Lato'。空=按 preset 默认。",
    )
    typography_mono: str = Field(
        default="",
        max_length=40,
        description="等宽字体（用于代码/数字），例如 'JetBrains Mono'。空=按 preset 默认。",
    )
    motion_density: MotionDensity = Field(
        default="balanced",
        description="动效密度。kinetic=高密度（推荐用于抖音/Reels）；minimal=克制（品牌/产品片）。",
    )
    grain_overlay: bool = Field(
        default=False,
        description="是否叠加颗粒/噪点纹理（参考 HyperFrames grain-overlay 组件）。",
    )
    vignette: bool = Field(
        default=False,
        description="是否叠加暗角（参考 HyperFrames vignette 组件）。",
    )
    notes: str = Field(
        default="",
        max_length=200,
        description="额外风格备注，自由文本。例：'阳光调，避免冷蓝'。",
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
    aspect_ratio: AspectRatio = Field(
        default="9:16",
        description="画面比例（v2 显式字段，独立于 target_platform）。"
                    "允许 B 站发竖屏、抖音发方版等组合；缺省走 9:16。",
    )
    tone: ToneStyle = Field(
        default="tight_hype",
        description="整体调性。影响 LLM 段落结构与口播倾向。",
    )
    migration_preference: MigrationPreference = Field(
        default="amp_emotion",
        description=(
            "结构迁移倾向（stage-23）：mirror=平淡复刻 / amp_emotion=情绪增强 / amp_pace=节奏紧凑。"
            "plan_agent + copy_outline_agent + aigc_prompt_agent 都会读这个字段调 prompt。"
        ),
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
    subtitle_enabled: bool = Field(
        default=False,
        description="是否生成 / 烧入字幕（独立于 TTS）。默认 False=纯画面无字幕；"
                    "用户在 step2 字幕轨上点开关后才把 scene.narration 作为字幕渲染。"
                    "`scene.text_card_spec is not None` 的段落始终跳过字幕（字卡画面已自带可读文字）。",
    )
    voiceover_enabled: bool = Field(
        default=False,
        description="是否做 TTS 口播合成（step3）。默认 False=纯 BGM 视频，不调 TTS；"
                    "True 时对每段 scene.narration 做 ARK TTS 合成并混入主轨。"
                    "字幕显隐由 subtitle_enabled 决定，与 TTS 解耦——可以只口播不上字幕，也可以只上字幕不口播。",
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
    frame_design: FrameDesignSystem = Field(
        default_factory=FrameDesignSystem,
        description="frame.md 设计系统 token（色板/字体/动效密度），全片包装统一。"
                    "preset=custom 时按字段值；非 custom 时由 packaging_agent 按预设展开缺省字段。",
    )


class Plan(BaseModel):
    """`POST /api/plan/build` 产物 / 后续渲染与编辑的核心数据结构。"""

    plan_id: str
    reference_versions: list[ReferenceVersion] = Field(
        ...,
        min_length=1,
        max_length=2,
        description=(
            "本 plan 改编自哪些拆解版本（1-2 个 (sample_id, slot_id) pair）。"
            "stage-15 起按 slot 粒度选取，可同 sample 双槽对比；"
            "多版本时 plan_agent 把段落结构合并为对等参考池。"
        ),
    )

    @computed_field
    @property
    def sample_ids(self) -> list[str]:
        """stage-15 兼容层：拍平 reference_versions 给老代码用。
        作为 computed_field 也会序列化到 JSON，供老前端兜底；新前端忽略即可。"""
        return [rv.sample_id for rv in self.reference_versions]

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy_sample_ids(cls, data: Any) -> Any:
        """老 plan 持久化里只有 `sample_ids: list[str]`：填占位 slot_id='legacy'。"""
        if not isinstance(data, dict):
            return data
        if data.get("reference_versions"):
            return data
        sids = data.get("sample_ids")
        if isinstance(sids, list) and sids:
            data["reference_versions"] = [
                {"sample_id": sid, "slot_id": "legacy"}
                for sid in sids if isinstance(sid, str)
            ]
            data.pop("sample_ids", None)
        return data
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
    initial_snapshot: Optional["PlanSnapshot"] = Field(
        default=None,
        description=(
            "Plan 在 /plan/build 首次生成时的蒸馏快照（PlanSnapshot：adapted_sections 摘要 + main_track 关键列）。"
            "render commit 时与当前 plan 做 diff 落 Trace A，用于 profile 蒸馏。"
            "后续 PATCH（scene 编辑、gap fill 重建）不修改本字段，确保 v0/v1 对比基准稳定。"
        ),
    )
    kb_rules_applied: int = Field(
        default=0,
        ge=0,
        description=(
            "本次 plan/build 注入的个性知识库规则总数（top-10 最近完成项目 + 用户额外启用项目）。"
            "前端 Compose 生成完成 modal 上 \"已应用 N 条 / 去管理\" 徽标读这个数。"
        ),
    )


# Forward import for Plan.initial_snapshot —— 放在 Plan 后避免循环（profile.schemas 不引 app.schemas）
from .services.profile.schemas import PlanSnapshot  # noqa: E402

Plan.model_rebuild()
SampleManifest.model_rebuild()
FillResult.model_rebuild()
Scene.model_rebuild()


class PlanBuildRequest(BaseModel):
    reference_versions: list[ReferenceVersion] = Field(
        ...,
        min_length=1,
        max_length=2,
        description=(
            "结构参考版本列表（1-2 个 (sample_id, slot_id) pair）。"
            "多选时两份段落结构会被合并成对等参考池喂给 plan_agent.adapt_structure。"
        ),
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
    reuse_sections: list[AdaptedSection] = Field(
        default_factory=list,
        description=(
            "增量重建：传入上一版 plan.adapted_sections，跳过 LLM 段落改编直接复用。"
            "用于『刚刚 fill 完一个 gap → 立刻重跑 plan/build 应用 fill』场景——"
            "用户只是补完缺口，没改 brief/refs/settings，不应让 LLM 把 5 段抖成 4 段。"
            "为空 → 走完整 adapt_structure。"
        ),
    )
    variant: Variant = "A"

    @property
    def sample_ids(self) -> list[str]:
        """stage-15 兼容层：拍平 reference_versions。"""
        return [rv.sample_id for rv in self.reference_versions]

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy_sample_ids(cls, data: Any) -> Any:
        """老前端 / 老测试还在传 `sample_ids: list[str]` —— 按 active slot 反查。"""
        if not isinstance(data, dict):
            return data
        if data.get("reference_versions"):
            return data
        sids = data.get("sample_ids")
        if isinstance(sids, list) and sids:
            try:
                from .services.library import manifest_store
            except Exception:  # noqa: BLE001
                manifest_store = None
            refs: list[dict] = []
            for sid in sids:
                if not isinstance(sid, str):
                    continue
                slot_id = None
                if manifest_store is not None:
                    try:
                        slot_id = manifest_store.get_active_slot(sid)
                    except Exception:  # noqa: BLE001
                        slot_id = None
                refs.append({"sample_id": sid, "slot_id": slot_id or "legacy"})
            if refs:
                data["reference_versions"] = refs
                data.pop("sample_ids", None)
        return data


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
    catalog_block: Optional[str] = Field(
        default=None,
        max_length=64,
        description="HyperFrames catalog block 名（参考 services/catalog/data/catalog.json），"
                    "例如 'flash-through-white' / 'whip-pan' / 'cinematic-zoom'。"
                    "当前阶段仅作为风格标签传递给前端 picker 与渲染端 hint，不替换 ffmpeg xfade 滤镜实现。",
    )
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
    catalog_block: Optional[str] = Field(
        default=None,
        max_length=64,
        description="HyperFrames catalog block 名（cover 类，例如 'logo-outro' / 'app-showcase' / "
                    "'blue-sweater-intro-video'）。当前阶段作为风格标签传给前端 picker 与渲染端 hint，"
                    "不替换 Remotion 封面渲染实现。",
    )
    style_note: str = Field(..., description="LLM 给出的一句话风格说明，比如『大字号 + 黑底白字 + 黄色高亮』")


class PackagingVariant(BaseModel):
    """单个包装版本（aggressive/elegant）的 transitions + cover 组合。

    Stage-16 起 packaging_agent 一次给两份方案：
    - `aggressive`：电商/带货风，高对比+强转场+大字
    - `elegant`：氛围/Vlog 风，柔切+留白+小字

    前端在 PackagingPanel 顶部 Tab 切换；落 plan 时默认 `versions[0]`（aggressive）。
    """

    version_id: Literal["aggressive", "elegant"] = Field(
        ..., description="版本 id：aggressive 强冲击 / elegant 高级感"
    )
    version_label: str = Field(
        ..., max_length=20, description="给前端 Tab 显示的中文标签，如『强冲击版』『高级感版』"
    )
    transitions: list[TransitionSuggestion] = Field(default_factory=list)
    cover: Optional[CoverDesign] = None


class PackagingRecommendation(BaseModel):
    """`POST /api/packaging/recommend` 产物，回写到 PlanStore 的 packaging_track。

    Stage-16 起改为多版本结构：`versions: list[PackagingVariant]`（默认 2 个：aggressive + elegant）。
    旧 manifest/缓存里的顶层 `transitions/cover` 会被 before-validator 自动包装成单个 aggressive variant。
    """

    plan_id: str
    versions: list[PackagingVariant] = Field(
        default_factory=list,
        description="多版本包装方案，至少 1 个；前端 Tab 切换；落 plan 取 versions[0]",
    )
    notes: list[str] = Field(default_factory=list, description="agent 调试日志（mock/失败原因等）")

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy_top_level(cls, data: Any) -> Any:
        """旧格式：`{plan_id, transitions, cover, notes}`——包成单 variant。

        触发条件：data 里有顶层 transitions 或 cover 但没有 versions。包装为
        `versions=[{version_id: "aggressive", version_label: "强冲击版", ...}]`。
        """
        if not isinstance(data, dict):
            return data
        if "versions" not in data and ("transitions" in data or "cover" in data):
            transitions = data.pop("transitions", []) or []
            cover = data.pop("cover", None)
            data["versions"] = [
                {
                    "version_id": "aggressive",
                    "version_label": "强冲击版",
                    "transitions": transitions,
                    "cover": cover,
                }
            ]
        return data


class PackagingRecommendRequest(BaseModel):
    plan_id: str
    apply: bool = Field(
        default=False,
        description="V2 起 /recommend 不再 mutate plan（无视此字段），用户挑完后调 /packaging/apply 落盘；保留字段仅为旧客户端兼容。",
    )
    preferences: Optional[PackagingPreferences] = Field(
        default=None,
        description="用户在 PackagingPanel 上配置的偏好（转场白名单/字幕样式/封面策略/温度）。"
                    "None 时直接复用 plan.settings.packaging_prefs；非空时与之合并（请求体优先），"
                    "结果回写到 plan.settings.packaging_prefs 持久化。",
    )


# =========================================================================
# Module 5b V2 — 5 维度独立多候选包装推荐
# =========================================================================

_StickerPosition = Literal["bottom-center", "top-right", "bottom-right", "middle"]
_TitleBarPosition = Literal["top", "middle"]
_TitleBarFontSize = Literal["small", "medium", "large"]


class SubtitleStyleCandidate(BaseModel):
    """字幕样式候选（一次推荐给 2-3 个）。"""

    candidate_id: str
    label: str = Field(..., max_length=40, description="给用户看的一句话风格名，如『底部大字｜阴影底｜对话感』")
    font_size: SubtitleFontSize
    position: SubtitlePosition
    background: SubtitleBackground
    bilingual: bool = False
    rationale: str = Field(..., max_length=60, description="为什么这套适合本片，≤30 字")


class TitleBarCandidate(BaseModel):
    """标题条 / 卖点卡片候选，每条挂在某个 scene 区间。"""

    candidate_id: str
    text: str = Field(..., max_length=20)
    target_scene_id: str
    start: float = Field(..., ge=0.0)
    end: float = Field(..., gt=0.0)
    font_size: _TitleBarFontSize = "medium"
    color: str = Field(default="#FFFFFF", description="字色 hex")
    background_color: str = Field(default="#14181F", description="底色 hex")
    position: _TitleBarPosition = "top"
    rationale: str = Field(..., max_length=60)


class StickerCandidate(BaseModel):
    """贴纸 / 强调元素候选。CTA 短语为主。"""

    candidate_id: str
    text: str = Field(..., max_length=10)
    target_scene_id: str
    start: float = Field(..., ge=0.0)
    end: float = Field(..., gt=0.0)
    color: str = Field(default="#FFE600", description="字色 hex")
    background_color: str = Field(default="#000000", description="底色 hex")
    position: _StickerPosition = "bottom-center"
    rationale: str = Field(..., max_length=60)


class TransitionCandidateBundle(BaseModel):
    """单个段落切换点的多候选（用户在 bundle 内三选一）。"""

    candidate_id: str
    at_seconds: float
    from_section: str
    to_section: str
    options: list[TransitionSuggestion] = Field(default_factory=list, min_length=1)
    rationale: str = Field(default="", max_length=80)


class CoverCandidate(BaseModel):
    """封面方案候选（一次给 2-3 个不同调性）。"""

    candidate_id: str
    title: str = Field(..., max_length=12)
    subtitle: Optional[str] = Field(default=None, max_length=20)
    palette: list[str] = Field(default_factory=list)
    layout: Literal["center", "left", "split", "stacked"] = "center"
    catalog_block: Optional[str] = Field(default=None, max_length=64)
    style_note: str = Field(default="", max_length=60)
    rationale: str = Field(default="", max_length=60)


class PackagingRecommendationV2(BaseModel):
    """V2 推荐响应：5 维度独立多候选，前端用户挑选后调 /packaging/apply 落盘。"""

    plan_id: str
    subtitle_styles: list[SubtitleStyleCandidate] = Field(default_factory=list)
    title_bars: list[TitleBarCandidate] = Field(default_factory=list)
    stickers: list[StickerCandidate] = Field(default_factory=list)
    transition_bundles: list[TransitionCandidateBundle] = Field(default_factory=list)
    covers: list[CoverCandidate] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class PackagingSelection(BaseModel):
    """用户在 PackagingPanel 挑完后提交的复合载荷。

    前端把完整 recommendation 带回来（服务端无状态），避免再保存一份。
    """

    plan_id: str
    subtitle_style_id: Optional[str] = None
    title_bar_ids: list[str] = Field(default_factory=list)
    sticker_ids: list[str] = Field(default_factory=list)
    transition_selections: dict[str, str] = Field(
        default_factory=dict,
        description="bundle_id → 用户选中的 TransitionStyle",
    )
    cover_id: Optional[str] = None
    recommendation: PackagingRecommendationV2


# ---- F2 · 单组件 picker 增量接口 ---------------------------------------
# F2 把"包装方案 = 5 维度全量提交"的交互改成"用户按钮添加单个组件"——
# /packaging/items/draft 给一个 kind 的 AI 推荐草稿，前端进 staging slot 后用户可改；
# /packaging/items/place 把改好的 PackagingItem 单独 append 进 plan.packaging_track；
# /packaging/items/{plan_id}/{item_id} (DELETE) 删除单条。
# 字幕仍由 PackagingPanel V2 + subtitle_enabled 开关管，本组接口只动 title_bar/sticker/cover。

class PackagingItemDraftRequest(BaseModel):
    plan_id: str
    kind: Literal["title_bar", "sticker", "cover"]


class PackagingItemDraftResponse(BaseModel):
    """draft 不写 plan，仅返回一个候选转换好的 PackagingItem，前端放进 staging slot 让用户编辑。"""

    item: PackagingItem
    rationale: str = Field(default="", description="LLM 推荐理由，用户决定要不要落进轨")


class PackagingItemPlaceRequest(BaseModel):
    """把 staging slot 里编辑好的 PackagingItem 落到 plan.packaging_track。"""

    plan_id: str
    item: PackagingItem


# =========================================================================
# Module 5c — Plan 命名快照（用户主动保存的版本点；与 editStore 撤销栈互补）
# =========================================================================
# 撤销栈是 RAM 短期 history；本组接口持久化到磁盘，配合后续账号系统按 user_id 鉴权可见性。

class PlanSnapshotMeta(BaseModel):
    snapshot_id: str
    name: str
    plan_id: str
    project_id: Optional[str] = None
    user_id: Optional[str] = None
    ts: float


class PlanSnapshotCreateRequest(BaseModel):
    name: str = Field(default="", description="用户起的名字；空字符串则后端按时间补一个『未命名 HH:MM』")


class PlanSnapshotEntry(BaseModel):
    """完整快照——含 plan 体，仅在 GET 单条 / restore 时返回。"""

    snapshot_id: str
    name: str
    plan_id: str
    project_id: Optional[str] = None
    user_id: Optional[str] = None
    ts: float
    plan: Plan


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


# ---- Compose 自然语言编辑（⌘K command bar / R6） ----------------------------

ComposeEditStep = Literal["step2", "step3"]
"""Compose 自然语言编辑作用域：
- step2 = 拆解-改编态，**只改内容轨**：文案 / 段时长 / 删段 / 重排
- step3 = 包装-渲染态，**禁内容轨**，其余全开：字卡 / 包装项 / BGM 偏移与音量 / compose 设置
"""


class ComposeEditDiff(BaseModel):
    """⌘K 编辑产出的单条 diff，前端预览用。"""

    op: str = Field(..., description="操作名，如 update_narration / delete_section / update_compose_setting")
    target_id: Optional[str] = Field(default=None, description="目标 id（section_id / scene_id / item_id），全局设置为空")
    before: Any = Field(default=None, description="改前值（JSON-able）")
    after: Any = Field(default=None, description="改后值")
    summary: str = Field(..., max_length=120, description="一句话人话描述")
    args: dict[str, Any] = Field(
        default_factory=dict,
        description="dry-run 落定的 mutator 参数；apply 时原样回放，跳过 LLM 二次推理避免不一致。",
    )


class ComposeEditRequest(BaseModel):
    plan_id: str
    step: ComposeEditStep = Field(..., description="step2 / step3 决定可用工具集")
    instruction: str = Field(..., min_length=1, max_length=500)
    apply: bool = Field(
        default=False,
        description="False=只算 diff 不落盘（预览），True=真改 plan 并落 plan_store",
    )
    confirmed_ops: Optional[list[dict[str, Any]]] = Field(
        default=None,
        description=(
            "apply=True 时前端回传 dry-run 拿到的 {op, args} 列表，后端原样回放跳过 LLM；"
            "保证 apply 落地的 diff 一定 = 用户看到并确认的 diff。"
            "None 时退回旧路径（让 LLM 重跑）。"
        ),
    )


class ComposeEditResponse(BaseModel):
    plan_id: str = Field(..., description="apply=True 时为新 plan id；apply=False 时为原 plan id")
    diffs: list[ComposeEditDiff] = Field(default_factory=list)
    applied: bool = Field(..., description="是否已落盘到 plan_store")
    plan: Optional["Plan"] = Field(default=None, description="apply=True 时返回新 plan")
    note: Optional[str] = Field(default=None, description="兜底说明（LLM 没识别出动作 / 越界等）")


class ComposeEditDismissRequest(BaseModel):
    """用户在 ⌘K 对话编辑里 dry-run 后撤回的指令——profile 蒸馏视为负信号。"""

    plan_id: str
    step: ComposeEditStep
    instruction: str = Field(..., min_length=1, max_length=500)
    dismissed_ops: list[dict[str, Any]] = Field(
        default_factory=list,
        description="被撤回的 {op, args} 列表（前端 diff.args 原样回传）。",
    )


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


class AssetSaveFromUrlRequest(BaseModel):
    """POST /api/asset/save-from-url —— 把外部图片 URL（如 Seedream CDN）落盘进资产库。

    Seedream 返回的 url 是临时 CDN（1h-7d 有效），用户可点『保存到素材库』触发本接口
    永久落盘到 `var/assets/<project_id>/reference_image/`。
    """

    project_id: str = Field(..., description="所属项目 ID")
    url: str = Field(..., min_length=8, description="外部图片 URL（http/https）")
    kind: AssetKind = Field(default="reference_image")
    title: Optional[str] = Field(default=None, max_length=120)
    tags: Optional[list[str]] = Field(default=None, max_length=12)


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
    - library:   {"references": list[{"sample_id": str, "slot_id": str}]}（1-2 个）
    - decompose: {"references": list[{"sample_id": str, "slot_id": str}]}（manifest 已落资产库的版本槽，不重存）
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
    video_type: Optional[VideoType] = Field(
        default=None,
        description=(
            "视频种类（marketing/editing/motion_graph）—— 新建项目时由用户选；"
            "样例选择推迟到 Decompose 页时，前端按此过滤系统样例。"
        ),
    )
    reference_versions: list[ReferenceVersion] = Field(
        default_factory=list,
        max_length=2,
        description=(
            "项目锚定的结构参考版本（最多 2 个 (sample_id, slot_id) pair，跨项目共享）。"
            "新建时可为空（样例在 Decompose 页选定后回填）；多选时 plan_agent 会合并参考。"
        ),
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

    @computed_field
    @property
    def sample_ids(self) -> list[str]:
        """stage-15 兼容层：老测试 / 老代码访问 `project.sample_ids` —— 拍平 reference_versions。
        作为 computed_field 也会序列化到 JSON，供老前端兜底；新前端忽略即可。"""
        return [rv.sample_id for rv in self.reference_versions]


class ProjectCreateRequest(BaseModel):
    name: str = Field(..., max_length=80)
    video_type: Optional[VideoType] = Field(
        default=None,
        description="视频种类（建项目时即定，可改）；空表示老前端兼容。",
    )
    reference_versions: list[ReferenceVersion] = Field(
        default_factory=list,
        max_length=2,
        description="新建项目锚定的结构参考版本列表（0-2 个 (sample_id, slot_id) pair）；为空时项目处于「未挑样例」状态。",
    )

    @property
    def sample_ids(self) -> list[str]:
        """stage-15 兼容层：老测试访问 `req.sample_ids`。"""
        return [rv.sample_id for rv in self.reference_versions]

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy_sample_ids(cls, data: Any) -> Any:
        """stage-15 兼容层：老前端 / 老测试还在传 `sample_ids: list[str]`。
        转换成 reference_versions：按当时 active slot 反查；找不到则用占位 'legacy'。
        """
        if not isinstance(data, dict):
            return data
        if data.get("reference_versions"):
            return data
        sids = data.get("sample_ids")
        if isinstance(sids, list) and sids:
            try:
                from .services.library import manifest_store
            except Exception:  # noqa: BLE001
                manifest_store = None
            refs: list[dict] = []
            for sid in sids:
                if not isinstance(sid, str):
                    continue
                slot_id = None
                if manifest_store is not None:
                    try:
                        slot_id = manifest_store.get_active_slot(sid)
                    except Exception:  # noqa: BLE001
                        slot_id = None
                refs.append({"sample_id": sid, "slot_id": slot_id or "legacy"})
            if refs:
                data["reference_versions"] = refs
                data.pop("sample_ids", None)
        return data


class ProjectUpdateRequest(BaseModel):
    """PATCH /api/project/{id}：所有字段可选，None 表示不动。"""

    name: Optional[str] = Field(default=None, max_length=80)
    video_type: Optional[VideoType] = None
    reference_versions: Optional[list[ReferenceVersion]] = Field(
        default=None,
        max_length=2,
        description=(
            "Decompose 页选定/切换样例时回写；显式传 [] 表示清空（回到「未选样例」态）。"
            "None 表示不动。"
        ),
    )
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


# Compose 编辑响应里嵌了 Plan（forward-ref），需要在 Plan 之后再 rebuild
ComposeEditResponse.model_rebuild()
