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
