/**
 * AnimatedImage：把 1~N 张 AI 生图渲染成带动效的视频片段。
 *
 * 设计动机（Seecript 内）：
 * - Seedance T2V 一段 5s 视频成本 ¥0.3~0.5，节省成本走 Seedream 文生图（¥0.05/张）+ Remotion 动效。
 * - 单图配 ken-burns / parallax / static；多图配 storyboard（cross-fade 串联）或 keyframe_morph（缓慢交叠形变）。
 * - 输出 mp4（h264 + yuv420p）→ 主轨与其它 scene 同源 concat。
 *
 * Props 与后端 schemas.py `AnimationSpec` 镜像；改字段时务必同步两边。
 */
import { AbsoluteFill, Img, interpolate, useCurrentFrame, useVideoConfig, Sequence, staticFile } from 'remotion'
import { z } from 'zod'

export const animationSpecSchema = z.object({
  engine: z.literal('remotion').default('remotion'),
  // 单图 / 多图模式：image_urls 长度决定走单图动效（ken-burns / parallax / static）还是多图（storyboard / keyframe_morph）
  image_urls: z.array(z.string()).default([]),
  animation_type: z
    .enum(['ken-burns', 'parallax', 'storyboard', 'keyframe_morph', 'static'])
    .default('ken-burns'),
  // 总时长（秒）；单图动效线性映射到这段时长，多图均分
  duration_seconds: z.number().default(4),
  // 视口比例：'9:16'/'16:9'/'1:1'；composition 用相应的 width/height
  // 实际 width/height 通过 CLI --props 传过来时不会复用 schema 默认，而是直接读 video config
  // 镜头方向：单图 ken-burns / parallax 有效。'in' = 推近，'out' = 拉远，'pan-left' / 'pan-right' 平移
  motion_direction: z
    .enum(['in', 'out', 'pan-left', 'pan-right', 'pan-up', 'pan-down'])
    .default('in'),
  // 强度：0~1，控制缩放/位移幅度；0.3 是温和动效，0.7 是夸张
  intensity: z.number().min(0).max(1).default(0.3),
  // 转场类型（仅多图）
  transition: z.enum(['cross-fade', 'cut', 'slide-left']).default('cross-fade'),
  transition_duration: z.number().default(0.4),
})

export type AnimationSpecProps = z.infer<typeof animationSpecSchema>

export const defaultAnimationSpec: AnimationSpecProps = {
  engine: 'remotion',
  image_urls: [],
  animation_type: 'ken-burns',
  duration_seconds: 4,
  motion_direction: 'in',
  intensity: 0.3,
  transition: 'cross-fade',
  transition_duration: 0.4,
}

// ============== 单图动效组件 ==============

function KenBurnsLayer({
  src,
  durationFrames,
  direction,
  intensity,
}: {
  src: string
  durationFrames: number
  direction: AnimationSpecProps['motion_direction']
  intensity: number
}) {
  const frame = useCurrentFrame()
  const t = interpolate(frame, [0, durationFrames], [0, 1], { extrapolateRight: 'clamp' })

  // 缩放幅度：intensity=0.3 → 1.0 → 1.15
  const zoomRange = 0.15 + intensity * 0.5
  // 位移幅度：intensity=0.3 → 0% → 4%
  const panRange = (0.03 + intensity * 0.12) * 100

  let scale = 1
  let translateX = 0
  let translateY = 0
  switch (direction) {
    case 'in':
      scale = 1 + t * zoomRange
      break
    case 'out':
      scale = 1 + zoomRange - t * zoomRange
      break
    case 'pan-left':
      scale = 1 + zoomRange * 0.3
      translateX = -t * panRange
      break
    case 'pan-right':
      scale = 1 + zoomRange * 0.3
      translateX = t * panRange
      break
    case 'pan-up':
      scale = 1 + zoomRange * 0.3
      translateY = -t * panRange
      break
    case 'pan-down':
      scale = 1 + zoomRange * 0.3
      translateY = t * panRange
      break
  }

  return (
    <AbsoluteFill
      style={{
        transform: `translate(${translateX}%, ${translateY}%) scale(${scale})`,
        transformOrigin: 'center center',
      }}
    >
      <Img
        src={src}
        style={{ width: '100%', height: '100%', objectFit: 'cover' }}
      />
    </AbsoluteFill>
  )
}

function ParallaxLayer({
  src,
  durationFrames,
  intensity,
}: {
  src: string
  durationFrames: number
  intensity: number
}) {
  // 简化版 parallax：双层 + 反向位移；上层 90% 透明叠加，模拟视差感
  const frame = useCurrentFrame()
  const t = interpolate(frame, [0, durationFrames], [-1, 1], { extrapolateLeft: 'clamp', extrapolateRight: 'clamp' })
  const backShift = t * intensity * 8
  const foreShift = t * intensity * 16
  return (
    <AbsoluteFill>
      <AbsoluteFill style={{ transform: `translateX(${backShift}%) scale(1.15)` }}>
        <Img src={src} style={{ width: '100%', height: '100%', objectFit: 'cover', filter: 'blur(8px) brightness(0.85)' }} />
      </AbsoluteFill>
      <AbsoluteFill style={{ transform: `translateX(${foreShift}%) scale(1.05)` }}>
        <Img src={src} style={{ width: '100%', height: '100%', objectFit: 'cover' }} />
      </AbsoluteFill>
    </AbsoluteFill>
  )
}

function StaticLayer({ src }: { src: string }) {
  return (
    <AbsoluteFill>
      <Img src={src} style={{ width: '100%', height: '100%', objectFit: 'cover' }} />
    </AbsoluteFill>
  )
}

// ============== 多图组件 ==============

function StoryboardLayer({
  imageUrls,
  totalFrames,
  transitionFrames,
  fade,
}: {
  imageUrls: string[]
  totalFrames: number
  transitionFrames: number
  fade: boolean
}) {
  const perShot = totalFrames / imageUrls.length
  return (
    <AbsoluteFill>
      {imageUrls.map((url, i) => {
        const from = Math.round(i * perShot)
        const duration = Math.round(perShot)
        return (
          <Sequence key={`${url}-${i}`} from={from} durationInFrames={duration + transitionFrames}>
            <FadeShot src={url} duration={duration + transitionFrames} fadeFrames={fade ? transitionFrames : 0} />
          </Sequence>
        )
      })}
    </AbsoluteFill>
  )
}

function FadeShot({
  src,
  duration,
  fadeFrames,
}: {
  src: string
  duration: number
  fadeFrames: number
}) {
  const frame = useCurrentFrame()
  let opacity = 1
  if (fadeFrames > 0) {
    if (frame < fadeFrames) {
      opacity = interpolate(frame, [0, fadeFrames], [0, 1])
    } else if (frame > duration - fadeFrames) {
      opacity = interpolate(frame, [duration - fadeFrames, duration], [1, 0])
    }
  }
  // 配 ken-burns 内嵌轻微缩放，让每张图本身也有动效，避免每段静止突兀
  const zoom = interpolate(frame, [0, duration], [1, 1.08], { extrapolateRight: 'clamp' })
  return (
    <AbsoluteFill style={{ opacity, transform: `scale(${zoom})` }}>
      <Img src={src} style={{ width: '100%', height: '100%', objectFit: 'cover' }} />
    </AbsoluteFill>
  )
}

function KeyframeMorphLayer({
  imageUrls,
  totalFrames,
}: {
  imageUrls: string[]
  totalFrames: number
}) {
  // keyframe morph：所有图等长平铺，cross-fade 占整段时长的 40%，让形变更连续
  const frame = useCurrentFrame()
  const n = imageUrls.length
  return (
    <AbsoluteFill>
      {imageUrls.map((url, i) => {
        const center = ((i + 0.5) * totalFrames) / n
        const halfWindow = totalFrames / n
        const opacity = interpolate(
          frame,
          [center - halfWindow, center - halfWindow * 0.3, center + halfWindow * 0.3, center + halfWindow],
          [0, 1, 1, 0],
          { extrapolateLeft: 'clamp', extrapolateRight: 'clamp' },
        )
        const zoom = interpolate(frame, [0, totalFrames], [1, 1.12], { extrapolateRight: 'clamp' })
        return (
          <AbsoluteFill key={`${url}-${i}`} style={{ opacity, transform: `scale(${zoom})` }}>
            <Img src={url} style={{ width: '100%', height: '100%', objectFit: 'cover' }} />
          </AbsoluteFill>
        )
      })}
    </AbsoluteFill>
  )
}

// ============== 主 Composition ==============

export const AnimatedImage: React.FC<AnimationSpecProps> = (props) => {
  const { fps } = useVideoConfig()
  const totalFrames = Math.max(1, Math.round(props.duration_seconds * fps))
  const transitionFrames = Math.max(1, Math.round(props.transition_duration * fps))

  // 把 staticFile 或绝对路径 / http url 都规范成可用 src
  const resolveSrc = (u: string): string => {
    if (!u) return ''
    if (u.startsWith('http://') || u.startsWith('https://')) return u
    if (u.startsWith('file://')) return u
    if (u.startsWith('/')) return u // 同源 web path
    return staticFile(u)
  }
  const urls = props.image_urls.map(resolveSrc).filter(Boolean)
  if (urls.length === 0) {
    return (
      <AbsoluteFill style={{ background: '#111', color: '#777', display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
        <span style={{ fontFamily: 'sans-serif', fontSize: 32 }}>缺图 · 渲染兜底</span>
      </AbsoluteFill>
    )
  }

  // 单图路径
  if (urls.length === 1) {
    const src = urls[0]
    switch (props.animation_type) {
      case 'parallax':
        return (
          <AbsoluteFill style={{ background: '#000' }}>
            <ParallaxLayer src={src} durationFrames={totalFrames} intensity={props.intensity} />
          </AbsoluteFill>
        )
      case 'static':
        return (
          <AbsoluteFill style={{ background: '#000' }}>
            <StaticLayer src={src} />
          </AbsoluteFill>
        )
      case 'ken-burns':
      default:
        return (
          <AbsoluteFill style={{ background: '#000' }}>
            <KenBurnsLayer
              src={src}
              durationFrames={totalFrames}
              direction={props.motion_direction}
              intensity={props.intensity}
            />
          </AbsoluteFill>
        )
    }
  }

  // 多图路径
  switch (props.animation_type) {
    case 'keyframe_morph':
      return (
        <AbsoluteFill style={{ background: '#000' }}>
          <KeyframeMorphLayer imageUrls={urls} totalFrames={totalFrames} />
        </AbsoluteFill>
      )
    case 'storyboard':
    default:
      return (
        <AbsoluteFill style={{ background: '#000' }}>
          <StoryboardLayer
            imageUrls={urls}
            totalFrames={totalFrames}
            transitionFrames={transitionFrames}
            fade={props.transition === 'cross-fade'}
          />
        </AbsoluteFill>
      )
  }
}
