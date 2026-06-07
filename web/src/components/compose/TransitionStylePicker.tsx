import { useEffect, useState } from 'react'

import { patchSceneTransition } from '@/api/plan'
import { cn } from '@/lib/utils'
import { TRANSITION_LABEL, TRANSITION_TONE } from '@/lib/transitions'
import type { Plan, PlanId, TransitionStyle } from '@/types/schemas'

const STYLE_ORDER: TransitionStyle[] = [
  'hard_cut',
  'dissolve',
  'slide',
  'zoom',
  'whip',
  'wipe',
]

const STYLE_HINT: Record<TransitionStyle, string> = {
  hard_cut: '硬切（无过渡，concat demuxer 拼接，0 开销）',
  dissolve: '溶解（前后两段交叉淡化，最稳妥的剧情转场）',
  slide: '滑动（下一段从一侧推入，节奏感强）',
  zoom: '推拉（焦点放大/缩小，强调反转或揭示）',
  whip: '甩切（横向甩切，适合短切、高能片段）',
  wipe: '扫切（以擦除方式切换，节目串联感强）',
}

/**
 * 转场样式选择弹窗（PR-I.2）：用户在包装轨上点击「⇆」节点时唤起。
 *
 * 后端 PATCH /plan/{plan_id}/scene/{scene_id}/transition 落盘 + 返回新 Plan。
 * 节点本身不可拖动（位置由两镜衔接点决定，是结构属性而不是可调位的元素）。
 */
export function TransitionStylePicker({
  open,
  sceneId,
  currentStyle,
  planId,
  onClose,
  onPlanUpdated,
}: {
  open: boolean
  sceneId: string | null
  currentStyle: TransitionStyle | null
  planId: PlanId | null
  onClose: () => void
  onPlanUpdated: (plan: Plan) => void
}) {
  const [picked, setPicked] = useState<TransitionStyle>('hard_cut')
  const [duration, setDuration] = useState<number>(0.4)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (!open) return
    setPicked(currentStyle ?? 'hard_cut')
    setDuration(0.4)
    setError(null)
  }, [open, currentStyle])

  if (!open || !sceneId) return null

  const handleSave = async () => {
    if (!sceneId || !planId) return
    setSaving(true)
    setError(null)
    try {
      const updated = await patchSceneTransition(planId, sceneId, {
        style: picked,
        duration: picked === 'hard_cut' ? null : duration,
      })
      onPlanUpdated(updated)
      onClose()
    } catch (err) {
      setError(err instanceof Error ? err.message : '保存失败')
    } finally {
      setSaving(false)
    }
  }

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/45"
      onClick={onClose}
    >
      <div
        className="w-full max-w-md rounded-lg border border-border bg-card p-4 shadow-xl"
        onClick={(e) => e.stopPropagation()}
      >
        <header className="mb-3 flex items-center justify-between">
          <h3 className="text-sm font-semibold">
            转场样式 · <span className="font-mono text-[11px] text-muted-foreground">{sceneId}</span>
          </h3>
          <button
            onClick={onClose}
            className="rounded text-muted-foreground hover:text-foreground"
            aria-label="关闭"
          >
            ×
          </button>
        </header>

        <p className="mb-3 text-[11px] leading-relaxed text-muted-foreground">
          切换该分镜与上一段的衔接方式；硬切走 concat 直拼，其他走 ffmpeg xfade。
        </p>

        <div className="grid grid-cols-3 gap-2">
          {STYLE_ORDER.map((style) => {
            const selected = picked === style
            return (
              <button
                key={style}
                type="button"
                onClick={() => setPicked(style)}
                className={cn(
                  'rounded-md border px-2 py-2 text-left text-xs transition',
                  TRANSITION_TONE[style],
                  selected
                    ? 'border-primary ring-2 ring-primary/40'
                    : 'border-border hover:brightness-110',
                )}
                title={STYLE_HINT[style]}
              >
                <div className="font-semibold">{TRANSITION_LABEL[style]}</div>
                <div className="mt-0.5 line-clamp-2 text-[10px] opacity-80">{STYLE_HINT[style]}</div>
              </button>
            )
          })}
        </div>

        {picked !== 'hard_cut' && (
          <label className="mt-3 block">
            <span className="mb-1 block text-[11px] font-medium text-muted-foreground">
              转场时长（秒，0.1 ~ 1.5）
            </span>
            <input
              type="range"
              min={0.1}
              max={1.5}
              step={0.1}
              value={duration}
              onChange={(e) => setDuration(parseFloat(e.target.value))}
              className="w-full accent-primary"
            />
            <div className="mt-1 text-right font-mono text-[11px] text-muted-foreground">
              {duration.toFixed(1)}s
            </div>
          </label>
        )}

        {error && (
          <p className="mt-3 rounded-md border border-destructive/40 bg-destructive/5 px-2 py-1 text-[11px] text-destructive">
            {error}
          </p>
        )}

        <div className="mt-4 flex items-center justify-end gap-2">
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
            {saving ? '保存中…' : '应用'}
          </button>
        </div>
      </div>
    </div>
  )
}
