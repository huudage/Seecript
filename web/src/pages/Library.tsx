import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'

import { api, ApiError } from '@/api/client'
import { PageShell } from '@/components/layout/PageShell'
import { useSessionStore } from '@/stores/session'
import type { LibraryItem } from '@/types/schemas'
import { VIDEO_TYPE_LABEL } from '@/lib/sections'
import { cn } from '@/lib/utils'

export default function LibraryPage() {
  const [items, setItems] = useState<LibraryItem[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const selectSample = useSessionStore((s) => s.selectSample)
  const selectedSampleId = useSessionStore((s) => s.selectedSampleId)
  const navigate = useNavigate()

  useEffect(() => {
    let cancelled = false
    api
      .get<LibraryItem[]>('/library')
      .then((data) => {
        if (!cancelled) setItems(data)
      })
      .catch((err: ApiError | Error) => {
        if (!cancelled) setError(err.message || '加载失败')
      })
    return () => {
      cancelled = true
    }
  }, [])

  const handlePick = (item: LibraryItem) => {
    selectSample(item.id, item.video_type)
    navigate('/decompose')
  }

  return (
    <PageShell
      title="素材库"
      subtitle="挑一个内置爆款样例，进入下一步『拆解』。"
    >
      {error && (
        <div className="mb-4 rounded-md border border-destructive/40 bg-destructive/5 px-4 py-3 text-sm text-destructive">
          {error}
        </div>
      )}

      {items === null && !error && (
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {[0, 1, 2].map((i) => (
            <div
              key={i}
              className="h-64 animate-pulse rounded-lg border border-border bg-card"
            />
          ))}
        </div>
      )}

      {items && (
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {items.map((item) => (
            <button
              key={item.id}
              onClick={() => handlePick(item)}
              className={cn(
                'group flex flex-col overflow-hidden rounded-lg border bg-card text-left transition-all',
                'hover:shadow-lg hover:-translate-y-0.5',
                selectedSampleId === item.id
                  ? 'border-primary ring-2 ring-primary/40'
                  : 'border-border',
              )}
            >
              <div
                className="relative h-40 w-full bg-gradient-to-br from-secondary to-muted"
                style={{
                  backgroundImage: item.cover_url ? `url(${item.cover_url})` : undefined,
                  backgroundSize: 'cover',
                  backgroundPosition: 'center',
                }}
              >
                <div className="absolute right-2 top-2 rounded-full bg-foreground/80 px-2 py-0.5 text-xs font-medium text-background">
                  {item.scene}
                </div>
                <div className="absolute left-2 top-2 rounded-full bg-primary/90 px-2 py-0.5 text-[10px] font-medium text-primary-foreground">
                  {VIDEO_TYPE_LABEL[item.video_type]}
                </div>
              </div>
              <div className="flex flex-1 flex-col gap-2 p-4">
                <h3 className="line-clamp-2 text-sm font-semibold leading-snug">
                  {item.title}
                </h3>
                <div className="mt-auto flex items-center justify-between text-xs text-muted-foreground">
                  <span>{item.duration_seconds.toFixed(1)}s</span>
                  <span>{item.shot_count} 镜头</span>
                </div>
              </div>
            </button>
          ))}
        </div>
      )}
    </PageShell>
  )
}
