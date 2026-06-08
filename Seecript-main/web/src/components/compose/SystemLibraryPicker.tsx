import { useCallback, useEffect, useState } from 'react'

import { api } from '@/api/client'
import { cn } from '@/lib/utils'
import type {
  Material,
  MaterialCloneFromSystemRequest,
  MaterialCloneFromSystemResponse,
} from '@/types/schemas'

/**
 * 系统素材库选择器。
 *
 * - 打开时拉 GET /material?project_id=__system__ 列出共享素材
 * - 多选 → 「克隆到本项目」 → POST /material/clone-from-system → 父级 appendMaterials
 * - 失败：分项 skipped 提示，不阻塞已成功克隆的部分
 *
 * 系统素材库的灌入约定：运维通过 `POST /material/upload` 带 project_id=__system__ 上传，
 * 任何项目都可只读列出 + 克隆补充到自己的项目素材库；克隆走完整 file copy + 元数据复制。
 */
export function SystemLibraryPicker({
  open,
  projectId,
  onClose,
  onCloned,
}: {
  open: boolean
  projectId: string | null
  onClose: () => void
  onCloned: (materials: Material[]) => void
}) {
  const [items, setItems] = useState<Material[]>([])
  const [loading, setLoading] = useState(false)
  const [cloning, setCloning] = useState(false)
  const [picked, setPicked] = useState<Set<string>>(() => new Set())
  const [err, setErr] = useState<string | null>(null)
  const [skippedCount, setSkippedCount] = useState(0)

  const refresh = useCallback(async () => {
    setLoading(true)
    setErr(null)
    try {
      const list = await api.get<Material[]>('/material?project_id=__system__')
      setItems(list)
    } catch (e) {
      setErr(e instanceof Error ? e.message : '加载系统素材库失败')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    if (!open) return
    setPicked(new Set())
    setSkippedCount(0)
    void refresh()
  }, [open, refresh])

  if (!open) return null

  const togglePick = (id: string) => {
    setPicked((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  const handleClone = async () => {
    if (!projectId || picked.size === 0) return
    if (picked.size > 20) {
      setErr('单次最多克隆 20 个；请分批选择')
      return
    }
    setCloning(true)
    setErr(null)
    setSkippedCount(0)
    try {
      const body: MaterialCloneFromSystemRequest = {
        project_id: projectId,
        source_material_ids: Array.from(picked),
      }
      const resp = await api.post<MaterialCloneFromSystemResponse>(
        '/material/clone-from-system',
        body,
      )
      if (resp.materials.length > 0) {
        onCloned(resp.materials)
      }
      setSkippedCount(resp.skipped.length)
      if (resp.materials.length > 0) {
        // 全部成功 → 关闭；部分失败 → 留在面板看 skipped 提示
        if (resp.skipped.length === 0) {
          onClose()
        } else {
          setPicked(new Set())
        }
      } else {
        setErr('全部源素材都缺文件，请联系运维补 seed')
      }
    } catch (e) {
      setErr(e instanceof Error ? e.message : '克隆失败')
    } finally {
      setCloning(false)
    }
  }

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4"
      onClick={cloning ? undefined : onClose}
    >
      <div
        className="flex w-full max-w-3xl flex-col rounded-lg border border-border bg-card shadow-xl"
        style={{ maxHeight: '85vh' }}
        onClick={(e) => e.stopPropagation()}
      >
        <header className="flex items-start justify-between border-b border-border px-4 py-3">
          <div>
            <h3 className="text-sm font-semibold">从系统素材库添加</h3>
            <p className="mt-0.5 text-xs text-muted-foreground">
              共享素材池——点击勾选后克隆到本项目；克隆生成新 material_id，与系统库独立。
            </p>
          </div>
          <button
            onClick={onClose}
            disabled={cloning}
            className="rounded text-lg leading-none text-muted-foreground hover:text-foreground disabled:opacity-50"
            aria-label="关闭"
          >
            ×
          </button>
        </header>

        <div className="flex-1 overflow-y-auto px-4 py-3">
          {loading ? (
            <div className="rounded-md border border-dashed border-border bg-background/30 p-8 text-center text-xs text-muted-foreground">
              加载中…
            </div>
          ) : items.length === 0 ? (
            <div className="rounded-md border border-dashed border-border bg-background/30 p-8 text-center text-xs text-muted-foreground">
              系统素材库还是空的——运维通过 <code className="rounded bg-secondary/60 px-1">/material/upload</code>{' '}
              带 <code className="rounded bg-secondary/60 px-1">project_id=__system__</code>{' '}
              往里塞素材后，这里会列出。
            </div>
          ) : (
            <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-4">
              {items.map((m) => {
                const active = picked.has(m.material_id)
                return (
                  <button
                    key={m.material_id}
                    type="button"
                    onClick={() => togglePick(m.material_id)}
                    disabled={cloning}
                    className={cn(
                      'overflow-hidden rounded border bg-background text-left transition-colors disabled:opacity-50',
                      active
                        ? 'border-primary ring-2 ring-primary/40'
                        : 'border-border hover:border-primary/60',
                    )}
                  >
                    <div className="relative aspect-video bg-muted">
                      {m.thumbnail_url ? (
                        <img
                          src={m.thumbnail_url}
                          alt={m.filename}
                          className="h-full w-full object-cover"
                          loading="lazy"
                        />
                      ) : (
                        <div className="flex h-full w-full items-center justify-center text-base text-muted-foreground">
                          {m.media_type === 'audio'
                            ? '🎵'
                            : m.media_type === 'video'
                              ? '🎬'
                              : '🖼'}
                        </div>
                      )}
                      {active && (
                        <span className="absolute right-1 top-1 rounded bg-primary px-1 text-xs text-primary-foreground">
                          ✓
                        </span>
                      )}
                    </div>
                    <div className="px-2 py-1.5">
                      <div className="truncate text-xs font-medium" title={m.filename}>
                        {m.filename}
                      </div>
                      <div className="flex items-center gap-1 text-xs text-muted-foreground">
                        <span className="font-mono">{m.media_type}</span>
                        {m.shots && m.shots.length > 1 && <span>· {m.shots.length} 镜</span>}
                      </div>
                      {m.tags.length > 0 && (
                        <div className="mt-0.5 line-clamp-1 text-xs text-muted-foreground">
                          {m.tags.slice(0, 3).join(' · ')}
                        </div>
                      )}
                    </div>
                  </button>
                )
              })}
            </div>
          )}
        </div>

        {(err || skippedCount > 0) && (
          <div className="border-t border-border bg-amber-500/10 px-4 py-1.5 text-xs">
            {err && <p className="text-destructive">{err}</p>}
            {skippedCount > 0 && (
              <p className="text-amber-700 dark:text-amber-300">
                有 {skippedCount} 个源素材缺文件，已跳过；其余已成功克隆。
              </p>
            )}
          </div>
        )}

        <footer className="flex shrink-0 items-center justify-between gap-2 border-t border-border px-4 py-2">
          <span className="text-xs text-muted-foreground">
            已选 {picked.size}/{items.length} 个
            {picked.size > 20 && '（单次最多 20 个）'}
          </span>
          <div className="flex items-center gap-2">
            <button
              onClick={onClose}
              disabled={cloning}
              className="rounded border border-border bg-background px-3 py-1 text-xs hover:bg-secondary disabled:opacity-50"
            >
              取消
            </button>
            <button
              onClick={handleClone}
              disabled={cloning || picked.size === 0 || picked.size > 20 || !projectId}
              className={cn(
                'rounded bg-primary px-3 py-1 text-xs font-medium text-primary-foreground',
                (cloning || picked.size === 0 || picked.size > 20 || !projectId) &&
                  'cursor-not-allowed opacity-60',
              )}
            >
              {cloning ? '克隆中…' : `克隆到本项目（${picked.size}）`}
            </button>
          </div>
        </footer>
      </div>
    </div>
  )
}
