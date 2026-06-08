import { useCallback, useState } from 'react'

import { api } from '@/api/client'
import { cn } from '@/lib/utils'
import type { GapFillAllRequest, GapFillAllResponse, TextCardSpec } from '@/types/schemas'

/**
 * 一键字卡画面补全：必须先有一个采纳过的字卡作为视觉样板才能批量。
 *
 * 设计取舍：
 * - 字卡画面是 ffmpeg 即烧（背景色/字号/对齐/字体可全自定义），AI 没有先验偏好；
 *   要求用户先手动出一个『样板字卡』，后端 batch 跑时引用其 text_card_spec 风格
 *   保证整片视觉统一。无样板时 disabled，并 hover 提示。
 * - 一键 AIGC 视频补全已删除——AIGC 视频是重资产 (¥10/段, ~3min)，不适合无差别批量。
 *   要补 AIGC 视频请用单段 FillAigcPanel 主动触发。
 */
export function BatchCopyButton({
  planId,
  pendingCount,
  skipGapIds,
  adoptedTextCardCount,
  existingTextCards,
  onDone,
  onLoadingChange,
}: {
  planId: string | null
  pendingCount: number
  /** 已采纳的 gap_id 列表；后端会跳过这些避免覆盖单条手动结果。 */
  skipGapIds?: string[]
  /** 已采纳的字卡数（action=copy 且 status=ok）。<1 则按钮 disabled。 */
  adoptedTextCardCount: number
  /** 已采纳的字卡 spec 列表——透传给后端做风格样板，绕过 plan_id 时序竞态。 */
  existingTextCards?: TextCardSpec[]
  onDone: (resp: GapFillAllResponse) => void
  /** stage-26 PR-N.6：把内部 loading 同步给父组件，用于 step3 准入门控 */
  onLoadingChange?: (busy: boolean) => void
}) {
  const [loading, setLoading] = useState(false)
  const [err, setErr] = useState<string | null>(null)

  const noSample = adoptedTextCardCount < 1
  const disabled = !planId || pendingCount === 0 || loading || noSample

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
        action: 'copy',
        skip_gap_ids: skipGapIds && skipGapIds.length > 0 ? skipGapIds : undefined,
        existing_text_cards:
          existingTextCards && existingTextCards.length > 0 ? existingTextCards : undefined,
      }
      const resp = await api.post<GapFillAllResponse>('/gap/fill-all', body)
      onDone(resp)
    } catch (e) {
      setErr(e instanceof Error ? e.message : '批量字卡补全失败')
    } finally {
      setBusy(false)
    }
  }, [existingTextCards, onDone, planId, setBusy, skipGapIds])

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
              : noSample
                ? '请先手动生成并采纳一个字卡作为样板，AI 才能参照样式批量补齐'
                : `按 ${adoptedTextCardCount} 个已采纳字卡的样式批量补齐 ${pendingCount} 段（已采纳的不会被覆盖）`
        }
        className={cn(
          'inline-flex items-center gap-1 rounded-md border px-2 py-1 text-[11px] font-medium transition-colors',
          disabled
            ? 'cursor-not-allowed border-border bg-background/40 text-muted-foreground'
            : 'border-emerald-500/60 bg-emerald-500/10 text-emerald-700 hover:bg-emerald-500/20 dark:text-emerald-300',
        )}
      >
        {loading
          ? `生成字卡中…（${pendingCount}）`
          : noSample
            ? `🔒 一键补字卡（需先手动出 1 张样板）`
            : `✨ 参照样板批量补字卡（${pendingCount}）`}
      </button>
      {err && <p className="text-[10px] text-destructive">{err}</p>}
    </div>
  )
}
