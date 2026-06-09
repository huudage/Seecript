import { Player, type PlayerRef } from '@remotion/player'
import { useEffect, useMemo, useRef, useState } from 'react'

import type { Material, Plan } from '@/types/schemas'
import { PlanComposition } from '@/components/preview/PlanComposition'

const FPS = 30
const WIDTH = 1080
const HEIGHT = 1920

interface Props {
  plan: Plan
  materials: Material[]
  /** 段落起点（秒）—— gap.section_id 对应 adapted_section 的 start。 */
  sectionStart: number
  /** 段落终点（秒）。 */
  sectionEnd: number
  /** 顶部小字标签（例如 "段 #2 · 3.4–8.1s"）。 */
  label?: string
}

/**
 * step2 单段预览卡：复用 Remotion 主 composition + inFrame/outFrame 把范围卡到当前
 * 选中 gap 的段落上。优势：
 *  - 完整体验：BGM / 字幕 / 包装 / 转场都按 plan 现状渲染，与 step3 全片预览一致
 *  - 不重写 Composition：直接 reuse PlanComposition，零代码分支
 *  - section 变化时 key 变化导致 Player 重新挂载，inFrame seek 到段首
 *
 * 注意 inFrame/outFrame 是 Remotion 的硬限制：超出 outFrame 后停止，回到开头时
 * 会 seek 到 inFrame。这正是我们想要的「只看这一段」体验。
 */
export function SectionPreviewCard({
  plan,
  materials,
  sectionStart,
  sectionEnd,
  label,
}: Props) {
  const playerRef = useRef<PlayerRef>(null)
  const [hint, setHint] = useState<string>('')
  const totalFrames = Math.max(1, Math.ceil(plan.duration_seconds * FPS))
  const inFrame = Math.max(0, Math.floor(sectionStart * FPS))
  const outFrame = Math.min(totalFrames - 1, Math.max(inFrame + 1, Math.ceil(sectionEnd * FPS)))
  const segDur = Math.max(0, sectionEnd - sectionStart)

  // section 变化 → 自动跳到段首并暂停（避免上一段的播放头停在屏幕中间）
  useEffect(() => {
    const p = playerRef.current
    if (!p) return
    try {
      p.pause()
      p.seekTo(inFrame)
    } catch {
      /* player 还没 ready，忽略 */
    }
  }, [inFrame])

  const previewKey = useMemo(
    () => `${plan.plan_id}-${inFrame}-${outFrame}`,
    [plan.plan_id, inFrame, outFrame],
  )

  if (segDur <= 0) {
    return (
      <div className="rounded-md border border-dashed border-border bg-background/30 px-3 py-2 text-[11px] text-muted-foreground">
        该段时长为 0，无法预览。
      </div>
    )
  }

  return (
    <div className="space-y-1.5 rounded-lg border border-border bg-background/40 p-2">
      <div className="flex items-center justify-between text-[10px]">
        <span className="font-semibold text-foreground">
          段落预览{label ? ` · ${label}` : ''}
        </span>
        <span className="text-muted-foreground">
          {sectionStart.toFixed(1)}–{sectionEnd.toFixed(1)}s · {segDur.toFixed(1)}s
        </span>
      </div>
      <div className="overflow-hidden rounded-md border border-border">
        <Player
          key={previewKey}
          ref={playerRef}
          component={PlanComposition}
          inputProps={{ plan, materials }}
          durationInFrames={totalFrames}
          inFrame={inFrame}
          outFrame={outFrame}
          fps={FPS}
          compositionWidth={WIDTH}
          compositionHeight={HEIGHT}
          style={{
            width: '100%',
            aspectRatio: `${WIDTH} / ${HEIGHT}`,
            maxHeight: 360,
            backgroundColor: '#000',
          }}
          controls
          clickToPlay
          acknowledgeRemotionLicense
          errorFallback={({ error }) => {
            if (!hint) setHint(error.message)
            return <span className="text-xs text-rose-300">预览失败：{error.message}</span>
          }}
        />
      </div>
      {hint && (
        <p className="text-[10px] text-muted-foreground">提示：{hint}</p>
      )}
    </div>
  )
}
