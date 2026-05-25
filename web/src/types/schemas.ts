/**
 * 与后端 schemas.py 镜像的 TS 类型。
 * 阶段 1 仅放骨架，正式契约在 #8 落地后再补全。
 */

export type SampleId = string
export type PlanId = string
export type JobId = string

export interface LibraryItem {
  id: SampleId
  title: string
  scene: string
  duration: number
  shot_count: number
  cover_url: string
}

export interface Shot {
  id: string
  start: number
  end: number
  thumbnail_url?: string
  labels?: string[]
}

export interface RhythmCurve {
  /** 镜头切换密度采样点，秒为 x 轴。 */
  cuts: { t: number; density: number }[]
  /** BGM 能量曲线。 */
  bgm: { t: number; energy: number }[]
}

export type SectionKind = 'hook' | 'body' | 'cta'

export interface Section {
  kind: SectionKind
  start: number
  end: number
  summary: string
}

export interface PackagingProfile {
  subtitle_style?: string
  title_bar_style?: string
  transition_types?: string[]
  cover_style?: string
}

export interface SampleManifest {
  sample_id: SampleId
  duration_seconds: number
  shots: Shot[]
  rhythm: RhythmCurve
  sections: Section[]
  packaging: PackagingProfile
}

export type GapStatus = 'ok' | 'warn' | 'miss'

export interface Gap {
  id: string
  slot_id: string
  status: GapStatus
  /** 影响等级：1 = 锦上添花，5 = 必填。 */
  impact: number
  reason: string
}

export type FillAction = 'rerank' | 'copy' | 'aigc'

export interface Scene {
  scene_id: string
  source: 'sample' | 'material' | 'aigc'
  start: number
  end: number
  caption?: string
}

export interface PackagingItem {
  kind: 'subtitle' | 'title_bar' | 'sticker' | 'transition'
  start: number
  end: number
  params: Record<string, unknown>
}

export interface BGMConfig {
  url?: string
  volume: number
}

export interface Plan {
  plan_id: PlanId
  sample_id: SampleId
  main_track: Scene[]
  packaging_track: PackagingItem[]
  bgm: BGMConfig
  variant: 'A' | 'B'
}

export interface HealthResponse {
  status: 'healthy' | 'degraded'
  version: string
  llm_provider: string
  asr_provider: string
  t2v_provider: string
}
