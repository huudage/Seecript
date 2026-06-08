import { useEffect, useMemo, useState } from 'react'

import { api } from '@/api/client'
import { cn } from '@/lib/utils'
import { useSessionStore } from '@/stores/session'
import type { ReferenceListItem, VideoType } from '@/types/schemas'
import { VIDEO_TYPE_LABEL, VIDEO_TYPE_HINT } from '@/lib/sections'

type VideoTypeFilter = 'all' | VideoType

/**
 * Compose 顶部结构参考选择器（stage-15）。
 *
 * - GET /api/references 拍平所有 (sample, slot)，按 sample 分组展示
 * - 最多选 2 个 slot（决策 4：同一 sample 的 v1+v2 允许并存）
 * - 已满时点新条目走 FIFO 替换（store 的 toggleReference 已实现）
 */
export function ReferencePicker() {
  const selected = useSessionStore((s) => s.selectedReferences)
  const toggleReference = useSessionStore((s) => s.toggleReference)
  const clearReferences = useSessionStore((s) => s.clearReferences)

  const [items, setItems] = useState<ReferenceListItem[] | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [videoTypeFilter, setVideoTypeFilter] = useState<VideoTypeFilter>('all')

  const refresh = async () => {
    setLoading(true)
    setError(null)
    try {
      const list = await api.get<ReferenceListItem[]>('/references')
      setItems(list)
    } catch (err) {
      setError(err instanceof Error ? err.message : '加载参考列表失败')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    void refresh()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // 按 sample 分组保持插入顺序——先按 videoTypeFilter 过滤再分组。
  const groups = useMemo(() => {
    if (!items) return [] as Array<{ sample_id: string; sample_title: string; cover_url: string; video_type: VideoType; scene: string; versions: ReferenceListItem[] }>
    const map = new Map<string, { sample_id: string; sample_title: string; cover_url: string; video_type: VideoType; scene: string; versions: ReferenceListItem[] }>()
    for (const it of items) {
      if (videoTypeFilter !== 'all' && it.video_type !== videoTypeFilter) continue
      const g = map.get(it.sample_id)
      if (g) {
        g.versions.push(it)
      } else {
        map.set(it.sample_id, {
          sample_id: it.sample_id,
          sample_title: it.sample_title,
          cover_url: it.cover_url,
          video_type: it.video_type,
          scene: it.scene,
          versions: [it],
        })
      }
    }
    return Array.from(map.values())
  }, [items, videoTypeFilter])

  // 各 video_type 的可用样例数（去重 sample_id），用于分类按钮徽标。
  const videoTypeCounts = useMemo(() => {
    const base: Record<VideoTypeFilter, number> = { all: 0, marketing: 0, editing: 0, motion_graph: 0 }
    if (!items) return base
    const seen = new Map<VideoTypeFilter, Set<string>>()
    seen.set('all', new Set())
    for (const it of items) {
      seen.get('all')!.add(it.sample_id)
      const s = seen.get(it.video_type) ?? new Set<string>()
      s.add(it.sample_id)
      seen.set(it.video_type, s)
    }
    for (const k of Object.keys(base) as VideoTypeFilter[]) {
      base[k] = seen.get(k)?.size ?? 0
    }
    return base
  }, [items])

  const isSelected = (sampleId: string, slotId: string) =>
    selected.some((r) => r.sample_id === sampleId && r.slot_id === slotId)

  return (
    <section className="rounded-lg border border-border bg-card p-4">
      <header className="mb-3 flex flex-wrap items-center gap-2">
        <h2 className="text-sm font-semibold">结构参考</h2>
        <span className="text-xs text-muted-foreground">
          从已拆解版本库挑 1-2 个 (sample, 槽) 作为结构知识——支持同一样例两个版本
        </span>
        <div className="ml-auto flex items-center gap-2 text-xs">
          <span className="text-muted-foreground">已选 {selected.length}/2</span>
          {selected.length > 0 && (
            <button
              type="button"
              onClick={clearReferences}
              className="rounded-md border border-border bg-background px-2 py-0.5 hover:bg-secondary"
            >
              清空
            </button>
          )}
          <button
            type="button"
            onClick={() => void refresh()}
            disabled={loading}
            className="rounded-md border border-border bg-background px-2 py-0.5 hover:bg-secondary disabled:opacity-60"
          >
            {loading ? '刷新中…' : '刷新'}
          </button>
        </div>
      </header>

      {/* 视频类型分类过滤——LibraryItem 已有 video_type，但之前 Compose 步骤 1 没暴露过滤项，
          有几十个样例时只能上下翻。这里按类型拍平选项让用户秒锁到目标类别。 */}
      <div className="mb-2 flex flex-wrap items-center gap-1.5">
        <span className="text-xs font-medium text-muted-foreground">类型</span>
        <div className="inline-flex flex-wrap items-center gap-0.5 rounded-md border border-border bg-background/40 p-0.5 text-xs">
          {(['all', 'marketing', 'editing', 'motion_graph'] as const).map((f) => {
            const label = f === 'all' ? '全部' : VIDEO_TYPE_LABEL[f]
            const active = videoTypeFilter === f
            const count = videoTypeCounts[f]
            return (
              <button
                key={f}
                type="button"
                onClick={() => setVideoTypeFilter(f)}
                title={f === 'all' ? '所有类型' : VIDEO_TYPE_HINT[f]}
                className={cn(
                  'rounded px-2 py-0.5 transition-colors',
                  active
                    ? 'bg-primary text-primary-foreground'
                    : 'text-muted-foreground hover:bg-secondary hover:text-foreground',
                )}
              >
                {label}
                <span className="ml-1 text-xs opacity-70">{count}</span>
              </button>
            )
          })}
        </div>
      </div>

      {/* 已选 chips */}
      {selected.length > 0 && (
        <div className="mb-3 flex flex-wrap items-center gap-1.5 text-xs">
          {selected.map((r, i) => {
            const meta = items?.find(
              (it) => it.sample_id === r.sample_id && it.slot_id === r.slot_id,
            )
            return (
              <span
                key={`${r.sample_id}-${r.slot_id}`}
                className="inline-flex items-center gap-1 rounded-full border border-primary/40 bg-primary/5 px-2 py-0.5 text-primary"
              >
                <span className="rounded-sm bg-primary px-1 text-xs font-bold text-primary-foreground">
                  {String.fromCharCode(65 + i)}
                </span>
                <span className="font-medium">
                  {meta?.sample_title ?? r.sample_id}
                </span>
                <span className="text-xs text-muted-foreground">{meta?.label ?? r.slot_id.slice(0, 8)}</span>
                <button
                  type="button"
                  onClick={() => toggleReference(r)}
                  className="ml-0.5 text-xs text-muted-foreground hover:text-foreground"
                  title="移除"
                >
                  ×
                </button>
              </span>
            )
          })}
        </div>
      )}

      {error && (
        <p className="mb-2 rounded-md border border-destructive/40 bg-destructive/5 px-3 py-2 text-xs text-destructive">
          {error}
        </p>
      )}

      {loading && !items && (
        <p className="text-xs text-muted-foreground">加载中…</p>
      )}

      {items && items.length === 0 && (
        <p className="rounded-md border border-dashed border-border bg-background/40 px-3 py-4 text-xs text-muted-foreground">
          资产库还没有任何拆解结果。先去 <a className="text-primary underline-offset-4 hover:underline" href="/library">素材库</a> 选一个样例进「视频拆解」页跑一次并保存。
        </p>
      )}

      {items && items.length > 0 && groups.length === 0 && (
        <p className="rounded-md border border-dashed border-border bg-background/40 px-3 py-4 text-xs text-muted-foreground">
          『{videoTypeFilter !== 'all' ? VIDEO_TYPE_LABEL[videoTypeFilter] : '当前过滤'}』下还没有可用样例。换个类型，或回 <a className="text-primary underline-offset-4 hover:underline" href="/library">素材库</a> 上传一个。
        </p>
      )}

      {groups.length > 0 && (
        <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-3">
          {groups.map((g) => (
            <div
              key={g.sample_id}
              className="rounded-md border border-border bg-background/40 p-2"
            >
              <div className="mb-1.5 flex items-start gap-2">
                <img
                  src={g.cover_url}
                  alt=""
                  className="h-10 w-16 flex-none rounded object-cover"
                  onError={(e) => {
                    ;(e.currentTarget as HTMLImageElement).style.visibility = 'hidden'
                  }}
                />
                <div className="min-w-0 flex-1">
                  <p className="truncate text-xs font-medium" title={g.sample_title}>{g.sample_title}</p>
                  <p className="text-xs text-muted-foreground">
                    {g.scene} · {VIDEO_TYPE_LABEL[g.video_type]}
                  </p>
                </div>
              </div>
              <div className="flex flex-wrap gap-1">
                {g.versions.map((v) => {
                  const active = isSelected(v.sample_id, v.slot_id)
                  return (
                    <button
                      key={v.slot_id}
                      type="button"
                      onClick={() => toggleReference({ sample_id: v.sample_id, slot_id: v.slot_id })}
                      title={`${v.label} · ${v.duration_seconds.toFixed(1)}s · ${v.shot_count} 镜头${v.is_active ? ' · active' : ''}`}
                      className={cn(
                        'rounded-md border px-2 py-0.5 text-xs transition-colors',
                        active
                          ? 'border-primary bg-primary text-primary-foreground'
                          : 'border-border bg-background hover:bg-secondary',
                      )}
                    >
                      {v.label}
                      {v.is_active && (
                        <span className={cn('ml-1 text-xs', active ? 'opacity-80' : 'text-emerald-500')}>●</span>
                      )}
                    </button>
                  )
                })}
              </div>
            </div>
          ))}
        </div>
      )}
    </section>
  )
}
