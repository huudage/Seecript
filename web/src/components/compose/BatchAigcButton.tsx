import { useCallback, useState } from 'react'

import { api } from '@/api/client'
import { cn } from '@/lib/utils'
import type { GapFillAllRequest, GapFillAllResponse } from '@/types/schemas'

/**
 * 一键 AI 视频补全所有缺口（链式视频生成 / Seedream 文生图）。
 *
 * - 后端 /gap/fill-all 顺序执行 + (video) 自动用上一段尾帧作为下一段首帧承接
 * - video 链式承接遇错即停；image / copy 段间独立，best-effort 跑完所有 gap
 * - video 单次任务可能 >5 分钟；image 模式 Seedream 同步出图，几十秒级
 */
export function BatchAigcButton({
  planId,
  pendingCount,
  skipGapIds,
  onDone,
  mode = 'video',
  onLoadingChange,
}: {
  planId: string | null
  pendingCount: number
  /** 已采纳的 gap_id 列表；后端会跳过这些避免覆盖单条手动结果。 */
  skipGapIds?: string[]
  onDone: (resp: GapFillAllResponse) => void
  /** 'video' = Seedance T2V（默认）；'image' = Seedream 文生图 + Remotion 动效。 */
  mode?: 'video' | 'image'
  /** stage-26 PR-N.6：把内部 loading 同步给父组件，用于 step3 准入门控 */
  onLoadingChange?: (busy: boolean) => void
}) {
  const [loading, setLoading] = useState(false)
  const [err, setErr] = useState<string | null>(null)

  const disabled = !planId || pendingCount === 0 || loading

  const setBusy = useCallback(
    (b: boolean) => {
      setLoading(b)
      onLoadingChange?.(b)
    },
    [onLoadingChange],
  )

  const handleRun = useCallback(async () => {
    if (!planId) return
    setBusy(true)
    setErr(null)
    try {
      const body: GapFillAllRequest = {
        plan_id: planId,
        action: mode === 'image' ? 'aigc_image' : 'aigc',
        skip_gap_ids: skipGapIds && skipGapIds.length > 0 ? skipGapIds : undefined,
      }
      const resp = await api.post<GapFillAllResponse>('/gap/fill-all', body)
      onDone(resp)
    } catch (e) {
      setErr(e instanceof Error ? e.message : '批量生成失败')
    } finally {
      setBusy(false)
    }
  }, [mode, onDone, planId, setBusy, skipGapIds])

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
              : mode === 'image'
                ? `按顺序为 ${pendingCount} 段生成 AI 图，几十秒一段`
                : `按顺序生成 ${pendingCount} 段视频，可能要几分钟`
        }
        className={cn(
          'inline-flex items-center gap-1 rounded-md border px-2 py-1 text-[11px] font-medium transition-colors',
          disabled
            ? 'cursor-not-allowed border-border bg-background/40 text-muted-foreground'
            : 'border-primary/60 bg-primary/10 text-primary hover:bg-primary/20',
        )}
      >
        {loading
          ? `生成中…（${pendingCount}）`
          : mode === 'image'
            ? `🖼️ 一键 AI 生图补齐所有缺口（${pendingCount}）`
            : `🪄 一键 AI 补齐缺素材的镜头（${pendingCount}）`}
      </button>
      {err && <p className="text-[10px] text-destructive">{err}</p>}
    </div>
  )
}
