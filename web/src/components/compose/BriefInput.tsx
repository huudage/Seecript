import { type ChangeEvent } from 'react'

import { cn } from '@/lib/utils'

/**
 * 主题/卖点输入框。Compose 页左栏第一块。
 * 受控组件——值绑到 session store 的 brief，submit 时透传给 /plan/build。
 * 500 字硬上限（与后端 schema PlanBuildRequest.brief 一致）。
 */
export function BriefInput({
  value,
  onChange,
  placeholder = '一句话讲清这条视频的主题或卖点，比如「咖啡店探店 · 突出沉浸式氛围 · 18-25 岁女性」',
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
        <label className="text-xs font-semibold text-foreground">主题 / 卖点</label>
        <span className={cn('font-mono text-[10px]', len > 450 ? 'text-amber-600' : 'text-muted-foreground')}>
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
    </div>
  )
}
