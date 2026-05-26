/**
 * 共享 TimelineTrack —— 简易时间轴色块。
 *
 * 从 pages/Render.tsx 抽出来；Compose 的 rerank 双轨对比图 / Render 的主轨包装轨都用同一组件。
 * 实现保持原样不动；调用方只需传 items[] + duration。
 */
import { cn } from '@/lib/utils'

export interface TimelineItem {
  key: string
  start: number
  end: number
  color: string // Tailwind bg-* 或 hex；这里仍按 Tailwind class 走
  text: string
}

export function TimelineTrack({
  label,
  duration,
  items,
  empty,
}: {
  label: string
  duration: number
  items: TimelineItem[]
  empty?: string
}) {
  return (
    <div>
      <div className="mb-1 flex items-center justify-between text-[11px] text-muted-foreground">
        <span>{label}</span>
        <span className="font-mono">
          {duration.toFixed(1)}s · {items.length}
        </span>
      </div>
      <div className="relative h-8 overflow-hidden rounded-md border border-border bg-background/40">
        {items.length === 0 ? (
          <div className="flex h-full items-center justify-center text-[11px] text-muted-foreground">
            {empty ?? '空'}
          </div>
        ) : (
          items.map((it) => {
            const left = Math.max(0, (it.start / duration) * 100)
            const width = Math.max(0.5, ((it.end - it.start) / duration) * 100)
            return (
              <div
                key={it.key}
                className={cn(
                  'absolute top-0 flex h-full items-center overflow-hidden border-r border-white/40 px-1 text-[10px] text-white',
                  it.color,
                )}
                style={{ left: `${left}%`, width: `${width}%` }}
                title={`${it.text} · ${it.start.toFixed(1)}–${it.end.toFixed(1)}s`}
              >
                <span className="truncate">{it.text}</span>
              </div>
            )
          })
        )}
      </div>
    </div>
  )
}
