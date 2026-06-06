/**
 * DecomposeTable —— stage-23 4 列拆解表：结构 / 分镜 / 内容 / 脚本。
 *
 * 用在 Decompose 页 ManifestView：把 sections + shots + visual_summary/script
 * 全部纵向铺开，结构列按 section 合并相邻行（rowspan），让用户一眼能把"哪段
 * 对应哪个分镜、画面在演什么、口播说什么"看穿。
 *
 * 编辑能力：当前版本只读；ManifestEditor 里的字段编辑走另一条路（沿用
 * Decompose.tsx 已有的 shots sub-form），表格本身不接管 onChange。
 */
import type { SampleManifest, Shot, Section } from '@/types/schemas'
import { SECTION_BG, SECTION_LABEL } from '@/lib/sections'
import { cn } from '@/lib/utils'

interface Props {
  manifest: SampleManifest
}

function formatTime(s: number): string {
  const m = Math.floor(s / 60)
  const sec = s % 60
  return `${m}:${sec.toFixed(1).padStart(4, '0')}`
}

/** 找包含某 shot 的 section（首匹配；section.shot_indices 为空时按时间窗兜底）。 */
function findSection(shot: Shot, sections: Section[]): Section | undefined {
  // 优先按 shot_indices
  const byIndex = sections.find((s) => s.shot_indices?.includes(shot.index))
  if (byIndex) return byIndex
  // 兜底：按时间窗（shot.start 落在 section [start,end] 内）
  return sections.find((s) => shot.start >= s.start && shot.start < s.end)
}

export function DecomposeTable({ manifest }: Props) {
  const sections = manifest.sections ?? []
  const shots = manifest.shots ?? []

  if (shots.length === 0) {
    return <p className="text-sm text-muted-foreground">（无分镜数据）</p>
  }

  // 给每个 shot 找其 section；按 shot 顺序铺，前一个 shot 同 section 时合并 rowspan
  const rows = shots.map((sh) => ({
    shot: sh,
    section: findSection(sh, sections),
  }))

  // 计算每个 section 起始行的 rowspan（连续同 section 的行数）
  const sectionRowSpans = new Map<number, number>() // key = 起始 row index, value = rowspan
  let i = 0
  while (i < rows.length) {
    const curSec = rows[i].section
    let j = i + 1
    while (j < rows.length && rows[j].section === curSec) j++
    sectionRowSpans.set(i, j - i)
    i = j
  }

  return (
    <div className="overflow-x-auto rounded-lg border border-border">
      <table className="w-full border-collapse text-xs">
        <thead className="bg-muted/50 text-left text-[11px] font-semibold text-muted-foreground">
          <tr>
            <th className="w-[14%] border-b border-border px-3 py-2">结构</th>
            <th className="w-[18%] border-b border-border px-3 py-2">分镜</th>
            <th className="w-[34%] border-b border-border px-3 py-2">内容（画面）</th>
            <th className="w-[34%] border-b border-border px-3 py-2">脚本（口播 / 字幕）</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row, idx) => {
            const { shot, section } = row
            const span = sectionRowSpans.get(idx)
            const showSectionCell = span !== undefined
            return (
              <tr key={shot.index} className="border-b border-border last:border-b-0 align-top">
                {showSectionCell && (
                  <td
                    rowSpan={span}
                    className={cn(
                      'border-r border-border px-3 py-2',
                      section ? SECTION_BG[section.role] : 'bg-muted/30',
                    )}
                  >
                    {section ? (
                      <div className="space-y-1">
                        <div className="font-mono text-[10px] uppercase opacity-70">
                          {section.role}
                        </div>
                        <div className="font-semibold">
                          {section.theme || SECTION_LABEL[section.role] || section.role}
                        </div>
                        <div className="text-[10px] text-muted-foreground">
                          {formatTime(section.start)} – {formatTime(section.end)}
                        </div>
                      </div>
                    ) : (
                      <span className="text-[10px] text-muted-foreground">未归段</span>
                    )}
                  </td>
                )}
                <td className="border-r border-border px-3 py-2">
                  <div className="flex items-start gap-2">
                    {shot.thumbnail_url ? (
                      <img
                        src={shot.thumbnail_url}
                        alt={`shot-${shot.index}`}
                        loading="lazy"
                        className="h-10 w-16 shrink-0 rounded border border-border object-cover"
                      />
                    ) : (
                      <div className="flex h-10 w-16 shrink-0 items-center justify-center rounded border border-dashed border-border text-[9px] text-muted-foreground">
                        无图
                      </div>
                    )}
                    <div className="min-w-0 flex-1">
                      <div className="flex items-center gap-1">
                        <span className="font-mono text-[10px] text-muted-foreground">
                          #{shot.index}
                        </span>
                        {shot.merged_from && shot.merged_from.length > 1 && (
                          <span className="rounded bg-amber-500/15 px-1 py-0.5 text-[9px] font-medium text-amber-700 dark:text-amber-300">
                            {shot.merged_from.length} 镜合 1
                          </span>
                        )}
                      </div>
                      <div className="text-[10px] text-muted-foreground">
                        {formatTime(shot.start)} – {formatTime(shot.end)} · {shot.duration.toFixed(1)}s
                      </div>
                    </div>
                  </div>
                </td>
                <td className="border-r border-border px-3 py-2">
                  {shot.visual_summary ? (
                    <p className="leading-relaxed">{shot.visual_summary}</p>
                  ) : (
                    <p className="text-[11px] italic text-muted-foreground">
                      （未生成画面描述）
                    </p>
                  )}
                  {shot.tags && shot.tags.length > 0 && (
                    <div className="mt-1.5 flex flex-wrap gap-1">
                      {shot.tags.slice(0, 5).map((t, ti) => (
                        <span
                          key={ti}
                          className="rounded bg-muted px-1 py-0.5 text-[9px] text-muted-foreground"
                        >
                          {t}
                        </span>
                      ))}
                    </div>
                  )}
                </td>
                <td className="px-3 py-2">
                  {shot.script ? (
                    <p className="leading-relaxed">{shot.script}</p>
                  ) : (
                    <p className="text-[11px] italic text-muted-foreground">（未生成脚本）</p>
                  )}
                  {shot.transcript && shot.transcript !== shot.script && (
                    <details className="mt-1">
                      <summary className="cursor-pointer text-[10px] text-muted-foreground hover:text-foreground">
                        原 ASR
                      </summary>
                      <p className="mt-1 text-[10px] text-muted-foreground">{shot.transcript}</p>
                    </details>
                  )}
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}
