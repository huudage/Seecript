/**
 * Plan settings + Scene 编辑 API 包装。
 *
 * 后端真源：
 * - PATCH /plan/{plan_id}/settings    —— 翻转 ComposeSettings 局部字段
 * - PATCH /plan/{plan_id}/scene/{scene_id} —— 编辑 Scene 文本 + 联动 AdaptedSection
 * - PATCH /plan/{plan_id}/scene/{scene_id}/transition —— 改某分镜入场转场样式
 *
 * 三个都不重跑 LLM：仅落盘 + 返回最新 Plan。
 */
import { api } from '@/api/client'
import type {
  Plan,
  PlanId,
  PlanSettingsPatch,
  SceneEditPatch,
  SceneTransitionPatch,
} from '@/types/schemas'

export async function patchPlanSettings(planId: PlanId, patch: PlanSettingsPatch): Promise<Plan> {
  return await api.patch<Plan>(`/plan/${planId}/settings`, patch)
}

export async function patchPlanScene(
  planId: PlanId,
  sceneId: string,
  patch: SceneEditPatch,
): Promise<Plan> {
  return await api.patch<Plan>(`/plan/${planId}/scene/${sceneId}`, patch)
}

export async function patchSceneTransition(
  planId: PlanId,
  sceneId: string,
  patch: SceneTransitionPatch,
): Promise<Plan> {
  return await api.patch<Plan>(`/plan/${planId}/scene/${sceneId}/transition`, patch)
}

/**
 * stage-26：编辑某分镜的画面主体（subject）。
 * 双写 Scene.shot_subject + 父 AdaptedSection.shots[shot_order].subject，
 * 让下游 AIGC prompt（aigc_prompt_agent）原样消费。
 */
export async function patchShotSubject(
  planId: PlanId,
  sceneId: string,
  subject: string,
): Promise<Plan> {
  return await api.patch<Plan>(`/plan/${planId}/scene/${sceneId}/shot-subject`, { subject })
}

/**
 * stage-37：弹窗里一次性提交单镜的多字段（subject / visual / narration）。
 * 后端会双写 Scene + 父 ShotPlan；不动 duration（改时长需要重排时间线）。
 */
export interface ShotFieldsPatch {
  subject?: string
  visual?: string
  narration?: string
  /** stage-43：运镜手法（≤30 字）。改完同时影响 Seedance 提示词 & Remotion 动效推荐。 */
  camera_technique?: string
}

export async function patchShotFields(
  planId: PlanId,
  sceneId: string,
  patch: ShotFieldsPatch,
): Promise<Plan> {
  return await api.patch<Plan>(`/plan/${planId}/scene/${sceneId}/shot-fields`, patch)
}

/**
 * stage-26 PR-N.4 / N.5：单镜换源。把某 Scene 的 source 切到
 * user_material / aigc_image / aigc_t2v / text_card；后端会同步调
 * Seedream / Seedance / 切素材入出点 / 装 TextCardSpec，成功后清掉 needs_fill。
 *
 * - aigc_t2v 路径同步轮询直到 Seedance 完成（最长 ~180s，超时返 504）
 * - aigc_image 同步出图（~6-15s）
 * - text_card / user_material 立即返回
 */
export interface SceneSwapSourceRequest {
  source: 'user_material' | 'aigc_image' | 'aigc_t2v' | 'text_card'
  material_id?: string
  material_shot_index?: number
  /** stage-29 手动裁剪起点（秒）；与 material_shot_index 互斥，需与 material_out_point 同时给。 */
  material_in_point?: number
  /** stage-29 手动裁剪终点（秒）；out>in，需 ≥ in+0.5s。 */
  material_out_point?: number
  prompt_hint?: string
  main_text?: string
  sub_text?: string
}

export async function swapSceneSource(
  planId: PlanId,
  sceneId: string,
  body: SceneSwapSourceRequest,
): Promise<Plan> {
  return await api.post<Plan>(`/plan/${planId}/scene/${sceneId}/swap-source`, body)
}

/**
 * v2 画布（F4/US-3.2）：段落块拖拽重排。sectionIds 必须是当前全部段落 id 的重排列，
 * 后端按 parent_section_id 分组重铺主轨（清字幕 / 裁超界包装），不跑 LLM。
 */
export async function reorderSections(planId: PlanId, sectionIds: string[]): Promise<Plan> {
  return await api.post<Plan>(`/plan/${planId}/sections/reorder`, { section_ids: sectionIds })
}

/** v2 D4：把 AI 初稿标成定稿。幂等，不改主轨。 */
export async function confirmStructure(planId: PlanId): Promise<Plan> {
  return await api.post<Plan>(`/plan/${planId}/confirm-structure`, {})
}

export interface NarrationProposal {
  scene_id: string
  old_narration?: string | null
  new_narration: string
}

export interface RegenerateNarrationsResponse {
  plan: Plan
  updated_scene_ids: string[]
  skipped_scene_ids: string[]
  note: string
  applied: boolean
  proposals: NarrationProposal[]
}

/**
 * v2 D4 配口播。apply=false 只出建议（diff 确认门）；apply=true 才写回 narration。
 * sectionIds 缺省时重写全片，盘内动作必须带上当前段落。
 */
export interface SceneAiInsight {
  plan_id: string
  scene_id: string
  summary: string
  tags: string[]
  highlights: string[]
  source: 'llm' | 'rule'
}

export async function fetchSceneInsight(planId: PlanId, sceneId: string): Promise<SceneAiInsight> {
  return await api.post<SceneAiInsight>(`/plan/${planId}/scene/${sceneId}/ai-insight`, {})
}

export interface SceneTrimSuggestion {
  plan_id: string
  scene_id: string
  source_duration: number
  current_in: number
  current_out: number
  suggested_in: number
  suggested_out: number
  reason: string
  source: 'llm' | 'rule'
}

export async function fetchTrimSuggestion(planId: PlanId, sceneId: string): Promise<SceneTrimSuggestion> {
  return await api.post<SceneTrimSuggestion>(`/plan/${planId}/scene/${sceneId}/suggest-trim`, {})
}

export interface SceneShotBrief {
  plan_id: string
  scene_id: string
  what_to_shoot: string
  duration_seconds: number
  emotion: string
  reference: string
  tips: string[]
  source: 'llm' | 'rule'
}

export async function fetchShotBrief(planId: PlanId, sceneId: string): Promise<SceneShotBrief> {
  return await api.post<SceneShotBrief>(`/plan/${planId}/scene/${sceneId}/shot-brief`, {})
}

/** 画布空白处追加一个待拖入素材的视频块。不跑结构 LLM。 */
export async function appendBlankVideoBlock(planId: PlanId): Promise<Plan> {
  return await api.post<Plan>(`/plan/${planId}/sections/append`, { source: 'user_material' })
}

export async function regenerateNarrations(
  planId: PlanId,
  body: { section_ids?: string[]; apply: boolean; proposals?: NarrationProposal[] },
): Promise<RegenerateNarrationsResponse> {
  return await api.post<RegenerateNarrationsResponse>(`/plan/${planId}/regenerate-narrations`, body)
}

/**
 * v2 画布（F4/US-3.5）：实拍块切分。splitAt 为切点距本镜起点的秒数；
 * 后端镜一分为二 + 段拆两段。含字幕/标题条/口播音轨的块会被 422 拒（先摘除内层）。
 */
export async function splitScene(planId: PlanId, sceneId: string, splitAt: number): Promise<Plan> {
  return await api.post<Plan>(`/plan/${planId}/scene/${sceneId}/split`, { split_at: splitAt })
}

/**
 * stage-77 (2026-06-12)：换源弹窗显示「切片适配度」。
 *
 * 给当前 scene × 指定 material 的每个 MaterialShot 打分（0-1），后端用
 * shot_matcher._score_pair——跟 build_plan 自动匹配同一份评分函数，避免
 * UI 跟物化层各走一套尺。前端在 video 素材展开的 shot 网格上显示分数 + 颜色徽章。
 */
export interface ShotFitScoreItem {
  shot_index: number
  score: number
  score_pct: number
  quality: 'good' | 'weak' | 'missing'
}

export interface ShotFitScoresResponse {
  plan_id: string
  scene_id: string
  material_id: string
  section_role: string
  scene_shot_subject: string
  scene_duration: number
  scores: ShotFitScoreItem[]
}

export async function getMaterialShotFitScores(
  planId: PlanId,
  sceneId: string,
  materialId: string,
): Promise<ShotFitScoresResponse> {
  return await api.get<ShotFitScoresResponse>(
    `/plan/${planId}/scene/${sceneId}/material/${encodeURIComponent(materialId)}/shot-scores`,
  )
}

/**
 * stage-80 (2026-06-12)：主轨预览 mp4。
 *
 * 把 plan.main_track 在后端实时合成 480p mp4，前端单 <video> 播替换 Remotion <Video>
 * —— 根治「单镜头内复读前 0.X 秒」（HTMLVideoElement 的 currentTime seek 不是 frame-accurate）。
 *
 * 后端按 main_track 关键字段 hash 缓存：plan 没改 → 返同一 url，毫秒命中；plan 改了 →
 * 重跑 ffmpeg（一般 3-15s）。前端用此接口的 url 喂 MainlinePreviewPlayer。
 */
export interface PreviewMainlineResponse {
  plan_id: string
  signature: string
  url: string
  duration_seconds: number
}

export async function buildMainlinePreview(planId: PlanId): Promise<PreviewMainlineResponse> {
  return await api.post<PreviewMainlineResponse>(`/plan/${planId}/preview-mainline`, {})
}
