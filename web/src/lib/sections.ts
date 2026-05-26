/**
 * 段落/视频类型显示信息——跟 schemas.SectionKind / VideoType 三选一对齐。
 *
 * 9 个 kind 分三组：
 * - marketing      hook / body / cta            粉 → 蓝 → 黄
 * - editing        opening / climax / closing   青 → 紫 → 靛
 * - motion_graph   intro / build / drop / outro 绿 → 橙 → 红 → 灰
 *
 * 同组的开/收尾用同冷暖系，主体段用对比色。颜色不要交叉，否则迁移图连线难看。
 */
import type { SectionKind, VideoType } from '@/types/schemas'

export const VIDEO_TYPE_LABEL: Record<VideoType, string> = {
  marketing: '营销 / 带货',
  editing: '剪辑 / Vlog',
  motion_graph: 'Motion Graph',
}

export const VIDEO_TYPE_HINT: Record<VideoType, string> = {
  marketing: 'hook → body → cta · 痛点钩子 / 产品演示 / 行动引导',
  editing: 'opening → climax → closing · 氛围铺垫 / 情绪高潮 / 余韵收尾',
  motion_graph: 'intro → build → drop → outro · 标题 / 铺陈 / 爆点 / 落版',
}

export const SECTION_LABEL: Record<SectionKind, string> = {
  hook: 'Hook 开场',
  body: 'Body 主体',
  cta: 'CTA 收尾',
  opening: 'Opening 铺垫',
  climax: 'Climax 高潮',
  closing: 'Closing 收尾',
  intro: 'Intro 入场',
  build: 'Build 铺陈',
  drop: 'Drop 爆点',
  outro: 'Outro 落版',
}

export const SECTION_SHORT: Record<SectionKind, string> = {
  hook: 'Hook',
  body: 'Body',
  cta: 'CTA',
  opening: 'Open',
  climax: 'Climax',
  closing: 'Close',
  intro: 'Intro',
  build: 'Build',
  drop: 'Drop',
  outro: 'Outro',
}

// Tailwind 背景类——用于 SectionsBar 横向色块
export const SECTION_BG: Record<SectionKind, string> = {
  hook: 'bg-pink-500/80',
  body: 'bg-sky-500/80',
  cta: 'bg-amber-500/80',
  opening: 'bg-cyan-500/80',
  climax: 'bg-violet-500/80',
  closing: 'bg-indigo-500/80',
  intro: 'bg-emerald-500/80',
  build: 'bg-orange-500/80',
  drop: 'bg-rose-500/80',
  outro: 'bg-slate-500/80',
}

// CSS 十六进制色——给 ReactFlow / 行内 style 用
export const SECTION_HEX: Record<SectionKind, string> = {
  hook: '#ec4899',
  body: '#0ea5e9',
  cta: '#f59e0b',
  opening: '#06b6d4',
  climax: '#8b5cf6',
  closing: '#6366f1',
  intro: '#10b981',
  build: '#f97316',
  drop: '#f43f5e',
  outro: '#64748b',
}
