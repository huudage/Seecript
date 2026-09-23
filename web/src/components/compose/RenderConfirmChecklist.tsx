import type { ChecklistItem } from '@/lib/renderChecklist'

/**
 * 出片前确认清单（PRD-v2 US-7.1 / US-8.3）。
 * 有未确认项时只列出来，不提供「忽略并渲染」。
 */

interface Props {
  items: ChecklistItem[]
  confirming: boolean
  onConfirmStructure: () => void
  onClose: () => void
}

export function RenderConfirmChecklist({
  items,
  confirming,
  onConfirmStructure,
  onClose,
}: Props) {
  const hasDraft = items.some((item) => item.kind === 'draft')
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4">
      <div className="flex max-h-[80vh] w-full max-w-lg flex-col gap-3 rounded-lg border border-border bg-card p-4 shadow-xl">
        <div>
          <h3 className="text-sm font-semibold">渲染确认清单</h3>
          <p className="mt-1 text-[11px] text-muted-foreground">
            结构还是初稿时需要先定稿。空槽不挡出片，系统也不会自动补缺口。
          </p>
        </div>
        <ul className="min-h-0 flex-1 space-y-2 overflow-y-auto">
          {items.map((item) => (
            <li
              key={item.id}
              className="rounded-md border border-amber-500/40 bg-amber-500/10 px-2 py-1.5 text-xs leading-relaxed"
            >
              {item.label}
            </li>
          ))}
        </ul>
        <div className="flex justify-end gap-2">
          <button
            type="button"
            onClick={onClose}
            className="rounded-md border border-border bg-card px-3 py-1.5 text-xs font-medium hover:bg-secondary"
          >
            回到画布
          </button>
          {hasDraft && (
            <button
              type="button"
              disabled={confirming}
              onClick={onConfirmStructure}
              className="rounded-md bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground hover:opacity-90 disabled:opacity-50"
            >
              {confirming ? '定稿中…' : '定稿'}
            </button>
          )}
        </div>
      </div>
    </div>
  )
}
