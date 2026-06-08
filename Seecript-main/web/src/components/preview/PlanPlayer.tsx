import { Player, type PlayerRef } from '@remotion/player'
import { forwardRef, useEffect, useImperativeHandle, useRef } from 'react'

import type { Material, Plan } from '@/types/schemas'

import { PlanComposition } from './PlanComposition'

const FPS = 30
const WIDTH = 1080
const HEIGHT = 1920

export interface PlanPlayerHandle {
  /** seek 到指定秒；秒会被换算成 30fps 的整数帧。 */
  seek: (seconds: number) => void
  /** 当前 PlayerRef，进阶交互用。 */
  player: PlayerRef | null
}

interface Props {
  plan: Plan
  materials: Material[]
  /** Player 每帧回调当前时间（秒），父级用来驱动时间轴游标。 */
  onTimeUpdate?: (seconds: number) => void
}

/**
 * Compose 页 Remotion 实时预览外壳：
 *
 * - 1080×1920 9:16 画布、30fps，与 remotion/ 子项目 Root.tsx 对齐
 * - durationInFrames = ceil(plan.duration_seconds × 30)；plan 空时退化成 1 帧避免崩
 * - 暴露 seek(seconds) 给父级（FourTrackBoard scene 点击 → 跳转 Player）
 * - 订阅 'frameupdate' 同步播放头给 FourTrackBoard 画游标
 */
export const PlanPlayer = forwardRef<PlanPlayerHandle, Props>(function PlanPlayer(
  { plan, materials, onTimeUpdate },
  ref,
) {
  const playerRef = useRef<PlayerRef>(null)
  const durationInFrames = Math.max(1, Math.ceil(plan.duration_seconds * FPS))

  useImperativeHandle(
    ref,
    () => ({
      seek: (seconds: number) => {
        const frame = Math.max(0, Math.round(seconds * FPS))
        playerRef.current?.seekTo(frame)
      },
      get player() {
        return playerRef.current
      },
    }),
    [],
  )

  useEffect(() => {
    if (!onTimeUpdate) return
    const p = playerRef.current
    if (!p) return
    const handler = (e: { detail: { frame: number } }) => {
      onTimeUpdate(e.detail.frame / FPS)
    }
    p.addEventListener('frameupdate', handler)
    return () => {
      p.removeEventListener('frameupdate', handler)
    }
  }, [onTimeUpdate])

  return (
    <Player
      ref={playerRef}
      component={PlanComposition}
      inputProps={{ plan, materials }}
      durationInFrames={durationInFrames}
      fps={FPS}
      compositionWidth={WIDTH}
      compositionHeight={HEIGHT}
      style={{
        width: '100%',
        aspectRatio: `${WIDTH} / ${HEIGHT}`,
        maxHeight: 520,
        backgroundColor: '#000',
        borderRadius: 8,
        overflow: 'hidden',
      }}
      controls
      clickToPlay
      doubleClickToFullscreen
      acknowledgeRemotionLicense
    />
  )
})
