import { useEffect } from 'react'

import type { Gap } from '@/types/schemas'
import { SECTION_LABEL } from '@/lib/sections'

/**
 * 点击缺口槽位后弹出的样例截图大图。
 * - sample_thumbnail_url 来自后端 detect 时按 sec.shot_indices[0] 写入
 * - Esc 关闭、点遮罩关闭；不引入 Dialog 库，原生 fixed overlay 够用
 */
export function GapPreviewDialog({ gap, onClose }: { gap: Gap | null; onClose: () => void }) {
  useEffect(() => {
    if (!gap) return
    const handler = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', handler)
    return () => window.removeEventListener('keydown', handler)
  }, [gap, onClose])

  if (!gap) return null

  return (
    <div
      role="dialog"
      aria-modal="true"
      onClick={onClose}
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4"
    >
      <div
        onClick={(e) => e.stopPropagation()}
        className="w-full max-w-2xl overflow-hidden rounded-lg border border-border bg-card shadow-xl"
      >
        <div className="flex items-center justify-between border-b border-border px-4 py-2">
          <h3 className="text-sm font-semibold">
            {SECTION_LABEL[gap.section]} · slot {gap.slot_index}
          </h3>
          <button
            onClick={onClose}
            className="rounded text-muted-foreground hover:text-foreground"
            aria-label="关闭"
          >
            ✕
          </button>
        </div>
        <div className="space-y-3 p-4">
          {gap.sample_thumbnail_url ? (
            <img
              src={gap.sample_thumbnail_url}
              alt={`样例 ${gap.section} 截图`}
              className="max-h-96 w-full rounded-md border border-border object-contain"
            />
          ) : (
            <div className="flex h-48 items-center justify-center rounded-md border border-dashed border-border text-xs text-muted-foreground">
              样例无截图（mock 数据或未抽帧）
            </div>
          )}
          <div className="space-y-1 text-sm">
            <p className="font-medium">需求</p>
            <p className="text-muted-foreground">{gap.requirement}</p>
          </div>
          {gap.note && (
            <div className="space-y-1 text-sm">
              <p className="font-medium">备注</p>
              <p className="text-muted-foreground">{gap.note}</p>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
