import type { AdaptedSection, Gap, GapStatus } from '@/types/schemas'
import { SECTION_BG, SECTION_LABEL, SECTION_SHORT } from '@/lib/sections'
import { cn } from '@/lib/utils'

const STATUS_COLOR: Record<GapStatus, string> = {
  ok: 'bg-emerald-500/15 text-emerald-700 dark:text-emerald-300',
  warn: 'bg-amber-500/15 text-amber-700 dark:text-amber-300',
  miss: 'bg-rose-500/15 text-rose-700 dark:text-rose-300',
}
const STATUS_GLYPH: Record<GapStatus, string> = { ok: '✅', warn: '⚠️', miss: '❌' }
const IMPACT_COLOR: Record<Gap['impact'], string> = {
  high: 'bg-rose-500',
  medium: 'bg-amber-500',
  low: 'bg-slate-400',
}

/** 段汇总状态：取 gaps 中最差状态（miss > warn > ok）。无 gap 时为 'empty'。 */
type SectionStatus = GapStatus | 'empty'

const STATUS_ORDER: Record<GapStatus, number> = { ok: 0, warn: 1, miss: 2 }

function rollup(gaps: Gap[], filledGapIds: Set<string>): SectionStatus {
  if (gaps.length === 0) return 'empty'
  // 已填补的 gap 视作 ok
  const effective = gaps.map((g) => (filledGapIds.has(g.gap_id) ? 'ok' : g.status))
  let worst: GapStatus = 'ok'
  for (const s of effective) {
    if (STATUS_ORDER[s] > STATUS_ORDER[worst]) worst = s
  }
  return worst
}

const SECTION_STATUS_LABEL: Record<SectionStatus, string> = {
  ok: '✅ 完整',
  warn: '⚠️ 需调整',
  miss: '❌ 待补全',
  empty: '— 无槽位',
}

const SECTION_STATUS_COLOR: Record<SectionStatus, string> = {
  ok: 'bg-emerald-500/15 text-emerald-700 dark:text-emerald-300',
  warn: 'bg-amber-500/15 text-amber-700 dark:text-amber-300',
  miss: 'bg-rose-500/15 text-rose-700 dark:text-rose-300',
  empty: 'bg-secondary text-muted-foreground',
}

/**
 * 适配结构清单——替代旧 GapList，按 AdaptedSection 分组展示。
 *
 * 设计目标（来自需求"不缺少的也要展示"）：
 * - 即使该段无 gap，也展示卡片 + content_description，让用户看到完整结构指导
 * - 段头展示 role 色块 + theme + 段汇总状态
 * - 段体展示 content_description（紧贴用户主题的内容说明）
 * - 段尾按 section_id 关联 gaps 渲染为可点击的槽位行
 */
export function AdaptedSectionList({
  adaptedSections,
  gaps,
  selectedGapId,
  filledGapIds,
  onSelect,
}: {
  adaptedSections: AdaptedSection[]
  gaps: Gap[]
  selectedGapId: string | null
  filledGapIds: Set<string>
  onSelect: (gapId: string) => void
}) {
  if (adaptedSections.length === 0) {
    return (
      <div className="rounded-md border border-dashed border-border bg-background/30 p-6 text-center text-xs text-muted-foreground">
        还没有改编后的结构；填好主题和视频目的，点上方「智能分析」开始。
      </div>
    )
  }

  // 按 section_id 索引 gaps；老 plan（无 section_id）按 role 兜底分组
  const gapsBySectionId = new Map<string, Gap[]>()
  const looseGapsByRole = new Map<string, Gap[]>()
  for (const g of gaps) {
    if (g.section_id) {
      const arr = gapsBySectionId.get(g.section_id) ?? []
      arr.push(g)
      gapsBySectionId.set(g.section_id, arr)
    } else {
      const arr = looseGapsByRole.get(g.section) ?? []
      arr.push(g)
      looseGapsByRole.set(g.section, arr)
    }
  }
  // 老 plan 兼容：按 role 顺位回填给同 role 的第一个未填段
  if (looseGapsByRole.size > 0) {
    for (const sec of adaptedSections) {
      if (gapsBySectionId.has(sec.section_id)) continue
      const queue = looseGapsByRole.get(sec.role)
      if (queue && queue.length) {
        gapsBySectionId.set(sec.section_id, queue.splice(0))
      }
    }
  }

  return (
    <ul className="space-y-3">
      {adaptedSections.map((sec) => {
        const sectionGaps = gapsBySectionId.get(sec.section_id) ?? []
        const status = rollup(sectionGaps, filledGapIds)
        return (
          <li
            key={sec.section_id}
            className="overflow-hidden rounded-md border border-border bg-background/40"
          >
            <header className="flex items-center gap-2 px-3 py-2 text-xs">
              <span
                className={cn(
                  'h-2 w-2 shrink-0 rounded-full',
                  SECTION_BG[sec.role],
                )}
              />
              <span className="font-mono text-[10px] text-muted-foreground">
                {SECTION_SHORT[sec.role]}
              </span>
              <span className="font-semibold text-foreground">
                {sec.theme || SECTION_LABEL[sec.role]}
              </span>
              <span
                className={cn(
                  'ml-auto rounded px-1.5 py-0.5 text-[10px]',
                  SECTION_STATUS_COLOR[status],
                )}
              >
                {SECTION_STATUS_LABEL[status]}
              </span>
            </header>
            <div className="border-t border-border/60 bg-secondary/30 px-3 py-2">
              <p className="text-[11px] leading-relaxed text-foreground/90">
                {sec.content_description || '（暂无内容说明）'}
              </p>
            </div>
            {sectionGaps.length > 0 ? (
              <ul className="space-y-1 px-3 py-2">
                {sectionGaps.map((gap) => {
                  const active = gap.gap_id === selectedGapId
                  const filled = filledGapIds.has(gap.gap_id)
                  return (
                    <li key={gap.gap_id}>
                      <button
                        onClick={() => onSelect(gap.gap_id)}
                        className={cn(
                          'w-full rounded-md border px-2 py-1.5 text-left transition-colors',
                          active
                            ? 'border-primary bg-primary/5'
                            : 'border-border bg-background/40 hover:bg-secondary/60',
                        )}
                      >
                        <div className="flex items-center gap-2 text-[11px]">
                          <span className={cn('rounded px-1 py-0.5', STATUS_COLOR[gap.status])}>
                            {STATUS_GLYPH[gap.status]}
                          </span>
                          <span className="rounded bg-secondary px-1 py-0.5 font-mono text-[10px]">
                            槽 {gap.slot_index + 1}
                          </span>
                          <span className="flex items-center gap-1">
                            <span className={cn('h-1.5 w-1.5 rounded-full', IMPACT_COLOR[gap.impact])} />
                          </span>
                          {filled && (
                            <span className="ml-auto text-emerald-500" title="已采纳补全" aria-label="已采纳">
                              ●
                            </span>
                          )}
                        </div>
                        <p className="mt-1 line-clamp-2 text-[11px] text-foreground">
                          {gap.requirement}
                        </p>
                        {gap.matched_material_id && (
                          <p className="mt-0.5 text-[10px] text-muted-foreground">
                            命中 <span className="font-mono">{gap.matched_material_id}</span>
                          </p>
                        )}
                      </button>
                    </li>
                  )
                })}
              </ul>
            ) : (
              <p className="px-3 py-2 text-[11px] text-muted-foreground">
                无槽位待补——本段已按结构规划完成。
              </p>
            )}
          </li>
        )
      })}
    </ul>
  )
}
