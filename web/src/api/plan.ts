/**
 * Plan settings + Scene 编辑 API 包装。
 *
 * 后端真源：
 * - PATCH /plan/{plan_id}/settings    —— 翻转 ComposeSettings 局部字段
 * - PATCH /plan/{plan_id}/scene/{scene_id} —— 编辑 Scene 文本 + 联动 AdaptedSection
 *
 * 两个都不重跑 LLM：仅落盘 + 返回最新 Plan。
 */
import { api } from '@/api/client'
import type { Plan, PlanId, PlanSettingsPatch, SceneEditPatch } from '@/types/schemas'

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
