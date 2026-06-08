import { cn } from '@/lib/utils'
import type { AdaptedSection, Gap, Plan, Scene } from '@/types/schemas'

interface Props {
  plan: Plan
  gaps: Gap[]
  filledGapIds: Set<string>
  selectedGapId: string | null
  onSelectScene: (scene: Scene) => void
  onFillGap: (gap: Gap) => void
}

/**
 * 故事板 —— 替代四轨面板 + 缺口列表。
 * 用户只看到：段 → 镜头 → 有没有素材 → 点空缺去补。
 * 不再暴露 gap / fill / scene_id 等内部概念。
 */
export function StoryBoard({
  plan,
  gaps,
  filledGapIds,
  selectedGapId,
  onSelectScene,
  onFillGap,
}: Props) {
  // 按 section 分组 scene
  const sectionGroups = plan.adapted_sections.map((section) => {
    const scenes = plan.main_track.filter(
      (sc) => sc.parent_section_id === section.section_id,
    )
    const sectionGaps = gaps.filter(
      (g) => g.section_id === section.section_id,
    )
    return { section, scenes, gaps: sectionGaps }
  })

  return (
    <div className="space-y-4">
      <h2 className="text-lg font-bold">你的内容结构</h2>

      {/* 段卡片 */}
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2 xl:grid-cols-3">
        {sectionGroups.map(({ section, scenes, gaps: sectionGaps }) => (
          <div
            key={section.section_id}
            className={cn(
              'overflow-hidden rounded-2xl border transition-all duration-300',
              sectionGaps.some((g) => g.status === 'miss')
                ? 'border-destructive/30 bg-destructive/5'
                : sectionGaps.every((g) => g.status === 'ok')
                  ? 'border-emerald-500/20 bg-emerald-500/5'
                  : 'border-border bg-card',
            )}
          >
            {/* 段头 */}
            <div className="flex items-center justify-between border-b border-border/50 px-4 py-3">
              <div>
                <div className="text-sm font-bold">{section.theme || section.role}</div>
                <div className="text-xs text-muted-foreground">
                  {section.duration_seconds}s · {scenes.length} 个镜头
                </div>
              </div>
              <span
                className={cn(
                  'rounded-full px-2 py-0.5 text-xs font-medium',
                  sectionGaps.every((g) => g.status === 'ok')
                    ? 'bg-emerald-500/20 text-emerald-400'
                    : sectionGaps.some((g) => g.status === 'miss')
                      ? 'bg-destructive/20 text-destructive'
                      : 'bg-amber-500/20 text-amber-400',
                )}
              >
                {sectionGaps.every((g) => g.status === 'ok')
                  ? '✅ 已就绪'
                  : sectionGaps.some((g) => g.status === 'miss')
                    ? `${sectionGaps.filter((g) => g.status === 'miss').length} 个待补`
                    : '⚠️ 部分匹配'}
              </span>
            </div>

            {/* 镜头列表 */}
            <div className="space-y-1 p-3">
              {scenes.length === 0 && (
                <div className="rounded-lg border border-dashed border-border py-6 text-center text-sm text-muted-foreground">
                  暂无镜头
                </div>
              )}
              {scenes.map((scene, idx) => {
                const gap = sectionGaps.find((g) =>
                  g.matched_material_id === scene.source_ref ||
                  (scene.needs_fill && g.status !== 'ok'),
                )
                const filled = scene.source !== 'text_card' &&
                  scene.source_ref &&
                  !scene.source_ref.startsWith('text-card-fill-empty')
                const isSelected = gap?.gap_id === selectedGapId

                return (
                  <button
                    key={scene.scene_id}
                    onClick={() => {
                      if (gap && gap.status !== 'ok') onFillGap(gap)
                      else onSelectScene(scene)
                    }}
                    className={cn(
                      'flex w-full items-center gap-3 rounded-xl px-3 py-2.5 text-left transition-all duration-200',
                      filled
                        ? 'bg-emerald-500/10 hover:bg-emerald-500/20'
                        : scene.source === 'text_card' || scene.source_ref?.startsWith('text-card-fill-empty')
                          ? 'border-2 border-dashed border-border bg-transparent hover:border-primary/30 hover:bg-accent/30'
                          : 'bg-card hover:bg-secondary',
                      isSelected && 'ring-2 ring-primary/40',
                    )}
                  >
                    {/* 序号 */}
                    <span className={cn(
                      'flex h-7 w-7 shrink-0 items-center justify-center rounded-lg text-xs font-bold',
                      filled ? 'bg-emerald-500/20 text-emerald-400' : 'bg-secondary text-muted-foreground',
                    )}>
                      {idx + 1}
                    </span>

                    {/* 内容 */}
                    <div className="min-w-0 flex-1">
                      <div className="truncate text-sm font-medium">
                        {scene.shot_subject || `镜头 ${idx + 1}`}
                      </div>
                      <div className="text-xs text-muted-foreground">
                        {scene.duration}s
                        {filled && ' · 已有素材'}
                        {!filled && ' · 点击补齐'}
                      </div>
                    </div>

                    {/* 状态图标 */}
                    <span className="shrink-0 text-lg">
                      {filled ? '✅' : '⬜'}
                    </span>
                  </button>
                )
              })}
            </div>
          </div>
        ))}
      </div>

      {/* 底部统计 */}
      <div className="flex items-center justify-between rounded-xl bg-secondary/40 px-4 py-3 text-sm">
        <span className="text-muted-foreground">
          {plan.main_track.length} 个镜头 ·
          {gaps.filter((g) => g.status === 'ok').length} 个已就绪 ·
          {gaps.filter((g) => g.status !== 'ok').length} 个待处理
        </span>
        {gaps.some((g) => g.status !== 'ok') && (
          <span className="text-xs text-amber-400">点击空格子补全素材</span>
        )}
      </div>
    </div>
  )
}
