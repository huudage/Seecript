import { useCallback, useState } from 'react'

import { api } from '@/api/client'
import type { FillResult, Gap, GapFillRequest } from '@/types/schemas'
import { cn } from '@/lib/utils'

/**
 * AIGC 补全：触发 Seedance T2V 生成 5-8s 短片。
 *
 * - 后端 /gap/fill 内部自带轮询（最长 180s），超时也会带 task_id 回来
 * - 已有 fill：展示状态；status=warn 且能解析出 task_id 时给"刷新任务"按钮
 *   走 /gap/aigc-refresh 再查一次而不是重新提交，省 Seedance 配额
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
  const [duration, setDuration] = useState<number>(5)
  const [loading, setLoading] = useState(false)
  const [refreshing, setRefreshing] = useState(false)
  const [err, setErr] = useState<string | null>(null)

  const handleRun = useCallback(async () => {
    setLoading(true)
    setErr(null)
    try {
      const body: GapFillRequest = {
        gap_id: gap.gap_id,
        action: 'aigc',
        params: { prompt: prompt.trim() || gap.requirement, duration_seconds: duration },
      }
      const result = await api.post<FillResult>('/gap/fill', body)
      onResult(result)
    } catch (e) {
      setErr(e instanceof Error ? e.message : 'AIGC 生成失败')
    } finally {
      setLoading(false)
    }
  }, [duration, gap.gap_id, gap.requirement, onResult, prompt])

  // task_id 优先取 new_material_id；超时 note 里也带 "task=cgt-..." 兜底解析。
  const taskId = fill?.new_material_id || extractTaskId(fill?.note)
  const canRefresh = !!fill && fill.status !== 'ok' && !!taskId

  const handleRefresh = useCallback(async () => {
    if (!fill || !taskId) return
    setRefreshing(true)
    setErr(null)
    try {
      const result = await api.post<FillResult>('/gap/aigc-refresh', {
        gap_id: fill.gap_id,
        task_id: taskId,
      })
      onResult(result)
    } catch (e) {
      setErr(e instanceof Error ? e.message : '刷新失败')
    } finally {
      setRefreshing(false)
    }
  }, [fill, onResult, taskId])

  return (
    <div className="space-y-2 rounded-md border border-border bg-background/40 p-3">
      <div className="flex items-center justify-between">
        <h4 className="text-xs font-semibold">AIGC · Seedance T2V</h4>
        <span className="text-[11px] text-muted-foreground">5–8s 短片填补槽位</span>
      </div>

      <textarea
        value={prompt}
        onChange={(e) => setPrompt(e.target.value.slice(0, 200))}
        rows={2}
        placeholder="生成 prompt（默认用 gap.requirement）"
        className="w-full resize-y rounded-md border border-border bg-background px-2 py-1.5 text-sm outline-none focus:border-primary"
      />

      <div className="flex items-center gap-2 text-xs">
        <label className="text-muted-foreground">时长</label>
        {[5, 6, 8].map((d) => (
          <button
            key={d}
            onClick={() => setDuration(d)}
            className={cn(
              'rounded-md border px-2 py-0.5',
              duration === d
                ? 'border-primary bg-primary/10 text-primary'
                : 'border-border bg-background hover:bg-secondary',
            )}
          >
            {d}s
          </button>
        ))}
        <button
          onClick={handleRun}
          disabled={loading || !prompt.trim()}
          className={cn(
            'ml-auto rounded-md bg-primary px-3 py-1 text-xs font-medium text-primary-foreground',
            (loading || !prompt.trim()) && 'cursor-not-allowed opacity-60',
          )}
        >
          {loading ? '生成中…（最长 180s）' : fill ? '重新生成' : '开始生成'}
        </button>
      </div>

      {err && <p className="text-[11px] text-destructive">{err}</p>}

      {fill && (
        <div className="space-y-2 rounded border border-border bg-secondary/50 p-2 text-xs">
          <p>
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
          </p>
          {fill.new_material_id && (
            <p className="font-mono text-[11px] text-muted-foreground">task = {fill.new_material_id}</p>
          )}
          {fill.note && <p className="text-muted-foreground">{fill.note}</p>}
          {canRefresh && (
            <button
              onClick={handleRefresh}
              disabled={refreshing}
              className={cn(
                'rounded-md border border-primary/60 bg-primary/10 px-2 py-1 text-[11px] font-medium text-primary transition-colors hover:bg-primary/20',
                refreshing && 'cursor-not-allowed opacity-60',
              )}
            >
              {refreshing ? '查询中…' : '刷新任务状态'}
            </button>
          )}
        </div>
      )}
    </div>
  )
}

/** 从超时 note 里抓 "task=cgt-xxx" 兜底；优先用 new_material_id 字段。 */
function extractTaskId(note: string | null | undefined): string | null {
  if (!note) return null
  const m = note.match(/task=([\w-]+)/)
  return m?.[1] ?? null
}
