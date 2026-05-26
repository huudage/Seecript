import type { Material } from '@/types/schemas'
import { SECTION_BG, SECTION_SHORT } from '@/lib/sections'
import { cn } from '@/lib/utils'

/**
 * 单张素材卡片：缩略图 + 文件名 + 时长 + AI 标签。
 * - 视频走 `<video poster>`，图片走 `<img>`，音频用占位图标
 * - dragHandleProps 由 MaterialGrid 透传（@dnd-kit useSortable 提供 listeners/attributes）
 * - onRemove 触发 store.removeMaterial
 */
export function MaterialCard({
  material,
  dragHandleProps,
  onRemove,
}: {
  material: Material
  dragHandleProps?: Record<string, unknown>
  onRemove?: (id: string) => void
}) {
  const thumb = material.thumbnail_url
  return (
    <div className="group relative flex flex-col overflow-hidden rounded-md border border-border bg-background/60 transition-shadow hover:shadow-md">
      <div className="relative h-24 w-full bg-muted">
        {material.media_type === 'video' && thumb ? (
          <video
            src={thumb}
            poster={thumb}
            className="h-full w-full object-cover"
            muted
            preload="metadata"
          />
        ) : material.media_type === 'image' && thumb ? (
          <img src={thumb} alt={material.filename} className="h-full w-full object-cover" />
        ) : (
          <div className="flex h-full w-full items-center justify-center text-xs text-muted-foreground">
            {material.media_type === 'audio' ? '🎵 音频' : '— 无预览'}
          </div>
        )}

        {/* drag handle 浮在左上 */}
        <button
          {...(dragHandleProps ?? {})}
          aria-label="拖拽排序"
          className="absolute left-1 top-1 rounded bg-black/40 px-1.5 py-0.5 text-[10px] text-white opacity-0 transition-opacity group-hover:opacity-100 cursor-grab active:cursor-grabbing"
        >
          ⋮⋮
        </button>

        {/* 媒体类型 chip */}
        <span className="absolute right-1 top-1 rounded bg-black/50 px-1.5 py-0.5 text-[10px] uppercase text-white">
          {material.media_type}
        </span>

        {/* 推荐段落色条 */}
        {material.recommended_section && (
          <span
            className={cn(
              'absolute bottom-0 left-0 right-0 px-1.5 py-0.5 text-[10px] font-medium text-white',
              SECTION_BG[material.recommended_section],
            )}
          >
            推荐 {SECTION_SHORT[material.recommended_section]}
          </span>
        )}
      </div>

      <div className="flex flex-1 flex-col gap-1 p-2 text-xs">
        <div className="flex items-start justify-between gap-1">
          <p className="min-w-0 flex-1 truncate font-medium" title={material.filename}>
            {material.filename}
          </p>
          {onRemove && (
            <button
              onClick={() => onRemove(material.material_id)}
              className="shrink-0 text-muted-foreground transition-colors hover:text-destructive"
              title="移除"
            >
              ×
            </button>
          )}
        </div>
        {material.duration_seconds != null && (
          <span className="font-mono text-[10px] text-muted-foreground">
            {material.duration_seconds.toFixed(1)}s
          </span>
        )}
        {material.tags.length > 0 && (
          <div className="flex flex-wrap gap-1">
            {material.tags.slice(0, 5).map((t) => (
              <span
                key={t}
                className="rounded-sm bg-secondary px-1 py-px text-[10px] text-muted-foreground"
              >
                {t}
              </span>
            ))}
            {material.tags.length > 5 && (
              <span className="text-[10px] text-muted-foreground">+{material.tags.length - 5}</span>
            )}
          </div>
        )}
      </div>
    </div>
  )
}
