import type { Plan, Scene } from '@/types/schemas'

/**
 * 渲染确认清单（PRD-v2 F7 前置 / F10 ③ / US-7.1）。
 *
 * 口径与 Compose 的 scene 级「待补」一致：用户已经换源审过的镜（user_edited）
 * 不再算缺口。清单非空时前端不提交渲染，也不替用户自动 copy 补缺。
 */

export type ChecklistKind = 'draft' | 'empty'

export interface ChecklistItem {
  id: string
  kind: ChecklistKind
  label: string
}

export function isUnfilledScene(scene: Scene): boolean {
  if (scene.user_edited === true) return false
  return (
    scene.needs_fill === true || (scene.source_ref ?? '').startsWith('text-card-fill-empty')
  )
}

export function buildRenderChecklist(plan: Plan): ChecklistItem[] {
  const items: ChecklistItem[] = []
  if (plan.structure_confirmed === false) {
    items.push({
      id: 'draft',
      kind: 'draft',
      label: '结构仍是 AI 初稿。拖改满意后点「定稿」，未定稿不进渲染。',
    })
  }
  for (const scene of plan.main_track) {
    if (!isUnfilledScene(scene)) continue
    const where = scene.shot_subject || scene.scene_id
    items.push({
      id: `empty-${scene.scene_id}`,
      kind: 'empty',
      label: `空槽 · ${where}。拖入素材，或在缺口里选字卡 / 图 / 补拍清单。系统不会自动补。`,
    })
  }
  return items
}
