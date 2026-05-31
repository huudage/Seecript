import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Link } from 'react-router-dom'

import { api } from '@/api/client'
import { deletePlanBgm, patchPlanBgm } from '@/api/bgm'
import { patchPlanSettings } from '@/api/plan'
import { commitStep } from '@/api/steps'
import { createSSE } from '@/api/sse'
import { deleteVoice, synthesizeAll, synthesizeOne } from '@/api/voice'
import { BgmPickerDialog } from '@/components/compose/BgmPickerDialog'
import { FourTrackBoard } from '@/components/compose/FourTrackBoard'
import { NLEditPanel } from '@/components/edit/NLEditPanel'
import { PageShell } from '@/components/layout/PageShell'
import { useEditStore } from '@/stores/edit'
import { usePlanStore } from '@/stores/plan'
import { useProjectsStore } from '@/stores/projects'
import { useSessionStore } from '@/stores/session'
import type {
  PackagingRecommendRequest,
  Plan,
  RenderDonePayload,
  RenderSubmitResponse,
  Scene,
  Variant,
} from '@/types/schemas'
import { cn } from '@/lib/utils'

/* -------------------------------------------------------------------------- */
/* 配色 / 常量                                                                 */
/* -------------------------------------------------------------------------- */

import type { SectionRole } from '@/types/schemas'

// 主轨 4 个 section role → Tailwind 背景类。和 lib/sections.SECTION_BG 同色系，
// 但 Render 时间线使用更深的纯色（无透明）以保证文字对比度。
const SECTION_COLOR: Record<SectionRole, string> = {
  opening: 'bg-blue-500',
  development: 'bg-slate-500',
  climax: 'bg-rose-500',
  closing: 'bg-emerald-500',
}

const RENDER_STEP_LABELS: Record<string, string> = {
  prepare: '准备',
  ffmpeg_concat: 'FFmpeg 主轨拼接',
  seedance_extend: '主轨直通',
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
  const gaps = usePlanStore((s) => s.gaps)
  const fills = usePlanStore((s) => s.fills)
  const variant = usePlanStore((s) => s.variant)
  const setVariant = usePlanStore((s) => s.setVariant)

  const currentProjectId = useProjectsStore((s) => s.currentProjectId)
  const refreshProjects = useProjectsStore((s) => s.refresh)

  const setSettings = useSessionStore((s) => s.setSettings)

  const editHistory = useEditStore((s) => s.history)
  const editCursor = useEditStore((s) => s.cursor)
  const pushEdit = useEditStore((s) => s.push)
  const undoEdit = useEditStore((s) => s.undo)
  const redoEdit = useEditStore((s) => s.redo)

  // 初次进入或切到新 plan：把当前 plan 作为 undo 栈起点（避免跨 plan 串台）
  const lastPushedPlanIdRef = useRef<string | null>(null)
  useEffect(() => {
    if (!plan) return
    if (plan.plan_id === lastPushedPlanIdRef.current) return
    useEditStore.getState().reset()
    pushEdit(plan)
    lastPushedPlanIdRef.current = plan.plan_id
  }, [plan, pushEdit])

  const [jobId, setJobId] = useState<string | null>(null)
  const [step, setStep] = useState<string>('idle')
  const [percent, setPercent] = useState(0)
  const [done, setDone] = useState<RenderDonePayload | null>(null)
  const [error, setError] = useState<string | null>(null)
  const sseRef = useRef<ReturnType<typeof createSSE> | null>(null)
  // 四轨板动作 busy 锁；与 isRendering 区分开。
  const [trackBusy, setTrackBusy] = useState(false)
  const [bgmPickerOpen, setBgmPickerOpen] = useState(false)

  const filledGapIds = useMemo(() => new Set(fills.map((f) => f.gap_id)), [fills])

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
      sseRef.current = createSSE<{ job_id: string; payload: RenderDonePayload }>(
        `/render/stream?job_id=${resp.job_id}`,
        {
        onProgress: (p) => {
          setStep(p.step)
          setPercent(p.percent)
        },
        onDone: (d) => {
          // 后端 JobStore.complete 包装成 {job_id, payload}；这里取里层结果。
          const result = d.payload
          setDone(result)
          setStep('done')
          setPercent(100)
          // 后端在 _do_render 完成时已自动 mark_rendered(project_id, job_id) 并落盘。
          // 这里仅刷一次首页项目列表，让 status=rendered 的更新立刻可见。
          if (currentProjectId) {
            void refreshProjects()
            // 同步把 render 步骤 commit 到 step_states——保证顶部 nav 显示 render=saved
            void commitStep(currentProjectId, 'render', { job_id: resp.job_id }).catch(() => {
              /* commit 失败不阻断结果展示 */
            })
          }
        },
        onError: (e) => setError(e.detail),
      })
    } catch (err) {
      setError(err instanceof Error ? err.message : '提交失败')
      setStep('idle')
    }
  }, [plan, variant, currentProjectId, refreshProjects])

  /* -- 自然语言编辑 -- */
  // 三轨 NLEditPanel 内置 instruction / marks / applying / editError；
  // Render 页只维护"选中哪个 scene"用于 mark 预填，再把 setPlan/pushEdit 透传给 onApplied。
  const [selectedSceneId, setSelectedSceneId] = useState<string | null>(null)
  const editSectionRef = useRef<HTMLDivElement | null>(null)

  /** 内容轨点击 → 区段预填 + 自然语言编辑器获取焦点 */
  const handleSelectScene = useCallback((scene: Scene) => {
    setSelectedSceneId(scene.scene_id)
    requestAnimationFrame(() => {
      editSectionRef.current?.scrollIntoView({ behavior: 'smooth', block: 'start' })
    })
  }, [])

  /* -- 轨道动作（与 Compose 同源；refetchPlan / synthesize / packaging / bgm） -- */

  const refetchPlan = useCallback(
    async (planId: string) => {
      try {
        const fresh = await api.get<Plan>(`/plan/${planId}`)
        setPlan(fresh)
        pushEdit(fresh)
      } catch {
        /* 拉新版失败由上层 error 兜底 */
      }
    },
    [pushEdit, setPlan],
  )

  const handleSynthesizeScene = useCallback(
    async (sceneId: string) => {
      if (!plan) return
      setTrackBusy(true)
      setError(null)
      try {
        await synthesizeOne({ plan_id: plan.plan_id, scene_id: sceneId })
        await refetchPlan(plan.plan_id)
      } catch (err) {
        setError(err instanceof Error ? err.message : `单段口播合成失败：${sceneId}`)
      } finally {
        setTrackBusy(false)
      }
    },
    [plan, refetchPlan],
  )

  const handleSynthesizeAll = useCallback(async () => {
    if (!plan) return
    setTrackBusy(true)
    setError(null)
    try {
      const resp = await synthesizeAll(plan.plan_id)
      await refetchPlan(plan.plan_id)
      if (resp.failures.length > 0) {
        setError(
          `${resp.synthesized.length} 段已合成；${resp.failures.length} 段失败：` +
            resp.failures.map((f) => f.scene_id).join(', '),
        )
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : '一键合成失败')
    } finally {
      setTrackBusy(false)
    }
  }, [plan, refetchPlan])

  const handleClearVoice = useCallback(
    async (sceneId: string) => {
      if (!plan) return
      setTrackBusy(true)
      setError(null)
      try {
        const fresh = await deleteVoice(plan.plan_id, sceneId)
        setPlan(fresh)
        pushEdit(fresh)
      } catch (err) {
        setError(err instanceof Error ? err.message : `清除口播失败：${sceneId}`)
      } finally {
        setTrackBusy(false)
      }
    },
    [plan, pushEdit, setPlan],
  )

  const handleRecommendPackaging = useCallback(async () => {
    if (!plan) return
    setTrackBusy(true)
    setError(null)
    try {
      const body: PackagingRecommendRequest = { plan_id: plan.plan_id, apply: true }
      await api.post('/packaging/recommend', body)
      await refetchPlan(plan.plan_id)
    } catch (err) {
      setError(err instanceof Error ? err.message : '包装推荐失败')
    } finally {
      setTrackBusy(false)
    }
  }, [plan, refetchPlan])

  const handleBgmAnchorChange = useCallback(
    async (newAnchor: number) => {
      if (!plan) return
      setTrackBusy(true)
      setError(null)
      try {
        const fresh = await patchPlanBgm(plan.plan_id, { video_anchor_seconds: newAnchor })
        setPlan(fresh)
        pushEdit(fresh)
      } catch (err) {
        setError(err instanceof Error ? err.message : '更新 BGM 锚点失败')
      } finally {
        setTrackBusy(false)
      }
    },
    [plan, pushEdit, setPlan],
  )

  const handleClearBgm = useCallback(async () => {
    if (!plan) return
    setTrackBusy(true)
    setError(null)
    try {
      const fresh = await deletePlanBgm(plan.plan_id)
      setPlan(fresh)
      pushEdit(fresh)
    } catch (err) {
      setError(err instanceof Error ? err.message : '清除 BGM 失败')
    } finally {
      setTrackBusy(false)
    }
  }, [plan, pushEdit, setPlan])

  const handleBgmVolumeChange = useCallback(
    async (volume: number) => {
      if (!plan) return
      setError(null)
      try {
        const fresh = await patchPlanBgm(plan.plan_id, { volume })
        setPlan(fresh)
        pushEdit(fresh)
      } catch (err) {
        setError(err instanceof Error ? err.message : '更新 BGM 音量失败')
      }
    },
    [plan, pushEdit, setPlan],
  )

  const handleToggleVoiceover = useCallback(
    async (enabled: boolean) => {
      setSettings({ voiceover_enabled: enabled })
      if (!plan) return
      setTrackBusy(true)
      setError(null)
      try {
        const fresh = await patchPlanSettings(plan.plan_id, { voiceover_enabled: enabled })
        setPlan(fresh)
        pushEdit(fresh)
      } catch (err) {
        setError(err instanceof Error ? err.message : '切换口播开关失败')
      } finally {
        setTrackBusy(false)
      }
    },
    [plan, pushEdit, setPlan, setSettings],
  )

  const handleNLEditApplied = useCallback(
    (newPlan: Plan) => {
      setPlan(newPlan)
      pushEdit(newPlan)
    },
    [pushEdit, setPlan],
  )

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

        {/* ============ 右 · 四轨工作台 + Plan 摘要 ============ */}
        <section className="space-y-4">
          <div className="rounded-lg border border-border bg-card p-4">
            <h2 className="mb-3 text-sm font-semibold">
              模块 6 · 主轨 / 口播 / 包装 / BGM
              <span className="ml-2 text-[10px] font-normal text-muted-foreground">
                点击内容轨的 scene 块 → 自动写入下方编辑区段
              </span>
            </h2>
            <FourTrackBoard
              plan={plan}
              gaps={gaps}
              filledGapIds={filledGapIds}
              selectedGapId={null}
              onSelectScene={(scene) => handleSelectScene(scene)}
              onSynthesizeScene={handleSynthesizeScene}
              onSynthesizeAll={handleSynthesizeAll}
              onClearVoice={handleClearVoice}
              onRecommendPackaging={handleRecommendPackaging}
              onPickBgm={() => setBgmPickerOpen(true)}
              onBgmAnchorChange={handleBgmAnchorChange}
              onClearBgm={handleClearBgm}
              onBgmVolumeChange={handleBgmVolumeChange}
              onToggleVoiceover={handleToggleVoiceover}
              busy={trackBusy || isRendering}
            />
          </div>

          <div className="rounded-lg border border-border bg-card p-4">
            <h2 className="mb-2 text-sm font-semibold">Plan 摘要</h2>
            <ul className="space-y-1 text-xs">
              {plan.main_track.map((sc) => (
                <li
                  key={sc.scene_id}
                  className={cn(
                    'flex items-start gap-2 rounded px-1 py-0.5',
                    selectedSceneId === sc.scene_id && 'bg-primary/10',
                  )}
                >
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

      {/* ============ 底部 · 自然语言编辑（三轨 tab）============ */}
      <section ref={editSectionRef} className="mt-4 space-y-2">
        <div className="flex items-center justify-end gap-2">
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
        <NLEditPanel
          plan={plan}
          projectStep="render"
          lockedTracks={['main']}
          onApplied={handleNLEditApplied}
          selectedSceneId={selectedSceneId}
        />
      </section>

      {/* BGM 选择 / 上传弹窗 */}
      {currentProjectId && (
        <BgmPickerDialog
          open={bgmPickerOpen}
          onClose={() => setBgmPickerOpen(false)}
          projectId={currentProjectId}
          planId={plan.plan_id}
          onPlanUpdated={(p) => {
            setPlan(p)
            pushEdit(p)
          }}
        />
      )}
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
