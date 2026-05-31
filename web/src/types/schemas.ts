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
  /** aigc chunks 数量；0 表示非 aigc 或失败。 */
  chunks_count: number
  /** aigc 各 chunk 对应的 Seedance task_id；refresh 接口按此重试单段。 */
  chunk_task_ids: string[]
  note?: string | null
  status: GapStatus
  /** 所属 AdaptedSection.section_id；由后端在 fill_gap 时回填，plan 重建时不再依赖 gap_store 内存。 */
  section_id?: string | null
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
  /** LLM 决定的本段目标时长（秒），驱动 Scene.duration 与 AIGC 链式分段。 */
  duration_seconds: number
}

export interface Scene {
  scene_id: string
  section: SectionRole
  source: 'sample' | 'user_material' | 'aigc_t2v' | 'text_card'
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

/** 整体调性 —— 影响 LLM 段落 prompt 倾向。 */
export type ToneStyle = 'tight_hype' | 'calm_narrative' | 'casual_daily' | 'professional_cool'

/**
 * Compose 页用户配置 —— 与 brief/video_goal 一起驱动结构改编。
 * 折叠"高级设置"暴露，全部带默认值。
 */
export interface ComposeSettings {
  /** 目标总时长（秒），驱动每段 duration_seconds 分配。 */
  target_duration_seconds: number
  /** 目标平台。决定画幅 + 节奏 + 字幕风格。 */
  target_platform: TargetPlatform
  /** 整体调性。影响 LLM 段落结构与口播倾向。 */
  tone: ToneStyle
  /** 核心 CTA 文案（≤20 字）。closing 段自动套用。 */
  cta: string
  /** 必须出现的关键词（最多 5 个）。每段 narration 至少出现 1 个。 */
  keywords: string[]
  /**
   * 是否需要口播 —— 关掉则跳过 TTS + 不烧字幕（纯 BGM 视频），
   * 但仍保留每段 narration 文本供 LLM 改编上下文。
   */
  voiceover_enabled: boolean
  /** TTS 音色。voiceover_enabled=False 时此字段忽略。 */
  tts_voice: TTSVoice
}

export const DEFAULT_COMPOSE_SETTINGS: ComposeSettings = {
  target_duration_seconds: 30,
  target_platform: 'douyin',
  tone: 'tight_hype',
  cta: '',
  keywords: [],
  voiceover_enabled: true,
  tts_voice: 'zh_female_qingxin',
}

export interface Plan {
  plan_id: PlanId
  sample_id: SampleId
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
}

export interface PlanBuildRequest {
  sample_id: SampleId
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
  variant: Variant
}

export interface GapFillAllRequest {
  plan_id: PlanId
  prompt_template?: string | null
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
  /** 兼容老前端：留作 project_id 别名；为空走 mock 素材。 */
  session_id?: SessionId | null
  /** false 时缺素材不回退 mock，所有 gap 都标 miss，逼用户走 copy/aigc 补全。 */
  allow_mock?: boolean
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
  voiceover_enabled?: boolean | null
  tts_voice?: TTSVoice | null
  target_platform?: TargetPlatform | null
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
 * - library:   { sample_id }
 * - decompose: { sample_id }
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
  sample_id: SampleId
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
  sample_id: SampleId
}

export interface ProjectUpdateRequest {
  name?: string | null
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
