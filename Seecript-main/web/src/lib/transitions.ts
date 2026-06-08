/**
 * 转场样式视觉常量 —— 在 PackagingPanel 推荐列表与 FourTrackBoard 主轨标记之间复用。
 *
 * TRANSITION_LABEL：中文 label，给 tooltip / Badge 用
 * TRANSITION_TONE ：tailwind 调色板，bg + text 一起给，方便直接拼 className
 */
import type { TransitionStyle } from '@/types/schemas'

export const TRANSITION_LABEL: Record<TransitionStyle, string> = {
  hard_cut: '硬切',
  dissolve: '溶解',
  slide: '滑动',
  zoom: '推拉',
  whip: '甩切',
  wipe: '扫切',
}

export const TRANSITION_TONE: Record<TransitionStyle, string> = {
  hard_cut: 'bg-slate-200 text-slate-700',
  dissolve: 'bg-sky-200 text-sky-800',
  slide: 'bg-amber-200 text-amber-800',
  zoom: 'bg-rose-200 text-rose-800',
  whip: 'bg-yellow-200 text-yellow-900',
  wipe: 'bg-emerald-200 text-emerald-800',
}
