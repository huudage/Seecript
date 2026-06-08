/**
 * /api/catalog —— HyperFrames catalog 元数据查询。
 *
 * 列出 + 单查 HyperFrames 仓库的 block/component 元信息，给 FrameDesignPicker
 * 和 PackagingPanel 的 CatalogPicker 渲染缩略图与名字。所有静态资源
 * (preview_video / preview_poster) 都直链 HeyGen 静态站点，没有跨域和签名问题。
 */
import { api } from './client'
import type { CatalogCategory, CatalogItem, CatalogListResponse } from '../types/schemas'

export interface ListCatalogParams {
  category?: CatalogCategory
  tag?: string
  limit?: number
}

export async function listCatalog(params: ListCatalogParams = {}): Promise<CatalogListResponse> {
  const search = new URLSearchParams()
  if (params.category) search.set('category', params.category)
  if (params.tag) search.set('tag', params.tag)
  if (params.limit != null) search.set('limit', String(params.limit))
  const qs = search.toString()
  const path = qs ? `/api/catalog/blocks?${qs}` : '/api/catalog/blocks'
  return api.get<CatalogListResponse>(path)
}

export async function getCatalogItem(name: string): Promise<CatalogItem> {
  return api.get<CatalogItem>(`/api/catalog/blocks/${encodeURIComponent(name)}`)
}
