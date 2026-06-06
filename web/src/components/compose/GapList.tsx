import type { Gap, GapStatus } from '@/types/schemas'
import { SECTION_SHORT } from '@/lib/sections'
import { cn } from '@/lib/utils'

const STATUS_COLOR: Record<GapStatus, string> = {
  ok: 'bg-emerald-500/15 text-emerald-700 dark:text-emerald-300',
  warn: 'bg-amber-500/15 text-amber-700 dark:text-amber-300',
  miss: 'bg-rose-500/15 text-rose-700 dark:text-rose-300',
}
const STATUS_LABEL: Record<GapStatus, string> = { ok: '✅', warn: '⚠️', miss: '❌' }
const IMPACT_COLOR: Record<Gap['impact'], string> = {
  high: 'bg-rose-500',
  medium: 'bg-amber-500',
  low: 'bg-slate-400',
}
const IMPACT_LABEL: Record<Gap['impact'], string> = { high: '高', medium: '中', low: '低' }

/**
 * 缺口槽位清单——Compose 页右栏第二块。
 * - 一行一个 gap，状态 + 影响 chip + section + slot_index
 * - 选中态由 selectedGapId 驱动；点击触发 onSelect
 * - 已采纳过的 gap 右侧补一个 ● 小标
 */
export function GapList({
  gaps,
  selectedGapId,
  filledGapIds,
  onSelect,
}: {
  gaps: Gap[]
  selectedGapId: string | null
  filledGapIds: Set<string>
  onSelect: (gapId: string) => void
}) {
  if (gaps.length === 0) {
    return (
      <div className="rounded-md border border-dashed border-border bg-background/30 p-6 text-center text-xs text-muted-foreground">
        还没找出需要补的素材。点上方「智能分析」开始。
      </div>
    )
  }
  return (
    <ul className="space-y-1.5">
      {gaps.map((gap) => {
        const active = gap.gap_id === selectedGapId
        const filled = filledGapIds.has(gap.gap_id)
        return (
          <li key={gap.gap_id}>
            <button
              onClick={() => onSelect(gap.gap_id)}
              className={cn(
                'w-full rounded-md border px-2.5 py-2 text-left transition-colors',
                active
                  ? 'border-primary bg-primary/5'
                  : 'border-border bg-background/40 hover:bg-secondary/60',
              )}
            >
              <div className="flex items-center gap-2 text-xs">
                <span className={cn('rounded px-1 py-0.5 font-medium', STATUS_COLOR[gap.status])}>
                  {STATUS_LABEL[gap.status]}
                </span>
                <span className="rounded bg-secondary px-1 py-0.5 font-mono">
                  {SECTION_SHORT[gap.section]} · {gap.slot_index}
                </span>
                <span className="flex items-center gap-1">
                  <span className={cn('h-1.5 w-1.5 rounded-full', IMPACT_COLOR[gap.impact])} />
                  <span className="text-[11px] text-muted-foreground">影响 {IMPACT_LABEL[gap.impact]}</span>
                </span>
                {filled && (
                  <span
                    className="ml-auto text-emerald-500"
                    title="已采纳"
                    aria-label="已采纳"
                  >
                    ●
                  </span>
                )}
              </div>
              <p className="mt-1 line-clamp-2 text-xs text-foreground">{gap.requirement}</p>
              {gap.matched_material_id && (
                <p className="mt-0.5 text-[10px] text-muted-foreground">
                  已配上 <span className="font-mono">{gap.matched_material_id}</span>
                </p>
              )}
            </button>
          </li>
        )
      })}
    </ul>
  )
}
