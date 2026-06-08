import { cn } from '@/lib/utils'
import type { StepStatus } from '@/types/schemas'

/** 步骤状态点：字节系增强——in_progress 带脉冲光环。 */
const COLOR: Record<StepStatus, string> = {
  pending: 'bg-muted',
  in_progress: 'bg-primary animate-step-pulse',
  saved: 'bg-emerald-500',
  dirty: 'bg-amber-400',
}

const LABEL: Record<StepStatus, string> = {
  pending: '未开始',
  in_progress: '处理中',
  saved: '已完成',
  dirty: '需要更新',
}

export function StepIndicator({ status }: { status: StepStatus }) {
  return (
    <span
      title={LABEL[status]}
      className={cn(
        'inline-block h-2 w-2 rounded-full border border-border/40',
        COLOR[status],
      )}
    />
  )
}
