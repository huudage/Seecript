/**
 * stage-80 (2026-06-12)：PlanPlayer 改用后端合成的主轨 mp4。
 *
 * 接口契约保留：
 *   - <PlanPlayer ref={playerRef} plan={plan} materials={materials} onTimeUpdate={...} />
 *   - playerRef.current.seek(seconds)
 * 调用方（Compose.tsx step2/step3）零改动。
 *
 * `materials` 参数已不再需要（后端拿 plan_id 自己解析素材），保留 prop 仅为兼容签名。
 */
import { forwardRef, useImperativeHandle, useRef } from 'react'

import type { Material, Plan } from '@/types/schemas'

import { MainlinePreviewPlayer, type MainlinePreviewPlayerHandle } from './MainlinePreviewPlayer'

export interface PlanPlayerHandle {
  /** seek 到指定秒。 */
  seek: (seconds: number) => void
  /** 触发后端重新合成（plan 变更后可手动调一次）。 */
  refresh?: () => Promise<void>
  /** 兼容旧签名占位（原 Remotion PlayerRef，新栈下永远为 null）。 */
  player: null
}

interface Props {
  plan: Plan
  // eslint-disable-next-line @typescript-eslint/no-unused-vars
  materials: Material[]
  onTimeUpdate?: (seconds: number) => void
}

export const PlanPlayer = forwardRef<PlanPlayerHandle, Props>(function PlanPlayer(
  { plan, onTimeUpdate },
  ref,
) {
  const innerRef = useRef<MainlinePreviewPlayerHandle>(null)

  useImperativeHandle(
    ref,
    () => ({
      seek: (seconds: number) => innerRef.current?.seek(seconds),
      refresh: () => innerRef.current?.refresh() ?? Promise.resolve(),
      player: null,
    }),
    [],
  )

  return (
    <MainlinePreviewPlayer
      ref={innerRef}
      plan={plan}
      onTimeUpdate={onTimeUpdate}
      maxHeight={520}
    />
  )
})
