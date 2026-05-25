import { AbsoluteFill, useCurrentFrame, useVideoConfig, spring } from 'remotion'

type Props = { text: string; style: Record<string, unknown> }

export const TitleBar: React.FC<Props> = ({ text, style }) => {
  const frame = useCurrentFrame()
  const { fps } = useVideoConfig()
  const slide = spring({ frame, fps, config: { damping: 200 } })
  const size = Number(style.size ?? 96)
  const color = String(style.color ?? '#FFFFFF')
  return (
    <AbsoluteFill style={{ justifyContent: 'flex-start', paddingTop: 160 }}>
      <div
        style={{
          backgroundColor: '#000000CC',
          color,
          fontWeight: 900,
          fontSize: size,
          padding: '24px 48px',
          margin: '0 80px',
          transform: `translateY(${(1 - slide) * -80}px)`,
          opacity: slide,
          fontFamily: '"Noto Sans CJK SC", "PingFang SC", sans-serif',
          letterSpacing: 4,
        }}
      >
        {text}
      </div>
    </AbsoluteFill>
  )
}
