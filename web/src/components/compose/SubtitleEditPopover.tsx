import { useEffect, useRef, useState } from 'react'

import { patchPlanScene } from '@/api/plan'
import { cn } from '@/lib/utils'
import type { Plan, PlanId, Scene } from '@/types/schemas'

/**
 * 字幕浮窗编辑器（R3）：step3 字幕轨某段被点击 → 在画面正中弹出此浮窗，
 * 用户用最朴实的 textarea 改这一段的 narration（也就是字幕文本）。
 *
 * 设计取舍：
 * - 不走自然语言 → 这是手动编辑，完全是用户精准输入；NL 编辑走 ⌘K command bar。
 * - 不联动重跑 LLM；后端 PATCH /plan/{plan_id}/scene/{scene_id} 只落盘 + 返回新 Plan。
 *   口播是否同步重合成由父级决策（当前策略：同字段 narration 写后下一轮 TTS 自动用新文案）。
 * - 不改 theme/content_description——那两条要在 step1 内容轨编辑场景里改，避免错位。
 */
export function SubtitleEditPopover({
  open,
  scene,
  planId,
  onClose,
  onPlanUpdated,
}: {
  open: boolean
  scene: Scene | null
  planId: PlanId | null
  onClose: () => void
  onPlanUpdated: (plan: Plan) => void
}) {
  const [text, setText] = useState('')
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const taRef = useRef<HTMLTextAreaElement | null>(null)

  useEffect(() => {
    if (open && scene) {
      setText(scene.narration ?? '')
      setError(null)
      // 进场即聚焦 + 全选，方便用户直接改写
      requestAnimationFrame(() => {
        if (taRef.current) {
          taRef.current.focus()
          taRef.current.select()
        }
      })
    }
  }, [open, scene])

  const handleSave = async () => {
    if (!scene || !planId) return
    setSaving(true)
    setError(null)
    try {
      const updated = await patchPlanScene(planId, scene.scene_id, {
        narration: text.trim(),
      })
      onPlanUpdated(updated)
      onClose()
    } catch (err) {
      setError(err instanceof Error ? err.message : '保存失败')
    } finally {
      setSaving(false)
    }
  }

  if (!open || !scene) return null

  const charCount = text.trim().length
  const expectedSec = scene.duration
  const recommendedChars = Math.max(8, Math.round(expectedSec * 4))
  const overLength = charCount > recommendedChars * 1.4

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/45"
      onClick={onClose}
    >
      <div
        className="w-full max-w-md rounded-lg border border-border bg-card p-4 shadow-xl"
        onClick={(e) => e.stopPropagation()}
      >
        <header className="mb-2 flex items-center justify-between">
          <h3 className="text-sm font-semibold">编辑字幕 · {scene.scene_id}</h3>
          <button
            onClick={onClose}
            className="rounded text-muted-foreground hover:text-foreground"
            aria-label="关闭"
          >
            ×
          </button>
        </header>

        <p className="mb-2 text-[11px] leading-relaxed text-muted-foreground">
          本段时长 {expectedSec.toFixed(1)}s，建议字数 ~{recommendedChars} 字（中文 4 字/秒）。
          字幕只决定下一次合成的烧入文本，不会重跑 AI；如需重合成口播，请在口播轨里点重合成。
        </p>

        <textarea
          ref={taRef}
          value={text}
          onChange={(e) => setText(e.target.value)}
          rows={4}
          placeholder="留空表示该段不烧字幕"
          className={cn(
            'w-full resize-none rounded-md border bg-background px-3 py-2 text-sm leading-relaxed shadow-sm focus:outline-none focus:ring-2 focus:ring-primary/40',
            overLength ? 'border-amber-500/60' : 'border-border',
          )}
        />

        <div className="mt-1 flex items-center justify-between text-[10px] text-muted-foreground">
          <span className={overLength ? 'text-amber-600 dark:text-amber-300' : ''}>
            {charCount} 字 {overLength ? `（超出建议 ${Math.round(charCount - recommendedChars)} 字，可能播不完）` : ''}
          </span>
          <span className="font-mono">duration {expectedSec.toFixed(1)}s</span>
        </div>

        {error && (
          <p className="mt-2 rounded-md border border-destructive/40 bg-destructive/5 px-2 py-1 text-[11px] text-destructive">
            {error}
          </p>
        )}

        <div className="mt-3 flex items-center justify-end gap-2">
          <button
            onClick={onClose}
            disabled={saving}
            className="rounded-md border border-border bg-background/60 px-3 py-1.5 text-xs hover:bg-secondary disabled:opacity-60"
          >
            取消
          </button>
          <button
            onClick={() => void handleSave()}
            disabled={saving}
            className={cn(
              'rounded-md bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground transition-opacity',
              saving && 'cursor-wait opacity-70',
            )}
          >
            {saving ? '保存中…' : '保存'}
          </button>
        </div>
      </div>
    </div>
  )
}
