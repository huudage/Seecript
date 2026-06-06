import { AbsoluteFill, Audio, Img, interpolate, Sequence, useCurrentFrame, useVideoConfig, Video } from 'remotion'

import { SECTION_HEX, SECTION_LABEL } from '@/lib/sections'
import { resolveSceneMedia } from '@/lib/scene_url'
import type { AnimationSpec, BGMConfig, Material, PackagingItem, Plan, Scene, TextCardSpec } from '@/types/schemas'

import { Cover, type CoverStyle } from './packaging/Cover'
import { StickerOverlay } from './packaging/StickerOverlay'
import { Subtitles } from './packaging/Subtitles'
import { TitleBar } from './packaging/TitleBar'
import { TransitionVisual } from './TransitionVisual'

export interface PlanCompositionProps {
  plan: Plan
  materials: Material[]
}

/**
 * 把 Plan 编译成 Remotion 可播放组合：
 *
 *   主轨：每个 Scene 一段 <Sequence>，绝对定位（from = scene.start × fps）
 *         - user_material / aigc_t2v → <Video startFrom endAt> 切片
 *         - image / text_card        → 全屏 section 色卡 + narration 文字
 *         - 内嵌 <Audio src={voiceover_url}> 实时叠 TTS
 *
 *   转场：i > 0 且 Scene.transition_in 存在 → 在 scene.start 附近覆一层 <TransitionVisual>
 *         （hard_cut 不渲染；其余 5 风格同 remotion/src/Transition.tsx）
 *
 *   包装轨：plan.packaging_track 里每个 item 按 (start, end) 绝对定位
 *           kind=transition 已被 Scene.transition_in 内化（schemas.py L501-L509 注释），过滤掉
 *
 *   BGM：top-level <Audio>，按 video_anchor_seconds 平移：
 *         anchor ≥ 0 → Sequence from=anchorFrames（延后播放）
 *         anchor < 0 → Audio startFrom=|anchor|frames（跳过前奏）
 *
 *   绝对定位的原因：保留 Scene.start / PackagingItem.start 等后端时间字段的权威性，
 *   不引 TransitionSeries 的"clip 重叠 → 总时长压缩"模型，避免与 packaging item
 *   绝对时间对齐踩坑。代价是 dissolve/slide/wipe 转场是"色块覆盖"而非真像素 crossfade。
 */
export const PlanComposition: React.FC<PlanCompositionProps> = ({ plan, materials }) => {
  const FPS = 30
  const sectionsByOrder = new Map(plan.adapted_sections.map((s) => [s.section_id, s]))

  return (
    <AbsoluteFill style={{ backgroundColor: '#000' }}>
      {/* === Main track scenes === */}
      {plan.main_track.map((scene) => {
        const from = secsToFrames(scene.start, FPS)
        const dur = Math.max(1, secsToFrames(scene.duration, FPS))
        return (
          <Sequence
            key={scene.scene_id}
            from={from}
            durationInFrames={dur}
            layout="none"
          >
            <SceneClip scene={scene} materials={materials} fps={FPS} />
          </Sequence>
        )
      })}

      {/* === Transitions overlay (per scene.transition_in) === */}
      {plan.main_track.map((scene, i) => {
        if (i === 0) return null
        const t = scene.transition_in
        if (!t || t.style === 'hard_cut') return null
        const halfDur = t.duration / 2
        const from = Math.max(0, secsToFrames(scene.start - halfDur, FPS))
        const dur = Math.max(1, secsToFrames(t.duration, FPS))
        return (
          <Sequence
            key={`tr-${scene.scene_id}`}
            from={from}
            durationInFrames={dur}
            layout="none"
          >
            <TransitionVisual style={t.style} />
          </Sequence>
        )
      })}

      {/* === Packaging overlay items === */}
      {plan.packaging_track
        .filter((item) => item.kind !== 'transition')
        .map((item) => {
          const from = secsToFrames(item.start, FPS)
          const dur = Math.max(1, secsToFrames(item.end - item.start, FPS))
          return (
            <Sequence
              key={item.item_id}
              from={from}
              durationInFrames={dur}
              layout="none"
            >
              <PackagingLayer item={item} sectionTitle={fallbackTitle(item, sectionsByOrder)} />
            </Sequence>
          )
        })}

      {/* === BGM === */}
      {plan.bgm.track_url && <BgmAudio bgm={plan.bgm} fps={FPS} />}
    </AbsoluteFill>
  )
}

function secsToFrames(seconds: number, fps: number): number {
  return Math.round(seconds * fps)
}

function fallbackTitle(
  item: PackagingItem,
  sectionsByOrder: Map<string, { theme: string }>,
): string {
  if (item.text && item.text.trim()) return item.text
  const sid = (item.style as { section_id?: string } | undefined)?.section_id
  if (sid) {
    const sec = sectionsByOrder.get(sid)
    if (sec?.theme) return sec.theme
  }
  return item.kind === 'cover' ? '封面' : ''
}

const SceneClip: React.FC<{ scene: Scene; materials: Material[]; fps: number }> = ({
  scene,
  materials,
  fps,
}) => {
  const voiceover = scene.voiceover_url ?? null

  // 优先：copy fill 的 TextCardSpec —— 即便 plan 还在 rebuild，scene.text_card_spec 已
  // 被后端 plan/build 落进 main_track；预览里直接复刻 ffmpeg drawtext 输出，让"刚生成
  // 的字卡画面"立刻可见，不要再退化成 SECTION_LABEL 色卡。
  if (scene.text_card_spec) {
    return (
      <AbsoluteFill style={{ backgroundColor: '#000' }}>
        <TextCardScene spec={scene.text_card_spec} />
        {voiceover && <Audio src={voiceover} />}
      </AbsoluteFill>
    )
  }

  const media = resolveSceneMedia(scene, materials)
  if (media.kind === 'video') {
    const mat = materials.find((m) => m.material_id === scene.source_ref)
    const isImage = mat?.media_type === 'image'
    return (
      <AbsoluteFill style={{ backgroundColor: '#000' }}>
        {isImage ? (
          <Img
            src={media.url}
            style={{ width: '100%', height: '100%', objectFit: 'cover' }}
          />
        ) : (
          <Video
            src={media.url}
            startFrom={Math.max(0, secsToFrames(scene.in_point, fps))}
            endAt={scene.out_point != null ? secsToFrames(scene.out_point, fps) : undefined}
            volume={voiceover ? 0 : 1}
            style={{ width: '100%', height: '100%', objectFit: 'cover' }}
          />
        )}
        {voiceover && <Audio src={voiceover} />}
      </AbsoluteFill>
    )
  }
  if (media.kind === 'image') {
    // aigc_image：Seedream 静帧；后端 /aigc-images/<file> 同源，直接给浏览器拉。
    // animation_spec.engine === 'remotion' 时，预览侧也跑动效——与 pipeline 真渲染（remotion CLI）对齐，
    // 避免「浏览器看到定格、最终视频出 ken-burns」的体验割裂。
    const spec = scene.animation_spec ?? null
    if (spec && spec.engine === 'remotion') {
      // 单图：scene.aigc_image_url 是兜底；多图：从 spec.image_urls 取
      const urls = (spec.image_urls && spec.image_urls.length > 0)
        ? spec.image_urls
        : [media.url]
      return (
        <AbsoluteFill style={{ backgroundColor: '#000' }}>
          <AnimatedImageScene spec={spec} urls={urls} />
          {voiceover && <Audio src={voiceover} />}
        </AbsoluteFill>
      )
    }
    return (
      <AbsoluteFill style={{ backgroundColor: '#000' }}>
        <Img
          src={media.url}
          style={{ width: '100%', height: '100%', objectFit: 'cover' }}
        />
        {voiceover && <Audio src={voiceover} />}
      </AbsoluteFill>
    )
  }

  // 资源缺失兜底
  return (
    <AbsoluteFill
      style={{
        backgroundColor: SECTION_HEX[scene.section],
        justifyContent: 'center',
        alignItems: 'center',
        padding: '80px 80px',
      }}
    >
      <div
        style={{
          color: '#0F172A',
          fontSize: 56,
          fontWeight: 700,
          opacity: 0.55,
          marginBottom: 24,
          letterSpacing: 4,
        }}
      >
        {SECTION_LABEL[scene.section]}
      </div>
      <div
        style={{
          color: '#FFFFFF',
          fontSize: 96,
          fontWeight: 900,
          lineHeight: 1.25,
          textAlign: 'center',
          textShadow: '0 4px 20px rgba(0,0,0,0.45)',
          fontFamily: '"Noto Sans CJK SC", "PingFang SC", sans-serif',
        }}
      >
        {media.text}
      </div>
      {voiceover && <Audio src={voiceover} />}
    </AbsoluteFill>
  )
}

/**
 * 把 TextCardSpec 在 1080×1920 画布上铺满复刻：与 FillCopyPanel 的 CardPreview / FourTrackBoard
 * 的 TextCardThumb 视觉对齐，但用大字号填充整屏，匹配后端 ffmpeg drawtext 最终输出。
 *
 * 由于 Remotion 画布是固定 1080×1920，所以这里直接用绝对像素值——比 CSS rem 更稳定。
 */
const TextCardScene: React.FC<{ spec: TextCardSpec }> = ({ spec }) => {
  const bg: React.CSSProperties = (() => {
    switch (spec.bg_mode) {
      case 'gradient':
        return { background: `linear-gradient(135deg, ${spec.bg_color} 0%, ${spec.accent_color} 100%)` }
      case 'dark_overlay':
        return { background: spec.bg_color, boxShadow: 'inset 0 0 0 9999px rgba(0,0,0,0.45)' }
      case 'image_blur':
        return { background: `radial-gradient(circle at 30% 30%, ${spec.accent_color}55, ${spec.bg_color})` }
      default:
        return { background: spec.bg_color }
    }
  })()
  const fontFamily =
    spec.font_family === 'tech_mono'
      ? '"JetBrains Mono", "Roboto Mono", monospace'
      : spec.font_family === 'serif_classic'
      ? '"Noto Serif SC", "Source Han Serif", serif'
      : spec.font_family === 'handwriting'
      ? '"Ma Shan Zheng", "Zhi Mang Xing", cursive'
      : '"Noto Sans CJK SC", "PingFang SC", sans-serif'
  const letterSpacing = spec.font_family === 'tech_mono' ? '0.05em' : 'normal'

  const isSplit = spec.layout === 'split_top_bottom'
  const justify =
    spec.layout === 'top'
      ? 'flex-start'
      : spec.layout === 'bottom'
      ? 'flex-end'
      : isSplit
      ? 'space-between'
      : 'center'
  const padding = spec.layout === 'top' ? '160px 80px 80px' : spec.layout === 'bottom' ? '80px 80px 160px' : '120px 80px'

  return (
    <AbsoluteFill
      style={{
        ...bg,
        flexDirection: 'column',
        justifyContent: justify,
        alignItems: 'center',
        padding,
        textAlign: 'center',
      }}
    >
      <div
        style={{
          color: spec.text_color,
          fontFamily,
          fontWeight: 900,
          fontSize: 120,
          lineHeight: 1.18,
          letterSpacing,
          textShadow: spec.animation === 'bounce_word' ? `0 6px 0 ${spec.accent_color}` : '0 4px 18px rgba(0,0,0,0.35)',
        }}
      >
        {spec.main_text || '主标题'}
      </div>
      {spec.sub_text && (
        <div
          style={{
            color: spec.accent_color,
            fontFamily,
            fontWeight: 500,
            fontSize: 56,
            lineHeight: 1.4,
            marginTop: isSplit ? 0 : 32,
            letterSpacing,
          }}
        >
          {spec.sub_text}
        </div>
      )}
      {spec.emoji_decor.length > 0 && (
        <div
          style={{
            position: 'absolute',
            top: 48,
            right: 64,
            display: 'flex',
            gap: 16,
            fontSize: 80,
            lineHeight: 1,
          }}
        >
          {spec.emoji_decor.slice(0, 3).map((e, i) => (
            <span key={`${i}-${e}`}>{e}</span>
          ))}
        </div>
      )}
    </AbsoluteFill>
  )
}

const PackagingLayer: React.FC<{ item: PackagingItem; sectionTitle: string }> = ({
  item,
  sectionTitle,
}) => {
  const text = item.text?.trim() || sectionTitle
  const style = (item.style ?? {}) as Record<string, unknown>
  switch (item.kind) {
    case 'cover':
      return <Cover title={text} style={style as CoverStyle} />
    case 'subtitle':
      return <Subtitles text={text} style={style} />
    case 'title_bar':
      return <TitleBar text={text} style={style} />
    case 'sticker':
      return <StickerOverlay text={text} style={style} />
    default:
      return null
  }
}

const BgmAudio: React.FC<{ bgm: BGMConfig; fps: number }> = ({ bgm, fps }) => {
  if (!bgm.track_url) return null
  const anchorFrames = Math.round(bgm.video_anchor_seconds * fps)
  const volume = clamp01(bgm.volume ?? 0.6)
  if (anchorFrames >= 0) {
    return (
      <Sequence from={anchorFrames} layout="none">
        <Audio src={bgm.track_url} volume={volume} />
      </Sequence>
    )
  }
  return (
    <Sequence from={0} layout="none">
      <Audio src={bgm.track_url} startFrom={-anchorFrames} volume={volume} />
    </Sequence>
  )
}

function clamp01(n: number): number {
  if (Number.isNaN(n)) return 0.6
  return Math.max(0, Math.min(1, n))
}

/**
 * 预览端 AnimatedImage：与 remotion/src/AnimatedImage.tsx 同算法（ken-burns / parallax / storyboard /
 * keyframe_morph / static），独立内联避免跨 bundle 引用。两边的行为必须保持一致——
 * 真渲染调 remotion CLI 用 remotion/src 的版本；预览端在 @remotion/player 里跑这份。
 */
const AnimatedImageScene: React.FC<{ spec: AnimationSpec; urls: string[] }> = ({ spec, urls }) => {
  const { fps, durationInFrames } = useVideoConfig()
  // 真正的 totalFrames 取自 video config（Sequence 已按 scene.duration 给定）
  const duration = durationInFrames
  const transitionFrames = Math.max(1, Math.round((spec.transition_duration || 0.4) * fps))

  if (urls.length === 0) {
    return (
      <AbsoluteFill
        style={{
          background: '#111',
          color: '#777',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
        }}
      >
        <span style={{ fontFamily: 'sans-serif', fontSize: 32 }}>缺图 · 渲染兜底</span>
      </AbsoluteFill>
    )
  }

  if (urls.length === 1) {
    const src = urls[0]
    switch (spec.animation_type) {
      case 'parallax':
        return <ParallaxLayer src={src} durationFrames={duration} intensity={spec.intensity} />
      case 'static':
        return (
          <Img src={src} style={{ width: '100%', height: '100%', objectFit: 'cover' }} />
        )
      case 'ken-burns':
      default:
        return (
          <KenBurnsLayer
            src={src}
            durationFrames={duration}
            direction={spec.motion_direction}
            intensity={spec.intensity}
          />
        )
    }
  }

  // 多图
  switch (spec.animation_type) {
    case 'keyframe_morph':
      return <KeyframeMorphLayer imageUrls={urls} totalFrames={duration} />
    case 'storyboard':
    default:
      return (
        <StoryboardLayer
          imageUrls={urls}
          totalFrames={duration}
          transitionFrames={transitionFrames}
          fade={spec.transition === 'cross-fade'}
        />
      )
  }
}

const KenBurnsLayer: React.FC<{
  src: string
  durationFrames: number
  direction: AnimationSpec['motion_direction']
  intensity: number
}> = ({ src, durationFrames, direction, intensity }) => {
  const frame = useCurrentFrame()
  const t = interpolate(frame, [0, durationFrames], [0, 1], { extrapolateRight: 'clamp' })
  const zoomRange = 0.15 + intensity * 0.5
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
      <Img src={src} style={{ width: '100%', height: '100%', objectFit: 'cover' }} />
    </AbsoluteFill>
  )
}

const ParallaxLayer: React.FC<{ src: string; durationFrames: number; intensity: number }> = ({
  src,
  durationFrames,
  intensity,
}) => {
  const frame = useCurrentFrame()
  const t = interpolate(frame, [0, durationFrames], [-1, 1], {
    extrapolateLeft: 'clamp',
    extrapolateRight: 'clamp',
  })
  const backShift = t * intensity * 8
  const foreShift = t * intensity * 16
  return (
    <AbsoluteFill>
      <AbsoluteFill style={{ transform: `translateX(${backShift}%) scale(1.15)` }}>
        <Img
          src={src}
          style={{
            width: '100%',
            height: '100%',
            objectFit: 'cover',
            filter: 'blur(8px) brightness(0.85)',
          }}
        />
      </AbsoluteFill>
      <AbsoluteFill style={{ transform: `translateX(${foreShift}%) scale(1.05)` }}>
        <Img src={src} style={{ width: '100%', height: '100%', objectFit: 'cover' }} />
      </AbsoluteFill>
    </AbsoluteFill>
  )
}

const StoryboardLayer: React.FC<{
  imageUrls: string[]
  totalFrames: number
  transitionFrames: number
  fade: boolean
}> = ({ imageUrls, totalFrames, transitionFrames, fade }) => {
  const perShot = totalFrames / imageUrls.length
  return (
    <AbsoluteFill>
      {imageUrls.map((url, i) => {
        const from = Math.round(i * perShot)
        const duration = Math.round(perShot)
        return (
          <Sequence
            key={`${url}-${i}`}
            from={from}
            durationInFrames={duration + transitionFrames}
            layout="none"
          >
            <FadeShot src={url} duration={duration + transitionFrames} fadeFrames={fade ? transitionFrames : 0} />
          </Sequence>
        )
      })}
    </AbsoluteFill>
  )
}

const FadeShot: React.FC<{ src: string; duration: number; fadeFrames: number }> = ({
  src,
  duration,
  fadeFrames,
}) => {
  const frame = useCurrentFrame()
  let opacity = 1
  if (fadeFrames > 0) {
    if (frame < fadeFrames) {
      opacity = interpolate(frame, [0, fadeFrames], [0, 1])
    } else if (frame > duration - fadeFrames) {
      opacity = interpolate(frame, [duration - fadeFrames, duration], [1, 0])
    }
  }
  const zoom = interpolate(frame, [0, duration], [1, 1.08], { extrapolateRight: 'clamp' })
  return (
    <AbsoluteFill style={{ opacity, transform: `scale(${zoom})` }}>
      <Img src={src} style={{ width: '100%', height: '100%', objectFit: 'cover' }} />
    </AbsoluteFill>
  )
}

const KeyframeMorphLayer: React.FC<{ imageUrls: string[]; totalFrames: number }> = ({
  imageUrls,
  totalFrames,
}) => {
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
