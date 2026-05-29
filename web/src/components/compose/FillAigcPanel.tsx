import { useCallback, useEffect, useRef, useState } from 'react'

import { api } from '@/api/client'
import type { AigcPromptResponse, FillResult, Gap, GapFillRequest } from '@/types/schemas'
import { cn } from '@/lib/utils'

/**
 * AIGC 补全：触发 Seedance T2V 生成片段填补本段。
 *
 * UX 规则（v4）：
 * - prompt 不再直接拿 gap.requirement（那是给创作者看的需求描述，缺 T2V 关键要素）
 *   切 gap 时先调 /api/gap/aigc-prompt 让 LLM 把段落上下文转写成完备的 T2V prompt
 * - 时长由 AdaptedSection.duration_seconds 决定，前端不再让用户挑——避免和段落规划脱节
 * - >12s 自动链式生成 N 段，FillResult.video_urls 返回多段 CDN URL
 * - 每段视频独立播放（HTMLVideoElement），方便逐段检查
 * - 可编辑 prompt 重新生成（按钮文案根据状态切换：开始生成 / 修改 prompt 重新生成）
 * - 「重新生成提示词」按钮：让 LLM 再转一版（会清掉用户当前编辑的 prompt）
 * - 拿到 status≠ok 但带 task_id 的 fill 后，每 8s 自动 refresh 一次，最多 30 次（4 min）
 */
const AUTO_POLL_INTERVAL_MS = 8000
const AUTO_POLL_MAX_ATTEMPTS = 30

export function FillAigcPanel({
  gap,
  fill,
  onResult,
}: {
  gap: Gap
  fill: FillResult | null
  onResult: (fill: FillResult) => void
}) {
  const [prompt, setPrompt] = useState<string>('')
  const [promptLoading, setPromptLoading] = useState(false)
  const [promptErr, setPromptErr] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const [refreshing, setRefreshing] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const [autoPolling, setAutoPolling] = useState(false)
  const [autoPollAttempts, setAutoPollAttempts] = useState(0)

  // 让用户手动编辑过的 prompt 在重新生成提示词时仍可被覆盖（按钮明确语义），
  // 但 gap 切换时只在 textarea 为空时才覆盖，避免吃掉创作者的编辑。
  const fetchGeneratedPrompt = useCallback(
    async (gapId: string, force: boolean) => {
      setPromptLoading(true)
      setPromptErr(null)
      try {
        const result = await api.post<AigcPromptResponse>('/gap/aigc-prompt', {
          gap_id: gapId,
        })
        setPrompt((current) => {
          if (!force && current.trim()) return current
          return result.prompt
        })
      } catch (e) {
        setPromptErr(e instanceof Error ? e.message : '提示词生成失败')
      } finally {
        setPromptLoading(false)
      }
    },
    [],
  )

  // 切到新 gap 时拉一次 LLM 提示词
  useEffect(() => {
    setPrompt('')
    setErr(null)
    void fetchGeneratedPrompt(gap.gap_id, true)
  }, [gap.gap_id, fetchGeneratedPrompt])

  const handleRegeneratePrompt = useCallback(() => {
    void fetchGeneratedPrompt(gap.gap_id, true)
  }, [gap.gap_id, fetchGeneratedPrompt])

  const handleRun = useCallback(async () => {
    setLoading(true)
    setErr(null)
    try {
      const body: GapFillRequest = {
        gap_id: gap.gap_id,
        action: 'aigc',
        // 不传 duration_seconds —— 后端 router 会根据 gap.section_id 反查 AdaptedSection.duration_seconds
        params: { prompt: prompt.trim() || gap.requirement },
      }
      const result = await api.post<FillResult>('/gap/fill', body)
      onResult(result)
    } catch (e) {
      setErr(e instanceof Error ? e.message : 'AIGC 生成失败')
    } finally {
      setLoading(false)
    }
  }, [gap.gap_id, gap.requirement, onResult, prompt])

  // 部分成功场景：fill.chunks_count < expected → 用第一个 chunk task_id refresh
  const firstTaskId = fill?.chunk_task_ids?.[0] ?? fill?.new_material_id ?? extractTaskId(fill?.note)
  const canRefresh = !!fill && fill.status !== 'ok' && !!firstTaskId
  const expectedChunks = extractExpectedChunks(fill?.note) ?? fill?.chunks_count ?? 0
  const hasPreview = !!fill?.video_urls && fill.video_urls.length > 0

  const handleRefresh = useCallback(async () => {
    if (!fill || !firstTaskId) return
    setRefreshing(true)
    setErr(null)
    try {
      const result = await api.post<FillResult>('/gap/aigc-refresh', {
        gap_id: fill.gap_id,
        task_id: firstTaskId,
      })
      onResult(result)
    } catch (e) {
      setErr(e instanceof Error ? e.message : '刷新失败')
    } finally {
      setRefreshing(false)
    }
  }, [fill, firstTaskId, onResult])

  // -- 自动轮询：fill 是 warn / pending 状态且有 task_id 时，每 8s 静默 refresh 一次 --
  // 用 ref 持有最新 onResult 避免每次都重置 interval；attempts 用 state 让 UI 能显示进度
  const onResultRef = useRef(onResult)
  useEffect(() => { onResultRef.current = onResult }, [onResult])

  useEffect(() => {
    if (!fill || fill.status === 'ok' || !firstTaskId) {
      setAutoPolling(false)
      setAutoPollAttempts(0)
      return
    }
    setAutoPolling(true)
    setAutoPollAttempts(0)
    let cancelled = false
    let attempts = 0

    const tick = async () => {
      if (cancelled) return
      attempts += 1
      setAutoPollAttempts(attempts)
      try {
        const result = await api.post<FillResult>('/gap/aigc-refresh', {
          gap_id: fill.gap_id,
          task_id: firstTaskId,
        })
        if (cancelled) return
        onResultRef.current(result)
        // result.status === 'ok' 时下一轮 effect 会清场（依赖 fill 变化）
      } catch {
        // 静默忽略——下一轮自动重试，最终用户手点也能补救
      }
    }

    const handle = window.setInterval(() => {
      if (attempts >= AUTO_POLL_MAX_ATTEMPTS) {
        window.clearInterval(handle)
        setAutoPolling(false)
        return
      }
      void tick()
    }, AUTO_POLL_INTERVAL_MS)

    return () => {
      cancelled = true
      window.clearInterval(handle)
      setAutoPolling(false)
    }
    // 依赖 fill.gap_id + status + firstTaskId：fill 整体变化（refresh 后）会重启 effect 但因为 ok 直接 return
  }, [fill, firstTaskId])

  return (
    <div className="space-y-2 rounded-md border border-border bg-background/40 p-3">
      <div className="flex items-center justify-between">
        <h4 className="text-xs font-semibold">AIGC · Seedance T2V</h4>
        <span className="text-[11px] text-muted-foreground">时长跟随段落规划自动分段</span>
      </div>

      <div>
        <div className="mb-1 flex items-center justify-between">
          <label className="text-[11px] font-semibold text-muted-foreground">
            生成 prompt（LLM 已转写为 T2V 完备提示词）
          </label>
          <button
            type="button"
            onClick={handleRegeneratePrompt}
            disabled={promptLoading || loading}
            className={cn(
              'text-[10px] text-primary underline-offset-2 hover:underline',
              (promptLoading || loading) && 'cursor-not-allowed opacity-60',
            )}
          >
            {promptLoading ? '生成中…' : '↻ 重新生成提示词'}
          </button>
        </div>
        <textarea
          value={prompt}
          onChange={(e) => setPrompt(e.target.value.slice(0, 300))}
          rows={4}
          placeholder={promptLoading ? 'LLM 正在为本段生成完备的 T2V prompt…' : '描述本段画面/风格；为空则用 gap.requirement'}
          disabled={promptLoading}
          className={cn(
            'w-full resize-y rounded-md border border-border bg-background px-2 py-1.5 text-sm outline-none focus:border-primary',
            promptLoading && 'cursor-wait opacity-60',
          )}
        />
        <div className="mt-0.5 flex items-center justify-between text-[10px]">
          <span className="text-muted-foreground">
            {promptErr ? <span className="text-destructive">{promptErr}</span> : '可手动修改后再点开始生成'}
          </span>
          <span className="font-mono text-muted-foreground">{prompt.length}/300</span>
        </div>
      </div>

      <button
        onClick={handleRun}
        disabled={loading || promptLoading || !prompt.trim()}
        className={cn(
          'w-full rounded-md bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground',
          (loading || promptLoading || !prompt.trim()) && 'cursor-not-allowed opacity-60',
        )}
      >
        {loading ? '生成中…（链式生成可能 >3 分钟）' : fill ? '修改 prompt 重新生成' : '开始生成'}
      </button>

      {err && <p className="text-[11px] text-destructive">{err}</p>}

      {fill && (
        <div className="space-y-2 rounded border border-border bg-secondary/50 p-2 text-xs">
          <div className="flex items-center justify-between">
            <span>
              状态：
              <span
                className={cn(
                  'ml-1 font-medium',
                  fill.status === 'ok'
                    ? 'text-emerald-600 dark:text-emerald-300'
                    : 'text-amber-600 dark:text-amber-300',
                )}
              >
                {fill.status === 'ok' ? '完成' : '进行中 / 异常'}
              </span>
            </span>
            <span className="text-muted-foreground">
              {fill.chunks_count}/{expectedChunks || '?'} 段
            </span>
          </div>
          {fill.note && <p className="text-muted-foreground">{fill.note}</p>}

          {!hasPreview && (
            <div className="rounded-md border border-amber-500/40 bg-amber-500/10 p-2">
              <p className="text-[11px] font-medium text-amber-700 dark:text-amber-300">
                暂无可预览的视频段
                {autoPolling && (
                  <span className="ml-2 text-[10px] font-normal text-amber-600/80">
                    · 自动轮询中 {autoPollAttempts}/{AUTO_POLL_MAX_ATTEMPTS}
                  </span>
                )}
              </p>
              <p className="mt-0.5 text-[10px] text-muted-foreground">
                {fill.status === 'ok'
                  ? '任务回报已完成但视频 URL 为空，请刷新或重试。'
                  : autoPolling
                    ? `Seedance 任务还在跑（队列 / 渲染 / 上传 CDN）。每 ${AUTO_POLL_INTERVAL_MS / 1000}s 自动查询一次，最长 ${(AUTO_POLL_INTERVAL_MS * AUTO_POLL_MAX_ATTEMPTS) / 60000} 分钟。`
                    : 'Seedance 任务还没拿到结果（超时 / 队列中 / 失败）。点下方刷新可重新查询。'}
              </p>
              {firstTaskId && (
                <p className="mt-1 font-mono text-[10px] text-muted-foreground">
                  task_id: {firstTaskId}
                </p>
              )}
              {firstTaskId && (
                <button
                  onClick={handleRefresh}
                  disabled={refreshing}
                  className={cn(
                    'mt-1.5 w-full rounded-md border border-amber-500/60 bg-amber-500/20 px-2 py-1 text-[11px] font-medium text-amber-700 transition-colors hover:bg-amber-500/30 dark:text-amber-200',
                    refreshing && 'cursor-not-allowed opacity-60',
                  )}
                >
                  {refreshing ? '查询中…' : autoPolling ? '立刻刷新一次' : '刷新任务状态'}
                </button>
              )}
            </div>
          )}

          {hasPreview && (
            <div className="space-y-1.5">
              {fill.video_urls.map((url, i) => (
                <div key={`${url}-${i}`} className="space-y-0.5">
                  <div className="flex items-center justify-between text-[10px] text-muted-foreground">
                    <span>第 {i + 1} 段</span>
                    <a
                      href={url}
                      target="_blank"
                      rel="noreferrer"
                      className="font-mono text-primary underline-offset-2 hover:underline"
                    >
                      新窗打开 ↗
                    </a>
                  </div>
                  <video
                    src={url}
                    controls
                    preload="metadata"
                    poster={i === 0 ? fill.cover_url ?? undefined : undefined}
                    className="w-full rounded-md border border-border bg-black"
                  />
                </div>
              ))}
            </div>
          )}

          {fill.chunk_task_ids && fill.chunk_task_ids.length > 0 && (
            <details className="text-[10px] text-muted-foreground">
              <summary className="cursor-pointer">task_ids（{fill.chunk_task_ids.length}）</summary>
              <ul className="mt-1 space-y-0.5 font-mono">
                {fill.chunk_task_ids.map((t) => (
                  <li key={t}>{t}</li>
                ))}
              </ul>
            </details>
          )}

          {hasPreview && canRefresh && (
            <button
              onClick={handleRefresh}
              disabled={refreshing}
              className={cn(
                'rounded-md border border-primary/60 bg-primary/10 px-2 py-1 text-[11px] font-medium text-primary transition-colors hover:bg-primary/20',
                refreshing && 'cursor-not-allowed opacity-60',
              )}
            >
              {refreshing ? '查询中…' : '刷新任务状态（仅首段）'}
            </button>
          )}
        </div>
      )}
    </div>
  )
}

function extractTaskId(note: string | null | undefined): string | null {
  if (!note) return null
  const m = note.match(/task=([\w-]+)/)
  return m?.[1] ?? null
}

function extractExpectedChunks(note: string | null | undefined): number | null {
  if (!note) return null
  // 匹配 'Seedance 仅完成 X/Y 段' 或 '链式生成完成（Y 段...'
  const partial = note.match(/(\d+)\s*\/\s*(\d+)\s*段/)
  if (partial) return Number(partial[2])
  const full = note.match(/链式生成完成（(\d+)\s*段/)
  if (full) return Number(full[1])
  return null
}
