import { AbsoluteFill, useCurrentFrame, useVideoConfig, interpolate } from 'remotion'

export const TransitionFlash: React.FC = () => {
  const frame = useCurrentFrame()
  const { durationInFrames } = useVideoConfig()
  const intensity = interpolate(
    frame,
    [0, durationInFrames / 2, durationInFrames],
    [0, 1, 0],
    { extrapolateLeft: 'clamp', extrapolateRight: 'clamp' },
  )
  return <AbsoluteFill style={{ backgroundColor: `rgba(255,255,255,${intensity * 0.85})` }} />
}
