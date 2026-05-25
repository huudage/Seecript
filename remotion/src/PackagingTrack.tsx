import { AbsoluteFill, useVideoConfig, Sequence } from 'remotion'
import { z } from 'zod'
import { Subtitles } from './Subtitles'
import { TitleBar } from './TitleBar'
import { StickerOverlay } from './StickerOverlay'
import { TransitionFlash } from './Transition'

// 与 server/app/schemas.py 的 PackagingItem 镜像。
const packagingItemSchema = z.object({
  item_id: z.string(),
  kind: z.enum(['subtitle', 'title_bar', 'sticker', 'transition', 'cover']),
  start: z.number(),
  end: z.number(),
  text: z.string().optional(),
  style: z.record(z.string(), z.any()).optional(),
})

export const packagingTrackSchema = z.object({
  duration_seconds: z.number(),
  packaging_track: z.array(packagingItemSchema),
})

export type PackagingProps = z.infer<typeof packagingTrackSchema>

export const defaultPackagingProps: PackagingProps = {
  duration_seconds: 22,
  packaging_track: [
    {
      item_id: 'pkg-title',
      kind: 'title_bar',
      start: 0,
      end: 3,
      text: '痛点开场',
      style: { size: 96, color: '#FFFFFF' },
    },
    {
      item_id: 'pkg-sub',
      kind: 'subtitle',
      start: 3,
      end: 18,
      text: '动态字幕跟随口播',
      style: { size: 64, stroke: '#000000' },
    },
    {
      item_id: 'pkg-cta',
      kind: 'sticker',
      start: 18,
      end: 22,
      text: '点赞收藏',
      style: { size: 80, color: '#FFE600' },
    },
  ],
}

export const PackagingTrack: React.FC<PackagingProps> = ({ packaging_track }) => {
  const { fps } = useVideoConfig()
  return (
    <AbsoluteFill style={{ backgroundColor: 'transparent' }}>
      {packaging_track.map((item) => {
        const from = Math.round(item.start * fps)
        const duration = Math.max(1, Math.round((item.end - item.start) * fps))
        return (
          <Sequence key={item.item_id} from={from} durationInFrames={duration}>
            {item.kind === 'subtitle' && <Subtitles text={item.text ?? ''} style={item.style ?? {}} />}
            {item.kind === 'title_bar' && <TitleBar text={item.text ?? ''} style={item.style ?? {}} />}
            {item.kind === 'sticker' && <StickerOverlay text={item.text ?? ''} style={item.style ?? {}} />}
            {item.kind === 'transition' && <TransitionFlash />}
          </Sequence>
        )
      })}
    </AbsoluteFill>
  )
}
