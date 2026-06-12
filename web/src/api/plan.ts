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
 * stage-28：手动触发情绪曲线重算。
 *
 * BGM 切换会自动重算（PATCH /plan/{id}/bgm 内部已 hook）；本接口用于：
 * - main_track 编辑后用户主动刷新
 * - migration_preference 切到 amp_emotion 后立即看到曲线整体抬高
 *
 * 后端跑 LLM 多信号打分；失败回落规则版（curve.backend === 'rule_fallback'）。
 */
export async function recomputeEmotion(planId: PlanId): Promise<Plan> {
  return await api.post<Plan>(`/plan/${planId}/recompute-emotion`, {})
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
