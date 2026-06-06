import { useCallback, useState } from 'react'

import { api } from '@/api/client'
import { cn } from '@/lib/utils'
import type { GapFillAllRequest, GapFillAllResponse } from '@/types/schemas'

/**
 * 一键 AI 画面补全所有缺口（链式视频生成）。
 *
 * - 后端 /gap/fill-all 顺序执行 + 自动用上一段尾帧作为下一段首帧承接
 * - 遇错即停（不浪费 AI 算力），返回已完成的 fills 让前端逐个回写
 * - 单次任务耗时可能 >5 分钟，按钮置 loading 时不阻塞用户继续编辑别的段
 */
export function BatchAigcButton({
  planId,
  pendingCount,
  skipGapIds,
  onDone,
}: {
  planId: string | null
  pendingCount: number
  /** 已采纳的 gap_id 列表；后端会跳过这些避免覆盖单条手动结果。 */
  skipGapIds?: string[]
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
      const body: GapFillAllRequest = {
        plan_id: planId,
        skip_gap_ids: skipGapIds && skipGapIds.length > 0 ? skipGapIds : undefined,
      }
      const resp = await api.post<GapFillAllResponse>('/gap/fill-all', body)
      onDone(resp)
    } catch (e) {
      setErr(e instanceof Error ? e.message : '批量生成失败')
    } finally {
      setLoading(false)
    }
  }, [onDone, planId, skipGapIds])

  return (
    <div className="flex flex-col gap-1">
      <button
        onClick={handleRun}
        disabled={disabled}
        title={
          !planId
            ? '请先点一次「智能分析」'
            : pendingCount === 0
              ? '已经全部齐了，不用再生成'
              : `按顺序生成 ${pendingCount} 段，可能要几分钟`
        }
        className={cn(
          'inline-flex items-center gap-1 rounded-md border px-2 py-1 text-[11px] font-medium transition-colors',
          disabled
            ? 'cursor-not-allowed border-border bg-background/40 text-muted-foreground'
            : 'border-primary/60 bg-primary/10 text-primary hover:bg-primary/20',
        )}
      >
        {loading ? `生成中…（${pendingCount}）` : `🪄 一键 AI 补齐缺素材的镜头（${pendingCount}）`}
      </button>
      {err && <p className="text-[10px] text-destructive">{err}</p>}
    </div>
  )
}
