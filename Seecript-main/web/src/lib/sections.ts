/**
 * 段落角色/视频类型显示信息——跟 schemas.SectionRole / VideoType / StructuralPattern 对齐。
 *
 * 设计取舍（R3）：
 * - 段色映射已废弃。所有 role 统一使用中性色 bg，区分通过 label/short 文字角标完成。
 *   原因：早期硬把 4 类 role（开场/铺陈/高潮/收尾）映成蓝/灰/红/绿，会逼用户接受
 *   "Vlog 没有高潮 = 异常"的视觉错觉；新方案让画面色按"是否缺失"等关系状态使用，
 *   role 本身只通过 label 文字传达。
 * - hex 字段保留，仅供需要语义图（如对位关系图）回溯查询，新代码不要再消费 bg。
 */
import type { StructuralPattern, VideoType } from '@/types/schemas'

export const VIDEO_TYPE_LABEL: Record<VideoType, string> = {
  marketing: '营销 / 带货',
  editing: '剪辑 / Vlog',
  motion_graph: 'Motion Graph',
}

export const VIDEO_TYPE_HINT: Record<VideoType, string> = {
  marketing: '风格提示：硬切 + 字幕条，痛点钩子 / 卖点演示 / 行动引导',
  editing: '风格提示：长镜叠化 + 氛围 BGM，铺垫 / 情绪高潮 / 余韵收尾',
  motion_graph: '风格提示：动画转场 + 信息可视化，标题 / 铺陈 / 爆点 / 落版',
}

export const STRUCTURAL_PATTERN_LABEL: Record<StructuralPattern, string> = {
  dramatic: '戏剧四段式',
  stepwise: '线性步骤式',
  listicle: '并列盘点式',
  atmospheric: '氛围推进式',
  info_dense: '信息密集快切式',
  vlog: '日常 Vlog 无高潮型',
}

export const STRUCTURAL_PATTERN_HINT: Record<StructuralPattern, string> = {
  dramatic: '起承转合：opening → development → climax → closing',
  stepwise: '教程/操作：intro → step_N → recap',
  listicle: '榜单/N 个理由：hook → item_N → closer',
  atmospheric: '氛围推进有峰值：establish → flow → peak → resolve',
  info_dense: '信息可视化：title_card → info_block → payoff',
  vlog: '无强情绪峰值的日常记录：intro_scene → daily_N → wrap_up',
}

/** SectionMeta —— 单个 role 的展示元数据。 */
export interface SectionMeta {
  label: string
  short: string
  /** 统一中性色 —— 不再按 role 区分，仅占位以兼容老调用方。 */
  bg: string
  /** hex 仅供需语义色的图（如关系对位图）使用，新组件不要消费此值。 */
  hex: string
}

const NEUTRAL_BG = 'bg-slate-500/70'
const NEUTRAL_HEX = '#64748B'

const STATIC_META: Record<string, SectionMeta> = {
  // dramatic
  opening:     { label: '开场',   short: 'Open',  bg: NEUTRAL_BG, hex: NEUTRAL_HEX },
  development: { label: '发展',   short: 'Dev',   bg: NEUTRAL_BG, hex: NEUTRAL_HEX },
  climax:      { label: '高潮',   short: 'Climax', bg: NEUTRAL_BG, hex: NEUTRAL_HEX },
  closing:     { label: '收尾',   short: 'Close', bg: NEUTRAL_BG, hex: NEUTRAL_HEX },
  // stepwise
  intro:       { label: '引入',   short: 'Intro', bg: NEUTRAL_BG, hex: NEUTRAL_HEX },
  recap:       { label: '总结',   short: 'Recap', bg: NEUTRAL_BG, hex: NEUTRAL_HEX },
  // listicle
  hook:        { label: '钩子',   short: 'Hook',  bg: NEUTRAL_BG, hex: NEUTRAL_HEX },
  closer:      { label: '收束',   short: 'End',   bg: NEUTRAL_BG, hex: NEUTRAL_HEX },
  // atmospheric
  establish:   { label: '起势',   short: 'Estab', bg: NEUTRAL_BG, hex: NEUTRAL_HEX },
  flow:        { label: '流转',   short: 'Flow',  bg: NEUTRAL_BG, hex: NEUTRAL_HEX },
  peak:        { label: '顶点',   short: 'Peak',  bg: NEUTRAL_BG, hex: NEUTRAL_HEX },
  resolve:     { label: '余韵',   short: 'Reslv', bg: NEUTRAL_BG, hex: NEUTRAL_HEX },
  // info_dense
  title_card:  { label: '标题',   short: 'Title', bg: NEUTRAL_BG, hex: NEUTRAL_HEX },
  info_block:  { label: '信息',   short: 'Info',  bg: NEUTRAL_BG, hex: NEUTRAL_HEX },
  payoff:      { label: '落版',   short: 'Pay',   bg: NEUTRAL_BG, hex: NEUTRAL_HEX },
  // vlog
  intro_scene: { label: '开场',   short: 'Intro', bg: NEUTRAL_BG, hex: NEUTRAL_HEX },
  wrap_up:     { label: '收尾',   short: 'Wrap',  bg: NEUTRAL_BG, hex: NEUTRAL_HEX },
}

/** 动态主体角色（step_N / item_N / daily_N）。 */
function dynamicMeta(prefix: 'step' | 'item' | 'daily', n: number): SectionMeta {
  const label =
    prefix === 'step' ? `步骤 ${n}` : prefix === 'item' ? `第 ${n} 项` : `日常 ${n}`
  const short = `${prefix === 'step' ? 'S' : prefix === 'item' ? 'I' : 'D'}${n}`
  return { label, short, bg: NEUTRAL_BG, hex: NEUTRAL_HEX }
}

/** 按 role 取展示元数据；step_N / item_N / daily_N 走动态 fallback。 */
export function getSectionMeta(role: string, _pattern?: StructuralPattern): SectionMeta {
  if (!role) return { label: '段落', short: 'Sec', bg: NEUTRAL_BG, hex: NEUTRAL_HEX }
  if (STATIC_META[role]) return STATIC_META[role]
  const stepM = role.match(/^step_(\d+)$/)
  if (stepM) return dynamicMeta('step', Number(stepM[1]))
  const itemM = role.match(/^item_(\d+)$/)
  if (itemM) return dynamicMeta('item', Number(itemM[1]))
  const dailyM = role.match(/^daily_(\d+)$/)
  if (dailyM) return dynamicMeta('daily', Number(dailyM[1]))
  return { label: role, short: role.slice(0, 4), bg: NEUTRAL_BG, hex: NEUTRAL_HEX }
}

// ---- 兼容旧 API（其他文件仍按 role-key 索引），新代码请用 getSectionMeta。 ----

export const SECTION_LABEL: Record<string, string> = Object.fromEntries(
  Object.entries(STATIC_META).map(([k, v]) => [k, v.label]),
) as Record<string, string>

export const SECTION_SHORT: Record<string, string> = Object.fromEntries(
  Object.entries(STATIC_META).map(([k, v]) => [k, v.short]),
) as Record<string, string>

export const SECTION_BG: Record<string, string> = Object.fromEntries(
  Object.entries(STATIC_META).map(([k, v]) => [k, v.bg]),
) as Record<string, string>

export const SECTION_HEX: Record<string, string> = Object.fromEntries(
  Object.entries(STATIC_META).map(([k, v]) => [k, v.hex]),
) as Record<string, string>
