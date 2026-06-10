import { useEffect, useMemo, useState } from 'react'

import { api } from '@/api/client'
import { cn } from '@/lib/utils'
import type { Material } from '@/types/schemas'

/**
 * 从项目素材库挑一张图片当 AIGC 参考图（stage-59）。
 *
 * 用户在 FillAigcPanel 的 spec 阶段，每个 slot 现在多了"从素材库选"入口——避免
 * 反复从硬盘上传同一张图，也允许把之前 AI 出图入库的成品复用为新 slot 的视觉参考。
 *
 * 数据源：
 * - GET /api/material?project_id=X 拿当前项目所有素材，前端过滤 media_type=image
 * - origin 字段区分 upload / aigc_image / aigc_video / system_clone（后端落库时写入）
 *
 * 不做：
 * - 视频参考挑选（暂走另一个组件，本期只补图）
 * - 跨项目挑图（按用户隔离原则，只能选当前 project 的图）
 */
export function LibraryImagePicker({
  projectId,
  onPick,
  onClose,
}: {
  projectId: string
  onPick: (material: Material) => void
  onClose: () => void
}) {
  const [items, setItems] = useState<Material[] | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [originFilter, setOriginFilter] = useState<'all' | 'upload' | 'aigc_image' | 'aigc_video' | 'system_clone'>('all')

  useEffect(() => {
    let cancelled = false
    const load = async () => {
      if (!projectId) {
        setError('当前没有项目 ID，无法读取素材库。')
        return
      }
      setLoading(true)
      setError(null)
      try {
        const list = await api.get<Material[]>(`/material?project_id=${encodeURIComponent(projectId)}`)
        if (cancelled) return
        // 只挑图片（aigc_video 仍是视频；aigc_image 是 Seedream 入库的图）
        setItems(list.filter((m) => m.media_type === 'image'))
      } catch (e) {
        if (cancelled) return
        setError(e instanceof Error ? e.message : '加载素材库失败')
      } finally {
        if (!cancelled) setLoading(false)
      }
    }
    void load()
    return () => {
      cancelled = true
    }
  }, [projectId])

  const filtered = useMemo(() => {
    if (!items) return []
    if (originFilter === 'all') return items
    return items.filter((m) => (m.origin ?? 'upload') === originFilter)
  }, [items, originFilter])

  const counts = useMemo(() => {
    const base = { all: 0, upload: 0, aigc_image: 0, aigc_video: 0, system_clone: 0 }
    if (!items) return base
    base.all = items.length
    for (const m of items) {
      const o = (m.origin ?? 'upload') as keyof typeof base
      if (o in base) base[o] += 1
    }
    return base
  }, [items])

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4"
      onClick={onClose}
    >
      <div
        className="flex max-h-[80vh] w-full max-w-3xl flex-col gap-3 rounded-lg border border-border bg-card p-4 shadow-xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between">
          <div>
            <h3 className="text-sm font-semibold">从素材库选参考图</h3>
            <p className="text-xs text-muted-foreground">
              选中的图会作为 AI 出图 / 视频生成的视觉参考——主体、构图、色调由这张图主导
            </p>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="rounded border border-border bg-background px-2 py-1 text-xs hover:bg-secondary"
          >
            关闭
          </button>
        </div>

        {/* origin 过滤——上传 / AI 出图 / AI 视频帧 / 系统克隆 */}
        <div className="flex flex-wrap items-center gap-1.5 text-xs">
          <span className="text-muted-foreground">来源</span>
          <div className="inline-flex flex-wrap items-center gap-0.5 rounded-md border border-border bg-background/40 p-0.5">
            {(
              [
                ['all', '全部'],
                ['upload', '上传'],
                ['aigc_image', 'AI 出图'],
                ['aigc_video', 'AI 视频帧'],
                ['system_clone', '系统克隆'],
              ] as const
            ).map(([key, label]) => {
              const active = originFilter === key
              const c = counts[key]
              return (
                <button
                  key={key}
                  type="button"
                  onClick={() => setOriginFilter(key)}
                  className={cn(
                    'rounded px-2 py-0.5 transition-colors',
                    active
                      ? 'bg-primary text-primary-foreground'
                      : 'text-muted-foreground hover:bg-secondary hover:text-foreground',
                  )}
                  disabled={c === 0 && key !== 'all'}
                >
                  {label}
                  <span className="ml-1 text-xs opacity-70">{c}</span>
                </button>
              )
            })}
          </div>
        </div>

        {error && (
          <p className="rounded-md border border-destructive/40 bg-destructive/5 px-3 py-2 text-xs text-destructive">
            {error}
          </p>
        )}

        {loading && !items && <p className="text-xs text-muted-foreground">加载中…</p>}

        {!loading && items && filtered.length === 0 && (
          <p className="rounded-md border border-dashed border-border bg-background/40 px-3 py-6 text-center text-xs text-muted-foreground">
            {items.length === 0
              ? '当前项目还没有图片素材。先在 Step1 上传图片，或在 AI 出图后点"保存到素材库"。'
              : '当前过滤下没有素材。换个来源试试。'}
          </p>
        )}

        {filtered.length > 0 && (
          <div className="grid flex-1 gap-2 overflow-y-auto pr-1 sm:grid-cols-3 md:grid-cols-4">
            {filtered.map((m) => {
              const cover = m.thumbnail_url || m.file_url
              return (
                <button
                  key={m.material_id}
                  type="button"
                  onClick={() => {
                    onPick(m)
                    onClose()
                  }}
                  className="group flex flex-col gap-1 overflow-hidden rounded-md border border-border bg-background/40 text-left transition-colors hover:border-primary"
                  title={`${m.filename}${m.highlight_reason ? ` · ${m.highlight_reason}` : ''}`}
                >
                  <div className="relative aspect-video w-full bg-black/20">
                    {cover && (
                      <img
                        src={cover}
                        alt={m.filename}
                        className="h-full w-full object-cover transition-transform group-hover:scale-[1.02]"
                        onError={(e) => {
                          ;(e.currentTarget as HTMLImageElement).style.visibility = 'hidden'
                        }}
                      />
                    )}
                    <span className="absolute right-1 top-1 rounded bg-black/60 px-1 py-0.5 text-xs text-white">
                      {originLabel(m.origin ?? 'upload')}
                    </span>
                  </div>
                  <div className="px-1.5 pb-1 pt-0.5">
                    <p className="truncate text-xs font-medium">{m.filename}</p>
                    {m.highlight_reason && (
                      <p className="truncate text-xs text-muted-foreground">{m.highlight_reason}</p>
                    )}
                    {(m.subjects?.length || m.tags?.length) ? (
                      <div className="mt-0.5 flex flex-wrap gap-0.5">
                        {(m.subjects ?? []).slice(0, 2).map((s) => (
                          <span
                            key={`subj-${s}`}
                            className="rounded bg-primary/10 px-1 text-xs text-primary"
                          >
                            {s.slice(0, 8)}
                          </span>
                        ))}
                        {(m.tags ?? []).slice(0, 2).map((t) => (
                          <span
                            key={`tag-${t}`}
                            className="rounded bg-secondary px-1 text-xs text-muted-foreground"
                          >
                            {t.slice(0, 8)}
                          </span>
                        ))}
                      </div>
                    ) : null}
                  </div>
                </button>
              )
            })}
          </div>
        )}
      </div>
    </div>
  )
}

function originLabel(origin: 'upload' | 'aigc_image' | 'aigc_video' | 'system_clone'): string {
  switch (origin) {
    case 'aigc_image': return 'AI 图'
    case 'aigc_video': return 'AI 视频'
    case 'system_clone': return '系统'
    case 'upload': default: return '上传'
  }
}
