import { AbsoluteFill, interpolate, useCurrentFrame, useVideoConfig } from 'remotion'

export interface CoverStyle {
  subtitle?: string | null
  palette?: string[]
  layout?: 'center' | 'left' | 'split' | 'stacked'
  style_note?: string
}

/**
 * 开场封面卡片 (PackagingItem kind="cover")。
 * - palette[0] 当背景，palette[1] 当文字主色，palette[2]（可选）当强调色
 * - layout 控制对齐：center / left / split（左字右色块）/ stacked（大标题压在小副标题上）
 * - 0.5s 淡入 + 末段淡出，避免突兀
 */
export const Cover: React.FC<{ title: string; style?: CoverStyle }> = ({ title, style }) => {
  const frame = useCurrentFrame()
  const { fps, durationInFrames } = useVideoConfig()
  const fadeIn = Math.min(fps * 0.4, durationInFrames / 3)
  const fadeOut = durationInFrames - Math.min(fps * 0.3, durationInFrames / 3)
  const opacity = interpolate(
    frame,
    [0, fadeIn, fadeOut, durationInFrames],
    [0, 1, 1, 0],
    { extrapolateLeft: 'clamp', extrapolateRight: 'clamp' },
  )

  const palette = style?.palette ?? []
  const bg = palette[1] ?? '#1F2937'
  const accent = palette[0] ?? '#FFE600'
  const sub = palette[2] ?? '#FFFFFF'
  const layout = style?.layout ?? 'center'
  const subtitle = style?.subtitle ?? ''

  const titleColor = layout === 'split' ? sub : accent
  const align: 'center' | 'flex-start' = layout === 'left' || layout === 'stacked' ? 'flex-start' : 'center'

  return (
    <AbsoluteFill
      style={{
        opacity,
        backgroundColor: bg,
        padding: '0 80px',
        flexDirection: 'column',
        justifyContent: 'center',
        alignItems: align,
        textAlign: layout === 'left' || layout === 'stacked' ? 'left' : 'center',
      }}
    >
      {layout === 'split' && (
        <div
          style={{
            position: 'absolute',
            right: 0,
            top: 0,
            bottom: 0,
            width: '40%',
            backgroundColor: accent,
          }}
        />
      )}
      <div
        style={{
          color: titleColor,
          fontSize: 132,
          fontWeight: 900,
          lineHeight: 1.1,
          letterSpacing: 2,
          textShadow: '0 4px 16px rgba(0,0,0,0.45)',
          zIndex: 2,
        }}
      >
        {title}
      </div>
      {subtitle && (
        <div
          style={{
            color: sub,
            fontSize: 56,
            fontWeight: 600,
            marginTop: 28,
            opacity: 0.85,
            zIndex: 2,
          }}
        >
          {subtitle}
        </div>
      )}
    </AbsoluteFill>
  )
}
