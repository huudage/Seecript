import { useEffect, useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'

import { api, ApiError } from '@/api/client'
import { PageShell } from '@/components/layout/PageShell'
import { AssetLibraryView } from '@/components/library/AssetLibraryView'
import { useProjectsStore } from '@/stores/projects'
import { useSessionStore } from '@/stores/session'
import type { LibraryItem } from '@/types/schemas'
import { VIDEO_TYPE_LABEL } from '@/lib/sections'
import { cn } from '@/lib/utils'

type Section = 'samples' | 'assets'
type Tab = 'system' | 'user'

export default function LibraryPage() {
  const [section, setSection] = useState<Section>('samples')
  const [items, setItems] = useState<LibraryItem[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [tab, setTab] = useState<Tab>('system')
  const [previewItem, setPreviewItem] = useState<LibraryItem | null>(null)
  const selectSample = useSessionStore((s) => s.selectSample)
  const selectedSampleId = useSessionStore((s) => s.selectedSampleId)
  const createFromCurrent = useProjectsStore((s) => s.createFromCurrent)
  const navigate = useNavigate()

  // 一次性拉合并列表（不带 ?source=）；前端按 source 字段切 tab，省一次请求。
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

  const counts = useMemo(() => {
    const sys = items?.filter((i) => i.source === 'system').length ?? 0
    const usr = items?.filter((i) => i.source === 'user').length ?? 0
    return { system: sys, user: usr }
  }, [items])

  const visible = useMemo(
    () => items?.filter((i) => i.source === tab) ?? null,
    [items, tab],
  )

  const handlePick = (item: LibraryItem) => {
    selectSample(item.id, item.video_type, item.source)
    createFromCurrent({
      sample_id: item.id,
      sample_title: item.title,
      video_type: item.video_type,
    })
    navigate('/decompose')
  }

  return (
    <PageShell
      title="素材库"
      subtitle="管理样例视频与你的常用素材：BGM、参考图、参考视频。"
    >
      <div className="mb-5 inline-flex items-center gap-1 rounded-lg border border-border bg-card p-1 text-sm">
        {(['samples', 'assets'] as const).map((s) => (
          <button
            key={s}
            onClick={() => setSection(s)}
            className={cn(
              'rounded-md px-4 py-1.5 font-medium transition-colors',
              section === s
                ? 'bg-primary text-primary-foreground'
                : 'text-muted-foreground hover:bg-secondary hover:text-foreground',
            )}
          >
            {s === 'samples' ? '样例视频' : '我的素材'}
          </button>
        ))}
      </div>

      {section === 'assets' && <AssetLibraryView />}

      {section === 'samples' && (
        <>
          {error && (
            <div className="mb-4 rounded-md border border-destructive/40 bg-destructive/5 px-4 py-3 text-sm text-destructive">
              {error}
            </div>
          )}

          <div className="mb-4 inline-flex items-center gap-1 rounded-lg border border-border bg-card p-1 text-sm">
            {(['system', 'user'] as const).map((t) => (
              <button
                key={t}
                onClick={() => setTab(t)}
                className={cn(
                  'rounded-md px-3 py-1.5 transition-colors',
                  tab === t
                    ? 'bg-primary text-primary-foreground'
                    : 'text-muted-foreground hover:bg-secondary hover:text-foreground',
                )}
              >
                {t === 'system' ? '系统样例库' : '我的样例库'}
                <span className="ml-1 text-[10px] opacity-70">
                  {t === 'system' ? counts.system : counts.user}
                </span>
              </button>
            ))}
          </div>

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

      {visible && visible.length === 0 && tab === 'user' && (
        <div className="rounded-lg border border-dashed border-border bg-card p-12 text-center">
          <p className="text-sm text-muted-foreground">
            你的样例库暂时还是空的。下一期会开放上传自己的爆款样例。
          </p>
        </div>
      )}

      {visible && visible.length > 0 && (
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {visible.map((item) => (
            <div
              key={item.id}
              role="button"
              tabIndex={0}
              onClick={() => handlePick(item)}
              onKeyDown={(e) => {
                if (e.key === 'Enter' || e.key === ' ') {
                  e.preventDefault()
                  handlePick(item)
                }
              }}
              className={cn(
                'group flex cursor-pointer flex-col overflow-hidden rounded-lg border bg-card text-left transition-all',
                'hover:shadow-lg hover:-translate-y-0.5 focus:outline-none focus:ring-2 focus:ring-primary/40',
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
                <button
                  type="button"
                  onClick={(e) => {
                    e.stopPropagation()
                    setPreviewItem(item)
                  }}
                  aria-label={`预览 ${item.title}`}
                  title="预览样例视频"
                  className="absolute bottom-2 right-2 flex h-9 w-9 items-center justify-center rounded-full bg-foreground/85 text-background opacity-90 shadow-md transition-all hover:scale-110 hover:bg-foreground hover:opacity-100"
                >
                  <svg
                    xmlns="http://www.w3.org/2000/svg"
                    viewBox="0 0 24 24"
                    fill="currentColor"
                    className="h-4 w-4 translate-x-[1px]"
                  >
                    <path d="M8 5v14l11-7z" />
                  </svg>
                </button>
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
            </div>
          ))}
        </div>
      )}

      {previewItem && (
        <PreviewModal item={previewItem} onClose={() => setPreviewItem(null)} />
      )}
        </>
      )}
    </PageShell>
  )
}

function PreviewModal({ item, onClose }: { item: LibraryItem; onClose: () => void }) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [onClose])

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/80 px-4 py-8"
      onClick={onClose}
    >
      <div
        className="relative flex w-full max-w-3xl flex-col overflow-hidden rounded-lg bg-card shadow-xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between border-b border-border px-4 py-3">
          <div className="min-w-0">
            <h3 className="truncate text-sm font-semibold">{item.title}</h3>
            <p className="text-xs text-muted-foreground">
              {VIDEO_TYPE_LABEL[item.video_type]} · {item.duration_seconds.toFixed(1)}s · {item.shot_count} 镜头
            </p>
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label="关闭预览"
            className="ml-3 flex h-8 w-8 shrink-0 items-center justify-center rounded-md text-muted-foreground hover:bg-secondary hover:text-foreground"
          >
            <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} className="h-4 w-4">
              <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
            </svg>
          </button>
        </div>
        <video
          key={item.id}
          src={`/samples/${item.id}/video.mp4`}
          controls
          autoPlay
          className="aspect-video w-full bg-black"
        >
          您的浏览器不支持视频播放。
        </video>
      </div>
    </div>
  )
}
