import { AbsoluteFill, interpolate, useCurrentFrame, useVideoConfig } from 'remotion'
import type { TransitionStyle } from '@/types/schemas'

/**
 * 转场视觉层：在两段 Scene 边界中点附近以 transition_in.duration 为生命周期，
 * 渲染一个浮在主轨之上的颜色/模糊/几何遮罩。
 *
 * 实现保持与 `remotion/src/Transition.tsx` 完全同款，让"预览看到的转场"和后端
 * Remotion 子项目最终烘出的样子一致。注意：后端主轨拼接其实是 ffmpeg xfade
 * 做真像素 crossfade，而非 webm overlay——所以 dissolve / slide / wipe 在最终
 * 输出里是真 crossfade，预览里只是白闪/侧滑遮罩。视觉允许有此差异（plan §不在本次范围）。
 */
export const TransitionVisual: React.FC<{ style: TransitionStyle }> = ({ style }) => {
  const frame = useCurrentFrame()
  const { durationInFrames } = useVideoConfig()
  const mid = durationInFrames / 2
  const tri = interpolate(frame, [0, mid, durationInFrames], [0, 1, 0], {
    extrapolateLeft: 'clamp',
    extrapolateRight: 'clamp',
  })

  if (style === 'hard_cut') return null
  if (style === 'whip') {
    return (
      <AbsoluteFill
        style={{
          backgroundColor: `rgba(255,255,255,${tri * 0.95})`,
          filter: `blur(${tri * 12}px)`,
        }}
      />
    )
  }
  if (style === 'dissolve') {
    return <AbsoluteFill style={{ backgroundColor: `rgba(255,255,255,${tri * 0.7})` }} />
  }
  if (style === 'zoom') {
    return (
      <AbsoluteFill
        style={{
          background: `radial-gradient(circle at center, transparent ${(1 - tri) * 100}%, rgba(0,0,0,${tri * 0.8}) 100%)`,
        }}
      />
    )
  }
  if (style === 'slide') {
    const x = interpolate(frame, [0, durationInFrames], [-100, 100], {
      extrapolateLeft: 'clamp',
      extrapolateRight: 'clamp',
    })
    return (
      <AbsoluteFill
        style={{
          backgroundColor: 'rgba(0,0,0,0.85)',
          transform: `translateX(${x}%)`,
        }}
      />
    )
  }
  if (style === 'wipe') {
    const y = interpolate(frame, [0, durationInFrames], [100, -100], {
      extrapolateLeft: 'clamp',
      extrapolateRight: 'clamp',
    })
    return (
      <AbsoluteFill
        style={{
          backgroundColor: 'rgba(255,255,255,0.92)',
          transform: `translateY(${y}%)`,
        }}
      />
    )
  }
  return <AbsoluteFill style={{ backgroundColor: `rgba(255,255,255,${tri * 0.85})` }} />
}
