/**
 * Voice (TTS) API 包装 —— Compose 页"口播轨"动作入口。
 *
 * 后端真源：server/app/routers/voice.py（mock / 火山方舟 ARK TTS 双后端）。
 * 合成成功后后端会同步更新 plan.main_track[i].voiceover_url 并落盘，
 * 前端要么用 synthesize() 直接拿 URL 单段更新本地 plan store，
 * 要么用 synthesizeAll() 拉到最新批量结果后 refetch 整个 plan。
 */

import { api } from '@/api/client'
import type {
  Plan,
  PlanId,
  TTSVoice,
  VoiceSynthesizeResponse,
  VoiceSynthesizeAllResponse,
} from '@/types/schemas'

export interface SynthesizeOneArgs {
  plan_id: PlanId
  scene_id: string
  /** 覆盖 scene.narration 的临时文案；不传则用现有 scene.narration。 */
  text?: string | null
  /** 覆盖 plan.settings.tts_voice 的临时音色。 */
  voice?: TTSVoice | null
}

/** 单段口播合成。失败抛 ApiError（502 上游 TTS 故障）。 */
export async function synthesizeOne(args: SynthesizeOneArgs): Promise<VoiceSynthesizeResponse> {
  return await api.post<VoiceSynthesizeResponse>('/voice/synthesize', args)
}

/**
 * 一键全段合成。voiceover_enabled=False 时返回 400。
 * 后端会跳过空 narration 的 scene 并把它们的 id 放进 skipped_scene_ids。
 */
export async function synthesizeAll(planId: PlanId): Promise<VoiceSynthesizeAllResponse> {
  return await api.post<VoiceSynthesizeAllResponse>('/voice/synthesize-all', {
    plan_id: planId,
  })
}

/** 清除某段已合成的口播，返回更新后的 Plan（scene.voiceover_url = null）。 */
export async function deleteVoice(planId: PlanId, sceneId: string): Promise<Plan> {
  return await api.delete<Plan>(`/voice/${planId}/${sceneId}`)
}
