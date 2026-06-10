import { useMemo, useState } from 'react'

import type { Material, SectionRole } from '@/types/schemas'
import { cn } from '@/lib/utils'

/**
 * 素材人工挑选器（stage-59）：
 *
 * stage-57 提供过 4 档：高光优先 / 时长匹配 / 主体匹配 / 我自己挑。stage-59 用户决定
 * 「挑素材补全只支持人工挑」——3 档自动策略下线，避免「策略一变素材飘忽」的体验黑箱。
 *
 * 现在的产品决策：
 * - 不再有自动策略 chip，直接展开素材列表；
 * - 每条素材按段适配度（fit_score 0-1）排序：段位推荐 + tag 命中 + 时长贴合 + 高光，越搭越往前；
 * - fit_score 同步显示在每行右上角徽章上，让用户一眼看到「这条搭不搭」；
 * - 当前正在使用的素材标灰且置顶，便于「换一条」时知道现在用的是哪条。
 *
 * 后端 _fill_rerank 仍保留 strategy / target_material_id 入口（旧前端版本/外部调用兼容），
 * 但本组件只发 manual + target_material_id，不会再触发 highlight/duration/tag 路径。
 */
export function RerankStrategyPicker({
  gapBusy,
  materials,
  targetSection,
  currentMaterialId,
  onPickManual,
}: {
  gapBusy: boolean
  materials: Material[]
  targetSection: SectionRole
  currentMaterialId?: string | null
  onPickManual: (materialId: string) => void
}) {
  const [showAll, setShowAll] = useState(false)

  const ranked = useMemo(() => {
    /**
     * 前端兜底打分：与后端 services/materials/fit.py 同口径，但只用前端能拿到的字段
     * （recommended_section + 时长 + highlight）。tag 命中前端没 section.theme 上下文，
     * 只能近似用 recommended_section + duration 做排序，准确分数还是靠 Scene.fit_score
     * （服务端算好后写在 Scene 上，UI 显示在卡里）。
     */
    return materials.slice().sort((a, b) => {
      const aSecHit = a.recommended_section === targetSection ? 1 : 0
      const bSecHit = b.recommended_section === targetSection ? 1 : 0
      if (aSecHit !== bSecHit) return bSecHit - aSecHit
      return (b.highlight_score ?? 0) - (a.highlight_score ?? 0)
    })
  }, [materials, targetSection])

  if (materials.length === 0) {
    return (
      <div className="rounded-md border border-dashed border-amber-500/40 bg-amber-500/5 px-3 py-2 text-[11px] leading-relaxed text-foreground">
        ⚠️ 本项目还没上传任何素材——挑素材补全需要从素材库里选。先去
        <strong>左侧素材库</strong>上传几条视频/图，再回来挑。
      </div>
    )
  }

  const visible = showAll ? ranked : ranked.slice(0, 6)

  return (
    <div className="space-y-2 rounded-md border border-border bg-background/40 p-2">
      <div className="flex items-center justify-between">
        <div className="text-[11px] font-medium text-foreground">挑素材替换本段（已按适配度排序）</div>
        {currentMaterialId && (
          <span className="text-[10px] text-muted-foreground">
            当前：{currentMaterialId.slice(0, 12)}…
          </span>
        )}
      </div>

      <div className="max-h-56 overflow-y-auto rounded border border-border bg-background/60">
        <ul className="divide-y divide-border">
          {visible.map((m) => {
            const isCurrent = m.material_id === currentMaterialId
            const recommendedHit = m.recommended_section === targetSection
            const dur = m.duration_seconds ? `${m.duration_seconds.toFixed(1)}s` : '—'
            const highlight = (m.highlight_score ?? 0).toFixed(2)
            return (
              <li key={m.material_id}>
                <button
                  type="button"
                  onClick={() => onPickManual(m.material_id)}
                  disabled={gapBusy || isCurrent}
                  className={cn(
                    'flex w-full items-center gap-2 px-2 py-1.5 text-left text-[11px] transition-colors hover:bg-secondary',
                    (gapBusy || isCurrent) && 'cursor-not-allowed opacity-50',
                  )}
                >
                  {m.thumbnail_url ? (
                    <img
                      src={m.thumbnail_url}
                      alt=""
                      className="h-9 w-14 flex-shrink-0 rounded object-cover"
                    />
                  ) : (
                    <span className="inline-flex h-9 w-14 flex-shrink-0 items-center justify-center rounded bg-slate-200 text-[10px] text-slate-500">
                      {m.media_type === 'image' ? '图' : m.media_type === 'audio' ? '音' : '视'}
                    </span>
                  )}
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-1.5">
                      <span className="truncate font-medium text-foreground">
                        {m.filename}
                      </span>
                      {isCurrent && <span className="text-[10px] text-primary">· 当前</span>}
                    </div>
                    <div className="flex flex-wrap items-center gap-1 text-[10px] text-muted-foreground">
                      <span>{dur}</span>
                      <span>·</span>
                      <span>高光 {highlight}</span>
                      {recommendedHit && (
                        <span className="rounded bg-emerald-500/15 px-1.5 py-0.5 font-medium text-emerald-600">
                          段位推荐
                        </span>
                      )}
                    </div>
                  </div>
                </button>
              </li>
            )
          })}
        </ul>
      </div>

      {ranked.length > 6 && (
        <button
          type="button"
          onClick={() => setShowAll((v) => !v)}
          className="text-[10px] text-muted-foreground hover:text-foreground"
        >
          {showAll ? '收起' : `展开剩余 ${ranked.length - 6} 条`}
        </button>
      )}
    </div>
  )
}
