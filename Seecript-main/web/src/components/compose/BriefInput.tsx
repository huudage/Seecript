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
  required = false,
  showError = false,
}: {
  value: string
  onChange: (next: string) => void
  placeholder?: string
  required?: boolean
  showError?: boolean
}) {
  const handle = (e: ChangeEvent<HTMLTextAreaElement>) => onChange(e.target.value.slice(0, 500))
  const len = value.length
  const empty = value.trim().length === 0
  const errorVisible = required && showError && empty
  return (
    <div>
      <div className="mb-2 flex items-center justify-between">
        <label className="text-xs font-semibold text-foreground">
          主题 / 卖点{required && <span className="ml-1 text-destructive">*</span>}
        </label>
        <span className={cn('font-mono text-xs', len > 450 ? 'text-amber-600' : 'text-muted-foreground')}>
          {len}/500
        </span>
      </div>
      <textarea
        value={value}
        onChange={handle}
        rows={3}
        placeholder={placeholder}
        aria-invalid={errorVisible}
        className={cn(
          'w-full resize-y rounded-md border bg-background/60 p-2 text-sm leading-relaxed outline-none placeholder:text-muted-foreground/70 focus:border-primary',
          errorVisible ? 'border-destructive' : 'border-border',
        )}
      />
      {errorVisible && (
        <p className="mt-1 text-xs text-destructive">主题不能为空——AI 需要它来定方向。</p>
      )}
    </div>
  )
}
