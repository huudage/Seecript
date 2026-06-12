/**
 * stage-80 (2026-06-12)：SectionPreviewCard 也切到后端合成的主轨 mp4。
 *
 * 实现：复用 MainlinePreviewPlayer，传 inSeconds/outSeconds 让它自动 seek 到段首
 * 并在段尾暂停。优势：与全片预览共享同一份 mp4 缓存（同 plan signature 命中），
 * step2 切段不重跑 ffmpeg；劣势：没有 BGM / 字幕 / 包装的预览效果（与全片一致，
 * 用户在 step4 渲染时才看完整效果）。
 */
import { useRef, useState } from 'react'

import type { Material, Plan } from '@/types/schemas'

import {
  MainlinePreviewPlayer,
  type MainlinePreviewPlayerHandle,
} from '@/components/preview/MainlinePreviewPlayer'

interface Props {
  plan: Plan
  // eslint-disable-next-line @typescript-eslint/no-unused-vars
  materials: Material[]
  /** 段落起点（秒）—— gap.section_id 对应 adapted_section 的 start。 */
  sectionStart: number
  /** 段落终点（秒）。 */
  sectionEnd: number
  /** 顶部小字标签（例如 "段 #2 · 3.4–8.1s"）。 */
  label?: string
  /** Player 每帧回调当前秒。父级用来驱动 FourTrackBoard 时间轴游标。 */
  onTimeUpdate?: (seconds: number) => void
}

export function SectionPreviewCard({
  plan,
  sectionStart,
  sectionEnd,
  label,
  onTimeUpdate,
}: Props) {
  const ref = useRef<MainlinePreviewPlayerHandle>(null)
  const [error] = useState<string>('')
  const segDur = Math.max(0, sectionEnd - sectionStart)

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
        <MainlinePreviewPlayer
          ref={ref}
          plan={plan}
          onTimeUpdate={onTimeUpdate}
          inSeconds={sectionStart}
          outSeconds={sectionEnd}
          maxHeight={360}
        />
      </div>
      {error && <p className="text-[10px] text-muted-foreground">提示：{error}</p>}
    </div>
  )
}
