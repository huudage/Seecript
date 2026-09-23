/** 画布跨组件拖拽的 dataTransfer MIME 约定。 */

/** 素材库卡片 → 结构画布槽位换源（F4/US-3.2，payload 为 material_id）。 */
export const CANVAS_MATERIAL_MIME = 'application/x-seecript-material'

export function hasCanvasMaterialPayload(e: { dataTransfer: DataTransfer | null }): boolean {
  return !!e.dataTransfer && Array.from(e.dataTransfer.types).includes(CANVAS_MATERIAL_MIME)
}
