/**
 * AnalysisCard —— stage-23 全片复盘卡片。
 *
 * 用在 Decompose 页 ManifestView 的「拆解结果」区域：左侧亮点 / 右侧改进建议。
 * analysis 不存在时（旧版本槽）显灰色占位，不挡功能。
 */
import type { SampleAnalysis, HighlightAspect, ImprovementAspect } from '@/types/schemas'
import { cn } from '@/lib/utils'

interface Props {
  analysis?: SampleAnalysis | null
}

const ASPECT_LABEL: Record<HighlightAspect | ImprovementAspect, string> = {
  hook: '钩子',
  narrative: '叙事',
  visual: '视觉',
  audio: '声音',
  rhythm: '节奏',
  copy: '文案',
  cta: 'CTA',
  structure: '结构',
}

const ASPECT_COLOR: Record<HighlightAspect | ImprovementAspect, string> = {
  hook: 'bg-rose-500/15 text-rose-700 dark:text-rose-300',
  narrative: 'bg-indigo-500/15 text-indigo-700 dark:text-indigo-300',
  visual: 'bg-emerald-500/15 text-emerald-700 dark:text-emerald-300',
  audio: 'bg-purple-500/15 text-purple-700 dark:text-purple-300',
  rhythm: 'bg-amber-500/15 text-amber-700 dark:text-amber-300',
  copy: 'bg-sky-500/15 text-sky-700 dark:text-sky-300',
  cta: 'bg-orange-500/15 text-orange-700 dark:text-orange-300',
  structure: 'bg-slate-500/15 text-slate-700 dark:text-slate-300',
}

export function AnalysisCard({ analysis }: Props) {
  if (!analysis) {
    return (
      <div className="rounded-lg border border-border bg-background/40 px-4 py-6 text-center text-xs text-muted-foreground">
        老版本拆解未携带亮点 / 改进分析；重新拆解此样例后才会生成。
      </div>
    )
  }

  const { highlights, improvements, overall_score, one_line_verdict } = analysis

  return (
    <div className="rounded-lg border border-border bg-background/40 p-4">
      {/* 顶部：分数 + 一句话总评 */}
      <div className="mb-4 flex items-baseline gap-4 border-b border-border pb-3">
        <div>
          <div className="text-3xl font-bold tabular-nums">{overall_score}</div>
          <div className="text-[10px] text-muted-foreground">综合评分</div>
        </div>
        {one_line_verdict && (
          <div className="flex-1 text-sm leading-relaxed text-foreground">{one_line_verdict}</div>
        )}
      </div>

      <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
        {/* 亮点 */}
        <div>
          <h4 className="mb-2 text-xs font-semibold text-emerald-700 dark:text-emerald-300">
            ✨ 亮点 · {highlights.length}
          </h4>
          <ul className="space-y-2">
            {highlights.length === 0 && (
              <li className="text-xs text-muted-foreground">（未识别明显亮点）</li>
            )}
            {highlights.map((h, i) => (
              <li key={i} className="flex items-start gap-2 text-xs leading-relaxed">
                <span
                  className={cn(
                    'shrink-0 rounded px-1.5 py-0.5 text-[10px] font-medium',
                    ASPECT_COLOR[h.aspect],
                  )}
                >
                  {ASPECT_LABEL[h.aspect]}
                </span>
                <span className="flex-1">{h.text}</span>
              </li>
            ))}
          </ul>
        </div>

        {/* 改进建议 */}
        <div>
          <h4 className="mb-2 text-xs font-semibold text-amber-700 dark:text-amber-300">
            ⚙️ 改进建议 · {improvements.length}
          </h4>
          <ul className="space-y-2">
            {improvements.length === 0 && (
              <li className="text-xs text-muted-foreground">（无明显改进项，原片已较成熟）</li>
            )}
            {improvements.map((im, i) => (
              <li key={i} className="text-xs leading-relaxed">
                <div className="flex items-start gap-2">
                  <span
                    className={cn(
                      'shrink-0 rounded px-1.5 py-0.5 text-[10px] font-medium',
                      ASPECT_COLOR[im.aspect],
                    )}
                  >
                    {ASPECT_LABEL[im.aspect]}
                  </span>
                  <span className="flex-1">{im.text}</span>
                </div>
                <div className="ml-12 mt-0.5 text-[11px] text-muted-foreground">
                  → {im.suggestion}
                </div>
              </li>
            ))}
          </ul>
        </div>
      </div>
    </div>
  )
}
