import { useMemo, useState } from 'react'

import { TimelineTrack } from '@/components/timeline/TimelineTrack'
import type { FillResult, Material, Plan, Scene } from '@/types/schemas'
import { SECTION_BG } from '@/lib/sections'
import { cn } from '@/lib/utils'

/**
 * 结构重排预览：双轨时间轴对比 (原序 vs 新序)。
 *
 * - 后端的 rerank 已经返回 new_material_id，但还不会改 plan.main_track 顺序；
 *   这里只做"如果把该素材放到目标 slot 会变成什么样"的可视化预览
 * - 用户点采纳 → 把 FillResult 写入 upsertFill；外层后续触发 /plan/build 重建
 * - 双轨配色统一用 SECTION_BG；scene_id 短化只取尾段
 */
export function FillRerankPanel({
  plan,
  fill,
  materials,
  onApply,
  onCancel,
  loading,
}: {
  plan: Plan
  fill: FillResult
  materials: Material[]
  onApply: () => void
  onCancel?: () => void
  loading?: boolean
}) {
  const [confirmed, setConfirmed] = useState(false)

  const swapMaterial = useMemo(
    () => materials.find((m) => m.material_id === fill.new_material_id) ?? null,
    [fill.new_material_id, materials],
  )

  const reranked = useMemo(() => simulateRerank(plan.main_track, fill, swapMaterial), [plan, fill, swapMaterial])

  const renderItems = (scenes: Scene[]) =>
    scenes.map((sc) => ({
      key: sc.scene_id,
      start: sc.start,
      end: sc.start + sc.duration,
      color: SECTION_BG[sc.section],
      text: shortRef(sc.scene_id),
    }))

  return (
    <div className="space-y-3 rounded-md border border-border bg-background/40 p-3">
      <div className="flex items-center justify-between">
        <h4 className="text-xs font-semibold">结构重排预览</h4>
        <span className="text-[11px] text-muted-foreground">
          {swapMaterial
            ? `候选素材 ${swapMaterial.filename}`
            : fill.new_material_id
              ? `候选 ${fill.new_material_id}`
              : '无候选素材'}
        </span>
      </div>

      <TimelineTrack label="原 Plan" duration={plan.duration_seconds || 1} items={renderItems(plan.main_track)} />
      <TimelineTrack label="重排后" duration={plan.duration_seconds || 1} items={renderItems(reranked)} />

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
          onClick={() => {
            setConfirmed(true)
            onApply()
          }}
          disabled={loading || confirmed}
          className={cn(
            'rounded-md bg-primary px-3 py-1 text-xs font-medium text-primary-foreground',
            (loading || confirmed) && 'cursor-not-allowed opacity-60',
          )}
        >
          {confirmed ? '已采纳' : loading ? '应用中…' : '采纳重排'}
        </button>
      </div>
    </div>
  )
}

/** 直接拷一份 main_track，把建议素材放到第一个匹配 section 的 slot；视觉用。 */
function simulateRerank(scenes: Scene[], fill: FillResult, material: Material | null): Scene[] {
  if (!material) return scenes
  const targetSection = material.recommended_section ?? scenes[0]?.section
  if (!targetSection) return scenes
  const idx = scenes.findIndex((s) => s.section === targetSection)
  if (idx < 0) return scenes
  const next = scenes.slice()
  next[idx] = {
    ...next[idx],
    source: 'user_material',
    source_ref: material.material_id,
    narration: fill.narration ?? next[idx].narration,
  }
  return next
}

function shortRef(id: string): string {
  const tail = id.split(/[-_]/).pop() ?? id
  return tail.length > 10 ? tail.slice(0, 10) : tail
}
