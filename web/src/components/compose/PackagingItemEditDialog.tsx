import { useEffect, useState } from 'react'

import { api } from '@/api/client'
import { cn } from '@/lib/utils'
import type {
  PackagingItem,
  PackagingItemPlaceRequest,
  Plan,
  PlanId,
} from '@/types/schemas'

/**
 * 包装组件编辑弹窗（PR-I.2）：替代 step3 包装轨上拖动+重生按钮的统一入口。
 *
 * 触发：FourTrackBoard 包装轨某个 item 被点击 → 父级 setEditingItem(item) → 此弹窗打开。
 * 保存：复用 POST /packaging/items/place（同 item_id 已存在 → 替换；不存在 → append）。
 *
 * 字段：
 * - text：文案（标题条 / 贴纸主文 / 封面标题，全用同一个字段）
 * - start / end：时间窗（秒，精确到 0.1s）
 * - style.color / style.position：常用样式快捷参数；其他保留原 style.* 不动
 *
 * 注：style 是 Record<string, unknown>，前端不强约束所有 key；
 * 这里只对 color / position 两个最常用的提供 UI；其他维度由 LLM 草稿生成时已经填好，
 * 用户改完高频字段后剩下的低频字段保持原值，避免误删 Remotion 渲染需要的元数据。
 */
export function PackagingItemEditDialog({
  open,
  item,
  planId,
  onClose,
  onPlanUpdated,
}: {
  open: boolean
  item: PackagingItem | null
  planId: PlanId | null
  onClose: () => void
  onPlanUpdated: (plan: Plan) => void
}) {
  const [text, setText] = useState('')
  const [start, setStart] = useState(0)
  const [end, setEnd] = useState(0)
  const [color, setColor] = useState('#FFFFFF')
  const [position, setPosition] = useState<string>('center')
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (!open || !item) return
    setText(item.text ?? '')
    setStart(item.start)
    setEnd(item.end)
    const style = item.style ?? {}
    setColor(typeof style.color === 'string' ? (style.color as string) : '#FFFFFF')
    setPosition(typeof style.position === 'string' ? (style.position as string) : 'center')
    setError(null)
  }, [open, item])

  if (!open || !item) return null

  const kindLabel: Record<PackagingItem['kind'], string> = {
    subtitle: '字幕',
    title_bar: '标题条',
    sticker: '贴纸',
    transition: '转场',
    cover: '封面',
  }

  const handleSave = async () => {
    if (!item || !planId) return
    if (end <= start) {
      setError('结束时间必须大于开始时间')
      return
    }
    setSaving(true)
    setError(null)
    try {
      const nextStyle: Record<string, unknown> = { ...(item.style ?? {}) }
      nextStyle.color = color
      nextStyle.position = position
      const nextItem: PackagingItem = {
        ...item,
        text: text.trim() || null,
        start: Math.max(0, Math.round(start * 10) / 10),
        end: Math.max(0.1, Math.round(end * 10) / 10),
        style: nextStyle,
      }
      const body: PackagingItemPlaceRequest = { plan_id: planId, item: nextItem }
      const updated = await api.post<Plan>('/packaging/items/place', body)
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
            编辑{kindLabel[item.kind]} · <span className="font-mono text-[11px] text-muted-foreground">{item.item_id}</span>
          </h3>
          <button
            onClick={onClose}
            className="rounded text-muted-foreground hover:text-foreground"
            aria-label="关闭"
          >
            ×
          </button>
        </header>

        <div className="space-y-3">
          <label className="block">
            <span className="mb-1 block text-[11px] font-medium text-muted-foreground">文案</span>
            <textarea
              value={text}
              onChange={(e) => setText(e.target.value)}
              rows={3}
              placeholder={item.kind === 'cover' ? '封面主标题' : '组件文案；留空使用默认占位'}
              className="w-full resize-none rounded-md border border-border bg-background px-2 py-1.5 text-sm leading-relaxed shadow-sm focus:outline-none focus:ring-2 focus:ring-primary/40"
            />
          </label>

          <div className="grid grid-cols-2 gap-3">
            <label className="block">
              <span className="mb-1 block text-[11px] font-medium text-muted-foreground">开始（秒）</span>
              <input
                type="number"
                step={0.1}
                min={0}
                value={start}
                onChange={(e) => setStart(parseFloat(e.target.value) || 0)}
                className="w-full rounded-md border border-border bg-background px-2 py-1.5 text-sm shadow-sm focus:outline-none focus:ring-2 focus:ring-primary/40"
              />
            </label>
            <label className="block">
              <span className="mb-1 block text-[11px] font-medium text-muted-foreground">结束（秒）</span>
              <input
                type="number"
                step={0.1}
                min={0}
                value={end}
                onChange={(e) => setEnd(parseFloat(e.target.value) || 0)}
                className="w-full rounded-md border border-border bg-background px-2 py-1.5 text-sm shadow-sm focus:outline-none focus:ring-2 focus:ring-primary/40"
              />
            </label>
          </div>

          <div className="grid grid-cols-2 gap-3">
            <label className="block">
              <span className="mb-1 block text-[11px] font-medium text-muted-foreground">主色</span>
              <div className="flex items-center gap-2">
                <input
                  type="color"
                  value={color.startsWith('#') ? color : '#FFFFFF'}
                  onChange={(e) => setColor(e.target.value.toUpperCase())}
                  className="h-8 w-12 cursor-pointer rounded border border-border bg-background"
                />
                <input
                  type="text"
                  value={color}
                  onChange={(e) => setColor(e.target.value)}
                  className="flex-1 rounded-md border border-border bg-background px-2 py-1 font-mono text-xs shadow-sm focus:outline-none focus:ring-2 focus:ring-primary/40"
                />
              </div>
            </label>
            <label className="block">
              <span className="mb-1 block text-[11px] font-medium text-muted-foreground">位置</span>
              <select
                value={position}
                onChange={(e) => setPosition(e.target.value)}
                className="w-full rounded-md border border-border bg-background px-2 py-1.5 text-sm shadow-sm focus:outline-none focus:ring-2 focus:ring-primary/40"
              >
                <option value="top">顶部</option>
                <option value="center">居中</option>
                <option value="bottom">底部</option>
                <option value="top_left">左上</option>
                <option value="top_right">右上</option>
                <option value="bottom_left">左下</option>
                <option value="bottom_right">右下</option>
              </select>
            </label>
          </div>
        </div>

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
            {saving ? '保存中…' : '保存'}
          </button>
        </div>
      </div>
    </div>
  )
}
