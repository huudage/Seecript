import { useCallback, useState } from 'react'

import { api } from '@/api/client'
import { cn } from '@/lib/utils'
import type { GapFillAllRequest, GapFillAllResponse } from '@/types/schemas'

/**
 * 一键文案补全所有缺口（LLM copy）。
 *
 * - 后端 /gap/fill-all action=copy 顺序对每个非 ok 缺口跑 LLM 文案补全，
 *   用 gap.requirement 当 prompt_hint，串行避免 LLM 配额抖动
 * - 遇错即停，返回已完成的 fills 让前端逐条回写
 * - 比 AIGC 批量快得多（每段 1-3s），UX 反馈区别于 AIGC 用不同色
 */
export function BatchCopyButton({
  planId,
  pendingCount,
  onDone,
}: {
  planId: string | null
  pendingCount: number
  onDone: (resp: GapFillAllResponse) => void
}) {
  const [loading, setLoading] = useState(false)
  const [err, setErr] = useState<string | null>(null)

  const disabled = !planId || pendingCount === 0 || loading

  const handleRun = useCallback(async () => {
    if (!planId) return
    setLoading(true)
    setErr(null)
    try {
      const body: GapFillAllRequest = { plan_id: planId, action: 'copy' }
      const resp = await api.post<GapFillAllResponse>('/gap/fill-all', body)
      onDone(resp)
    } catch (e) {
      setErr(e instanceof Error ? e.message : '批量文案补全失败')
    } finally {
      setLoading(false)
    }
  }, [onDone, planId])

  return (
    <div className="flex flex-col gap-1">
      <button
        onClick={handleRun}
        disabled={disabled}
        title={
          !planId
            ? '请先做一次「智能分析」'
            : pendingCount === 0
              ? '所有缺口已 ok，无需生成'
              : `顺序写 ${pendingCount} 段文案；通常 ${pendingCount * 2}-${pendingCount * 4}s`
        }
        className={cn(
          'inline-flex items-center gap-1 rounded-md border px-2 py-1 text-[11px] font-medium transition-colors',
          disabled
            ? 'cursor-not-allowed border-border bg-background/40 text-muted-foreground'
            : 'border-emerald-500/60 bg-emerald-500/10 text-emerald-700 hover:bg-emerald-500/20 dark:text-emerald-300',
        )}
      >
        {loading ? `批量写文案中…（${pendingCount}）` : `✍ 一键文案补全全部缺口（${pendingCount}）`}
      </button>
      {err && <p className="text-[10px] text-destructive">{err}</p>}
    </div>
  )
}
