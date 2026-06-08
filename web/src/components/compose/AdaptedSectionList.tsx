import type { AdaptedSection, Gap, GapStatus, StructuralPattern } from '@/types/schemas'
import { getSectionMeta } from '@/lib/sections'
import { cn } from '@/lib/utils'

const TEMPO_LABEL: Record<string, string> = {
  slow: '慢',
  medium: '中',
  fast: '快',
  peak: '峰值',
  deceleration: '减速',
}
const TEMPO_TONE: Record<string, string> = {
  slow: 'bg-sky-500/15 text-sky-700 dark:text-sky-300',
  medium: 'bg-slate-500/15 text-slate-700 dark:text-slate-300',
  fast: 'bg-amber-500/15 text-amber-700 dark:text-amber-300',
  peak: 'bg-rose-500/15 text-rose-700 dark:text-rose-300',
  deceleration: 'bg-emerald-500/15 text-emerald-700 dark:text-emerald-300',
}

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

/** stage-26 PR-N.3：分镜级匹配质量色条聚合。
 *
 * 段卡顶部显示一条三色质量色条，按本段 shots[*].match_quality 比例分段：
 *   - good (≥0.30)  → 绿
 *   - weak (≥0.10)  → 橙（『待修补』提醒）
 *   - missing (<0.10 / 无匹配) → 灰（物化层兜底成 text_card 占位）
 *
 * 用户一眼能看到本段哪些镜头需要换源；具体到镜头的换源 UI 在 SceneEditPanel（PR-N.5）。
 */
type QualityCounts = { good: number; weak: number; missing: number; total: number }

function countShotQuality(sec: AdaptedSection): QualityCounts {
  const counts: QualityCounts = { good: 0, weak: 0, missing: 0, total: 0 }
  for (const sh of sec.shots ?? []) {
    counts.total += 1
    const q = sh.match_quality
    if (q === 'good') counts.good += 1
    else if (q === 'weak') counts.weak += 1
    else counts.missing += 1
  }
  return counts
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
  warn: '⚠️ 还差点',
  miss: '❌ 缺素材',
  empty: '— 不需要补',
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
  pattern,
  onSelect,
}: {
  adaptedSections: AdaptedSection[]
  gaps: Gap[]
  selectedGapId: string | null
  filledGapIds: Set<string>
  pattern?: StructuralPattern
  onSelect: (gapId: string) => void
}) {
  if (adaptedSections.length === 0) {
    return (
      <div className="rounded-md border border-dashed border-border bg-background/30 p-6 text-center text-xs text-muted-foreground">
        还没生成结构。先填好主题和目的，再点上方「智能分析」。
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
        const meta = getSectionMeta(sec.role, pattern)
        const tempo = sec.tempo
        const qc = countShotQuality(sec)
        return (
          <li
            key={sec.section_id}
            className="overflow-hidden rounded-md border border-border bg-background/40"
          >
            <header className="flex items-center gap-2 px-3 py-2 text-xs">
              <span
                className={cn(
                  'h-2 w-2 shrink-0 rounded-full',
                  meta.bg,
                )}
              />
              <span className="font-mono text-xs text-muted-foreground">
                {meta.short}
              </span>
              <span className="font-semibold text-foreground">
                {sec.theme || meta.label}
              </span>
              {tempo && (
                <span className={cn('rounded px-1.5 py-0.5 text-xs', TEMPO_TONE[tempo] ?? 'bg-secondary text-muted-foreground')}>
                  {TEMPO_LABEL[tempo] ?? tempo}
                </span>
              )}
              <span
                className={cn(
                  'ml-auto rounded px-1.5 py-0.5 text-xs',
                  SECTION_STATUS_COLOR[status],
                )}
              >
                {SECTION_STATUS_LABEL[status]}
              </span>
            </header>
            {qc.total > 0 && (
              <div
                className="flex items-center gap-2 border-t border-border/60 bg-background/60 px-3 py-1.5"
                title={`分镜匹配质量：精准 ${qc.good} / 待修补 ${qc.weak} / 缺匹配 ${qc.missing}`}
              >
                <div className="flex h-1.5 flex-1 overflow-hidden rounded bg-muted">
                  {qc.good > 0 && (
                    <div
                      className="bg-emerald-500"
                      style={{ width: `${(qc.good / qc.total) * 100}%` }}
                    />
                  )}
                  {qc.weak > 0 && (
                    <div
                      className="bg-amber-500"
                      style={{ width: `${(qc.weak / qc.total) * 100}%` }}
                    />
                  )}
                  {qc.missing > 0 && (
                    <div
                      className="bg-slate-400"
                      style={{ width: `${(qc.missing / qc.total) * 100}%` }}
                    />
                  )}
                </div>
                <div className="flex shrink-0 items-center gap-1.5 text-xs font-mono">
                  {qc.good > 0 && (
                    <span className="text-emerald-700 dark:text-emerald-300">●{qc.good}</span>
                  )}
                  {qc.weak > 0 && (
                    <span className="text-amber-700 dark:text-amber-300">●{qc.weak}</span>
                  )}
                  {qc.missing > 0 && (
                    <span className="text-slate-500">●{qc.missing}</span>
                  )}
                  <span className="text-muted-foreground">/{qc.total}</span>
                </div>
              </div>
            )}
            <div className="border-t border-border/60 bg-secondary/30 px-3 py-2">
              <p className="text-xs leading-relaxed text-foreground/90">
                {sec.content_description || '（暂无说明）'}
              </p>
              {sec.adaptation_note && (
                <p className="mt-1 text-xs italic text-muted-foreground">
                  改编思路：{sec.adaptation_note}
                </p>
              )}
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
                        <div className="flex items-center gap-2 text-xs">
                          <span className={cn('rounded px-1 py-0.5', STATUS_COLOR[gap.status])}>
                            {STATUS_GLYPH[gap.status]}
                          </span>
                          <span className="rounded bg-secondary px-1 py-0.5 font-mono text-xs">
                            镜头 {gap.slot_index + 1}
                          </span>
                          <span className="flex items-center gap-1">
                            <span className={cn('h-1.5 w-1.5 rounded-full', IMPACT_COLOR[gap.impact])} />
                          </span>
                          {filled && (
                            <span className="ml-auto text-emerald-500" title="已采纳" aria-label="已采纳">
                              ●
                            </span>
                          )}
                        </div>
                        <p className="mt-1 line-clamp-2 text-xs text-foreground">
                          {gap.requirement}
                        </p>
                        {gap.matched_material_id && (
                          <p className="mt-0.5 text-xs text-muted-foreground">
                            已配上 <span className="font-mono">{gap.matched_material_id}</span>
                          </p>
                        )}
                      </button>
                    </li>
                  )
                })}
              </ul>
            ) : (
              <p className="px-3 py-2 text-xs text-muted-foreground">
                这一段不需要补素材——已经按结构规划完成。
              </p>
            )}
          </li>
        )
      })}
    </ul>
  )
}
