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
 * 段落角色 —— 任何视频都适用的抽象骨架。每个 manifest 必须有恰好 1 个 opening +
 * 1 个 closing，最多 1 个 climax，其余皆 development。
 */
export type SectionRole = 'opening' | 'development' | 'climax' | 'closing'

export type GapStatus = 'ok' | 'warn' | 'miss'
export type FillAction = 'rerank' | 'copy' | 'aigc'
export type Variant = 'A' | 'B'
export type JobStatus = 'pending' | 'running' | 'succeeded' | 'failed' | 'cancelled'
export type MediaType = 'video' | 'image' | 'audio'

// =========================================================================
// Module 1 — Library
// =========================================================================

export interface LibraryItem {
  id: SampleId
  title: string
  video_type: VideoType
  scene: string
  duration_seconds: number
  shot_count: number
  cover_url: string
  source: 'system' | 'user'
}

export interface Shot {
  index: number
  start: number
  end: number
  duration: number
  thumbnail_url?: string | null
  transcript?: string | null
  tags: string[]
}

export interface RhythmCurve {
  times: number[]
  cut_density: number[]
  bgm_energy: number[]
  tempo_bpm?: number | null
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
  /** LLM 建议切几段（3-6） */
  suggested_segments: number
  /** 基调描述：『冷静克制』『高燃热血』『诙谐自嘲』等 */
  tone: string
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
}

// =========================================================================
// Module 2 — Decompose
// =========================================================================

export interface DecomposeRequest {
  sample_id: SampleId
  video_type: VideoType
}

export interface DecomposeSubmitResponse {
  job_id: JobId
}

// =========================================================================
// Module 3 — Material
// =========================================================================

export interface Material {
  material_id: MaterialId
  filename: string
  media_type: MediaType
  duration_seconds?: number | null
  thumbnail_url?: string | null
  tags: string[]
  recommended_section?: SectionRole | null
  /** 高光评分 0-1：0.7+ 适合开头/高潮，0.4-0.7 中段铺陈，<0.4 仅 B-roll。 */
  highlight_score: number
  highlight_reason?: string | null
  sort_order: number
}

export interface MaterialUploadResponse {
  session_id: SessionId
  materials: Material[]
}

// =========================================================================
// Module 4 — Gap
// =========================================================================

export interface Gap {
  gap_id: GapId
  section: SectionRole
  /** 所属 AdaptedSection.section_id；老 plan 为 null（前端按段分组时回落到 section role）。 */
  section_id?: string | null
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
  alternatives: string[]
  note?: string | null
  status: GapStatus
}

export interface GapFillRequest {
  gap_id: GapId
  action: FillAction
  params: Record<string, unknown>
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
export interface AdaptedSection {
  section_id: string
  role: SectionRole
  theme: string
  content_description: string
  /** 改编自原 manifest.sections 的下标；纯新增段为 []。 */
  source_section_indices: number[]
  /** 该段对应的样例 shot index 列表（用于缩略图反查）；纯新增段借相邻段的 shot。 */
  source_shot_indices: number[]
  order: number
}

export interface Scene {
  scene_id: string
  section: SectionRole
  source: 'sample' | 'user_material' | 'aigc_t2v'
  source_ref: string
  start: number
  duration: number
  in_point: number
  out_point?: number | null
  narration?: string | null
}

export interface PackagingItem {
  item_id: string
  kind: 'subtitle' | 'title_bar' | 'sticker' | 'transition' | 'cover'
  start: number
  end: number
  text?: string | null
  style: Record<string, unknown>
}

export interface BGMConfig {
  track_url?: string | null
  volume: number
  fade_in: number
  fade_out: number
}

export interface Plan {
  plan_id: PlanId
  sample_id: SampleId
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
}

export interface PlanBuildRequest {
  sample_id: SampleId
  session_id: SessionId
  brief?: string | null
  /** 视频要求与目的，与 brief 一起驱动结构改编。 */
  video_goal?: string | null
  selected_materials: MaterialId[]
  fills: FillResult[]
  variant: Variant
}

export interface GapDetectRequest {
  plan_id: PlanId
  session_id?: SessionId | null
  /** false 时缺素材不回退 mock，所有 gap 都标 miss，逼用户走 copy/aigc 补全。 */
  allow_mock?: boolean
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
  reason: string
}

export interface CoverDesign {
  item_id: string
  title: string
  subtitle?: string | null
  palette: string[]
  layout: 'center' | 'left' | 'split' | 'stacked'
  style_note: string
}

export interface PackagingRecommendation {
  plan_id: PlanId
  transitions: TransitionSuggestion[]
  cover?: CoverDesign | null
  notes: string[]
}

export interface PackagingRecommendRequest {
  plan_id: PlanId
  apply?: boolean
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
  instruction: string
  marks: EditMark[]
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
