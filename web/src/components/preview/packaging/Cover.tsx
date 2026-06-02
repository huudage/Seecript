import { AbsoluteFill, interpolate, useCurrentFrame, useVideoConfig } from 'remotion'

export interface CoverStyle {
  subtitle?: string | null
  palette?: string[]
  layout?: 'center' | 'left' | 'split' | 'stacked'
  style_note?: string
}

/**
 * Compose 实时预览的封面卡片，对齐 remotion/src/Cover.tsx 的视觉，
 * 让预览的视觉与最终 ffmpeg+remotion 渲染一致。
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
