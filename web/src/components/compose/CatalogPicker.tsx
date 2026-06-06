/**
 * CatalogPicker —— HyperFrames catalog 浏览器（弹窗）。
 *
 * 给 PackagingPanel 用：在 transition / cover 候选上点「选风格」按钮，
 * 拉出来按 category 过滤的缩略图网格，hover 播 preview_video，点中
 * 把 item.name 写回 catalog_block 字段。
 *
 * 设计：
 * - 不缓存：每次打开都重新调 listCatalog（catalog 只 ~110 条，体感够快）。
 * - 不预下载视频：列表用 preview_poster <img>，hover 才换 <video> autoplay muted loop。
 * - 关闭：ESC / 点遮罩 / 点右上角 ×。
 */
import { useEffect, useMemo, useRef, useState } from 'react'

import { listCatalog } from '@/api/catalog'
import { cn } from '@/lib/utils'
import type { CatalogCategory, CatalogItem } from '@/types/schemas'

interface Props {
  open: boolean
  category: CatalogCategory
  current?: string | null
  onPick: (name: string | null) => void
  onClose: () => void
}

export function CatalogPicker({ open, category, current, onPick, onClose }: Props) {
  const [items, setItems] = useState<CatalogItem[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [tag, setTag] = useState<string | null>(null)

  useEffect(() => {
    if (!open) return
    let cancelled = false
    setLoading(true)
    setError(null)
    listCatalog({ category, limit: 200 })
      .then((resp) => {
        if (!cancelled) setItems(resp.items)
      })
      .catch((err) => {
        if (!cancelled) setError(err instanceof Error ? err.message : '加载 catalog 失败')
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [open, category])

  useEffect(() => {
    if (!open) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open, onClose])

  const allTags = useMemo(() => {
    const set = new Set<string>()
    for (const it of items) for (const t of it.tags) set.add(t)
    return Array.from(set).sort()
  }, [items])

  const filtered = useMemo(() => {
    if (!tag) return items
    return items.filter((it) => it.tags.includes(tag))
  }, [items, tag])

  if (!open) return null

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4"
      onClick={onClose}
    >
      <div
        className="relative flex h-[80vh] w-full max-w-5xl flex-col overflow-hidden rounded-lg border border-border bg-background shadow-xl"
        onClick={(e) => e.stopPropagation()}
      >
        {/* header */}
        <div className="flex items-center justify-between border-b border-border px-4 py-2">
          <div>
            <h3 className="text-sm font-semibold">HyperFrames catalog · {category}</h3>
            <p className="text-[11px] text-muted-foreground">
              点击挑一个风格 hint；后端 packaging_agent 会把 name 当作风格基准。当前：
              <span className="ml-1 font-mono">{current ?? '（无）'}</span>
            </p>
          </div>
          <div className="flex items-center gap-2">
            {current && (
              <button
                type="button"
                onClick={() => {
                  onPick(null)
                  onClose()
                }}
                className="rounded border border-border px-2 py-1 text-[11px] text-muted-foreground hover:bg-accent"
              >
                清除
              </button>
            )}
            <button
              type="button"
              onClick={onClose}
              className="rounded p-1 text-muted-foreground hover:bg-accent"
              aria-label="关闭"
            >
              ×
            </button>
          </div>
        </div>

        {/* tag filter */}
        {allTags.length > 0 && (
          <div className="flex flex-wrap items-center gap-1 border-b border-border bg-background/40 px-4 py-2">
            <button
              type="button"
              onClick={() => setTag(null)}
              className={cn(
                'rounded-full border px-2 py-0.5 text-[10px] transition',
                tag === null
                  ? 'border-primary bg-primary/10 text-foreground'
                  : 'border-border bg-background/60 text-muted-foreground hover:border-primary/60',
              )}
            >
              全部 · {items.length}
            </button>
            {allTags.map((t) => (
              <button
                key={t}
                type="button"
                onClick={() => setTag(tag === t ? null : t)}
                className={cn(
                  'rounded-full border px-2 py-0.5 text-[10px] transition',
                  tag === t
                    ? 'border-primary bg-primary/10 text-foreground'
                    : 'border-border bg-background/60 text-muted-foreground hover:border-primary/60',
                )}
              >
                {t}
              </button>
            ))}
          </div>
        )}

        {/* body */}
        <div className="flex-1 overflow-auto p-4">
          {loading && <p className="text-sm text-muted-foreground">加载中…</p>}
          {error && (
            <p className="rounded border border-destructive/40 bg-destructive/5 px-3 py-2 text-xs text-destructive">
              {error}
            </p>
          )}
          {!loading && !error && filtered.length === 0 && (
            <p className="text-sm text-muted-foreground">没有匹配的条目。</p>
          )}
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 md:grid-cols-4">
            {filtered.map((it) => (
              <CatalogCard
                key={it.name}
                item={it}
                selected={it.name === current}
                onPick={() => {
                  onPick(it.name)
                  onClose()
                }}
              />
            ))}
          </div>
        </div>
      </div>
    </div>
  )
}

function CatalogCard({
  item,
  selected,
  onPick,
}: {
  item: CatalogItem
  selected: boolean
  onPick: () => void
}) {
  const [hover, setHover] = useState(false)
  const videoRef = useRef<HTMLVideoElement | null>(null)

  useEffect(() => {
    if (!videoRef.current) return
    if (hover) videoRef.current.play().catch(() => {})
    else {
      videoRef.current.pause()
      videoRef.current.currentTime = 0
    }
  }, [hover])

  return (
    <button
      type="button"
      onClick={onPick}
      onMouseEnter={() => setHover(true)}
      onMouseLeave={() => setHover(false)}
      className={cn(
        'group flex flex-col gap-1 overflow-hidden rounded-md border bg-background/40 text-left transition',
        selected
          ? 'border-primary ring-2 ring-primary/40'
          : 'border-border hover:border-primary/60',
      )}
    >
      <div className="relative aspect-video w-full overflow-hidden bg-muted">
        {item.preview_poster && (
          <img
            src={item.preview_poster}
            alt={item.title ?? item.name}
            loading="lazy"
            className="absolute inset-0 h-full w-full object-cover"
          />
        )}
        {item.preview_video && hover && (
          <video
            ref={videoRef}
            src={item.preview_video}
            muted
            loop
            playsInline
            className="absolute inset-0 h-full w-full object-cover"
          />
        )}
        {selected && (
          <span className="absolute right-1 top-1 rounded bg-primary px-1.5 py-0.5 text-[10px] font-semibold text-primary-foreground">
            ✓ 已选
          </span>
        )}
      </div>
      <div className="px-2 pb-2">
        <div className="truncate text-xs font-semibold">{item.title ?? item.name}</div>
        <div className="truncate font-mono text-[10px] text-muted-foreground">{item.name}</div>
        {item.tags.length > 0 && (
          <div className="mt-1 flex flex-wrap gap-1">
            {item.tags.slice(0, 3).map((t) => (
              <span
                key={t}
                className="rounded bg-muted px-1 py-0.5 text-[9px] text-muted-foreground"
              >
                {t}
              </span>
            ))}
          </div>
        )}
      </div>
    </button>
  )
}
