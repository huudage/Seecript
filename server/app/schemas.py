"""Pydantic request/response schemas for all API endpoints.

Design rationale:
- Single Responsibility: this file only defines I/O contracts; no business logic.
- Each endpoint has a clearly named request/response pair (Interface Segregation).
- Optional fields default to None / [] / "" so frontend can submit partial data during draft.
- We keep field names snake_case in JSON to match Python conventions; frontend should adapt.
"""
from typing import List, Literal, Optional

from pydantic import BaseModel, Field


# 拆解骨架 / 问答 / 脚本链路共用：粘贴台词或 ASR 全文上限（中文台词约 1 分钟数千字，留足余量）。
TRANSCRIPT_MAX_CHARS = 50000


# =========================================================================
# Health
# =========================================================================
class HealthResponse(BaseModel):
    status: Literal["healthy", "degraded"]
    version: str
    llm_provider: str
    asr_provider: str
    t2v_provider: str = "mock"


# =========================================================================
# Module 2 — Persona Generation
# =========================================================================
class PersonaRequest(BaseModel):
    background: str = Field(..., min_length=1, max_length=500, description="职业背景")
    interests: str = Field(..., min_length=1, max_length=500, description="兴趣 / 可拍内容")
    resources: str = Field(..., min_length=1, max_length=500, description="可用资源")


class PersonaPlan(BaseModel):
    name: str = Field(..., description="人设名（短句标题）")
    differentiation: str = Field(..., description="差异化逻辑")
    rationale: str = Field(..., description="为什么这个人设值得做")
    reference_accounts: List[str] = Field(default_factory=list, description="对标账号示意")
    onboarding_advice: str = Field(..., description="起号建议")
    monetization_outlook: str = Field(..., description="变现预判")
    score: int = Field(..., ge=1, le=5, description="推荐星级 1-5")


class PersonaResponse(BaseModel):
    personas: List[PersonaPlan] = Field(..., min_length=1, max_length=5)
    model_used: str
    elapsed_ms: int


# =========================================================================
# Module 1 — Skeleton Extraction (脚本拆解)
# =========================================================================
class SkeletonRequest(BaseModel):
    """v0.1：只接受文本输入；ASR 路径在 /api/asr/transcribe 单独提供。"""
    transcript: str = Field(
        ...,
        min_length=20,
        max_length=TRANSCRIPT_MAX_CHARS,
        description="视频台词文本",
    )
    persona_hint: Optional[str] = Field(default=None, max_length=500, description="用户当前人设上下文（可选）")


class NarrativeBeat(BaseModel):
    timestamp: str = Field(..., description="时间区间，如 0:05-1:30")
    title: str
    description: str
    emotion_arc: Optional[str] = None


class HookSection(BaseModel):
    strategy: Literal[
        "痛点前置", "反常识陈述", "悬念提问", "视觉冲击", "身份认同", "数字罗列", "其他"
    ]
    text: str = Field(..., description="原视频前 3 秒台词原文")
    explanation: str = Field(..., description="钩子设计原理与可迁移方法论")


class CTASection(BaseModel):
    strategy: Literal[
        "点赞收藏", "评论区留言", "关注追更", "引导私域", "其他"
    ]
    text: str
    explanation: str


class SkeletonResponse(BaseModel):
    hook: HookSection
    body: List[NarrativeBeat]
    cta: CTASection
    transferable_template: str = Field(..., description="去除原内容、保留结构的可复用模板")
    model_used: str
    elapsed_ms: int


# =========================================================================
# Module 3 — SEO / Metadata
# =========================================================================
class SEORequest(BaseModel):
    """Module 3 request payload.

    `platform` is currently locked to "douyin" (the multi-platform picker was
    removed from the UI). The field is kept (instead of dropped) so older
    clients that still send `platform=douyin` keep working, and so we have a
    forward-compat seam for future platform-specific prompt files.
    """

    script: str = Field(..., min_length=20, max_length=10000)
    platform: Literal["douyin"] = "douyin"
    persona_hint: Optional[str] = Field(default=None, max_length=500)


class TitleCandidate(BaseModel):
    type: Literal["反常识型", "数字型", "身份型", "痛点型", "悬念型", "其他"]
    text: str
    char_count: int
    notes: Optional[str] = None


class TagCluster(BaseModel):
    broad_traffic: List[str] = Field(default_factory=list, description="泛流量词")
    long_tail: List[str] = Field(default_factory=list, description="精准长尾词")
    challenge_topics: List[str] = Field(default_factory=list, description="话题挑战")


class SEOResponse(BaseModel):
    titles: List[TitleCandidate] = Field(..., min_length=3, max_length=8)
    description: str = Field(..., max_length=200)
    tags: TagCluster
    platform: str
    model_used: str
    elapsed_ms: int


# =========================================================================
# Module 4 — Comments Sorting
# =========================================================================
class CommentsRequest(BaseModel):
    raw_text: str = Field(..., min_length=10, max_length=20000, description="原始评论区文本（每行一条）")
    persona_hint: Optional[str] = Field(default=None, max_length=500)


class ReplyDraft(BaseModel):
    tone: Literal["专业解读", "幽默调侃", "共情安抚"]
    text: str = Field(..., max_length=300)


class ClassifiedComment(BaseModel):
    author: Optional[str] = None
    text: str
    classification: Literal["干货提问", "争议探讨", "高互动潜力", "下期选题", "敏感场", "中价值", "灌水"]
    replies: List[ReplyDraft] = Field(default_factory=list)


class CommentsResponse(BaseModel):
    high_value: List[ClassifiedComment]
    medium_value: List[ClassifiedComment]
    low_value_count: int = Field(..., description="低价值灌水仅返回数量，不返回内容")
    model_used: str
    elapsed_ms: int


# =========================================================================
# Module 5 — Guided Q&A (引导式问答)
# =========================================================================
# 设计哲学：
#   feature-1 的第 3 步把"对标拆解"转化为"原创素材"——不是让 AI 替用户写，
#   而是用 ≤ 3 轮纯选项题让用户做出 3 个关键创作决策（Hook 角度 / Body 切入 / CTA 风格）。
#
# 为什么不开放自由输入：
#   早期方案曾保留「让我自己输入…」自由文本出口，但实测发现：
#   1. 用户一旦写自由文本，对话很容易"发散"，下一轮问题失去锚点；
#   2. LLM 把自由文本回填到下一轮 prompt 里时，会出现"重复确认"循环；
#   3. v0.x 阶段优先保收敛、保产物质量，自由输入留到后续版本再做。
#   所以现在 100% 是"AI 出 3-4 个具体可朗读选项 → 用户单选 → 进入下一轮"。
#
# 轮次约束：
#   MAX_QA_ROUNDS = 3 —— 3 轮足以覆盖 Hook / Body 关键差异化 / CTA 三个核心维度，
#   超过 3 轮用户就会失去耐心。Router 在 answers 数组长度 ≥ 3 时直接返回 done=true
#   而不再调用 LLM——确定性收敛。
MAX_QA_ROUNDS = 3


class QAAnswer(BaseModel):
    """已答轮次的回放（前端把累积历史回传给后端，让 AI 出下一题时知道前面选了什么）。"""

    round: int = Field(..., ge=1, le=MAX_QA_ROUNDS)
    question: str = Field(..., max_length=500)
    choice: str = Field(..., min_length=1, max_length=500, description="用户选中的那个 option.label 文本")


class QARequest(BaseModel):
    """每一轮问答的请求体；前端在每次 /next 调用时回传完整 history。"""

    skeleton: dict = Field(..., description="第 2 步生成的骨架（hook/body/cta）原样回传")
    transcript: Optional[str] = Field(
        default=None,
        max_length=TRANSCRIPT_MAX_CHARS,
        description="原视频台词（可选，给 AI 补充上下文）",
    )
    persona_hint: Optional[str] = Field(default=None, max_length=500, description="当前人设")
    # 用户在第 3 步开始前自行填的「创作要求」——时长 / 节奏 / 风格 / 自由补充。
    # 这个字段只是『软约束』：影响 LLM 出题选项的取向（如时长 = 30s 时 options 就该短促有冲击），
    # 不影响轮次硬收敛（Router 仍按 MAX_QA_ROUNDS 拦截）。
    brief: Optional[str] = Field(default=None, max_length=1000, description="用户自填的创作要求（时长/节奏/风格/自由补充）")
    answers: List[QAAnswer] = Field(default_factory=list, max_length=MAX_QA_ROUNDS)


class QAOption(BaseModel):
    """单选选项；不再有 freeform 出口，所有选项都是 AI 提前生成的可朗读具体内容。"""

    label: str = Field(..., min_length=1, max_length=200)


class QAResponse(BaseModel):
    """单轮回复：要么是新一轮的题，要么是 done=True 进入脚本阶段。"""

    round: int = Field(..., ge=1, le=MAX_QA_ROUNDS)
    done: bool = Field(..., description="True 时前端跳到第 4 步生成脚本，忽略 question/options")
    question: Optional[str] = Field(default=None, max_length=500)
    rationale: Optional[str] = Field(default=None, max_length=300, description="给前端可选展示的『为什么问这个』")
    options: List[QAOption] = Field(default_factory=list, max_length=4)
    model_used: str
    elapsed_ms: int


# =========================================================================
# Module 6 — Final Script (基于骨架 + Q&A 回答生成原创分镜脚本)
# =========================================================================
class ScriptRequest(BaseModel):
    skeleton: dict = Field(..., description="第 2 步骨架原样回传")
    answers: List[QAAnswer] = Field(default_factory=list, max_length=MAX_QA_ROUNDS)
    persona_hint: Optional[str] = Field(default=None, max_length=500)
    transcript: Optional[str] = Field(default=None, max_length=TRANSCRIPT_MAX_CHARS)
    # 与 QARequest.brief 一致——前端把第 3 步开始前用户填的创作要求继续透传到第 4 步，
    # 让脚本生成阶段做到「时长/节奏/风格」与出题阶段保持一致。
    brief: Optional[str] = Field(default=None, max_length=1000, description="用户自填的创作要求（时长/节奏/风格/自由补充）")


class ScriptScene(BaseModel):
    """脚本里一个分镜片段。结构刻意与 NarrativeBeat 对齐，方便前端复用 .seecript-skeleton 卡片样式。"""

    timestamp: str
    title: str
    narration: str = Field(..., description="该片段的具体口播文字（创作者可直接朗读）")
    visual: Optional[str] = Field(default=None, max_length=500, description="画面/镜头建议")


class ScriptResponse(BaseModel):
    hook_narration: str = Field(..., max_length=500, description="开场 3 秒的口播台词")
    scenes: List[ScriptScene] = Field(..., min_length=2, max_length=8)
    cta_narration: str = Field(..., max_length=500)
    full_text: str = Field(..., description="拼接后的完整脚本纯文本（供前端一键复制）")
    model_used: str
    elapsed_ms: int


# =========================================================================
# ASR — separate endpoint (only used by Module 1's frontend uploader)
# =========================================================================
class ASRResponse(BaseModel):
    transcript: str
    duration_seconds: float
    provider: str
    elapsed_ms: int


# =========================================================================
# Module 7 — Text-to-Video（智谱清影；默认 cogvideox-3）
# =========================================================================
# 设计决策：
#   - 异步两段式（submit → poll query）：视频生成 30s-3min，blocking 调用必超时。
#   - prompt 硬限 500 字符（智谱官方 512，留 12 字安全余量；与 config.t2v_max_prompt_chars 对齐）。
#   - size：合并 CogVideoX-3 官方枚举 + CogVideoX-2 独有分辨率（用户若把 ZHIPU_VIDEO_MODEL
#     改为 cogvideox-2，可选 720x480 / 960x1280 等；与 v3 叠用时以智谱接口校验为准）。
#   - quality 默认 speed：速出优先；可切 quality。
#   - with_audio 默认 false：KOC 一般自配口播/BGM。
#   - duration_seconds：可选；与 shot_preview_mode 配合见 routers/t2v.py。
T2VSize = Literal[
    # CogVideoX-3（开放平台 OpenAPI 枚举）
    "1280x720",
    "720x1280",
    "1024x1024",
    "1920x1080",
    "1080x1920",
    "2048x1080",
    "3840x2160",
    # CogVideoX-2 / flash 额外分辨率（仅旧模型可用）
    "720x480",
    "1280x960",
    "960x1280",
]


class T2VSubmitRequest(BaseModel):
    """文生视频 · 提交生成任务的请求体。"""

    prompt: str = Field(
        ...,
        min_length=4,
        max_length=500,
        description="视频文本描述（≤ 500 字）。建议结构：主体（描述）+ 环境 + 镜头/光线 + 氛围。",
    )
    size: T2VSize = Field(
        default="720x1280",
        description="分辨率。默认 9:16 竖屏（与 cogvideox-3 OpenAPI 对齐）；KOC 短视频常用竖版。",
    )
    quality: Literal["speed", "quality"] = Field(
        default="speed",
        description="speed=30s 速出，quality=60-120s 高质。默认 speed。",
    )
    with_audio: bool = Field(
        default=False,
        description="是否生成 AI 音轨。默认 false，留给创作者自行配音/配 BGM。",
    )
    # 可选 user_id：路由层会拿请求 trace_id 做兜底，但前端可显式传一个稳定标识符（如浏览器指纹），
    # 让智谱侧能跨多次请求识别同一个终端用户用于内容审核/限频（智谱要求 6-128 字符）。
    user_id: Optional[str] = Field(
        default=None,
        min_length=6,
        max_length=128,
        description="终端用户唯一 ID（智谱内容审核用）；未提供则路由层自动生成。",
    )
    shot_preview_mode: bool = Field(
        default=False,
        description=(
            "为 true 时：在服务端拼接「分镜演示」系统提示词（见 t2v_shot_prompts），"
            "并将 cogvideox-3 单次生成时长固定为 10 秒（预期效果预览）。"
            "此时 prompt 字段应只写所选分镜的画面/口播要点，不要自带长前缀。"
        ),
    )
    duration_seconds: Optional[Literal[5, 10]] = Field(
        default=None,
        description="仅 cogvideox-3 写入智谱请求体；不设则用服务端环境变量默认。与 shot_preview_mode 同时出现时以分镜演示为准（10 秒）。",
    )


class T2VSubmitResponse(BaseModel):
    """提交成功后立即返回；前端拿 task_id 去轮询 query。"""

    task_id: str
    request_id: str
    model: str
    provider: str
    status: Literal["pending"] = "pending"
    elapsed_ms: int


class T2VQueryResponse(BaseModel):
    """轮询单次结果。

    - status="pending"：还在生成，前端继续等
    - status="succeeded"：video_url / cover_image_url 就位
    - status="failed"：fail_reason 给出原因（仅供前端展示，不重试，不扣额）
    """

    task_id: str
    status: Literal["pending", "succeeded", "failed"]
    model: str
    provider: str
    video_url: Optional[str] = None
    cover_image_url: Optional[str] = None
    fail_reason: Optional[str] = None
    elapsed_ms: int


# =========================================================================
# Common error envelope
# =========================================================================
class ErrorResponse(BaseModel):
    detail: str
    code: Optional[str] = None
    trace_id: Optional[str] = None
