import { create } from 'zustand'

import type { FillResult, Gap, Plan, PlanId, Variant } from '@/types/schemas'

/**
 * Plan 当前构建状态：Plan / 缺口列表 / 已确认的补全 / A·B 变体。
 * 编辑动作走 useEditStore（撤销栈在那边）。
 */
interface PlanState {
  plan: Plan | null
  gaps: Gap[]
  fills: FillResult[]
  variant: Variant

  setPlan: (plan: Plan | null) => void
  setGaps: (gaps: Gap[]) => void
  upsertFill: (fill: FillResult) => void
  removeFill: (gapId: string) => void
  setVariant: (variant: Variant) => void
  reset: () => void
}

export const usePlanStore = create<PlanState>((set) => ({
  plan: null,
  gaps: [],
  fills: [],
  variant: 'A',

  setPlan: (plan) => set({ plan }),
  setGaps: (gaps) => set({ gaps }),
  upsertFill: (fill) =>
    set((state) => {
      const idx = state.fills.findIndex((f) => f.gap_id === fill.gap_id)
      if (idx < 0) return { fills: [...state.fills, fill] }
      const next = state.fills.slice()
      next[idx] = fill
      return { fills: next }
    }),
  removeFill: (gapId) =>
    set((state) => ({ fills: state.fills.filter((f) => f.gap_id !== gapId) })),
  setVariant: (variant) => set({ variant }),
  reset: () => set({ plan: null, gaps: [], fills: [], variant: 'A' }),
}))

export type PlanIdOrNull = PlanId | null
