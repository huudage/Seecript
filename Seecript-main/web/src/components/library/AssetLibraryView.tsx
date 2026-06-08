import { useEffect, useMemo, useRef, useState } from 'react'

import { api, ApiError } from '@/api/client'
import { cn } from '@/lib/utils'
import { useProjectsStore } from '@/stores/projects'
import type {
  Asset,
  AssetKind,
  AssetListResponse,
  AssetStatus,
} from '@/types/schemas'

const KIND_LABEL: Record<AssetKind, string> = {
  bgm: '背景音乐',
  reference_image: '参考图',
  reference_video: '参考视频',
}

const KIND_DESC: Record<AssetKind, string> = {
  bgm: '出片时混入视频的背景音乐',
  reference_image: '风格 / 构图 / 调性参考画面',
  reference_video: '叙事节奏参考视频（自动抽 8 帧）',
}

const KIND_ACCEPT: Record<AssetKind, string> = {
  bgm: 'audio/mpeg,audio/mp3,audio/wav,audio/x-wav,audio/aac,audio/m4a,audio/mp4',
  reference_image: 'image/jpeg,image/png,image/webp',
  reference_video: 'video/mp4,video/quicktime,video/webm',
}

const STATUS_LABEL: Record<AssetStatus, string> = {
  processing: '处理中',
  ready: '就绪',
  failed: '失败',
}

const STATUS_BADGE: Record<AssetStatus, string> = {
  processing: 'bg-amber-500/15 text-amber-700 border-amber-500/30',
  ready: 'bg-emerald-500/15 text-emerald-700 border-emerald-500/30',
  failed: 'bg-destructive/15 text-destructive border-destructive/30',
}

function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`
  return `${(n / 1024 / 1024).toFixed(1)} MB`
}

function thumbnailUrl(asset: Asset): string | null {
  const meta = asset.metadata as Record<string, unknown>
  const t = meta?.thumbnail_url
  if (typeof t === 'string') return t
  if (asset.kind === 'reference_image' && asset.status === 'ready') return asset.file_url
  return null
}

export function AssetLibraryView() {
  const [kind, setKind] = useState<AssetKind>('bgm')
  const [items, setItems] = useState<Asset[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [editingId, setEditingId] = useState<string | null>(null)
  const fileInputRef = useRef<HTMLInputElement | null>(null)
  const [pollTick, setPollTick] = useState(0)
  // 资产库按 project_id 隔离：每个项目有自己的 BGM/参考图/参考视频池
  const currentProjectId = useProjectsStore((s) => s.currentProjectId)

  // 拉当前 kind 下的列表
  useEffect(() => {
    if (!currentProjectId) {
      setItems([])
      return
    }
    let cancelled = false
    setItems(null)
    setError(null)
    api
      .get<AssetListResponse>(`/asset/library?project_id=${encodeURIComponent(currentProjectId)}&kind=${kind}`)
      .then((data) => {
        if (!cancelled) setItems(data.items)
      })
      .catch((err: ApiError | Error) => {
        if (!cancelled) setError(err.message || '加载素材库失败')
      })
    return () => {
      cancelled = true
    }
  }, [kind, pollTick, currentProjectId])

  // 有 processing 中的资产时轮询，等后台探测完成
  useEffect(() => {
    if (!items) return
    if (!items.some((a) => a.status === 'processing')) return
    const id = setTimeout(() => setPollTick((t) => t + 1), 2000)
    return () => clearTimeout(id)
  }, [items])

  const counts = useMemo(() => items?.length ?? 0, [items])

  const onPickFiles = () => fileInputRef.current?.click()

  const onFilesSelected = async (files: FileList | null) => {
    if (!files || files.length === 0) return
    if (!currentProjectId) {
      setError('请先在首页新建或选一个项目，再上传素材')
      return
    }
    setBusy(true)
    setError(null)
    try {
      for (const file of Array.from(files)) {
        const form = new FormData()
        form.append('file', file)
        form.append('kind', kind)
        form.append('project_id', currentProjectId)
        await api.post<Asset>('/asset/upload', form)
      }
      setPollTick((t) => t + 1)
    } catch (err) {
      const msg = err instanceof Error ? err.message : '上传失败'
      setError(msg)
    } finally {
      setBusy(false)
      if (fileInputRef.current) fileInputRef.current.value = ''
    }
  }

  const onDelete = async (assetId: string) => {
    if (!confirm('确定删除这份素材？已经在用它的旧方案还能继续出片，但素材库里看不到了。')) {
      return
    }
    try {
      await api.delete(`/asset/${assetId}`)
      setPollTick((t) => t + 1)
    } catch (err) {
      setError(err instanceof Error ? err.message : '删除失败')
    }
  }

  const handleDrop = async (e: React.DragEvent<HTMLDivElement>) => {
    e.preventDefault()
    if (busy) return
    await onFilesSelected(e.dataTransfer.files)
  }

  return (
    <div className="space-y-4">
      {/* kind 切换 */}
      <div className="flex flex-wrap items-center gap-2">
        <div className="inline-flex items-center gap-1 rounded-lg border border-border bg-card p-1 text-sm">
          {(['bgm', 'reference_image', 'reference_video'] as const).map((k) => (
            <button
              key={k}
              onClick={() => setKind(k)}
              className={cn(
                'rounded-md px-3 py-1.5 transition-colors',
                kind === k
                  ? 'bg-primary text-primary-foreground'
                  : 'text-muted-foreground hover:bg-secondary hover:text-foreground',
              )}
            >
              {KIND_LABEL[k]}
              {kind === k && counts > 0 && (
                <span className="ml-1 text-xs opacity-70">{counts}</span>
              )}
            </button>
          ))}
        </div>
        <p className="text-xs text-muted-foreground">{KIND_DESC[kind]}</p>
      </div>

      {/* 上传区 */}
      <div
        onDragOver={(e) => e.preventDefault()}
        onDrop={handleDrop}
        className={cn(
          'flex flex-col items-center justify-center gap-2 rounded-lg border-2 border-dashed border-border bg-card/50 px-6 py-8 text-center transition-colors',
          busy && 'opacity-60',
          'hover:border-primary/50 hover:bg-card',
        )}
      >
        <p className="text-sm font-medium">把文件拖到这里上传</p>
        <p className="text-xs text-muted-foreground">
          或者
          <button
            type="button"
            onClick={onPickFiles}
            disabled={busy}
            className="mx-1 text-primary underline-offset-2 hover:underline disabled:opacity-50"
          >
            点击选择
          </button>
          {KIND_LABEL[kind]} 文件（支持多选）
        </p>
        <input
          ref={fileInputRef}
          type="file"
          accept={KIND_ACCEPT[kind]}
          multiple
          className="hidden"
          onChange={(e) => onFilesSelected(e.target.files)}
        />
        {busy && <p className="text-xs text-muted-foreground">上传中…</p>}
      </div>

      {error && (
        <div className="rounded-md border border-destructive/40 bg-destructive/5 px-4 py-3 text-sm text-destructive">
          {error}
        </div>
      )}

      {/* 列表 */}
      {items === null && !error && (
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
          {[0, 1, 2].map((i) => (
            <div key={i} className="h-32 animate-pulse rounded-lg border border-border bg-card" />
          ))}
        </div>
      )}

      {items && items.length === 0 && (
        <div className="rounded-lg border border-dashed border-border bg-card p-8 text-center">
          <p className="text-sm text-muted-foreground">
            还没有 {KIND_LABEL[kind]} 素材。上传后就能在视频工坊里选用。
          </p>
        </div>
      )}

      {items && items.length > 0 && (
        <div
          className={cn(
            kind === 'bgm'
              ? 'divide-y divide-border overflow-hidden rounded-lg border border-border bg-card'
              : 'grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3',
          )}
        >
          {items.map((asset) => (
            <AssetCard
              key={asset.asset_id}
              asset={asset}
              isEditing={editingId === asset.asset_id}
              onEdit={() => setEditingId(asset.asset_id)}
              onCancelEdit={() => setEditingId(null)}
              onSaved={() => {
                setEditingId(null)
                setPollTick((t) => t + 1)
              }}
              onDelete={() => onDelete(asset.asset_id)}
            />
          ))}
        </div>
      )}
    </div>
  )
}

function AssetCard({
  asset,
  isEditing,
  onEdit,
  onCancelEdit,
  onSaved,
  onDelete,
}: {
  asset: Asset
  isEditing: boolean
  onEdit: () => void
  onCancelEdit: () => void
  onSaved: () => void
  onDelete: () => void
}) {
  const [title, setTitle] = useState(asset.title || asset.file_name)
  const [tags, setTags] = useState((asset.tags || []).join(', '))
  const [saving, setSaving] = useState(false)

  const thumb = thumbnailUrl(asset)
  const meta = asset.metadata as Record<string, unknown>
  const dur = typeof meta?.duration_seconds === 'number' ? `${meta.duration_seconds.toFixed(1)}s` : null

  const onSave = async () => {
    setSaving(true)
    try {
      const tagList = tags
        .split(',')
        .map((t) => t.trim())
        .filter(Boolean)
        .slice(0, 12)
      await api.patch<Asset>(`/asset/${asset.asset_id}`, {
        title: title.slice(0, 120),
        tags: tagList,
      })
      onSaved()
    } finally {
      setSaving(false)
    }
  }

  // BGM 紧密排布：不展示图，单行罗列名字 + 状态 + 音频 + 编辑/删除
  if (asset.kind === 'bgm') {
    return (
      <div className="flex items-center gap-3 px-3 py-1.5 text-sm hover:bg-muted/30">
        <span
          className={cn(
            'shrink-0 rounded-full border px-1.5 py-0.5 text-xs font-medium',
            STATUS_BADGE[asset.status],
          )}
        >
          {STATUS_LABEL[asset.status]}
        </span>
        {isEditing ? (
          <div className="flex flex-1 items-center gap-2">
            <input
              type="text"
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              maxLength={120}
              className="flex-1 rounded border border-input bg-background px-2 py-1 text-sm"
              placeholder="标题"
            />
            <input
              type="text"
              value={tags}
              onChange={(e) => setTags(e.target.value)}
              className="flex-1 rounded border border-input bg-background px-2 py-1 text-xs"
              placeholder="标签，逗号分隔"
            />
            <button
              type="button"
              onClick={onCancelEdit}
              disabled={saving}
              className="shrink-0 rounded-md px-2 py-1 text-xs text-muted-foreground hover:bg-secondary"
            >
              取消
            </button>
            <button
              type="button"
              onClick={onSave}
              disabled={saving}
              className="shrink-0 rounded-md bg-primary px-2 py-1 text-xs text-primary-foreground hover:opacity-90 disabled:opacity-50"
            >
              {saving ? '保存中…' : '保存'}
            </button>
          </div>
        ) : (
          <>
            <span className="flex-1 truncate font-medium" title={asset.title || asset.file_name}>
              {asset.title || asset.file_name}
            </span>
            <span className="shrink-0 text-xs text-muted-foreground">
              {formatBytes(asset.file_size)}
              {dur && ` · ${dur}`}
              {asset.use_count > 0 && ` · 已用 ${asset.use_count}`}
            </span>
            {asset.status === 'ready' && (
              <audio
                src={asset.file_url}
                controls
                className="h-7 w-48 shrink-0"
                preload="none"
              />
            )}
            <button
              type="button"
              onClick={onEdit}
              className="shrink-0 rounded-md px-2 py-1 text-xs text-muted-foreground hover:bg-secondary hover:text-foreground"
            >
              编辑
            </button>
            <button
              type="button"
              onClick={onDelete}
              className="shrink-0 rounded-md px-2 py-1 text-xs text-destructive hover:bg-destructive/10"
            >
              删除
            </button>
          </>
        )}
      </div>
    )
  }

  return (
    <div className="overflow-hidden rounded-lg border border-border bg-card">
      <div className="relative h-32 w-full bg-gradient-to-br from-secondary to-muted">
        {thumb && (
          <img
            src={thumb}
            alt={asset.title || asset.file_name}
            className="h-full w-full object-cover"
            loading="lazy"
          />
        )}
        {!thumb && (
          <div className="flex h-full w-full items-center justify-center text-2xl text-muted-foreground">
            {asset.status === 'processing' ? '⏳' : '?'}
          </div>
        )}
        <div
          className={cn(
            'absolute right-2 top-2 rounded-full border px-2 py-0.5 text-xs font-medium',
            STATUS_BADGE[asset.status],
          )}
        >
          {STATUS_LABEL[asset.status]}
        </div>
      </div>

      <div className="space-y-2 p-3">
        {isEditing ? (
          <>
            <input
              type="text"
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              maxLength={120}
              className="w-full rounded border border-input bg-background px-2 py-1 text-sm"
              placeholder="标题"
            />
            <input
              type="text"
              value={tags}
              onChange={(e) => setTags(e.target.value)}
              className="w-full rounded border border-input bg-background px-2 py-1 text-xs"
              placeholder="标签，用逗号分隔（最多 12 个）"
            />
            <div className="flex justify-end gap-2">
              <button
                type="button"
                onClick={onCancelEdit}
                disabled={saving}
                className="rounded-md px-2 py-1 text-xs text-muted-foreground hover:bg-secondary"
              >
                取消
              </button>
              <button
                type="button"
                onClick={onSave}
                disabled={saving}
                className="rounded-md bg-primary px-2 py-1 text-xs text-primary-foreground hover:opacity-90 disabled:opacity-50"
              >
                {saving ? '保存中…' : '保存'}
              </button>
            </div>
          </>
        ) : (
          <>
            <h4 className="line-clamp-1 text-sm font-medium" title={asset.title || asset.file_name}>
              {asset.title || asset.file_name}
            </h4>
            <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
              <span>{formatBytes(asset.file_size)}</span>
              {dur && <span>· {dur}</span>}
              {asset.use_count > 0 && <span>· 已用 {asset.use_count} 次</span>}
            </div>
            {asset.tags.length > 0 && (
              <div className="flex flex-wrap gap-1">
                {asset.tags.map((t) => (
                  <span
                    key={t}
                    className="rounded bg-secondary px-1.5 py-0.5 text-xs text-muted-foreground"
                  >
                    {t}
                  </span>
                ))}
              </div>
            )}
            {asset.error && (
              <p className="text-xs text-destructive" title={asset.error}>
                分析失败：{asset.error.slice(0, 60)}
              </p>
            )}
            <div className="flex items-center justify-between gap-1 pt-1">
              <div className="ml-auto flex gap-1">
                <button
                  type="button"
                  onClick={onEdit}
                  className="rounded-md px-2 py-1 text-xs text-muted-foreground hover:bg-secondary hover:text-foreground"
                >
                  编辑
                </button>
                <button
                  type="button"
                  onClick={onDelete}
                  className="rounded-md px-2 py-1 text-xs text-destructive hover:bg-destructive/10"
                >
                  删除
                </button>
              </div>
            </div>
          </>
        )}
      </div>
    </div>
  )
}
