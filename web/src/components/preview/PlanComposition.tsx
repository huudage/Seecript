import { AbsoluteFill, Audio, Img, Sequence, Video } from 'remotion'

import { SECTION_HEX, SECTION_LABEL } from '@/lib/sections'
import { resolveSceneMedia } from '@/lib/scene_url'
import type { BGMConfig, Material, PackagingItem, Plan, Scene } from '@/types/schemas'

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
  const media = resolveSceneMedia(scene, materials)
  const voiceover = scene.voiceover_url ?? null

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

  // text_card / 资源缺失兜底
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
