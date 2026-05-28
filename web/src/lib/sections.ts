/**
 * 段落角色/视频类型显示信息——跟 schemas.SectionRole / VideoType 对齐。
 *
 * v2：从 9 元 SectionKind（按 video_type 三选一）改为 4 元 SectionRole（任意视频通用）。
 * 颜色按情绪温度排：
 *   opening      蓝   冷启动、注意力锚
 *   development  灰   中性铺陈、信息密度
 *   climax       红   情绪峰、爆点
 *   closing      绿   收束、行动引导
 */
import type { SectionRole, VideoType } from '@/types/schemas'

export const VIDEO_TYPE_LABEL: Record<VideoType, string> = {
  marketing: '营销 / 带货',
  editing: '剪辑 / Vlog',
  motion_graph: 'Motion Graph',
}

/**
 * video_type 现在仅作风格提示（驱动 BGM/字幕/转场/封面），不再决定段落结构。
 * Hint 文字反映典型成片观感，结构骨架统一走 opening → development → climax → closing。
 */
export const VIDEO_TYPE_HINT: Record<VideoType, string> = {
  marketing: '风格提示：硬切 + 字幕条，痛点钩子 / 卖点演示 / 行动引导',
  editing: '风格提示：长镜叠化 + 氛围 BGM，铺垫 / 情绪高潮 / 余韵收尾',
  motion_graph: '风格提示：动画转场 + 信息可视化，标题 / 铺陈 / 爆点 / 落版',
}

export const SECTION_LABEL: Record<SectionRole, string> = {
  opening: '开场',
  development: '发展',
  climax: '高潮',
  closing: '收尾',
}

export const SECTION_SHORT: Record<SectionRole, string> = {
  opening: 'Open',
  development: 'Dev',
  climax: 'Climax',
  closing: 'Close',
}

// Tailwind 背景类——用于 SectionsBar 横向色块
export const SECTION_BG: Record<SectionRole, string> = {
  opening: 'bg-blue-500/80',
  development: 'bg-slate-500/80',
  climax: 'bg-rose-500/80',
  closing: 'bg-emerald-500/80',
}

// CSS 十六进制色——给 ReactFlow / 行内 style 用
export const SECTION_HEX: Record<SectionRole, string> = {
  opening: '#3B82F6',
  development: '#64748B',
  climax: '#EF4444',
  closing: '#10B981',
}
