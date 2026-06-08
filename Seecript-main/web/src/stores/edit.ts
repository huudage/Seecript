import { create } from 'zustand'

import type { Plan } from '@/types/schemas'

/**
 * 自然语言编辑：撤销栈 + 双轨标注式编辑。
 *
 * 设计：
 * - 每次 LLM tool-calling 返回新 Plan → push(plan) → 渲染。
 * - undo() 回退一帧；redo() 重放。栈最长 N=50。
 * - marks 是用户在时间轴上选中的区段，作为下次 instruction 的隐式上下文。
 */

interface EditMark {
  start: number
  end: number
  note?: string
}

interface EditState {
  history: Plan[]
  cursor: number
  marks: EditMark[]
  pending: boolean

  push: (plan: Plan) => void
  undo: () => Plan | null
  redo: () => Plan | null
  setMarks: (marks: EditMark[]) => void
  setPending: (pending: boolean) => void
  reset: () => void
}

const MAX_HISTORY = 50

export const useEditStore = create<EditState>((set, get) => ({
  history: [],
  cursor: -1,
  marks: [],
  pending: false,

  push: (plan) => {
    const { history, cursor } = get()
    const truncated = history.slice(0, cursor + 1)
    truncated.push(plan)
    const overflow = Math.max(0, truncated.length - MAX_HISTORY)
    const next = truncated.slice(overflow)
    set({ history: next, cursor: next.length - 1 })
  },

  undo: () => {
    const { history, cursor } = get()
    if (cursor <= 0) return null
    const nextCursor = cursor - 1
    set({ cursor: nextCursor })
    return history[nextCursor]
  },

  redo: () => {
    const { history, cursor } = get()
    if (cursor >= history.length - 1) return null
    const nextCursor = cursor + 1
    set({ cursor: nextCursor })
    return history[nextCursor]
  },

  setMarks: (marks) => set({ marks }),
  setPending: (pending) => set({ pending }),
  reset: () => set({ history: [], cursor: -1, marks: [], pending: false }),
}))
