import { cn } from '@/lib/utils'
import type { StepStatus } from '@/types/schemas'

/** 步骤状态点：颜色编码本步在工作流中的位置。 */
const COLOR: Record<StepStatus, string> = {
  pending: 'bg-muted',
  in_progress: 'bg-sky-400 animate-pulse',
  saved: 'bg-emerald-500',
  dirty: 'bg-amber-400',
}

const LABEL: Record<StepStatus, string> = {
  pending: '未开始',
  in_progress: '进行中',
  saved: '已保存',
  dirty: '上游已变，建议重新提交',
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
