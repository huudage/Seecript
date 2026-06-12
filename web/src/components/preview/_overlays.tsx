/**
 * stage-83 (2026-06-12)：从 PlanComposition 抽出的「绝对秒定位叠加组件」公共子集。
 *
 * 为什么独立：
 * - BurnedMainlinePlayer（step3 用，mp4 底图 + Remotion 叠加）只需要叠加部分，
 *   不需要 PlanComposition 里 600+ 行的 SceneClip / AnimatedImageScene / TextCardScene
 * - PlanComposition 当前是死代码（0 import），但保留以备复活；它也从这里 re-import 同名符号
 *
 * 这些组件/工具的共同特征：按 plan.packaging_track[i].start / plan.bgm.video_anchor_seconds
 * 等**绝对秒数**定位，不依赖具体场景如何渲染——可与任何底图（pre-rendered mp4 / Remotion 实时
 * SceneClip）组合使用。
 */
import { Audio, Sequence } from 'remotion'

import type { BGMConfig, PackagingItem } from '@/types/schemas'

import { Cover, type CoverStyle } from './packaging/Cover'
import { StickerOverlay } from './packaging/StickerOverlay'
import { Subtitles } from './packaging/Subtitles'
import { TitleBar } from './packaging/TitleBar'

export function secsToFrames(seconds: number, fps: number): number {
  return Math.round(seconds * fps)
}

export function fallbackTitle(
  item: PackagingItem,
  sectionsByOrder: Map<string, { theme: string }>,
): string {
  if (item.text && item.text.trim()) return item.text
  const sid = (item.style as { section_id?: string } | undefined)?.section_id
  if (sid) {
    const sec = sectionsByOrder.get(sid)
    if (sec?.theme) return sec.theme
  }
  return item.kind === 'cover' ? '封面' : ''
}

export const PackagingLayer: React.FC<{ item: PackagingItem; sectionTitle: string }> = ({
  item,
  sectionTitle,
}) => {
  const text = item.text?.trim() || sectionTitle
  const style = (item.style ?? {}) as Record<string, unknown>
  switch (item.kind) {
    case 'cover':
      return <Cover title={text} style={style as CoverStyle} />
    case 'subtitle':
      return <Subtitles text={text} style={style} />
    case 'title_bar':
      return <TitleBar text={text} style={style} />
    case 'sticker':
      return <StickerOverlay text={text} style={style} />
    default:
      return null
  }
}

export function clamp01(n: number): number {
  if (Number.isNaN(n)) return 0.6
  return Math.max(0, Math.min(1, n))
}

export const BgmAudio: React.FC<{ bgm: BGMConfig; fps: number }> = ({ bgm, fps }) => {
  if (!bgm.track_url) return null
  const anchorFrames = Math.round(bgm.video_anchor_seconds * fps)
  const volume = clamp01(bgm.volume ?? 0.6)
  if (anchorFrames >= 0) {
    return (
      <Sequence from={anchorFrames} layout="none">
        <Audio src={bgm.track_url} volume={volume} />
      </Sequence>
    )
  }
  return (
    <Sequence from={0} layout="none">
      <Audio src={bgm.track_url} startFrom={-anchorFrames} volume={volume} />
    </Sequence>
  )
}
