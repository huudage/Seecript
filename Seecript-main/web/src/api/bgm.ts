/**
 * BGM API 包装 —— Compose 页"BGM 轨"动作入口。
 *
 * 后端真源：
 * - server/app/routers/asset.py 处理上传（POST /api/asset/upload，kind=bgm），
 *   返回 Asset。其 metadata.peak_at_seconds / duration_seconds 由 librosa 异步分析回填。
 * - server/app/routers/plan.py 的 PATCH/DELETE /plan/{plan_id}/bgm 处理 plan 内绑定。
 *
 * 设计取舍：plan 引用 bgm_asset_id（资产层）而不是直接挂 file_url，是为了多 plan 复用同一首歌
 * 仅分析/上传一次。这里的 wrapper 也按这个边界拆分。
 */

import { api } from '@/api/client'
import type { Asset, AssetListResponse, Plan, PlanBgmPatch, PlanId } from '@/types/schemas'

/**
 * 上传 BGM 资产文件 —— multipart/form-data。
 *
 * 后端会校验 MIME (audio/mpeg|wav|aac|m4a|ogg) 与 ≤20MB；
 * 返回的 Asset.status 通常是 'processing'，之后 librosa 异步分析完成后变 'ready'
 * 并把 metadata.peak_at_seconds / duration_seconds 写入。
 */
export async function uploadBgm(projectId: string, file: File, title?: string): Promise<Asset> {
  const form = new FormData()
  form.append('file', file)
  form.append('kind', 'bgm')
  form.append('project_id', projectId)
  if (title) form.append('title', title)
  return await api.post<Asset>('/asset/upload', form)
}

/** 拉一份资产详情（轮询 status: processing → ready 用）。 */
export async function getAsset(assetId: string): Promise<Asset> {
  return await api.get<Asset>(`/asset/${assetId}`)
}

/**
 * 列出当前项目可用的 BGM 资产。
 * 调用 /asset/library，仅返回 kind=bgm 项以匹配 Compose 页 BGM 轨需求。
 */
export async function listBgmAssets(projectId: string): Promise<Asset[]> {
  const res = await api.get<AssetListResponse>(
    `/asset/library?project_id=${encodeURIComponent(projectId)}&kind=bgm`,
  )
  return res.items
}

/**
 * 修改 plan 的 BGM 绑定：换曲 / 拖动 anchor / 调音量 / ducking 开关。
 *
 * bgm_asset_id 给空字符串或 null 等同于清空 BGM；不传该 key 则保留现有绑定。
 * 服务端会重新计算 anchor 并把变更落盘后返回最新 Plan。
 */
export async function patchPlanBgm(planId: PlanId, patch: PlanBgmPatch): Promise<Plan> {
  return await api.patch<Plan>(`/plan/${planId}/bgm`, patch)
}

/** 清空 plan 的 BGM 引用（保留资产库里的文件本身）。 */
export async function deletePlanBgm(planId: PlanId): Promise<Plan> {
  return await api.delete<Plan>(`/plan/${planId}/bgm`)
}
