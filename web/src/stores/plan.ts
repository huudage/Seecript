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
  /** Compose 页右栏点击的 gap_id；驱动 GapPreviewDialog + 补全面板的目标。
   *  注意：每次 silent runAnalyze 后端会重写 gap_id（plan-scoped 唯一性），
   *  这个字段会被对应 useEffect 重置到第一个 miss gap。**真正的选段持久化用
   *  selectedSectionId**——它跨 silent rebuild 稳定。selectedGapId 是 selectedSectionId
   *  在当前 gaps 数组里反查出来的 gap_id 快照，仅供需要真 gap_id 的下游用（如 dialog/board）。 */
  selectedGapId: string | null
  /** stage-36：选段的稳定主键——AdaptedSection.section_id（sec-N 形式），跨 silent rebuild 不变。
   *  selectedGap = gaps.find(g => g.section_id === selectedSectionId)。Fill 工作台
   *  按它 keepalive，FillCopyPanel/FillAigcPanel 的 reset useEffect 也依赖它。 */
  selectedSectionId: string | null

  setPlan: (plan: Plan | null) => void
  setGaps: (gaps: Gap[]) => void
  /** 整体替换 fills——「重新分析」时清空旧 plan 的 fills 用，避免跨 plan 串台。 */
  setFills: (fills: FillResult[]) => void
  upsertFill: (fill: FillResult) => void
  removeFill: (gapId: string) => void
  setVariant: (variant: Variant) => void
  setSelectedGapId: (gapId: string | null) => void
  setSelectedSectionId: (sectionId: string | null) => void
  reset: () => void
}

export const usePlanStore = create<PlanState>((set) => ({
  plan: null,
  gaps: [],
  fills: [],
  variant: 'A',
  selectedGapId: null,
  selectedSectionId: null,

  setPlan: (plan) => set({ plan }),
  setGaps: (gaps) => set({ gaps }),
  setFills: (fills) => set({ fills }),
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
  setSelectedGapId: (gapId) => set({ selectedGapId: gapId }),
  setSelectedSectionId: (sectionId) => set({ selectedSectionId: sectionId }),
  reset: () =>
    set({
      plan: null,
      gaps: [],
      fills: [],
      variant: 'A',
      selectedGapId: null,
      selectedSectionId: null,
    }),
}))

export type PlanIdOrNull = PlanId | null
