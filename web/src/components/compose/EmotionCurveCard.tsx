/**
 * EmotionCurveCard —— Compose step3 工作坊的情绪曲线视图。
 *
 * 状态：
 * - 折叠：单行小条，「情绪走势 · 峰值 1:18 (92%) · ↻ 重算」
 * - 展开：320×120 LineChart + main_track section 色块底层 + 当前播放头红色竖线 + peaks/valleys ReferenceDot
 *
 * 重算：调 POST /plan/{id}/recompute-emotion，期间按钮变 spinner。
 * 过期徽标：当 plan.emotion_curve.computed_at < (max scene 任意编辑戳) 时显示「曲线可能过期」——
 *           我们没有 scene 级时间戳，简化为 "computed_at 与 plan.duration_seconds × 段数 的乘积比照"
 *           不可靠，故仅在 emotion_curve 为 rule_fallback 或 null 时显示提示徽标。
 */
import { useMemo, useState } from 'react'
import {
  CartesianGrid,
  Line,
  LineChart,
  ReferenceDot,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'

import { recomputeEmotion } from '@/api/plan'
import { cn } from '@/lib/utils'
import type { Plan } from '@/types/schemas'

interface Props {
  plan: Plan
  playheadSeconds: number
  onPlanUpdate: (next: Plan) => void
  className?: string
}

function fmtTime(sec: number): string {
  const m = Math.floor(sec / 60)
  const s = Math.floor(sec % 60)
  return `${m}:${s.toString().padStart(2, '0')}`
}

const ROLE_COLOR: Record<string, string> = {
  opening: 'rgba(99, 102, 241, 0.10)',     // indigo
  development: 'rgba(16, 185, 129, 0.10)',  // emerald
  climax: 'rgba(244, 63, 94, 0.14)',         // rose
  closing: 'rgba(148, 163, 184, 0.12)',      // slate
}

export function EmotionCurveCard({ plan, playheadSeconds, onPlanUpdate, className }: Props) {
  const [expanded, setExpanded] = useState(false)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)

  const curve = plan.emotion_curve ?? null
  const points = curve?.points ?? []
  const peaks = curve?.peaks ?? []
  const valleys = curve?.valleys ?? []
  const summary = curve?.summary ?? ''
  const isFallback = curve?.backend === 'rule_fallback'

  const data = useMemo(() => points.map((p) => ({ t: p.t, v: p.intensity })), [points])

  const topPeak = peaks.length
    ? [...peaks].sort((a, b) => b.intensity - a.intensity)[0]
    : null

  // section 色块底层：按 main_track Scene 的 [start, start+duration] 染色
  const bands = useMemo(() => {
    return plan.main_track.map((sc) => ({
      x1: sc.start,
      x2: sc.start + sc.duration,
      role: sc.section,
    }))
  }, [plan.main_track])

  async function handleRecompute() {
    if (busy) return
    setBusy(true)
    setErr(null)
    try {
      const fresh = await recomputeEmotion(plan.plan_id)
      onPlanUpdate(fresh)
    } catch (e) {
      setErr(e instanceof Error ? e.message : '重算失败')
    } finally {
      setBusy(false)
    }
  }

  if (!curve || points.length === 0) {
    return (
      <div
        className={cn(
          'flex items-center gap-2 rounded-md border border-border bg-card px-3 py-1.5 text-[11px] text-muted-foreground',
          className,
        )}
      >
        <span>情绪走势 · 暂未生成</span>
        <button
          type="button"
          onClick={handleRecompute}
          disabled={busy}
          className="ml-auto rounded-md border border-border bg-background px-2 py-0.5 text-[11px] hover:bg-secondary disabled:opacity-50"
        >
          {busy ? '⏳ 重算中…' : '↻ 重算'}
        </button>
      </div>
    )
  }

  return (
    <div className={cn('rounded-lg border border-border bg-card text-sm', className)}>
      {/* 折叠/展开头部 */}
      <button
        type="button"
        onClick={() => setExpanded((v) => !v)}
        className="flex w-full items-center gap-2 px-3 py-1.5 text-left text-[12px]"
      >
        <span className="inline-block h-2 w-2 rounded-full bg-violet-600" />
        <span className="font-medium text-foreground">情绪走势</span>
        {topPeak && (
          <span className="text-[11px] text-muted-foreground">
            · 峰值 {fmtTime(topPeak.t)} ({Math.round(topPeak.intensity * 100)}%)
          </span>
        )}
        {curve.signals_used && curve.signals_used.length > 0 && (
          <span className="text-[10px] text-muted-foreground">· {curve.signals_used.length} 个信号</span>
        )}
        {isFallback && (
          <span className="rounded-full bg-amber-500/15 px-1.5 py-0.5 text-[10px] text-amber-700 dark:text-amber-300">
            规则兜底
          </span>
        )}
        <span className="ml-auto inline-flex items-center gap-2">
          <span
            role="button"
            tabIndex={0}
            onClick={(e) => {
              e.stopPropagation()
              handleRecompute()
            }}
            onKeyDown={(e) => {
              if (e.key === 'Enter' || e.key === ' ') {
                e.preventDefault()
                e.stopPropagation()
                handleRecompute()
              }
            }}
            aria-disabled={busy}
            className={cn(
              'rounded-md border border-border bg-background px-2 py-0.5 text-[11px] hover:bg-secondary',
              busy && 'opacity-50',
            )}
          >
            {busy ? '⏳' : '↻'} 重算
          </span>
          <span className="text-[10px] text-muted-foreground">{expanded ? '收起 ▴' : '展开 ▾'}</span>
        </span>
      </button>

      {expanded && (
        <div className="border-t border-border px-3 pb-3 pt-2">
          <div className="h-32 w-full">
            <ResponsiveContainer width="100%" height="100%">
              <LineChart data={data} margin={{ top: 6, right: 8, bottom: 4, left: 0 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="hsl(240 6% 90%)" />
                <XAxis
                  dataKey="t"
                  type="number"
                  domain={[0, plan.duration_seconds]}
                  tickFormatter={(v: number) => fmtTime(v)}
                  tick={{ fontSize: 10, fill: 'hsl(240 4% 46%)' }}
                />
                <YAxis
                  domain={[0, 1]}
                  tickFormatter={(v: number) => `${Math.round(v * 100)}%`}
                  tick={{ fontSize: 10, fill: 'hsl(240 4% 46%)' }}
                  width={32}
                />
                <Tooltip
                  formatter={(value) => {
                    if (typeof value !== 'number') return String(value ?? '')
                    return [`${Math.round(value * 100)}%`, '情绪强度']
                  }}
                  labelFormatter={(label) => (typeof label === 'number' ? `t=${fmtTime(label)}` : String(label))}
                  contentStyle={{ fontSize: 12 }}
                />
                {/* section 色块底层：每段 ReferenceLine 用 stroke 模拟（recharts 没有 ReferenceArea x1/x2 的颜色填充原生表现，
                    但 ReferenceLine 在 LineChart 里依然能用渐变。这里简化为分段 ReferenceLine。） */}
                {bands.map((b, i) => (
                  <ReferenceLine
                    key={`band-${i}`}
                    segment={[
                      { x: b.x1, y: 0 },
                      { x: b.x2, y: 0 },
                    ]}
                    stroke={ROLE_COLOR[b.role] ?? 'rgba(148,163,184,0.10)'}
                    strokeWidth={6}
                    ifOverflow="extendDomain"
                  />
                ))}
                <Line
                  type="monotone"
                  dataKey="v"
                  stroke="hsl(265 87% 56%)"
                  strokeWidth={2.5}
                  dot={false}
                  isAnimationActive={false}
                />
                <ReferenceLine
                  x={Math.max(0, Math.min(plan.duration_seconds, playheadSeconds))}
                  stroke="hsl(0 84% 60%)"
                  strokeWidth={1.2}
                  strokeDasharray="2 2"
                />
                {peaks.map((pk, i) => (
                  <ReferenceDot
                    key={`peak-${i}`}
                    x={pk.t}
                    y={pk.intensity}
                    r={4}
                    fill="hsl(0 84% 55%)"
                    stroke="white"
                    strokeWidth={1}
                  >
                    <title>{`高潮 ${fmtTime(pk.t)} · ${Math.round(pk.intensity * 100)}%${pk.reason ? ` · ${pk.reason}` : ''}`}</title>
                  </ReferenceDot>
                ))}
                {valleys.map((vy, i) => (
                  <ReferenceDot
                    key={`valley-${i}`}
                    x={vy.t}
                    y={vy.intensity}
                    r={3.5}
                    fill="hsl(240 5% 50%)"
                    stroke="white"
                    strokeWidth={1}
                  >
                    <title>{`低谷 ${fmtTime(vy.t)} · ${Math.round(vy.intensity * 100)}%${vy.reason ? ` · ${vy.reason}` : ''}`}</title>
                  </ReferenceDot>
                ))}
              </LineChart>
            </ResponsiveContainer>
          </div>

          {summary && (
            <p className="mt-2 rounded-md bg-violet-500/10 px-2 py-1.5 text-[11px] leading-relaxed text-violet-900 dark:text-violet-200">
              {summary}
            </p>
          )}

          {curve.signals_used && curve.signals_used.length > 0 && (
            <div className="mt-1 flex flex-wrap items-center gap-1">
              <span className="text-[10px] text-muted-foreground">信号：</span>
              {curve.signals_used.map((s) => (
                <span
                  key={s}
                  className="rounded-full bg-secondary/60 px-1.5 py-0.5 text-[10px] text-muted-foreground"
                >
                  {s}
                </span>
              ))}
            </div>
          )}

          {err && (
            <p className="mt-1 text-[11px] text-rose-600 dark:text-rose-400">{err}</p>
          )}
        </div>
      )}
    </div>
  )
}
