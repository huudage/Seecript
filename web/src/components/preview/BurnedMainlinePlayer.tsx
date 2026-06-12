/**
 * stage-83 (2026-06-12)：step3 预览播放器，hybrid 架构。
 *
 * 背景
 * ====
 * stage-80 把整套 Remotion <Player> + PlanComposition 换成「后端单 mp4 + 浏览器原生 <video>」，
 * 修了「单镜头复读」的根因（Remotion <Video startFrom endAt> 关键帧回退），但代价是
 * **包装/字幕/口播/BGM 全部从预览里消失了** —— 用户在 step3 改组件时看不到效果，相当于盲改。
 *
 * 方案：mp4 底图（pre-rendered 主轨画面，无字幕/包装/音轨混音）+ Remotion 叠加层
 * （PackagingLayer / 口播 Audio / BgmAudio），两层独立：
 *   - 底图 mp4 由 buildMainlinePreview 在用户点「进入第 3 步」时拉一次，缓存在 mainlineUrl
 *   - Remotion <Video volume={0}> 把 mp4 当画面源，整轨静音（避免和 voiceover/BGM 撞）
 *   - 包装/口播/BGM 叠在上面，按绝对秒数定位（与 PlanComposition 相同模式，共用 _overlays.tsx）
 *
 * 与 PlanComposition 的关键差异
 * =============================
 * 不抄 SceneClip 那套（resolveSceneMedia / Video startFrom endAt / TextCardScene / AnimatedImageScene）
 * —— mp4 已经把所有画面拼好，预览不需要再次解析素材路径或重做切片，这就是 hybrid 的最大化简。
 *
 * 接口
 * ====
 * 与原 PlanPlayerHandle 兼容（外部 seekPlayer / playerRef.current?.player 调用零改动）：
 *   - seek(seconds) → playerRef.seekTo(frames)
 *   - player: PlayerRef | null
 *
 * 失败处理
 * ========
 * mainlineUrl=null 时（底图未就绪 / 失败 / 切换 plan 重置）：<Video> 不渲染，只留黑底 + 上层叠加。
 * 用户能正常预览包装/字幕/口播/BGM 调整，只是看不到视频底图。Compose.tsx 顶部黄条提示重试。
 */
import { forwardRef, useEffect, useImperativeHandle, useRef } from 'react'

import { Player, type PlayerRef } from '@remotion/player'
import { AbsoluteFill, Audio, Sequence, Video } from 'remotion'

import type { Plan } from '@/types/schemas'

import { BgmAudio, PackagingLayer, fallbackTitle, secsToFrames } from './_overlays'

export interface BurnedMainlinePlayerHandle {
  seek: (seconds: number) => void
  /** 兼容原 PlanPlayerHandle 占位字段，避免 Compose.tsx 老调用炸 */
  player: PlayerRef | null
}

interface Props {
  plan: Plan
  /** 后端合成的主轨 mp4 URL；null 时叠加层照常显示，底图黑屏。 */
  mainlineUrl: string | null
  /** 时间游标回调；用 setInterval 120ms 轮询 currentFrame，Remotion <Player> 没有 timeupdate 事件 */
  onTimeUpdate?: (seconds: number) => void
}

const FPS = 30

export const BurnedMainlinePlayer = forwardRef<BurnedMainlinePlayerHandle, Props>(
  function BurnedMainlinePlayer({ plan, mainlineUrl, onTimeUpdate }, ref) {
    const playerRef = useRef<PlayerRef>(null)

    useImperativeHandle(
      ref,
      () => ({
        seek: (s: number) => {
          const p = playerRef.current
          if (!p) return
          p.seekTo(secsToFrames(Math.max(0, s), FPS))
        },
        player: playerRef.current,
      }),
      [],
    )

    // Remotion <Player> 无 timeupdate 事件，轮询 currentFrame 上抛。120ms 节流：
    // FourTrackBoard 时间轴游标动起来够丝滑，又不会每 16ms 触发 re-render。
    useEffect(() => {
      if (!onTimeUpdate) return
      const id = window.setInterval(() => {
        const p = playerRef.current
        if (!p) return
        try {
          const f = p.getCurrentFrame()
          if (typeof f === 'number') onTimeUpdate(f / FPS)
        } catch {
          // Player 还在初始化或已卸载，跳过即可
        }
      }, 120)
      return () => window.clearInterval(id)
    }, [onTimeUpdate])

    const total = Math.max(1, secsToFrames(plan.duration_seconds, FPS))
    const { w, h } = canvasFromAspect(plan.settings.aspect_ratio ?? '9:16')

    return (
      <Player
        ref={playerRef}
        component={BurnedComposition}
        inputProps={{ plan, mainlineUrl }}
        durationInFrames={total}
        compositionWidth={w}
        compositionHeight={h}
        fps={FPS}
        controls
        clickToPlay
        loop={false}
        style={{
          width: '100%',
          maxHeight: 520,
          aspectRatio: `${w}/${h}`,
          backgroundColor: '#000',
          borderRadius: 6,
          overflow: 'hidden',
        }}
        acknowledgeRemotionLicense
      />
    )
  },
)

const BurnedComposition: React.FC<{ plan: Plan; mainlineUrl: string | null }> = ({
  plan,
  mainlineUrl,
}) => {
  const sectionsByOrder = new Map(plan.adapted_sections.map((s) => [s.section_id, s]))

  return (
    <AbsoluteFill style={{ backgroundColor: '#000' }}>
      {/* 底图：整轨 mp4，整体静音（avoid 与 voiceover/BGM 重叠）。
          mainlineUrl=null（未就绪 / 失败）时直接黑屏，上层叠加照常显示。 */}
      {mainlineUrl && (
        <Video
          src={mainlineUrl}
          volume={0}
          style={{ width: '100%', height: '100%', objectFit: 'cover' }}
        />
      )}

      {/* 口播：每段 scene 一条 Sequence，按 scene.start 绝对定位（与 PlanComposition 同模式） */}
      {plan.main_track.map((sc) => {
        if (!sc.voiceover_url) return null
        return (
          <Sequence
            key={`vo-${sc.scene_id}`}
            from={secsToFrames(sc.start, FPS)}
            durationInFrames={Math.max(1, secsToFrames(sc.duration, FPS))}
            layout="none"
          >
            <Audio src={sc.voiceover_url} />
          </Sequence>
        )
      })}

      {/* 包装（封面/字幕/标题/贴纸）—— 复用 PackagingLayer */}
      {plan.packaging_track
        .filter((it) => it.kind !== 'transition')
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

      {/* BGM —— 复用 BgmAudio（按 video_anchor_seconds 平移） */}
      {plan.bgm.track_url && <BgmAudio bgm={plan.bgm} fps={FPS} />}
    </AbsoluteFill>
  )
}

/**
 * 与 server/app/services/render/preview.py `_preview_canvas` 同算法：长边 854，短边按比例。
 * 用 480p 是因为后端合成本就是 480p mp4，画布大于素材没意义。
 */
function canvasFromAspect(ratio: string): { w: number; h: number } {
  const m = ratio.match(/^(\d+):(\d+)$/)
  const aw = m ? parseInt(m[1], 10) : 9
  const ah = m ? parseInt(m[2], 10) : 16
  if (aw >= ah) {
    const w = 854
    const h = Math.max(2, Math.round((w * ah) / aw / 2) * 2)
    return { w, h }
  }
  const h = 854
  const w = Math.max(2, Math.round((h * aw) / ah / 2) * 2)
  return { w, h }
}
