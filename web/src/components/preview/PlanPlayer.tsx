/**
 * stage-84 (2026-06-13)：回退到 Remotion `<Player>` + PlanComposition。
 *
 * 历史
 * ====
 * - stage-80 把 Remotion 全干掉换成「后端单 mp4 + 原生 <video>」修「单镜头复读」bug，
 *   但代价是包装/字幕/BGM/口播全部从预览里消失了
 * - stage-83 hybrid 方案（mp4 底图 + Remotion 叠加）三次修都没让用户看到包装/字幕轨
 * - 用户最终选择「回到 Remotion PlanComposition，接受单镜头偶发复读 bug，
 *   换取边改边看包装/字幕/BGM/口播效果」
 *
 * 已知问题
 * ========
 * Remotion <Video startFrom endAt> 内部按帧重设 video.currentTime，HTMLVideoElement
 * 的 seek 不是 frame-accurate，落到关键帧附近偶发回退几帧 → 单镜头内可能复读前 0.X 秒。
 * 这是浏览器层面 video seek 精度问题，预览侧无解。最终渲染（remotion CLI）走 server
 * 端 Chrome headless，已在 stage-79 修复，与预览无关。
 *
 * 接口
 * ====
 * 与原 PlanPlayer 接口兼容：
 *   - playerRef.current.seek(seconds)
 *   - playerRef.current.player（PlayerRef 实例，供 controls 等高级用例）
 */
import { forwardRef, useEffect, useImperativeHandle, useRef } from 'react'

import { Player, type PlayerRef } from '@remotion/player'

import type { Material, Plan } from '@/types/schemas'

import { PlanComposition } from './PlanComposition'

const FPS = 30

export interface PlanPlayerHandle {
  seek: (seconds: number) => void
  player: PlayerRef | null
}

interface Props {
  plan: Plan
  materials: Material[]
  onTimeUpdate?: (seconds: number) => void
}

export const PlanPlayer = forwardRef<PlanPlayerHandle, Props>(function PlanPlayer(
  { plan, materials, onTimeUpdate },
  ref,
) {
  const playerRef = useRef<PlayerRef>(null)

  useImperativeHandle(
    ref,
    () => ({
      seek: (s: number) => {
        const p = playerRef.current
        if (!p) return
        p.seekTo(Math.round(Math.max(0, s) * FPS))
      },
      player: playerRef.current,
    }),
    [],
  )

  // Remotion <Player> 无 timeupdate 事件；setInterval 120ms 轮询 currentFrame。
  useEffect(() => {
    if (!onTimeUpdate) return
    const id = window.setInterval(() => {
      const p = playerRef.current
      if (!p) return
      try {
        const f = p.getCurrentFrame()
        if (typeof f === 'number') onTimeUpdate(f / FPS)
      } catch {
        // Player 还在初始化或已卸载，跳过
      }
    }, 120)
    return () => window.clearInterval(id)
  }, [onTimeUpdate])

  const total = Math.max(1, Math.round(plan.duration_seconds * FPS))
  const { w, h } = canvasFromAspect(plan.settings?.aspect_ratio ?? '9:16')

  return (
    <Player
      ref={playerRef}
      component={PlanComposition}
      inputProps={{ plan, materials }}
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
})

/**
 * 与 remotion/src/Root.tsx 真渲染端对齐：1080×1920（9:16）/ 1920×1080（16:9）。
 * PlanComposition 里 TextCardScene / AnimatedImageScene 的字号/padding 都是按
 * 1080×1920 绝对像素写的，画布更小会让字溢出或比例错位。
 */
function canvasFromAspect(ratio: string): { w: number; h: number } {
  const m = ratio.match(/^(\d+):(\d+)$/)
  const aw = m ? parseInt(m[1], 10) : 9
  const ah = m ? parseInt(m[2], 10) : 16
  if (aw >= ah) {
    const w = 1920
    const h = Math.max(2, Math.round((w * ah) / aw / 2) * 2)
    return { w, h }
  }
  const h = 1920
  const w = Math.max(2, Math.round((h * aw) / ah / 2) * 2)
  return { w, h }
}
