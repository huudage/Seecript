import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Link } from 'react-router-dom'

import { api } from '@/api/client'
import { createSSE } from '@/api/sse'
import { PageShell } from '@/components/layout/PageShell'
import { useEditStore } from '@/stores/edit'
import { usePlanStore } from '@/stores/plan'
import type {
  EditApplyRequest,
  PackagingItem,
  Plan,
  RenderDonePayload,
  RenderSubmitResponse,
  SectionKind,
  Variant,
} from '@/types/schemas'
import { cn } from '@/lib/utils'

/* -------------------------------------------------------------------------- */
/* 配色 / 常量                                                                 */
/* -------------------------------------------------------------------------- */

const SECTION_COLOR: Record<SectionKind, string> = {
  hook: 'bg-pink-500',
  body: 'bg-sky-500',
  cta: 'bg-amber-500',
}
const PKG_COLOR: Record<PackagingItem['kind'], string> = {
  subtitle: 'bg-emerald-500',
  title_bar: 'bg-indigo-500',
  sticker: 'bg-fuchsia-500',
  transition: 'bg-zinc-400',
  cover: 'bg-rose-500',
}
const PKG_LABEL: Record<PackagingItem['kind'], string> = {
  subtitle: '字幕',
  title_bar: '标题条',
  sticker: '贴纸',
  transition: '转场',
  cover: '封面',
}

const RENDER_STEP_LABELS: Record<string, string> = {
  prepare: '准备',
  ffmpeg_concat: 'FFmpeg 主轨拼接',
  seedance_extend: 'Seedance 首尾帧扩展',
  remotion_render: 'Remotion 包装渲染',
  ffmpeg_overlay: 'FFmpeg 叠加输出',
  finalize: '收尾',
}
const RENDER_STEP_ORDER = [
  'prepare',
  'ffmpeg_concat',
  'seedance_extend',
  'remotion_render',
  'ffmpeg_overlay',
  'finalize',
] as const

/* -------------------------------------------------------------------------- */
/* 主页面                                                                       */
/* -------------------------------------------------------------------------- */

export default function RenderPage() {
  const plan = usePlanStore((s) => s.plan)
  const setPlan = usePlanStore((s) => s.setPlan)
  const variant = usePlanStore((s) => s.variant)
  const setVariant = usePlanStore((s) => s.setVariant)

  const editHistory = useEditStore((s) => s.history)
  const editCursor = useEditStore((s) => s.cursor)
  const pushEdit = useEditStore((s) => s.push)
  const undoEdit = useEditStore((s) => s.undo)
  const redoEdit = useEditStore((s) => s.redo)

  // 初次进入：将当前 plan 推入 undo 栈作为起点
  useEffect(() => {
    if (plan && editHistory.length === 0) pushEdit(plan)
  }, [plan, editHistory.length, pushEdit])

  const [jobId, setJobId] = useState<string | null>(null)
  const [step, setStep] = useState<string>('idle')
  const [percent, setPercent] = useState(0)
  const [done, setDone] = useState<RenderDonePayload | null>(null)
  const [error, setError] = useState<string | null>(null)
  const sseRef = useRef<ReturnType<typeof createSSE> | null>(null)

  useEffect(() => () => sseRef.current?.close(), [])

  const handleSubmit = useCallback(async () => {
    if (!plan) return
    setError(null)
    setDone(null)
    setPercent(0)
    setStep('submit')
    try {
      const resp = await api.post<RenderSubmitResponse>('/render/submit', {
        plan_id: plan.plan_id,
        variant,
      })
      setJobId(resp.job_id)
      sseRef.current?.close()
      sseRef.current = createSSE<RenderDonePayload>(`/render/stream?job_id=${resp.job_id}`, {
        onProgress: (p) => {
          setStep(p.step)
          setPercent(p.percent)
        },
        onDone: (d) => {
          setDone(d)
          setStep('done')
          setPercent(100)
        },
        onError: (e) => setError(e.detail),
      })
    } catch (err) {
      setError(err instanceof Error ? err.message : '提交失败')
      setStep('idle')
    }
  }, [plan, variant])

  /* -- 自然语言编辑 -- */
  const [instruction, setInstruction] = useState('')
  const [applying, setApplying] = useState(false)
  const [editError, setEditError] = useState<string | null>(null)
  const [markStart, setMarkStart] = useState('')
  const [markEnd, setMarkEnd] = useState('')
  const [markTrack, setMarkTrack] = useState<'main' | 'packaging'>('main')

  const handleApplyEdit = useCallback(async () => {
    if (!plan || !instruction.trim()) return
    setApplying(true)
    setEditError(null)
    try {
      const marks: EditApplyRequest['marks'] = []
      const s = parseFloat(markStart)
      const e = parseFloat(markEnd)
      if (!Number.isNaN(s) && !Number.isNaN(e) && e > s) {
        marks.push({ track: markTrack, start: s, end: e })
      }
      const newPlan = await api.post<Plan>('/edit/apply', {
        plan_id: plan.plan_id,
        instruction: instruction.trim(),
        marks,
      } satisfies EditApplyRequest)
      setPlan(newPlan)
      pushEdit(newPlan)
      setInstruction('')
    } catch (err) {
      setEditError(err instanceof Error ? err.message : '编辑失败')
    } finally {
      setApplying(false)
    }
  }, [instruction, markEnd, markStart, markTrack, plan, pushEdit, setPlan])

  const handleUndo = useCallback(() => {
    const p = undoEdit()
    if (p) setPlan(p)
  }, [setPlan, undoEdit])

  const handleRedo = useCallback(() => {
    const p = redoEdit()
    if (p) setPlan(p)
  }, [redoEdit, setPlan])

  if (!plan) {
    return (
      <PageShell title="生成 / 自然语言编辑" subtitle="还没有 Plan。">
        <div className="rounded-lg border border-dashed border-border bg-card p-8 text-sm text-muted-foreground">
          请先在
          <Link to="/compose" className="mx-1 text-primary underline-offset-4 hover:underline">
            新素材 / 缺口
          </Link>
          页构建 Plan。
        </div>
      </PageShell>
    )
  }

  const canUndo = editCursor > 0
  const canRedo = editCursor >= 0 && editCursor < editHistory.length - 1
  const isRendering = jobId !== null && !done && !error
  const totalDuration = plan.duration_seconds || 1

  return (
    <PageShell
      title="生成 / 自然语言编辑"
      subtitle={`Plan ${plan.plan_id} · ${plan.main_track.length} scene · ${plan.packaging_track.length} 包装 · 总时长 ${plan.duration_seconds.toFixed(1)}s`}
    >
      {/* ---- 顶部：变体切换 + 提交按钮 ---- */}
      <section className="mb-4 flex flex-wrap items-center gap-3 rounded-lg border border-border bg-card p-4">
        <span className="text-sm font-medium">变体</span>
        <div className="flex overflow-hidden rounded-md border border-border">
          {(['A', 'B'] as Variant[]).map((v) => (
            <button
              key={v}
              onClick={() => setVariant(v)}
              className={cn(
                'px-4 py-1.5 text-sm transition-colors',
                variant === v ? 'bg-primary text-primary-foreground' : 'bg-background hover:bg-secondary',
              )}
            >
              {v}
            </button>
          ))}
        </div>
        <div className="ml-auto flex items-center gap-2">
          {error && <span className="text-xs text-destructive">{error}</span>}
          <button
            onClick={handleSubmit}
            disabled={isRendering}
            className={cn(
              'rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground',
              isRendering && 'cursor-not-allowed opacity-60',
            )}
          >
            {isRendering ? `渲染中 · ${percent}%` : done ? '重新渲染' : '提交渲染'}
          </button>
        </div>
      </section>

      <div className="grid grid-cols-1 gap-4 xl:grid-cols-[1.4fr_1fr]">
        {/* ============ 左 · 渲染进度 + 视频预览 ============ */}
        <section className="rounded-lg border border-border bg-card p-4">
          <h2 className="mb-3 text-sm font-semibold">模块 5 · 渲染流水线</h2>
          <RenderProgress step={step} percent={percent} />
          {done ? (
            <RenderResult done={done} />
          ) : (
            <div className="mt-4 rounded-md border border-dashed border-border p-6 text-center text-xs text-muted-foreground">
              {isRendering ? '渲染流水线运行中…' : '点击「提交渲染」开始生成。'}
            </div>
          )}
        </section>

        {/* ============ 右 · 包装轨可视化 + Plan 摘要 ============ */}
        <section className="space-y-4">
          <div className="rounded-lg border border-border bg-card p-4">
            <h2 className="mb-3 text-sm font-semibold">模块 6 · 主轨 + 包装轨</h2>
            <TimelineTrack
              label="主轨"
              duration={totalDuration}
              items={plan.main_track.map((sc) => ({
                key: sc.scene_id,
                start: sc.start,
                end: sc.start + sc.duration,
                color: SECTION_COLOR[sc.section],
                text: `${sc.section} · ${sc.scene_id}`,
              }))}
            />
            <div className="my-2 h-px bg-border" />
            <TimelineTrack
              label="包装轨"
              duration={totalDuration}
              items={plan.packaging_track.map((it) => ({
                key: it.item_id,
                start: it.start,
                end: it.end,
                color: PKG_COLOR[it.kind],
                text: `${PKG_LABEL[it.kind]}${it.text ? ` · ${it.text.slice(0, 12)}` : ''}`,
              }))}
              empty="尚无包装 item"
            />
            <PackagingLegend />
          </div>

          <div className="rounded-lg border border-border bg-card p-4">
            <h2 className="mb-2 text-sm font-semibold">Plan 摘要</h2>
            <ul className="space-y-1 text-xs">
              {plan.main_track.map((sc) => (
                <li key={sc.scene_id} className="flex items-start gap-2">
                  <span
                    className={cn('mt-0.5 inline-block h-2 w-2 rounded-full', SECTION_COLOR[sc.section])}
                  />
                  <span className="font-mono text-[11px] text-muted-foreground">
                    {sc.scene_id}
                  </span>
                  <span className="text-muted-foreground">·</span>
                  <span>{sc.duration.toFixed(1)}s</span>
                  <span className="text-muted-foreground">·</span>
                  <span className="truncate text-foreground" title={sc.narration ?? ''}>
                    {sc.narration ?? <em className="text-muted-foreground">无口播</em>}
                  </span>
                </li>
              ))}
            </ul>
          </div>
        </section>
      </div>

      {/* ============ 底部 · 自然语言编辑 ============ */}
      <section className="mt-4 rounded-lg border border-border bg-card p-4">
        <div className="mb-2 flex items-center justify-between">
          <h2 className="text-sm font-semibold">模块 7 · 自然语言编辑</h2>
          <div className="flex items-center gap-2">
            <span className="text-xs text-muted-foreground">
              历史 {Math.max(editCursor + 1, 0)}/{editHistory.length}
            </span>
            <button
              onClick={handleUndo}
              disabled={!canUndo}
              className={cn(
                'rounded-md border border-border bg-background px-3 py-1 text-xs hover:bg-secondary',
                !canUndo && 'cursor-not-allowed opacity-40',
              )}
            >
              ↶ 撤销
            </button>
            <button
              onClick={handleRedo}
              disabled={!canRedo}
              className={cn(
                'rounded-md border border-border bg-background px-3 py-1 text-xs hover:bg-secondary',
                !canRedo && 'cursor-not-allowed opacity-40',
              )}
            >
              重做 ↷
            </button>
          </div>
        </div>

        <div className="grid grid-cols-1 gap-3 lg:grid-cols-[1fr_280px]">
          <div>
            <textarea
              value={instruction}
              onChange={(e) => setInstruction(e.target.value)}
              placeholder="例如：把 Hook 改得更口语化；缩短 cta-1 到 3 秒；BGM 调到 0.3；替换 body-2 的素材为 m-xxx"
              rows={3}
              className="w-full rounded-md border border-border bg-background px-3 py-2 text-sm placeholder:text-muted-foreground focus:outline-none focus:ring-1 focus:ring-primary"
            />
            <div className="mt-2 flex items-center gap-2 text-xs text-muted-foreground">
              <span>提示词会和当前 Plan + marks 一起发给 LLM；模型会调用最匹配的 1–3 个原子 tool。</span>
            </div>
          </div>
          <div className="space-y-2 text-xs">
            <div>
              <label className="text-muted-foreground">marks（可选区段）</label>
              <div className="mt-1 flex gap-2">
                <input
                  type="number"
                  placeholder="start"
                  value={markStart}
                  onChange={(e) => setMarkStart(e.target.value)}
                  className="w-20 rounded-md border border-border bg-background px-2 py-1"
                />
                <span className="self-center text-muted-foreground">–</span>
                <input
                  type="number"
                  placeholder="end"
                  value={markEnd}
                  onChange={(e) => setMarkEnd(e.target.value)}
                  className="w-20 rounded-md border border-border bg-background px-2 py-1"
                />
                <span className="self-center text-muted-foreground">秒</span>
              </div>
            </div>
            <div className="flex items-center gap-3">
              <label className="text-muted-foreground">轨道</label>
              {(['main', 'packaging'] as const).map((t) => (
                <label key={t} className="flex items-center gap-1">
                  <input
                    type="radio"
                    checked={markTrack === t}
                    onChange={() => setMarkTrack(t)}
                  />
                  {t === 'main' ? '主轨' : '包装'}
                </label>
              ))}
            </div>
            <button
              onClick={handleApplyEdit}
              disabled={applying || !instruction.trim()}
              className={cn(
                'mt-2 w-full rounded-md bg-primary px-3 py-2 text-sm font-medium text-primary-foreground',
                (applying || !instruction.trim()) && 'cursor-not-allowed opacity-60',
              )}
            >
              {applying ? '应用中…' : '应用编辑'}
            </button>
          </div>
        </div>
        {editError && (
          <p className="mt-2 text-xs text-destructive">{editError}</p>
        )}
      </section>
    </PageShell>
  )
}

/* -------------------------------------------------------------------------- */
/* 子组件                                                                       */
/* -------------------------------------------------------------------------- */

function RenderProgress({ step, percent }: { step: string; percent: number }) {
  const currentIdx = RENDER_STEP_ORDER.indexOf(step as (typeof RENDER_STEP_ORDER)[number])
  return (
    <div>
      <div className="mb-2 flex items-center justify-between text-xs">
        <span className="font-mono text-muted-foreground">
          {step === 'idle' ? '待命' : step === 'done' ? '完成' : RENDER_STEP_LABELS[step] ?? step}
        </span>
        <span className="font-mono text-muted-foreground">{percent}%</span>
      </div>
      <div className="h-2 overflow-hidden rounded-full bg-secondary">
        <div
          className="h-full bg-primary transition-all duration-300"
          style={{ width: `${Math.min(100, percent)}%` }}
        />
      </div>
      <ol className="mt-3 grid grid-cols-3 gap-1 text-[11px] sm:grid-cols-6">
        {RENDER_STEP_ORDER.map((s, i) => (
          <li
            key={s}
            className={cn(
              'rounded border px-1.5 py-1 text-center',
              i < currentIdx
                ? 'border-emerald-500/40 bg-emerald-500/10 text-emerald-700 dark:text-emerald-300'
                : i === currentIdx
                  ? 'border-primary bg-primary/10 text-primary'
                  : 'border-border bg-background text-muted-foreground',
            )}
          >
            {RENDER_STEP_LABELS[s]}
          </li>
        ))}
      </ol>
    </div>
  )
}

function RenderResult({ done }: { done: RenderDonePayload }) {
  return (
    <div className="mt-4 space-y-3">
      <video
        controls
        poster={done.cover_url}
        src={done.video_url}
        className="w-full rounded-md border border-border bg-black"
      />
      <div className="grid grid-cols-2 gap-2 text-xs sm:grid-cols-4">
        <Stat label="时长" value={`${done.duration_seconds.toFixed(1)}s`} />
        <Stat label="variant" value={done.variant} />
        <Stat label="plan_id" value={done.plan_id} mono />
        <Stat
          label="总耗时"
          value={
            done.timings_ms
              ? `${Math.round(Object.values(done.timings_ms).reduce((a, b) => a + b, 0))} ms`
              : '—'
          }
        />
      </div>
      {done.timings_ms && (
        <details className="text-xs">
          <summary className="cursor-pointer text-muted-foreground">分步耗时</summary>
          <ul className="mt-2 grid grid-cols-2 gap-1 font-mono">
            {Object.entries(done.timings_ms).map(([k, v]) => (
              <li key={k} className="flex justify-between rounded bg-background/50 px-2 py-1">
                <span>{RENDER_STEP_LABELS[k] ?? k}</span>
                <span>{Math.round(v)} ms</span>
              </li>
            ))}
          </ul>
        </details>
      )}
      {done.notes && done.notes.length > 0 && (
        <details className="text-xs">
          <summary className="cursor-pointer text-muted-foreground">流水线日志（{done.notes.length}）</summary>
          <ul className="mt-2 space-y-0.5 font-mono text-[11px] text-muted-foreground">
            {done.notes.map((n, i) => (
              <li key={i}>· {n}</li>
            ))}
          </ul>
        </details>
      )}
    </div>
  )
}

function Stat({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  return (
    <div className="rounded-md border border-border bg-background/40 px-2 py-1.5">
      <div className="text-[10px] uppercase tracking-wider text-muted-foreground">{label}</div>
      <div className={cn('truncate text-sm', mono && 'font-mono')}>{value}</div>
    </div>
  )
}

interface TimelineItem {
  key: string
  start: number
  end: number
  color: string
  text: string
}

function TimelineTrack({
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
        <span className="font-mono">{duration.toFixed(1)}s · {items.length}</span>
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

function PackagingLegend() {
  return (
    <div className="mt-2 flex flex-wrap gap-2 text-[11px] text-muted-foreground">
      {(Object.keys(PKG_LABEL) as PackagingItem['kind'][]).map((k) => (
        <span key={k} className="inline-flex items-center gap-1">
          <i className={cn('inline-block h-2 w-2 rounded-sm', PKG_COLOR[k])} />
          {PKG_LABEL[k]}
        </span>
      ))}
    </div>
  )
}
