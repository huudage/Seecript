import { AbsoluteFill, useCurrentFrame, useVideoConfig, spring } from 'remotion'

type Props = { text: string; style: Record<string, unknown> }

export const StickerOverlay: React.FC<Props> = ({ text, style }) => {
  const frame = useCurrentFrame()
  const { fps } = useVideoConfig()
  const pop = spring({ frame, fps, config: { stiffness: 300, damping: 12 } })
  const size = Number(style.size ?? 80)
  const color = String(style.color ?? '#FFE600')
  return (
    <AbsoluteFill style={{ justifyContent: 'center', alignItems: 'center' }}>
      <div
        style={{
          backgroundColor: color,
          color: '#111',
          fontWeight: 900,
          fontSize: size,
          padding: '20px 56px',
          borderRadius: 999,
          transform: `scale(${pop})`,
          fontFamily: '"Noto Sans CJK SC", "PingFang SC", sans-serif',
          boxShadow: '0 12px 32px rgba(0,0,0,0.35)',
        }}
      >
        {text}
      </div>
    </AbsoluteFill>
  )
}
