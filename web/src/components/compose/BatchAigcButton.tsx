import { useCallback, useState } from 'react'

import { api } from '@/api/client'
import { cn } from '@/lib/utils'
import type { GapFillAllRequest, GapFillAllResponse } from '@/types/schemas'

/**
 * 一键 AI 生成所有缺口（Seedance T2V）。
 *
 * - 后端 /gap/fill-all 顺序执行 + 链式生成首尾帧参考
 * - 遇错即停（不浪费配额），返回已完成的 fills 让前端逐个回写
 * - 单次任务耗时可能 >5 分钟，按钮置 loading 时不阻塞用户继续编辑别的段
 */
export function BatchAigcButton({
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
      const body: GapFillAllRequest = { plan_id: planId }
      const resp = await api.post<GapFillAllResponse>('/gap/fill-all', body)
      onDone(resp)
    } catch (e) {
      setErr(e instanceof Error ? e.message : '批量生成失败')
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
              : `顺序生成 ${pendingCount} 段；可能耗时数分钟`
        }
        className={cn(
          'inline-flex items-center gap-1 rounded-md border px-2 py-1 text-[11px] font-medium transition-colors',
          disabled
            ? 'cursor-not-allowed border-border bg-background/40 text-muted-foreground'
            : 'border-primary/60 bg-primary/10 text-primary hover:bg-primary/20',
        )}
      >
        {loading ? `批量生成中…（${pendingCount}）` : `🪄 一键 AI 生成全部缺口（${pendingCount}）`}
      </button>
      {err && <p className="text-[10px] text-destructive">{err}</p>}
    </div>
  )
}
