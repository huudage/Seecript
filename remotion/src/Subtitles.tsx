import { AbsoluteFill, useCurrentFrame, useVideoConfig, interpolate } from 'remotion'

type Props = { text: string; style: Record<string, unknown> }

export const Subtitles: React.FC<Props> = ({ text, style }) => {
  const frame = useCurrentFrame()
  const { fps } = useVideoConfig()
  const fadeIn = interpolate(frame, [0, fps * 0.2], [0, 1], { extrapolateRight: 'clamp' })
  const size = Number(style.size ?? 64)
  const stroke = String(style.stroke ?? '#000000')
  return (
    <AbsoluteFill style={{ justifyContent: 'flex-end', alignItems: 'center', paddingBottom: 240 }}>
      <div
        style={{
          fontFamily: '"Noto Sans CJK SC", "PingFang SC", "Microsoft YaHei", sans-serif',
          fontWeight: 900,
          fontSize: size,
          color: '#ffffff',
          WebkitTextStroke: `${Math.round(size / 16)}px ${stroke}`,
          opacity: fadeIn,
          letterSpacing: 2,
          textAlign: 'center',
        }}
      >
        {text}
      </div>
    </AbsoluteFill>
  )
}
