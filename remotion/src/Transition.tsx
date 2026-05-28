import { AbsoluteFill, useCurrentFrame, useVideoConfig, interpolate } from 'remotion'

export type TransitionStyle = 'hard_cut' | 'dissolve' | 'slide' | 'zoom' | 'whip' | 'wipe'

/**
 * 6 种转场风格的实现：
 * - hard_cut    完全透明（剪辑约定上 transition 序列不可见，靠 Sequence 边界自然过渡）
 * - dissolve    白色淡入淡出（柔和过渡）
 * - whip        强白闪 + 模糊（爆点切换）
 * - zoom        中心放射黑边（情绪收尾）
 * - slide       从左侧滑入黑条
 * - wipe        从下到上扫一道白条
 */
export const Transition: React.FC<{ style?: TransitionStyle }> = ({ style = 'dissolve' }) => {
  const frame = useCurrentFrame()
  const { durationInFrames } = useVideoConfig()
  const mid = durationInFrames / 2
  // 0 → 1 → 0 三角函数式强度
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

// 保留旧导出以兼容现有 PackagingTrack 导入路径；新代码请用 <Transition style=... />。
export const TransitionFlash: React.FC = () => <Transition style="whip" />
