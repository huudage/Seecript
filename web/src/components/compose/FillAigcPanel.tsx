import { useCallback, useEffect, useState } from 'react'

import { api } from '@/api/client'
import type { FillResult, Gap, GapFillRequest } from '@/types/schemas'
import { cn } from '@/lib/utils'

/**
 * AIGC 补全：触发 Seedance T2V 生成片段填补本段。
 *
 * UX 规则（v3）：
 * - 时长由 AdaptedSection.duration_seconds 决定，前端不再让用户挑——避免和段落规划脱节
 * - >12s 自动链式生成 N 段，FillResult.video_urls 返回多段 CDN URL
 * - 每段视频独立播放（HTMLVideoElement），方便逐段检查
 * - 可编辑 prompt 重新生成（按钮文案根据状态切换：开始生成 / 修改 prompt 重新生成）
 */
export function FillAigcPanel({
  gap,
  fill,
  onResult,
}: {
  gap: Gap
  fill: FillResult | null
  onResult: (fill: FillResult) => void
}) {
  const [prompt, setPrompt] = useState<string>(gap.requirement)
  const [loading, setLoading] = useState(false)
  const [refreshing, setRefreshing] = useState(false)
  const [err, setErr] = useState<string | null>(null)

  // 切到新 gap 时 prompt 重置为该 gap.requirement
  useEffect(() => {
    setPrompt(gap.requirement)
    setErr(null)
  }, [gap.gap_id, gap.requirement])

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

  return (
    <div className="space-y-2 rounded-md border border-border bg-background/40 p-3">
      <div className="flex items-center justify-between">
        <h4 className="text-xs font-semibold">AIGC · Seedance T2V</h4>
        <span className="text-[11px] text-muted-foreground">时长跟随段落规划自动分段</span>
      </div>

      <div>
        <label className="mb-1 block text-[11px] font-semibold text-muted-foreground">
          生成 prompt
        </label>
        <textarea
          value={prompt}
          onChange={(e) => setPrompt(e.target.value.slice(0, 300))}
          rows={3}
          placeholder="描述本段画面/风格；为空则用 gap.requirement"
          className="w-full resize-y rounded-md border border-border bg-background px-2 py-1.5 text-sm outline-none focus:border-primary"
        />
        <div className="mt-0.5 text-right font-mono text-[10px] text-muted-foreground">
          {prompt.length}/300
        </div>
      </div>

      <button
        onClick={handleRun}
        disabled={loading || !prompt.trim()}
        className={cn(
          'w-full rounded-md bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground',
          (loading || !prompt.trim()) && 'cursor-not-allowed opacity-60',
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
            {fill.chunks_count > 0 && (
              <span className="text-muted-foreground">{fill.chunks_count} 段</span>
            )}
          </div>
          {fill.note && <p className="text-muted-foreground">{fill.note}</p>}

          {fill.video_urls && fill.video_urls.length > 0 && (
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

          {canRefresh && (
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
