import { type ChangeEvent } from 'react'

import { cn } from '@/lib/utils'

/**
 * 视频要求与目的输入框。Compose 页左栏，紧跟在 BriefInput 之下。
 * 受控组件——值绑到 session store 的 videoGoal，submit 时透传给 /plan/build。
 * 与 brief 一起驱动后端 plan_agent 的结构改编。非必填，500 字硬上限。
 */
export function VideoGoalInput({
  value,
  onChange,
  placeholder = '说清视频的要求与目的，比如「30 秒内讲清产品差异化卖点；面向初次接触的用户；节奏紧凑、避免行业黑话」',
}: {
  value: string
  onChange: (next: string) => void
  placeholder?: string
}) {
  const handle = (e: ChangeEvent<HTMLTextAreaElement>) => onChange(e.target.value.slice(0, 500))
  const len = value.length
  return (
    <div>
      <div className="mb-2 flex items-center justify-between">
        <label className="text-xs font-semibold text-foreground">视频要求与目的</label>
        <span className={cn('font-mono text-xs', len > 450 ? 'text-amber-600' : 'text-muted-foreground')}>
          {len}/500
        </span>
      </div>
      <textarea
        value={value}
        onChange={handle}
        rows={3}
        placeholder={placeholder}
        className="w-full resize-y rounded-md border border-border bg-background/60 p-2 text-sm leading-relaxed outline-none placeholder:text-muted-foreground/70 focus:border-primary"
      />
      <p className="mt-1 text-xs text-muted-foreground">
        可选——填了它，结构会按你的目的改编样例骨架，而不是照搬。
      </p>
    </div>
  )
}
