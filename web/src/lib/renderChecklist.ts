import type { Plan, Scene } from '@/types/schemas'

/**
 * 渲染确认清单（PRD-v2 F7 前置 / F10 ③ / US-7.1）。
 *
 * 结构迁移只作参考：空槽可以留着，不因为样例有这段就挡住出片。
 * 清单只拦住未定稿。前端不替用户自动 copy 补缺。
 * isUnfilledScene 仍给功能盘用，用来决定空槽才出现补全动作。
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
  return items
}
