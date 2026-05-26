import type { Plan } from '@/types/schemas'
import { SECTION_BG, SECTION_SHORT } from '@/lib/sections'
import { cn } from '@/lib/utils'

/**
 * 底部"适配结果预览"：scene 卡片横向滚动条带。
 *
 * - 每张卡上方一根 section 色条（SECTION_BG）+ 段落简称
 * - 主体：source（sample / user_material / aigc_t2v）+ source_ref 短化 + duration
 * - narration 显示在底部（最多 2 行）；点击卡片高亮 onPick(scene_id) 可联动选中
 */
export function StoryboardPreview({
  plan,
  highlightedSceneId,
  onPick,
}: {
  plan: Plan
  highlightedSceneId?: string | null
  onPick?: (sceneId: string) => void
}) {
  if (!plan.main_track.length) {
    return (
      <div className="rounded-md border border-dashed border-border bg-background/30 p-6 text-center text-xs text-muted-foreground">
        Plan 主轨为空——先点「智能分析」构建 Plan。
      </div>
    )
  }

  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between text-xs text-muted-foreground">
        <span>
          适配结果预览 · {plan.main_track.length} scene · {plan.duration_seconds.toFixed(1)}s
        </span>
        <span className="font-mono">
          plan_id <span className="text-foreground">{plan.plan_id}</span>
        </span>
      </div>
      <div className="flex gap-2 overflow-x-auto pb-1">
        {plan.main_track.map((sc, i) => {
          const active = sc.scene_id === highlightedSceneId
          return (
            <button
              key={sc.scene_id}
              onClick={() => onPick?.(sc.scene_id)}
              className={cn(
                'flex w-44 shrink-0 flex-col gap-1 rounded-md border bg-background/50 p-2 text-left transition-colors',
                active
                  ? 'border-primary bg-primary/5'
                  : 'border-border hover:border-primary/50 hover:bg-secondary/40',
              )}
            >
              <div className="flex items-center gap-1 text-[10px]">
                <span className="font-mono text-muted-foreground">#{i + 1}</span>
                <span
                  className={cn('rounded px-1 py-0.5 font-medium text-white', SECTION_BG[sc.section])}
                >
                  {SECTION_SHORT[sc.section]}
                </span>
                <span className="ml-auto font-mono text-muted-foreground">
                  {sc.duration.toFixed(1)}s
                </span>
              </div>
              <p className="truncate text-[11px] text-foreground" title={sc.source_ref}>
                <SourceBadge source={sc.source} /> {shortRef(sc.source_ref)}
              </p>
              {sc.narration ? (
                <p className="line-clamp-2 text-[11px] text-muted-foreground" title={sc.narration}>
                  {sc.narration}
                </p>
              ) : (
                <p className="text-[11px] italic text-muted-foreground/60">无口播</p>
              )}
            </button>
          )
        })}
      </div>
    </div>
  )
}

function SourceBadge({ source }: { source: 'sample' | 'user_material' | 'aigc_t2v' }) {
  const cls =
    source === 'sample'
      ? 'bg-sky-500/15 text-sky-700 dark:text-sky-300'
      : source === 'user_material'
        ? 'bg-emerald-500/15 text-emerald-700 dark:text-emerald-300'
        : 'bg-fuchsia-500/15 text-fuchsia-700 dark:text-fuchsia-300'
  const label = source === 'sample' ? '样例' : source === 'user_material' ? '我的' : 'AIGC'
  return <span className={cn('mr-1 rounded px-1 py-px text-[9px] font-medium', cls)}>{label}</span>
}

function shortRef(ref: string): string {
  if (ref.length <= 14) return ref
  return `${ref.slice(0, 6)}…${ref.slice(-6)}`
}
