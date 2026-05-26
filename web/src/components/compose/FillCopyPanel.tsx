import { useEffect, useState } from 'react'

import type { FillResult } from '@/types/schemas'
import { cn } from '@/lib/utils'

/**
 * 文案补全：editable narration + alternatives 三选一。
 *
 * - fill.narration 是 LLM 第一版；fill.alternatives 是 2 个备选
 * - 用户可以编辑后采纳；onAdopt 把最终 narration 回写到 store（覆盖 fill）
 * - 已确认：不做 TTS 试听
 */
export function FillCopyPanel({
  fill,
  onAdopt,
  onCancel,
  loading,
}: {
  fill: FillResult
  onAdopt: (finalNarration: string) => void
  onCancel?: () => void
  loading?: boolean
}) {
  const [text, setText] = useState(fill.narration ?? '')

  // fill 变化（重新生成）时同步 textarea
  useEffect(() => {
    setText(fill.narration ?? '')
  }, [fill.narration, fill.gap_id])

  const len = text.length
  return (
    <div className="space-y-2 rounded-md border border-border bg-background/40 p-3">
      <div className="flex items-center justify-between">
        <h4 className="text-xs font-semibold">文案补全</h4>
        <span className="font-mono text-[10px] text-muted-foreground">{len} 字</span>
      </div>

      <textarea
        value={text}
        onChange={(e) => setText(e.target.value.slice(0, 300))}
        rows={3}
        placeholder="LLM 还没给出文案"
        className="w-full resize-y rounded-md border border-border bg-background px-2 py-1.5 text-sm leading-relaxed outline-none focus:border-primary"
      />

      {fill.alternatives.length > 0 && (
        <div className="space-y-1">
          <p className="text-[11px] text-muted-foreground">备选（点击替换上方文案）</p>
          <div className="flex flex-col gap-1">
            {fill.alternatives.map((alt, i) => (
              <button
                key={`${i}-${alt.slice(0, 8)}`}
                onClick={() => setText(alt)}
                className={cn(
                  'rounded-md border border-border bg-background px-2 py-1 text-left text-xs hover:bg-secondary',
                  text === alt && 'border-primary bg-primary/5',
                )}
              >
                {alt}
              </button>
            ))}
          </div>
        </div>
      )}

      {fill.note && <p className="text-[11px] text-muted-foreground">{fill.note}</p>}

      <div className="flex items-center justify-end gap-2">
        {onCancel && (
          <button
            onClick={onCancel}
            className="rounded-md border border-border bg-background px-3 py-1 text-xs hover:bg-secondary"
          >
            取消
          </button>
        )}
        <button
          onClick={() => onAdopt(text.trim())}
          disabled={loading || text.trim().length === 0}
          className={cn(
            'rounded-md bg-primary px-3 py-1 text-xs font-medium text-primary-foreground',
            (loading || text.trim().length === 0) && 'cursor-not-allowed opacity-60',
          )}
        >
          {loading ? '应用中…' : '采纳文案'}
        </button>
      </div>
    </div>
  )
}
