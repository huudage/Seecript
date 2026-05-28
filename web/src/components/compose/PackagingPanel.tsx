import { useState } from 'react'

import { api } from '@/api/client'
import { SECTION_BG, SECTION_SHORT } from '@/lib/sections'
import { cn } from '@/lib/utils'
import type {
  PackagingRecommendation,
  PackagingRecommendRequest,
  Plan,
  TransitionStyle,
} from '@/types/schemas'

const TRANSITION_LABEL: Record<TransitionStyle, string> = {
  hard_cut: '硬切',
  dissolve: '溶解',
  slide: '滑动',
  zoom: '推拉',
  whip: '甩切',
  wipe: '扫切',
}

const TRANSITION_TONE: Record<TransitionStyle, string> = {
  hard_cut: 'bg-slate-200 text-slate-700',
  dissolve: 'bg-sky-200 text-sky-800',
  slide: 'bg-amber-200 text-amber-800',
  zoom: 'bg-rose-200 text-rose-800',
  whip: 'bg-yellow-200 text-yellow-900',
  wipe: 'bg-emerald-200 text-emerald-800',
}

/**
 * 包装推荐面板：调 POST /api/packaging/recommend，apply=true 把转场+封面写到 plan.packaging_track。
 * 推荐成功后回调 onPlanUpdated 让父级重拉 plan，storyboard 同步。
 */
export function PackagingPanel({
  plan,
  onPlanUpdated,
}: {
  plan: Plan
  onPlanUpdated?: (plan: Plan) => void
}) {
  const [running, setRunning] = useState(false)
  const [rec, setRec] = useState<PackagingRecommendation | null>(null)
  const [error, setError] = useState<string | null>(null)

  const run = async () => {
    setRunning(true)
    setError(null)
    try {
      const body: PackagingRecommendRequest = { plan_id: plan.plan_id, apply: true }
      const resp = await api.post<PackagingRecommendation>('/packaging/recommend', body)
      setRec(resp)
      // apply=true 服务端已写回 plan_store；前端拉一遍最新版本，让 storyboard / 渲染共用
      try {
        const fresh = await api.get<Plan>(`/plan/${plan.plan_id}`)
        onPlanUpdated?.(fresh)
      } catch {
        /* 拉新版失败不阻塞展示 */
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : '包装推荐失败')
    } finally {
      setRunning(false)
    }
  }

  return (
    <section className="space-y-3 rounded-lg border border-border bg-card p-4">
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-sm font-semibold">包装推荐 · 转场 + 封面</h2>
          <p className="text-[11px] text-muted-foreground">
            LLM 看完主轨之后，给每段切换挑一种转场，再写一份开场封面，自动落到 packaging_track。
          </p>
        </div>
        <button
          onClick={() => void run()}
          disabled={running}
          className={cn(
            'rounded-md bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground transition-colors',
            running && 'cursor-not-allowed opacity-60',
          )}
        >
          {running ? '推荐中…' : rec ? '重新推荐' : '一键包装'}
        </button>
      </div>

      {error && (
        <div className="rounded-md border border-destructive/40 bg-destructive/5 px-3 py-2 text-xs text-destructive">
          {error}
        </div>
      )}

      {rec && (
        <div className="space-y-3">
          {rec.cover && <CoverPreview cover={rec.cover} />}
          {rec.transitions.length > 0 && (
            <div className="space-y-1.5">
              <h3 className="text-xs font-semibold text-muted-foreground">段落转场（{rec.transitions.length}）</h3>
              <ul className="space-y-1.5">
                {rec.transitions.map((t) => (
                  <li
                    key={t.item_id}
                    className="flex flex-wrap items-center gap-2 rounded-md border border-border bg-background/40 px-2.5 py-1.5 text-xs"
                  >
                    <span className="font-mono text-[11px] text-muted-foreground">
                      {t.at_seconds.toFixed(1)}s
                    </span>
                    <span
                      className={cn(
                        'rounded px-1.5 py-0.5 text-[10px] font-medium text-white',
                        SECTION_BG[t.from_section],
                      )}
                    >
                      {SECTION_SHORT[t.from_section]}
                    </span>
                    <span className="text-muted-foreground">→</span>
                    <span
                      className={cn(
                        'rounded px-1.5 py-0.5 text-[10px] font-medium text-white',
                        SECTION_BG[t.to_section],
                      )}
                    >
                      {SECTION_SHORT[t.to_section]}
                    </span>
                    <span
                      className={cn(
                        'rounded px-1.5 py-0.5 text-[10px] font-medium',
                        TRANSITION_TONE[t.style],
                      )}
                      title={t.style}
                    >
                      {TRANSITION_LABEL[t.style]} · {t.duration.toFixed(1)}s
                    </span>
                    <span className="min-w-0 flex-1 truncate text-muted-foreground" title={t.reason}>
                      {t.reason}
                    </span>
                  </li>
                ))}
              </ul>
            </div>
          )}
          {rec.notes.length > 0 && (
            <ul className="space-y-0.5 text-[10px] text-muted-foreground">
              {rec.notes.map((n, i) => (
                <li key={i}>· {n}</li>
              ))}
            </ul>
          )}
        </div>
      )}

      {!rec && !running && !error && (
        <p className="rounded-md border border-dashed border-border bg-background/30 px-3 py-2 text-[11px] text-muted-foreground">
          点击右上方按钮：LLM 会基于当前 plan 的段落顺序与你的主题，写一份转场表 + 一份封面方案。
        </p>
      )}
    </section>
  )
}

function CoverPreview({ cover }: { cover: NonNullable<PackagingRecommendation['cover']> }) {
  const bg = cover.palette[1] ?? '#1F2937'
  const accent = cover.palette[0] ?? '#FFE600'
  const sub = cover.palette[2] ?? '#FFFFFF'
  const isLeft = cover.layout === 'left' || cover.layout === 'stacked'

  return (
    <div className="space-y-1.5">
      <h3 className="text-xs font-semibold text-muted-foreground">封面方案</h3>
      <div className="flex items-stretch gap-3">
        <div
          className="relative h-32 w-56 shrink-0 overflow-hidden rounded-md border border-border"
          style={{
            backgroundColor: bg,
            display: 'flex',
            flexDirection: 'column',
            justifyContent: 'center',
            alignItems: isLeft ? 'flex-start' : 'center',
            padding: '0 14px',
          }}
        >
          {cover.layout === 'split' && (
            <div
              className="absolute right-0 top-0 bottom-0 w-2/5"
              style={{ backgroundColor: accent }}
            />
          )}
          <div
            style={{
              color: cover.layout === 'split' ? sub : accent,
              fontSize: 22,
              fontWeight: 900,
              lineHeight: 1.1,
              textAlign: isLeft ? 'left' : 'center',
              zIndex: 2,
            }}
          >
            {cover.title}
          </div>
          {cover.subtitle && (
            <div
              style={{
                color: sub,
                fontSize: 11,
                fontWeight: 500,
                marginTop: 6,
                opacity: 0.85,
                zIndex: 2,
              }}
            >
              {cover.subtitle}
            </div>
          )}
        </div>
        <div className="flex flex-1 flex-col gap-1 text-xs">
          <div className="flex items-center gap-1.5">
            <span className="text-muted-foreground">布局</span>
            <span className="rounded bg-secondary px-1.5 py-0.5 text-[10px] font-medium">
              {cover.layout}
            </span>
          </div>
          <div className="flex items-center gap-1.5">
            <span className="text-muted-foreground">主色</span>
            {cover.palette.map((c) => (
              <span
                key={c}
                className="inline-block h-4 w-8 rounded border border-border"
                style={{ backgroundColor: c }}
                title={c}
              />
            ))}
          </div>
          <p className="text-[11px] text-muted-foreground">{cover.style_note}</p>
        </div>
      </div>
    </div>
  )
}
