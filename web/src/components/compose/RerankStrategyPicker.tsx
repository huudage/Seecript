import { useMemo, useState } from 'react'

import type { Material, SectionRole } from '@/types/schemas'
import { cn } from '@/lib/utils'

/**
 * 重排策略选择器（stage-57）：
 *
 * 之前 rerank tab 只有「让 AI 挑一个素材填进来」单按钮，调用 /gap/fill 后端是个伪 stub
 * （fake mat-rerank-<hex> ID），且 plan.py:_pick 完全不认 rerank fill——这意味着用户根本
 * 没法挑素材填段。
 *
 * 现在拆三档：
 * - 🎯 高光优先：按 highlight_score + recommended_section 匹配度排，选评分最高的
 * - ⏱ 时长匹配：按 |mat.duration - section.duration_seconds| 升序选最贴近目标段时长的
 * - 🏷 主体匹配：按 mat.tags ∩ section.theme/content_description 词汇命中数排
 * - ✋ 我自己挑：展开下方素材网格手动选——会传 strategy=manual + target_material_id
 *
 * 已有 fill 时（用户已经填过本段），点策略 chip 会带 exclude_material_ids，避免点"换一个"
 * 返回同一个素材。
 *
 * 后端 _fill_rerank 会按本段 gap.section 优先过滤 material.recommended_section，
 * 拿不到时回退到全池排序——所以即使本段没有"专属推荐"，也始终能给出一个候选。
 */
export function RerankStrategyPicker({
  gapBusy,
  materials,
  targetSection,
  currentMaterialId,
  onPickStrategy,
  onPickManual,
}: {
  gapBusy: boolean
  materials: Material[]
  targetSection: SectionRole
  currentMaterialId?: string | null
  onPickStrategy: (strategy: 'highlight' | 'duration' | 'tag') => void
  onPickManual: (materialId: string) => void
}) {
  const [manualOpen, setManualOpen] = useState(false)

  const recommendedFirst = useMemo(() => {
    return materials.slice().sort((a, b) => {
      const aHit = a.recommended_section === targetSection ? 0 : 1
      const bHit = b.recommended_section === targetSection ? 0 : 1
      if (aHit !== bHit) return aHit - bHit
      return (b.highlight_score ?? 0) - (a.highlight_score ?? 0)
    })
  }, [materials, targetSection])

  const chipClass = (active = false) =>
    cn(
      'inline-flex items-center gap-1 rounded-md border px-2 py-1 text-[11px] transition-colors',
      active
        ? 'border-primary bg-primary/10 text-primary'
        : 'border-border bg-background hover:bg-secondary',
      gapBusy && 'cursor-not-allowed opacity-60',
    )

  if (materials.length === 0) {
    return (
      <div className="rounded-md border border-dashed border-amber-500/40 bg-amber-500/5 px-3 py-2 text-[11px] leading-relaxed text-foreground">
        ⚠️ 本项目还没上传任何素材——重排需要从素材库里挑画面。先去
        <strong>左侧素材库</strong>上传几条视频/图，再回到这里挑策略。
      </div>
    )
  }

  return (
    <div className="space-y-2 rounded-md border border-border bg-background/40 p-2">
      <div className="text-[11px] font-medium text-foreground">挑素材策略</div>
      <div className="flex flex-wrap items-center gap-1">
        <button
          type="button"
          onClick={() => onPickStrategy('highlight')}
          disabled={gapBusy}
          className={chipClass()}
          title="按高光评分（VLM 给的画面冲击力打分）+ 段位推荐挑"
        >
          🎯 高光优先
        </button>
        <button
          type="button"
          onClick={() => onPickStrategy('duration')}
          disabled={gapBusy}
          className={chipClass()}
          title="按时长贴近本段目标时长挑"
        >
          ⏱ 时长匹配
        </button>
        <button
          type="button"
          onClick={() => onPickStrategy('tag')}
          disabled={gapBusy}
          className={chipClass()}
          title="按 VLM 主体/标签与本段主题词重合度挑"
        >
          🏷 主体匹配
        </button>
        <button
          type="button"
          onClick={() => setManualOpen((v) => !v)}
          disabled={gapBusy}
          className={chipClass(manualOpen)}
          title="自己从素材库里挑一条"
        >
          ✋ 我自己挑
        </button>
        {currentMaterialId && (
          <span className="ml-auto text-[10px] text-muted-foreground">
            当前：{currentMaterialId.slice(0, 12)}…（点策略可换一个）
          </span>
        )}
      </div>

      {manualOpen && (
        <div className="max-h-44 overflow-y-auto rounded border border-border bg-background/60">
          <ul className="divide-y divide-border">
            {recommendedFirst.map((m) => {
              const isCurrent = m.material_id === currentMaterialId
              const recommendedHit = m.recommended_section === targetSection
              return (
                <li key={m.material_id}>
                  <button
                    type="button"
                    onClick={() => onPickManual(m.material_id)}
                    disabled={gapBusy || isCurrent}
                    className={cn(
                      'flex w-full items-center gap-2 px-2 py-1.5 text-left text-[11px] hover:bg-secondary',
                      (gapBusy || isCurrent) && 'cursor-not-allowed opacity-50',
                    )}
                  >
                    {m.thumbnail_url ? (
                      <img
                        src={m.thumbnail_url}
                        alt=""
                        className="h-8 w-12 flex-shrink-0 rounded object-cover"
                      />
                    ) : (
                      <span className="inline-flex h-8 w-12 flex-shrink-0 items-center justify-center rounded bg-slate-200 text-[10px] text-slate-500">
                        {m.media_type === 'image' ? '图' : m.media_type === 'audio' ? '音' : '视'}
                      </span>
                    )}
                    <div className="min-w-0 flex-1">
                      <div className="truncate font-medium text-foreground">
                        {m.filename}
                        {isCurrent && <span className="ml-1 text-primary">· 当前</span>}
                      </div>
                      <div className="truncate text-[10px] text-muted-foreground">
                        {m.duration_seconds ? `${m.duration_seconds.toFixed(1)}s · ` : ''}
                        高光 {(m.highlight_score ?? 0).toFixed(2)}
                        {recommendedHit && ' · 段位推荐'}
                      </div>
                    </div>
                  </button>
                </li>
              )
            })}
          </ul>
        </div>
      )}
    </div>
  )
}
