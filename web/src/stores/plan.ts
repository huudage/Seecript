import { create } from 'zustand'

import type { Gap, Plan, PlanId } from '@/types/schemas'

/**
 * Plan 当前构建状态：选中的 Plan、缺口列表、变体（A/B）。
 * 真正的时间线 / 包装轨数据放 plan.main_track / plan.packaging_track；
 * 编辑动作走 useEditStore（撤销栈在那边）。
 */
interface PlanState {
  plan: Plan | null
  gaps: Gap[]
  variant: 'A' | 'B'

  setPlan: (plan: Plan | null) => void
  setGaps: (gaps: Gap[]) => void
  setVariant: (variant: 'A' | 'B') => void
  reset: () => void
}

export const usePlanStore = create<PlanState>((set) => ({
  plan: null,
  gaps: [],
  variant: 'A',

  setPlan: (plan) => set({ plan }),
  setGaps: (gaps) => set({ gaps }),
  setVariant: (variant) => set({ variant }),
  reset: () => set({ plan: null, gaps: [], variant: 'A' }),
}))

export type PlanIdOrNull = PlanId | null
