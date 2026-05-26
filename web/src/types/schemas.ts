/**
 * 与后端 server/app/schemas.py 镜像的 TS 类型。
 * 修改后端 schema 时务必同步这里，否则编辑器报错也就罢了，运行时会沉默断链。
 */

export type SampleId = string
export type PlanId = string
export type JobId = string
export type MaterialId = string
export type SessionId = string
export type GapId = string

/**
 * 视频类型 —— 用户在上传/选样例时挑选，驱动拆解 Agent 段落 prompt 三选一。
 * - marketing      hook → body → cta
 * - editing        opening → climax → closing
 * - motion_graph   intro → build → drop → outro
 */
export type VideoType = 'marketing' | 'editing' | 'motion_graph'

export type SectionKind =
  // marketing
  | 'hook' | 'body' | 'cta'
  // editing
  | 'opening' | 'climax' | 'closing'
  // motion_graph
  | 'intro' | 'build' | 'drop' | 'outro'

export type GapStatus = 'ok' | 'warn' | 'miss'
export type FillAction = 'rerank' | 'copy' | 'aigc'
export type Variant = 'A' | 'B'
export type JobStatus = 'pending' | 'running' | 'succeeded' | 'failed' | 'cancelled'
export type MediaType = 'video' | 'image' | 'audio'

/** 给定 video_type 返回对应的段落 kind 序列，跟后端 kinds_for_video_type 对齐。 */
export const KINDS_BY_VIDEO_TYPE: Record<VideoType, SectionKind[]> = {
  marketing: ['hook', 'body', 'cta'],
  editing: ['opening', 'climax', 'closing'],
  motion_graph: ['intro', 'build', 'drop', 'outro'],
}

export function kindsForVideoType(videoType: VideoType): SectionKind[] {
  return KINDS_BY_VIDEO_TYPE[videoType] ?? KINDS_BY_VIDEO_TYPE.marketing
}

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

export interface Section {
  kind: SectionKind
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
  recommended_section?: SectionKind | null
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
  section: SectionKind
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

export interface Scene {
  scene_id: string
  section: SectionKind
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
