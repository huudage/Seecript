/**
 * 与后端 server/app/schemas.py 镜像的 TS 类型。
 * 修改后端 schema 时务必同步这里，否则编辑器报错也就罢了，运行时会沉默断链。
 *
 * v2：段落结构从 9 元 SectionKind（按 video_type 三选一）改为 4 元 SectionRole（任意视频通用）
 * + 自由文本 theme（LLM 看完视频后给的真实主题标签）。video_type 仍保留但仅作风格提示。
 */

export type SampleId = string
export type PlanId = string
export type JobId = string
export type MaterialId = string
export type SessionId = string
export type GapId = string

/**
 * 视频类型 —— 用户在上传/选样例时挑选，仅作风格提示（驱动 BGM/字幕/转场/封面），
 * 不再决定段落结构。
 * - marketing      营销/带货/动态海报
 * - editing        剪辑/Vlog/纪录
 * - motion_graph   合成动画/信息可视化
 */
export type VideoType = 'marketing' | 'editing' | 'motion_graph'

/**
 * 段落角色 —— stage-16 起为自由字符串以支持 5 种结构模式 17 角色 + step_N/item_N 动态后缀。
 * 合法值由 STRUCTURAL_PATTERNS 决定；前端用 getSectionMeta(role, pattern) 取展示元数据。
 */
export type SectionRole = string

/** 6 种结构模式：戏剧/线性步骤/并列盘点/氛围推进/信息密集/无高潮 Vlog。决定下游用哪套角色体系。 */
export type StructuralPattern =
  | 'dramatic'
  | 'stepwise'
  | 'listicle'
  | 'atmospheric'
  | 'info_dense'
  | 'vlog'

/** 节奏标签——对单镜头/单段落的节奏感分类。 */
export type Tempo = 'slow' | 'medium' | 'fast' | 'peak' | 'deceleration'

export type GapStatus = 'ok' | 'warn' | 'miss'
export type FillAction = 'rerank' | 'copy' | 'aigc' | 'aigc_image'
export type Variant = 'A' | 'B'
export type JobStatus = 'pending' | 'running' | 'succeeded' | 'failed' | 'cancelled'
export type MediaType = 'video' | 'image' | 'audio'

// =========================================================================
// Module 1 — Library
// =========================================================================

export type ManifestStatus = 'none' | 'ready'

export interface SampleVersionInfo {
  slot_id: string
  /** 展示用标签 v1/v2（按 updated_at 升序）。 */
  label: string
  updated_at: number
  is_active: boolean
}

export interface LibraryItem {
  id: SampleId
  title: string
  video_type: VideoType
  scene: string
  duration_seconds: number
  shot_count: number
  cover_url: string
  source: 'system' | 'user'
  /** none = 未拆解；ready = 至少 1 个版本槽可用。 */
  manifest_status: ManifestStatus
  /** 已存在的版本槽数量（0–2）。 */
  version_count: number
  /** 当前 active slot id；version_count=0 时为空。 */
  active_slot: string | null
}

export interface VersionMutationResponse {
  sample_id: SampleId
  version_count: number
  active_slot: string | null
  versions: SampleVersionInfo[]
}

export interface ManifestStatusResponse {
  sample_id: SampleId
  version_count: number
  max_versions: number
  active_slot: string | null
  versions: SampleVersionInfo[]
}

/**
 * (sample_id, slot_id) 二元组——stage-15 起 Plan / Project / Compose 都按槽精确引用，
 * 让用户能选同一样例的 v1 / v2 做对比迁移。
 */
export interface ReferenceVersion {
  sample_id: SampleId
  slot_id: string
}

/**
 * GET /api/references 列表项——拍平所有 sample × 所有槽。
 * 供 Compose 顶部 ReferencePicker 选 1-2 个版本作为结构参考。
 */
export interface ReferenceListItem {
  sample_id: SampleId
  sample_title: string
  slot_id: string
  /** 该 sample 下的展示标签 v1/v2 */
  label: string
  video_type: VideoType
  scene: string
  duration_seconds: number
  shot_count: number
  cover_url: string
  source: 'system' | 'user'
  updated_at: number
  is_active: boolean
}

/**
 * POST /api/sample/{id}/manifest/save body —— 把前端 zustand 草稿存进版本槽。
 * 槽未满 create_version；槽满 + replace_slot 覆盖；槽满 + 无 replace_slot 返 409。
 */
export interface ManifestSaveRequest {
  manifest: SampleManifest
  replace_slot?: string | null
}

export interface Shot {
  index: number
  start: number
  end: number
  duration: number
  thumbnail_url?: string | null
  transcript?: string | null
  tags: string[]
  /** stage-26：本镜画面主体（具象名词，禁比喻/上位词/营销词）。下游 AIGC prompt 会原样使用。 */
  subject?: string
  /** stage-23：画面内容描述（≤60 中文字）。LLM 看缩略图 + tags 写出来的画面在演什么。 */
  visual_summary?: string
  /** stage-23：本镜口播 / 代字幕脚本。有口播时清洗自 transcript；无口播时 LLM 写代字幕参考文案。 */
  script?: string
  /** stage-23：语义合并保留——被并入的原 shot indices；length>1 表示「N 镜合 1」。 */
  merged_from?: number[]
  /** stage-25：本镜的目标分布（0-4 个，可空）。样例 Shot.targets 仅作为 plan_agent 节奏参考，
   *  graphic 类的具体动效图形（如莫比乌斯环）绝不会被原样迁移到目标主题。 */
  targets?: ShotTarget[]
}

export type ShotTargetKind = 'person' | 'object' | 'scene' | 'text' | 'graphic' | 'other'

export interface ShotTarget {
  kind: ShotTargetKind
  /** 目标的简短名（≤12 中文字），如『主播』『青铜鼎』『展厅全景』『品牌字』 */
  name: string
  /** primary=主体 / secondary=陪体 / background=背景。空等价 primary。 */
  role?: 'primary' | 'secondary' | 'background' | null
  /** 该目标的视觉特征/动作（≤40 字），辅助 Seedream 出图 */
  visual_hint?: string | null
}

export interface RhythmCurve {
  times: number[]
  /** [已弃用] 单位时间镜头切换密度——前端不再消费,保留以兼容老数据。 */
  cut_density: number[]
  /** librosa RMS 能量,归一到 [0,1]。前端作为参考线展示（暗灰）。 */
  bgm_energy: number[]
  /** [已弃用] 整体 BPM。 */
  tempo_bpm?: number | null
  /** R1：基于段落结构低频平滑的情绪走势（0..1）。前端蓝线展示。 */
  mood_curve?: number[]
  /** R1：BGM 与情绪走势的契合度评分（0..1）。null 表示无 BGM 或样本不足。 */
  bgm_fit_score?: number | null
  /** R1：一句话评注 BGM 是否服务结构。 */
  bgm_fit_note?: string | null
  /** stage-28：LLM 多信号情绪曲线；优先级高于 mood_curve；老 manifest 为 null。 */
  emotion?: EmotionCurve | null
}

/** stage-28 情绪曲线相关 4 个 schema —— 镜像 server.app.schemas.EmotionCurve 等。 */
export interface EmotionAnchor {
  section_idx: number
  intensity: number
  reason?: string
}

export interface EmotionPeak {
  t: number
  intensity: number
  reason?: string
}

export interface EmotionPoint {
  t: number
  intensity: number
}

export interface EmotionCurve {
  points: EmotionPoint[]
  anchors: EmotionAnchor[]
  peaks: EmotionPeak[]
  valleys: EmotionPeak[]
  summary?: string
  backend?: 'llm' | 'rule_fallback'
  signals_used?: string[]
  /** unix epoch 秒；前端用来判断曲线是否相对 main_track 编辑过期 */
  computed_at?: number | null
}

/**
 * 段落 = 抽象骨架（role）+ 真实主题（theme）。
 * role 是 4 元枚举，theme 是 LLM 看完视频后给的中文短标签（≤10 字），
 * 反映这一段真实在讲什么——比 role 信息量大很多。
 */
export interface Section {
  role: SectionRole
  /** LLM 给出的本段中文主题标签（≤10 字）。例：『展品揭幕』『痛点钩子』『晨光启程』 */
  theme: string
  start: number
  end: number
  summary: string
  shot_indices: number[]
}

export interface PackagingProfile {
  subtitle_style: string
  has_title_bar: boolean
  transition_types: string[]
  cover_style?: string | null
  sticker_density: number
}

/** ASR 逐句时间戳（秒）。模块 5 字幕烧录直接读这个列表。 */
export interface Utterance {
  text: string
  start: number
  end: number
}

/**
 * 视频画像 —— 多模态 LLM 对整支视频的语义画像，driver 后续 role 切段。
 * 旧 manifest 无此字段时为 null。
 */
export interface VideoUnderstanding {
  /** 视频原型，如『艺术展宣传』『带货种草』『城市 Vlog』 */
  archetype: string
  /** 一段话讲清整支视频在说什么、怎么说 */
  narrative_summary: string
  /** LLM 建议切几段（stage-16 改名为 estimated_segments；保留兼容字段） */
  suggested_segments?: number
  /** stage-16：LLM 估计的段数（2-8）；老 manifest 没有则回落 suggested_segments */
  estimated_segments?: number
  /** 基调描述：『冷静克制』『高燃热血』『诙谐自嘲』等 */
  tone: string
  /** stage-16：5 种结构模式之一；老 manifest 缺失时由后端兜底为 dramatic。 */
  structural_pattern?: StructuralPattern
  /** stage-16：节奏标签，可空。 */
  tempo?: Tempo | null
}

export interface SampleManifest {
  sample_id: SampleId
  title: string
  video_type: VideoType
  duration_seconds: number
  video_url: string
  has_voice: boolean
  shots: Shot[]
  rhythm: RhythmCurve
  sections: Section[]
  packaging: PackagingProfile
  /** LLM 视频画像，旧缓存可能为 null。前端 Decompose 页用它做画像卡片。 */
  understanding?: VideoUnderstanding | null
  utterances: Utterance[]
  /** 高潮时间点（秒）。前端 Decompose 节奏图叠 ReferenceLine。 */
  climax_position?: number | null
  /** LLM 多模态音频理解（拿样例视频音轨跑 doubao），有此字段时优先于 librosa 的 BPM 单点估算。 */
  audio_understanding?: BGMAnalysis | null
  /** stage-23：全片亮点 / 改进建议 / 总评分。旧版本槽未跑过此步骤时为 null。 */
  analysis?: SampleAnalysis | null
}

/** stage-23：全片复盘亮点的维度。 */
export type HighlightAspect = 'hook' | 'narrative' | 'visual' | 'audio' | 'rhythm' | 'copy' | 'cta'

/** stage-23：改进建议的维度（多一个 structure）。 */
export type ImprovementAspect = HighlightAspect | 'structure'

export interface HighlightItem {
  aspect: HighlightAspect
  text: string
  shot_indices: number[]
}

export interface ImprovementItem {
  aspect: ImprovementAspect
  text: string
  suggestion: string
  shot_indices: number[]
}

export interface SampleAnalysis {
  highlights: HighlightItem[]
  improvements: ImprovementItem[]
  overall_score: number
  one_line_verdict: string
}

// =========================================================================
// Module 2 — Decompose
// =========================================================================

export interface DecomposeRequest {
  sample_id: SampleId
  video_type: VideoType
  /** 参考素材 id 列表（图/视频抽帧），喂给多模态 LLM 做风格/调性参考；最多 6 个。 */
  reference_asset_ids?: string[]
  /** 用户自由文本指引（≤500 字），影响 LLM 视频画像 + 分段决策。 */
  nl_prompt?: string | null
  /** 版本槽已满时要覆盖的 slot_id（前端在 409 槽满后让用户挑）。 */
  replace_slot?: string | null
  /**
   * stage-15：默认 false 走草稿态——后端跑完 SSE done 把 manifest 推前端 zustand，
   * 不直接落盘；用户点「保存到资产库」时再走 POST /sample/{id}/manifest/save 入库。
   * true 走老行为（直接 create_version），仅供需要无人值守自动入库的内部场景使用。
   */
  persist?: boolean
}

export interface DecomposeSubmitResponse {
  job_id: JobId
}

// =========================================================================
// Module 3 — Material
// =========================================================================

/** 视频镜头切片：与 server schemas.py::MaterialShot 镜像。*/
export interface MaterialShot {
  index: number
  start: number
  end: number
  duration: number
  thumbnail_url?: string | null
  caption?: string | null
  /** 0~1；越大动作越剧烈。决定能否当 hook/climax。 */
  action_density: number
  recommended_role?: SectionRole | null
}

export type MaterialPreprocessStatus =
  | 'pending'
  | 'running'
  | 'ready'
  | 'failed'
  | 'skipped'

export interface Material {
  material_id: MaterialId
  filename: string
  media_type: MediaType
  duration_seconds?: number | null
  thumbnail_url?: string | null
  /** 原文件 URL，如 /uploads/<sid>/<material_id>_<filename>，Remotion Player 直接喂 <Video src>。 */
  file_url?: string | null
  tags: string[]
  recommended_section?: SectionRole | null
  /** 高光评分 0-1：0.7+ 适合开头/高潮，0.4-0.7 中段铺陈，<0.4 仅 B-roll。 */
  highlight_score: number
  highlight_reason?: string | null
  sort_order: number
  /** 视频预处理状态。skipped = 非视频 / 关闭；pending = 入队；running = 切片+VLM 中；ready/failed 终态。 */
  preprocess_status?: MaterialPreprocessStatus
  /** failed 时一句话原因，前端 hover 显示。 */
  preprocess_error?: string | null
  /** PySceneDetect 切片产物；空数组 = 未预处理或失败回退。 */
  shots?: MaterialShot[]
}

export interface MaterialUploadResponse {
  session_id: SessionId
  materials: Material[]
}

/** 从系统素材库克隆到当前项目 —— 与 server schemas.py::MaterialCloneFromSystemRequest 镜像。 */
export interface MaterialCloneFromSystemRequest {
  project_id: string
  source_material_ids: MaterialId[]
}

export interface MaterialCloneFromSystemResponse {
  project_id: string
  materials: Material[]
  /** 未找到的源 material_id（不阻断），前端可在结果摘要里 hover 提示。 */
  skipped: MaterialId[]
}

// =========================================================================
// Module 4 — Gap
// =========================================================================

export interface Gap {
  gap_id: GapId
  section: SectionRole
  /** 所属 AdaptedSection.section_id；老 plan 为 null（前端按段分组时回落到 section role）。 */
  section_id?: string | null
  /** 所属项目 ID；老 gap 为 null。 */
  project_id?: string | null
  slot_index: number
  requirement: string
  status: GapStatus
  impact: 'high' | 'medium' | 'low'
  matched_material_id?: MaterialId | null
  note?: string | null
  sample_thumbnail_url?: string | null
}

export interface FillResult {
  gap_id: GapId
  action: FillAction
  new_material_id?: MaterialId | null
  narration?: string | null
  /** copy 动作 + voiceover_enabled=True 时后端自动 TTS 后回写的 wav URL。 */
  voiceover_url?: string | null
  alternatives: string[]
  /** aigc 链式生成的 N 段 CDN URL（按时序）；单段 = 1 元素，>12s 走链式。 */
  video_urls: string[]
  /** aigc 第一段封面 URL，前端预览缩略图。 */
  cover_url?: string | null
  /** aigc_image 产出：Seedream 文生图本地化路径 /aigc-images/<filename>。 */
  aigc_image_url?: string | null
  /** aigc_image 多镜头模式产出的 N 张图（同源 /aigc-images/...）。n_shots>1 时 Seedream sequential 故事板生成；
   *  plan.py 会把这段 section 展开成 N 个等长子 Scene，每个子 Scene 取列表中一张图。 */
  aigc_image_urls?: string[]
  /** aigc chunks 数量；0 表示非 aigc 或失败。 */
  chunks_count: number
  /** aigc 各 chunk 对应的 Seedance task_id；refresh 接口按此重试单段。 */
  chunk_task_ids: string[]
  note?: string | null
  status: GapStatus
  /** 所属 AdaptedSection.section_id；由后端在 fill_gap 时回填，plan 重建时不再依赖 gap_store 内存。 */
  section_id?: string | null
  /** copy fill 产出的字卡画面规格——决定 Scene 渲染什么字卡。 */
  text_card_spec?: TextCardSpec | null
}

export interface GapFillRequest {
  gap_id: GapId
  action: FillAction
  params: Record<string, unknown>
}

export interface AigcPromptRequest {
  gap_id: GapId
  /** 创作者额外提示（可选）：风格倾向、必须出现的元素等 */
  hint?: string | null
}

export interface AigcPromptResponse {
  gap_id: GapId
  /** LLM 转写出的完备 T2V prompt */
  prompt: string
  /** Agent 思考链：2-4 条短句，前端可视化『LLM 怎么想出这条 prompt 的』。 */
  thinking?: string[]
}

/** D2 图片参考：LLM 给出『本段需要哪几张图』的清单元素。 */
export interface ImageSpec {
  /** 槽位 ID（img-1 / img-2 / ...）。前端 imageSlots Map 的 key。 */
  slot_id: string
  /** 给创作者看的人话标题（≤80 字）。 */
  caption: string
  /** 给 Seedream 直接消费的中文 prompt（≤300 字）。 */
  prompt: string
  /** 画幅，如 16:9 / 9:16 / 1:1。 */
  ratio: string
}

export interface AigcImageSpecRequest {
  gap_id: GapId
  hint?: string | null
  /** 主体锚点清单——前端从 plan.adapted_sections.shots[].subject 取出，
   *  传给后端后会**强制**写进 LLM user prompt + post-validation 注入到每张 spec.prompt 前缀。 */
  subjects?: string[]
}

export interface AigcImageSpecResponse {
  gap_id: GapId
  specs: ImageSpec[]
  /** Agent 思考链：2-4 条短句，前端展示『LLM 怎么决定要这几张图的』。 */
  thinking?: string[]
}

/** Copy Outline Agent（T5/T6）—— 文案 fill 的"分析阶段"产出。 */
export type EmotionalHook = 'anxiety' | 'wow' | 'anticipation' | 'twist' | 'resonance'

/** 字卡字体族——LLM/前端共用的有限枚举。 */
export type TextCardFontFamily = 'bold_sans' | 'serif_classic' | 'handwriting' | 'tech_mono'
/** 字卡版式——主副文本的位置布局。 */
export type TextCardLayout = 'center' | 'top' | 'bottom' | 'split_top_bottom'
/** 字卡背景模式——纯色 / 渐变 / 模糊图片 / 暗罩。 */
export type TextCardBgMode = 'solid' | 'gradient' | 'image_blur' | 'dark_overlay'
/** 字卡入场动画。 */
export type TextCardAnimation = 'fade_in' | 'typewriter' | 'bounce_word' | 'zoom_pop'

/**
 * 个性化字卡画面规格——copy fill 的核心产出，决定后端 ffmpeg drawtext 怎么烧字。
 * 与后端 schemas.TextCardSpec 字段一致。
 */
export interface TextCardSpec {
  /** 主标题文本（≤24 字）。 */
  main_text: string
  /** 副标题/补充（≤40 字，可空）。 */
  sub_text: string
  font_family: TextCardFontFamily
  layout: TextCardLayout
  bg_mode: TextCardBgMode
  /** 背景主色 #RRGGBB。 */
  bg_color: string
  /** 文本主色 #RRGGBB。 */
  text_color: string
  /** 强调色（副文本/装饰） #RRGGBB。 */
  accent_color: string
  animation: TextCardAnimation
  /** 表情/小图标点缀，最多 3 个。 */
  emoji_decor: string[]
  /** 字卡播放时长（秒），1.5-15.0。 */
  duration_seconds: number
  /** 字号缩放系数；1.0=默认，<1 缩小、>1 放大；范围 [0.6, 1.6]。 */
  font_size_pct?: number
}

export interface CopyOutline {
  /** 字卡主文本（LLM 推荐）。 */
  main_text: string
  /** 字卡副文本（LLM 推荐）。 */
  sub_text: string
  /** 本段最该说的核心信息（≤20 字最佳，最大 80 字）。 */
  core_message: string
  emotional_hook: EmotionalHook
  /** 从 compose_settings.keywords 中本段最该承载的 1-2 个。 */
  must_include_keywords: string[]
  /** LLM 推荐的字卡完整规格——前端调参面板用作 defaults。 */
  recommended_spec: TextCardSpec
  /** 在全局 tone 基础上的微调，≤40 字。 */
  tone_lean: string
}

export interface CopyOutlineRequest {
  gap_id: GapId
  hint?: string | null
}

export interface CopyOutlineResponse {
  gap_id: GapId
  outline: CopyOutline
  /** Agent 思考链：2-4 条短句。 */
  thinking?: string[]
}

export interface AigcSeedreamRequest {
  prompt: string
  ratio: string
  /** 一次生成几张，1-4。 */
  n?: number
  /** 主体锚点（具象名词）。后端会在调 Seedream 前把 [必须画出且不可替换的主体: X]
   *  强制前缀注入 prompt——绕过 LLM 输出的同义化漂移。 */
  subject?: string
}

export interface SeedreamImage {
  url: string
  width: number
  height: number
}

export interface AigcSeedreamResponse {
  images: SeedreamImage[]
}

export interface AigcTailFrameRequest {
  plan_id: PlanId
  scene_id: string
}

export interface AigcTailFrameResponse {
  /** base64 data URL（image/jpeg），直接喂 Seedance first_frame_url。 */
  frame_data_url: string
}

// =========================================================================
// Module 5 — Plan
// =========================================================================

/**
 * 改编后的段落 —— 由 LLM 基于"样例 manifest.sections + 用户 brief + video_goal"产出。
 * 是 Plan 的"叙事单位"层，位于 Scene"剪辑单位"之上。
 *
 * 每段除了 role/theme，还携带 `content_description`（30-300 字内容说明），
 * 告诉创作者"本段画面/口播该呈现什么"——前端 AdaptedSectionList 直接展示。
 */
/**
 * stage-24：分镜计划——AdaptedSection 内的最小创作单位。
 * 1-3 个为常态，最多 5。subject 是 chip 展示，visual 是图像/视频生成 prompt，
 * narration 是本镜口播/字幕脚本，duration_seconds 决定 Scene 时长。
 */
export interface ShotPlan {
  order: number
  subject: string
  visual: string
  narration: string
  duration_seconds: number
  source_hint?: 'sample' | 'user_material' | 'aigc_t2v' | 'aigc_image' | 'text_card' | null
  matched_material_id?: string | null
  matched_material_shot_index?: number | null
  /** stage-26 PR-N.1：匹配质量三档。
   *  good=匹配分≥0.30，weak=≥0.10，missing=<0.10。
   *  物化层据此决策：missing 不再 cyclic 取错素材，改走 text_card 占位；
   *  weak 仍走 user_material 但前端段卡显示『待修补』提醒。 */
  match_quality?: 'good' | 'weak' | 'missing'
  /** shot_matcher 给的原始匹配分（0-1），仅用于排错；前端不直显数字。 */
  match_score?: number
  /** stage-25：本镜要呈现的目标列表（0-4 个）。空 = 单目标按 visual 整段出图（老路）；
   *  非空 = aigc 多图合成（N 张 Seedream → 喂 T2V）。 */
  targets?: ShotTarget[]
}

export interface AdaptedSection {
  section_id: string
  role: SectionRole
  theme: string
  content_description: string
  /** stage-16：本段相比样例做了什么调整 + 为什么（≤60 字）；旧数据为空串。 */
  adaptation_note?: string
  /** stage-16：本段节奏标签；可空。 */
  tempo?: Tempo | null
  /** 改编自原 manifest.sections 的下标；纯新增段为 []。 */
  source_section_indices: number[]
  /** 该段对应的样例 shot index 列表（用于缩略图反查）；纯新增段借相邻段的 shot。 */
  source_shot_indices: number[]
  order: number
  /** LLM 决定的本段目标时长（秒），驱动 Scene.duration 与 AIGC 链式分段。 */
  duration_seconds: number
  /** stage-24：本段内部分镜列表（0-5）。空列表→走单镜旧路径；非空→驱动 Scene 多镜物化。 */
  shots: ShotPlan[]
}

export interface Scene {
  scene_id: string
  section: SectionRole
  /** stage-24：本 Scene 属于哪个 AdaptedSection.section_id；旧 plan 为空时按 section（role）回退分组。 */
  parent_section_id?: string | null
  /** stage-24：本 Scene 在 section 内的分镜序号（从 0 起）；无切分时为 0。 */
  shot_order: number
  /** stage-24：分镜主体（人物/产品/动作短词），由 ShotPlan.subject 填，前端 chip 展示。 */
  shot_subject: string
  source: 'sample' | 'user_material' | 'aigc_t2v' | 'aigc_image' | 'text_card'
  source_ref: string
  start: number
  duration: number
  in_point: number
  out_point?: number | null
  narration?: string | null
  /** 本场口播 TTS 合成后的本地音频 URL（/voiceovers/<plan>/<scene>.wav）。 */
  voiceover_url?: string | null
  /** source=aigc_t2v 时 Seedance 返回的 N 段 CDN URL。 */
  aigc_video_urls: string[]
  /** source=aigc_image 时 Seedream 文生图本地化后的同源 URL（/aigc-images/...）。 */
  aigc_image_url?: string | null
  /** source=text_card 时的字卡规格；其他 source 为 null/undefined。 */
  text_card_spec?: TextCardSpec | null
  /** source=aigc_image 时 Remotion AnimatedImage 渲染规格；预览侧用来跑动效。 */
  animation_spec?: AnimationSpec | null
  /** stage-26 PR-N.1：本 Scene 需要用户介入修补（弱匹配/缺匹配/兜底占位）。
   *  true 时段卡质量色条把这一镜计入『待修补』，UI 挂橙色提示 chip。
   *  换源接口成功后清回 false。 */
  needs_fill?: boolean
  /** 与上一段衔接方式；sc-0 永远忽略此字段。None / hard_cut 走 concat demuxer，其他走 xfade。 */
  transition_in?: SceneTransition | null
}

export interface SceneTransition {
  style: TransitionStyle
  /** 转场持续秒数（0.1–1.5），与上一段尾部 overlap 长度。 */
  duration: number
}

/** 与后端 schemas.py::AnimationSpec / remotion/src/AnimatedImage.tsx 镜像；改字段时务必同步三处。 */
export type AnimationType = 'ken-burns' | 'parallax' | 'storyboard' | 'keyframe_morph' | 'static'
export type AnimationMotionDirection = 'in' | 'out' | 'pan-left' | 'pan-right' | 'pan-up' | 'pan-down'
export type AnimationTransition = 'cross-fade' | 'cut' | 'slide-left'
export interface AnimationSpec {
  engine: 'remotion' | 'ffmpeg'
  animation_type: AnimationType
  motion_direction: AnimationMotionDirection
  intensity: number
  transition: AnimationTransition
  transition_duration: number
  image_urls: string[]
}

export interface PackagingItem {
  item_id: string
  kind: 'subtitle' | 'title_bar' | 'sticker' | 'transition' | 'cover'
  start: number
  end: number
  text?: string | null
  style: Record<string, unknown>
}

export type BGMEnergyShape = 'flat' | 'single_peak' | 'multi_peak' | 'build_up' | 'wave'
/**
 * BGM 能量形态——叙事性的整体走向，决定怎么和视频配合。
 *
 * - flat         全程平稳，适合科普/Vlog/治愈类视频做底色
 * - single_peak  单峰爆发，适合带 CTA / 卖点对比 / 反转视频
 * - multi_peak   多峰起伏，适合长剧情 / 多卖点串烧
 * - build_up     渐强推进，适合预告 / 蓄势 / 反差揭示
 * - wave         波浪起伏，适合情绪 Vlog / 故事性叙事
 */

export type BGMHighlightKind = 'climax' | 'drop' | 'build_start' | 'release' | 'break'

export interface BGMHighlight {
  /** 节点出现的时间（秒，相对 BGM t=0）。 */
  at_seconds: number
  kind: BGMHighlightKind
  /** 节点小标，例『副歌入』『鼓点 drop』（≤12 字）。 */
  label: string
  /** 建议把这个节点对齐到视频的什么动作（卖点/反转/CTA）。 */
  fit_with_video: string
}

export interface BGMCalmSegment {
  start: number
  end: number
  /** 为什么这段适合做铺垫，例『纯钢琴留白，适合压口播』。 */
  note: string
}

/**
 * LLM 音频理解结果（doubao-seed-2.0-lite v2）：能量形态 + 关键节点 + 平稳段 + 视频配合建议。
 *
 * 替代旧 4-6 段切片色块——重点不是"段落罗列"而是"叙事性能量走向 + 真正值得对齐的鼓点"。
 * 全程平稳的曲子可以 climaxes=[]，由 overall_advice 解释为什么平稳反而合适。
 *
 * 后端在 plan 绑定 BGM 时一次性算好，挂在 BGMConfig.analysis；失败/超时则保持 null。
 */
export interface BGMAnalysis {
  title_guess: string
  mood_tags: string[]
  /** 能量整体走向——决定视频该怎么用这首曲子。 */
  energy_shape: BGMEnergyShape
  /** 一句话讲为什么是这种形态、适合什么类型视频。 */
  energy_shape_reason: string
  /** 0-1：曲子与 brief 的契合度。 */
  theme_fit_score: number
  theme_fit_reason: string
  /** 真正值得对齐的高潮/鼓点（0-3 个）；全程平稳时为空数组。 */
  climaxes: BGMHighlight[]
  /** 平稳/留白区间，可以承载长口播 / 慢镜头。 */
  calm_segments: BGMCalmSegment[]
  /** 叙事性总建议：曲子和视频的配合策略。 */
  overall_advice: string
  /** 生成 backend：doubao_ark / mock。 */
  backend: string
}

export interface BGMConfig {
  /** BGM 资产 ID（asset library 中的 id）。 */
  asset_id?: string | null
  track_url?: string | null
  volume: number
  fade_in: number
  fade_out: number
  /** BGM 总时长（秒）；上传后 librosa 探测填入。 */
  duration_seconds?: number | null
  /** AI 识别的能量峰值时间点（秒）；前端拖动 anchor 时显示参考线。 */
  peak_seconds?: number | null
  /**
   * BGM 起点在视频时间轴上的位置（秒）。
   * 正值 = 视频先静音 N 秒再起 BGM；
   * 负值 = 跳过 BGM 开头 -N 秒，立刻在 t=0 起。
   */
  video_anchor_seconds: number
  /** 是否在口播时段降低 BGM 音量（sidechain ducking）。 */
  duck_with_voice: boolean
  /** ducking 衰减强度（dB），负值。 */
  duck_attenuation_db: number
  /** LLM 音频理解结果；绑定时异步填充，失败/超时保持 null。 */
  analysis?: BGMAnalysis | null
}

/** TTS voice 角色 —— 火山引擎可选音色（与后端 TTSVoice 镜像）。 */
export type TTSVoice =
  | 'zh_female_qingxin'
  | 'zh_male_jieshuo'
  | 'zh_female_wenrou'
  | 'zh_male_xueyi'
  | 'zh_female_xiaoyu'

/** 目标平台 —— 决定画幅 + 节奏 + 字幕风格。 */
export type TargetPlatform = 'douyin' | 'wechat' | 'xiaohongshu' | 'bilibili'

/** 画面比例 —— v2 起独立于 target_platform。 */
export type AspectRatio = '9:16' | '16:9' | '1:1'

/** 整体调性 —— 影响 LLM 段落 prompt 倾向。 */
export type ToneStyle = 'tight_hype' | 'calm_narrative' | 'casual_daily' | 'professional_cool'

/**
 * stage-23 结构迁移倾向 —— 用户在 Compose Step1 选「我想要哪个版本」。
 * mirror 平淡复刻 / amp_emotion 情绪增强（默认） / amp_pace 节奏紧凑。
 */
export type MigrationPreference = 'mirror' | 'amp_emotion' | 'amp_pace'

/**
 * Compose 页用户配置 —— 与 brief/video_goal 一起驱动结构改编。
 * 折叠"高级设置"暴露，全部带默认值。
 */
export interface ComposeSettings {
  /** 目标总时长（秒），驱动每段 duration_seconds 分配。 */
  target_duration_seconds: number
  /** 目标平台。决定画幅 + 节奏 + 字幕风格。 */
  target_platform: TargetPlatform
  /** 画面比例（v2 显式字段，独立于 target_platform）。允许 B 站发竖屏、抖音发方版等组合。 */
  aspect_ratio: AspectRatio
  /** 整体调性。影响 LLM 段落结构与口播倾向。 */
  tone: ToneStyle
  /** stage-23：结构迁移倾向（情绪增强 / 节奏紧凑 / 平淡复刻）。 */
  migration_preference: MigrationPreference
  /** 核心 CTA 文案（≤20 字）。closing 段自动套用。 */
  cta: string
  /** 必须出现的关键词（最多 5 个）。每段 narration 至少出现 1 个。 */
  keywords: string[]
  /**
   * 是否显示字幕（step2 字幕轨开关；与 TTS 解耦）。
   * 关掉 → packaging_track 不生成 subtitle 项，渲染时无字幕；
   * 开启 → 每段 scene.narration 烧成字幕，但 `text_card_spec` 非空的段始终跳过（字卡画面已自带文字）。
   */
  subtitle_enabled: boolean
  /**
   * 是否做 TTS 口播合成（step3 单独开关；与字幕显隐解耦）。
   * 关掉 → 跳过 TTS、纯 BGM 视频；
   * 开启 → 对每段 scene.narration 调 ARK TTS 合成并混入主轨。
   */
  voiceover_enabled: boolean
  /** TTS 音色。voiceover_enabled=False 时此字段忽略。 */
  tts_voice: TTSVoice
  /** 包装阶段用户配置（转场白名单/字幕样式/封面策略/LLM 温度）。 */
  packaging_prefs: PackagingPreferences
  /** frame.md 设计系统 token（色板/字体/动效密度），全片包装统一。 */
  frame_design: FrameDesignSystem
}

/** 包装风格预设。custom 表示用户在 UI 上动了任何具体字段。 */
export type PackagingPreset =
  | 'minimalist'
  | 'energetic'
  | 'info_feed'
  | 'dialogue'
  | 'custom'

/** 字幕字号 —— ffmpeg drawtext fontsize 对齐：small=36/medium=48/large=64。 */
export type SubtitleFontSize = 'small' | 'medium' | 'large'

/** 字幕画面位置 —— top=画面上 1/8 / middle=正中 / bottom=底部（默认）。 */
export type SubtitlePosition = 'top' | 'middle' | 'bottom'

/** 字幕底色 —— none=只描边 / shadow=黑底半透明 / gradient=厚底高斯模糊。 */
export type SubtitleBackground = 'none' | 'shadow' | 'gradient'

/** 封面主标题文字来源。 */
export type CoverTextSource = 'auto' | 'video_goal' | 'custom'

/**
 * 包装阶段用户配置 —— 存在 plan.settings.packaging_prefs，
 * PackagingPanel 调推荐时可经请求体覆盖并回写。
 */
export interface PackagingPreferences {
  preset: PackagingPreset
  /** 允许的转场风格白名单。LLM 输出不在此列表内的会被替换成首项。 */
  allowed_transition_styles: TransitionStyle[]
  /** 转场持续秒数上限。LLM 输出超过此值会被 clamp。 */
  max_transition_duration: number
  subtitle_font_size: SubtitleFontSize
  subtitle_position: SubtitlePosition
  subtitle_background: SubtitleBackground
  /** 开启后 LLM 给每段 narration 翻译一句英文，drawtext 两行展示。 */
  subtitle_bilingual: boolean
  cover_text_source: CoverTextSource
  /** cover_text_source=custom 时使用（≤20 字，渲染时截到 12 字）。 */
  cover_custom_text?: string | null
  /** 封面停留秒数（0.6 ~ 2.0）。 */
  cover_duration: number
  /** 封面副标题是否同时显示；false 时即使 LLM 给了 subtitle 也不渲染。 */
  cover_with_subtitle: boolean
  /** PackagingAgent 调 LLM 的温度（0.3 ~ 0.9）。 */
  llm_temperature: number
}

export const DEFAULT_PACKAGING_PREFERENCES: PackagingPreferences = {
  preset: 'custom',
  allowed_transition_styles: ['hard_cut', 'dissolve', 'slide', 'zoom', 'whip', 'wipe'],
  max_transition_duration: 0.8,
  subtitle_font_size: 'medium',
  subtitle_position: 'bottom',
  subtitle_background: 'shadow',
  subtitle_bilingual: false,
  cover_text_source: 'auto',
  cover_custom_text: null,
  cover_duration: 1.2,
  cover_with_subtitle: true,
  llm_temperature: 0.7,
}

/** frame.md 设计系统预设（参考 HyperFrames frame.md 模板）。 */
export type FrameDesignPreset =
  | 'custom'
  | 'biennale-yellow'
  | 'blockframe'
  | 'blue-professional'
  | 'bold-poster'
  | 'broadside'
  | 'capsule'
  | 'cartesian'
  | 'cobalt-grid'
  | 'coral'
  | 'creative-mode'

/** 画面动效密度。 */
export type MotionDensity = 'minimal' | 'balanced' | 'kinetic'

/**
 * frame.md —— 为相机重写的设计系统 token。
 * HyperFrames 提出的"DESIGN.md 视频版"，packaging/copy/aigc agent 都从这里读色板/字号/动效密度，
 * 避免 4 段视频视觉割裂。preset != custom 时空字段由 agent 按预设填充。
 */
export interface FrameDesignSystem {
  preset: FrameDesignPreset
  /** 主色板 HEX，最多 6 色。第一色=primary，第二=accent，余下=supporting。 */
  palette: string[]
  /** 主背景色 HEX，例如 #03071e。空=按 preset 默认。 */
  background_color: string
  /** 标题字体族。 */
  typography_display: string
  /** 正文字体族。 */
  typography_body: string
  /** 等宽字体（用于代码/数字）。 */
  typography_mono: string
  /** 动效密度：kinetic=高密度推荐社媒；minimal=克制品牌片。 */
  motion_density: MotionDensity
  /** 颗粒/胶片质感（参考 HyperFrames grain-overlay 组件）。 */
  grain_overlay: boolean
  /** 暗角（参考 HyperFrames vignette 组件）。 */
  vignette: boolean
  /** 额外风格备注，自由文本。 */
  notes: string
}

export const DEFAULT_FRAME_DESIGN: FrameDesignSystem = {
  preset: 'custom',
  palette: [],
  background_color: '',
  typography_display: '',
  typography_body: '',
  typography_mono: '',
  motion_density: 'balanced',
  grain_overlay: false,
  vignette: false,
  notes: '',
}

export const DEFAULT_COMPOSE_SETTINGS: ComposeSettings = {
  target_duration_seconds: 30,
  target_platform: 'douyin',
  aspect_ratio: '9:16',
  tone: 'tight_hype',
  migration_preference: 'amp_emotion',
  cta: '',
  keywords: [],
  subtitle_enabled: false,
  voiceover_enabled: false,
  tts_voice: 'zh_female_qingxin',
  packaging_prefs: DEFAULT_PACKAGING_PREFERENCES,
  frame_design: DEFAULT_FRAME_DESIGN,
}

export interface Plan {
  plan_id: PlanId
  /** 本 plan 改编自哪些 (sample_id, slot_id) 版本（1-2 个）。多样例时段落结构是合并喂给 LLM 的对等参考池。 */
  reference_versions: ReferenceVersion[]
  /** 所属项目 ID；老 plan 为 null。 */
  project_id?: string | null
  session_id?: string | null
  brief?: string | null
  /** 视频要求与目的（受众/时长/调性等），驱动结构改编。 */
  video_goal?: string | null
  /** LLM 改编后的段落结构；空数组表示老 plan（前端兜底用 main_track 渲染）。 */
  adapted_sections: AdaptedSection[]
  variant: Variant
  duration_seconds: number
  main_track: Scene[]
  packaging_track: PackagingItem[]
  bgm: BGMConfig
  /** 创作设置回写。 */
  settings: ComposeSettings
  /** 本次 plan/build 注入的个性知识库规则总数（0 = 默认库以外没有命中项目级规则）。 */
  kb_rules_applied?: number
  /** stage-28 LLM 多信号情绪曲线；老 plan 为 null（Compose EmotionCurveCard 时 fallback 不画）。 */
  emotion_curve?: EmotionCurve | null
}

export interface PlanBuildRequest {
  /** 参考版本列表（1-2 个 (sample_id, slot_id) pair）。多选时两份段落结构会被合并成对等参考池喂给 plan_agent。 */
  reference_versions: ReferenceVersion[]
  /** 所属项目 ID（前端 currentProjectId）；后端按它路由素材/资产/落盘。 */
  project_id: string
  /** 兼容老前端：留作 project_id 别名；为空时回退到 project_id。 */
  session_id?: SessionId | null
  brief?: string | null
  /** 视频要求与目的，与 brief 一起驱动结构改编。 */
  video_goal?: string | null
  /** 创作设置。 */
  settings?: ComposeSettings
  selected_materials: MaterialId[]
  fills: FillResult[]
  /** 增量重建：透传上一版 plan.adapted_sections，跳过 LLM 段落改编（修复 5→4 抖动 bug）。 */
  reuse_sections?: AdaptedSection[]
  variant: Variant
}

export interface GapFillAllRequest {
  plan_id: PlanId
  /** 批量补全使用的动作；默认 aigc（向后兼容）。rerank 不支持批量。 */
  action?: 'copy' | 'aigc' | 'aigc_image'
  prompt_template?: string | null
  /** 已采纳/已完成的 gap_id 列表，传过去让后端跳过它们（避免覆盖单条手动生成的字卡/镜头）。 */
  skip_gap_ids?: GapId[]
  /** 已采纳字卡 spec 列表——作为风格样板透传，绕过 plan_id 时序竞态。 */
  existing_text_cards?: TextCardSpec[]
}

export interface GapFillAllResponse {
  plan_id: PlanId
  fills: FillResult[]
  failed_gap_id?: GapId | null
  stopped_reason?: string | null
}

export interface GapDetectRequest {
  plan_id: PlanId
  /** 所属项目 ID（推荐显式传）；与 session_id 等价。 */
  project_id?: string | null
  /** 兼容老前端：留作 project_id 别名。 */
  session_id?: SessionId | null
}

// =========================================================================
// Module 5c — Voice (TTS)
// =========================================================================

export interface VoiceSynthesizeRequest {
  plan_id: PlanId
  scene_id: string
  /** 覆盖 scene.narration 用的临时文案；不传则用 scene.narration。 */
  text?: string | null
  /** 覆盖 plan.settings.tts_voice。 */
  voice?: TTSVoice | null
}

export interface VoiceSynthesizeResponse {
  plan_id: PlanId
  scene_id: string
  voiceover_url: string
  /** 实际使用的后端：mock = 单元测试/无 Key 兜底；volc = 火山引擎 TTS。 */
  backend: 'mock' | 'volc'
  chars: number
}

export interface VoiceSynthesizeAllResponse {
  plan_id: PlanId
  backend: 'mock' | 'volc'
  synthesized: VoiceSynthesizeResponse[]
  skipped_scene_ids: string[]
  failures: Array<{ scene_id: string; code?: string | null; error: string }>
}

// =========================================================================
// Module 5d — BGM patch
// =========================================================================

export interface PlanBgmPatch {
  /** 替换 BGM 资产；不传则保留现有 asset_id。 */
  bgm_asset_id?: string | null
  /** 拖动到的视频时间轴位置（秒）。 */
  video_anchor_seconds?: number | null
  /** 0.0 ~ 1.0 之间。 */
  volume?: number | null
  fade_in?: number | null
  fade_out?: number | null
  duck_with_voice?: boolean | null
}

/**
 * PATCH /plan/{plan_id}/settings —— 直接翻转单个 ComposeSettings 项；
 * 所有字段可选，未传字段保持现值。
 * 后端 router.plan.PlanSettingsPatch 的前端镜像。
 */
export interface PlanSettingsPatch {
  subtitle_enabled?: boolean | null
  voiceover_enabled?: boolean | null
  tts_voice?: TTSVoice | null
  target_platform?: TargetPlatform | null
  aspect_ratio?: AspectRatio | null
  tone?: ToneStyle | null
  cta?: string | null
  keywords?: string[] | null
  target_duration_seconds?: number | null
}

/**
 * PATCH /plan/{plan_id}/scene/{scene_id} —— 直接编辑一个 Scene 的可改文本字段，
 * theme/content_description 联动到对应 AdaptedSection（按 sc-<order> 解析）。
 * 后端 router.plan.SceneEditPatch 的前端镜像。
 */
export interface SceneEditPatch {
  narration?: string | null
  theme?: string | null
  content_description?: string | null
}

/**
 * PATCH /plan/{plan_id}/scene/{scene_id}/transition —— 改某分镜入场转场样式。
 * style=hard_cut 表示清空 transition_in（concat demuxer 走硬切）。
 */
export interface SceneTransitionPatch {
  style: TransitionStyle
  duration?: number | null
}

// =========================================================================
// Module 5b — Packaging Agent
// =========================================================================

export type TransitionStyle = 'hard_cut' | 'dissolve' | 'slide' | 'zoom' | 'whip' | 'wipe'

export interface TransitionSuggestion {
  item_id: string
  at_seconds: number
  from_section: SectionRole
  to_section: SectionRole
  style: TransitionStyle
  duration: number
  /** HyperFrames catalog block 名（风格 hint，不替换 ffmpeg xfade）。 */
  catalog_block?: string | null
  reason: string
}

export interface CoverDesign {
  item_id: string
  title: string
  subtitle?: string | null
  palette: string[]
  layout: 'center' | 'left' | 'split' | 'stacked'
  /** HyperFrames cover catalog block 名（风格 hint）。 */
  catalog_block?: string | null
  style_note: string
}

/** stage-16：包装多版本（aggressive 强冲击 / elegant 高级感）。 */
export interface PackagingVariant {
  version_id: 'aggressive' | 'elegant'
  version_label: string
  transitions: TransitionSuggestion[]
  cover?: CoverDesign | null
}

export interface PackagingRecommendation {
  plan_id: PlanId
  /** stage-16：多版本数组；前端 Tab 切换展示。落地到 plan 时取 versions[0]（aggressive）。 */
  versions: PackagingVariant[]
  notes: string[]
  /** 兼容旧数据：顶层 transitions/cover；新代码请用 versions[0]。 */
  transitions?: TransitionSuggestion[]
  cover?: CoverDesign | null
}

export interface PackagingRecommendRequest {
  plan_id: PlanId
  apply?: boolean
  /**
   * 用户偏好。None 时直接用 plan.settings.packaging_prefs；
   * 非空时与之合并（请求体优先），结果回写到 plan.settings.packaging_prefs。
   */
  preferences?: PackagingPreferences | null
}

// ---- V2：5 维度独立多候选 ----

export interface SubtitleStyleCandidate {
  candidate_id: string
  label: string
  font_size: SubtitleFontSize
  position: SubtitlePosition
  background: SubtitleBackground
  bilingual: boolean
  rationale: string
}

export interface TitleBarCandidate {
  candidate_id: string
  text: string
  target_scene_id: string
  start: number
  end: number
  font_size: 'small' | 'medium' | 'large'
  color: string
  background_color: string
  position: 'top' | 'middle'
  rationale: string
}

export interface StickerCandidate {
  candidate_id: string
  text: string
  target_scene_id: string
  start: number
  end: number
  color: string
  background_color: string
  position: 'bottom-center' | 'top-right' | 'bottom-right' | 'middle'
  rationale: string
}

export interface TransitionCandidateBundle {
  candidate_id: string
  at_seconds: number
  from_section: string
  to_section: string
  options: TransitionSuggestion[]
  rationale: string
}

export interface CoverCandidate {
  candidate_id: string
  title: string
  subtitle?: string | null
  palette: string[]
  layout: 'center' | 'left' | 'split' | 'stacked'
  catalog_block?: string | null
  style_note: string
  rationale: string
}

/** /api/catalog/blocks 返回的 HyperFrames catalog 单条目。 */
export type CatalogCategory =
  | 'transition'
  | 'caption'
  | 'vfx'
  | 'overlay'
  | 'data-viz'
  | 'cover'
  | 'code-snippet'
  | 'other'

export interface CatalogItem {
  name: string
  title?: string | null
  description: string
  tags: string[]
  kind: 'block' | 'component'
  category: CatalogCategory
  duration?: number | null
  preview_video?: string | null
  preview_poster?: string | null
}

export interface CatalogListResponse {
  source: string
  version: string
  license: string
  items: CatalogItem[]
  total: number
}

export interface PackagingRecommendationV2 {
  plan_id: PlanId
  subtitle_styles: SubtitleStyleCandidate[]
  title_bars: TitleBarCandidate[]
  stickers: StickerCandidate[]
  transition_bundles: TransitionCandidateBundle[]
  covers: CoverCandidate[]
  notes: string[]
}

export interface PackagingSelection {
  plan_id: PlanId
  subtitle_style_id?: string | null
  title_bar_ids: string[]
  sticker_ids: string[]
  /** bundle_id → 用户挑选的 TransitionStyle。 */
  transition_selections: Record<string, TransitionStyle>
  cover_id?: string | null
  /** 推荐快照随请求送回（服务端无状态）。 */
  recommendation: PackagingRecommendationV2
}

// F2 · 单组件 picker 增量接口
export interface PackagingItemDraftRequest {
  plan_id: PlanId
  kind: 'title_bar' | 'sticker' | 'cover'
}

export interface PackagingItemDraftResponse {
  item: PackagingItem
  rationale: string
}

export interface PackagingItemPlaceRequest {
  plan_id: PlanId
  item: PackagingItem
}

// =========================================================================
// Module 5c — Plan 命名快照
// =========================================================================

export interface PlanSnapshotMeta {
  snapshot_id: string
  name: string
  plan_id: PlanId
  project_id?: string | null
  user_id?: string | null
  ts: number
}

export interface PlanSnapshotCreateRequest {
  name: string
}

// =========================================================================
// Module 6 — Render
// =========================================================================

export interface RenderSubmitRequest {
  plan_id: PlanId
  variant: Variant
}

export interface RenderSubmitResponse {
  job_id: JobId
}

export interface RenderDonePayload {
  plan_id: PlanId
  variant: Variant
  video_url: string
  cover_url: string
  duration_seconds: number
  timings_ms?: Record<string, number>
  notes?: string[]
}

// =========================================================================
// Module 7 — Edit
// =========================================================================

export interface EditMark {
  track: 'main' | 'packaging'
  start: number
  end: number
  target_id?: string | null
}

export interface EditApplyRequest {
  plan_id: PlanId
  /** 轨道意图。main 在 project.current_step==='render' 时被后端 409 拒绝。 */
  track: 'main' | 'packaging' | 'voice'
  instruction: string
  marks: EditMark[]
}

// ---- Compose 对话编辑小助手（⌘K command bar / R6） ---------------------------

export type ComposeEditStep = 'step2' | 'step3'

export interface ComposeEditDiff {
  op: string
  target_id?: string | null
  before?: unknown
  after?: unknown
  summary: string
  /** dry-run 阶段后端回传的 mutator 参数，apply 时原样回放（确定性落地） */
  args?: Record<string, unknown>
}

export interface ComposeEditRequest {
  plan_id: PlanId
  step: ComposeEditStep
  instruction: string
  apply?: boolean
  /** apply=true 时回传 dry-run 拿到的 ops，后端跳过 LLM 直接回放，保证多 diff 全部落地 */
  confirmed_ops?: Array<Record<string, unknown>>
}

export interface ComposeEditResponse {
  plan_id: PlanId
  diffs: ComposeEditDiff[]
  applied: boolean
  plan?: Plan | null
  note?: string | null
}

/** ⌘K dry-run 后用户撤回某条 diff —— 落 profile TraceB 负信号 */
export interface ComposeEditDismissRequest {
  plan_id: PlanId
  step: ComposeEditStep
  instruction: string
  dismissed_ops: Array<Record<string, unknown>>
}

// =========================================================================
// SSE
// =========================================================================

export interface ProgressEventPayload {
  step: string
  percent: number
  payload: Record<string, unknown>
}

// =========================================================================
// Health
// =========================================================================

export interface HealthResponse {
  status: 'healthy' | 'degraded'
  version: string
  llm_provider: string
  t2v_provider: string
  asr_provider: string
}

// =========================================================================
// Asset Library（素材库：BGM + 参考图 + 参考视频）
// =========================================================================

export type AssetKind = 'bgm' | 'reference_image' | 'reference_video'
export type AssetStatus = 'processing' | 'ready' | 'failed'

export interface Asset {
  asset_id: string
  owner: string
  kind: AssetKind
  file_name: string
  file_url: string
  file_size: number
  content_hash: string
  mime: string
  title: string
  description: string
  tags: string[]
  metadata: Record<string, unknown>
  status: AssetStatus
  error: string | null
  created_at: number
  last_used_at: number | null
  use_count: number
}

export interface AssetUpdateRequest {
  title?: string | null
  description?: string | null
  tags?: string[] | null
}

export interface AssetListResponse {
  items: Asset[]
  total: number
}

/** POST /api/asset/save-from-url —— 把 Seedream 临时 CDN 图片永久落进资产库。 */
export interface AssetSaveFromUrlRequest {
  project_id: string
  url: string
  kind?: AssetKind
  title?: string | null
  tags?: string[] | null
}

// =========================================================================
// Project（项目工作流容器）
// =========================================================================

/** 项目状态：草稿（刚选样例）/ 已规划（plan/build 跑过）/ 已渲染（拿到视频）。 */
export type ProjectStatus = 'draft' | 'planned' | 'rendered'

/** 线性工作流的四个步骤；Migrate 是 view-only，不在 commit 序列里。 */
export type StepName = 'library' | 'decompose' | 'compose' | 'render'

/** 单步状态：未开始 / 进行中 / 已保存 / 上游变了快照过期但产物仍可看。 */
export type StepStatus = 'pending' | 'in_progress' | 'saved' | 'dirty'

/**
 * 「下一步」点击时落盘的单步产物快照。payload 内容随 step 不同：
 * - library:   { references: ReferenceVersion[] }（1-2 个；老前端可仍传 sample_ids 走后端兼容）
 * - decompose: { references: ReferenceVersion[] }
 * - compose:   { plan_id, fill_ids }
 * - render:    { job_id }
 */
export interface StepSnapshot {
  step: StepName
  saved_at: number
  payload: Record<string, unknown>
}

/** Project.step_states 字段——顶部导航徽章数据源。 */
export interface ProjectStepState {
  library: StepStatus
  decompose: StepStatus
  compose: StepStatus
  render: StepStatus
}

/**
 * Project = 一次完整的「样例 → 改编 → 补全 → 渲染」流程容器。
 * 后端用 project_id 作为唯一隔离键：素材 / 资产库 / plans / gaps 都按它分组。
 */
export interface Project {
  project_id: string
  name: string
  /** 视频种类（建项目时选定，可改）；老项目可能为 null。 */
  video_type?: VideoType | null
  /** (sample_id, slot_id) 对（0-2 个，跨项目共享）。新项目可暂为空，等用户进 Decompose 选样例后回填。 */
  reference_versions: ReferenceVersion[]
  brief?: string | null
  video_goal?: string | null
  settings: ComposeSettings
  last_plan_id?: PlanId | null
  last_render_job_id?: JobId | null
  status: ProjectStatus
  step_states: ProjectStepState
  current_step: StepName
  created_at: number
  updated_at: number
}

export interface ProjectCreateRequest {
  name: string
  /** 视频种类（建项目时即定）。 */
  video_type?: VideoType | null
  /** 0-2 个 (sample_id, slot_id) 参考版本。建项目时可为空——用户在 Decompose 页选样例后回填。 */
  reference_versions?: ReferenceVersion[]
}

export interface ProjectUpdateRequest {
  name?: string | null
  video_type?: VideoType | null
  /** 显式传 [] 表示清空（回到「未选样例」态）；undefined / null 表示不动。 */
  reference_versions?: ReferenceVersion[] | null
  brief?: string | null
  video_goal?: string | null
  settings?: ComposeSettings | null
  last_plan_id?: PlanId | null
  last_render_job_id?: JobId | null
  status?: ProjectStatus | null
  step_states?: ProjectStepState | null
  current_step?: StepName | null
}

export interface ProjectListResponse {
  items: Project[]
}
