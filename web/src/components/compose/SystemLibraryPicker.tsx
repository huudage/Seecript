import { useCallback, useEffect, useMemo, useState } from 'react'

import { api } from '@/api/client'
import { cn } from '@/lib/utils'
import { useProjectsStore } from '@/stores/projects'
import type {
  Material,
  MaterialCloneFromSystemRequest,
  MaterialCloneFromSystemResponse,
} from '@/types/schemas'

/**
 * 跨项目素材库选择器（"+ 从素材库选取"）。
 *
 * 用户原话（2026-06-11）："从素材库选取看的是我的素材，不是样例视频"——
 * 早先版本只看 __system__ 共享池（运维灌入的演示样例），用户不感兴趣；
 * 现在改成枚举用户自己 useProjectsStore.projects[]（排除当前项目）：
 *
 * - 顶部下拉切换源项目（默认第一个非当前项目）
 * - 中部网格展示该项目的 GET /material?project_id=<src> 列表
 * - 多选 + 「克隆到本项目」 → POST /material/clone-from-system 带 source_project_id
 * - 父级 onCloned 接住返回的新 Material[]，appendMaterials 进当前 session
 *
 * 文件名沿用 SystemLibraryPicker 是历史包袱（路由名也叫 clone-from-system），
 * 后端已扩 source_project_id 参数（默认仍 __system__ 保持向后兼容）。
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
  const projects = useProjectsStore((s) => s.projects)
  const refreshProjects = useProjectsStore((s) => s.refresh)

  // 候选源项目：用户其他项目（排除当前项目 + __system__）
  const candidateProjects = useMemo(
    () => projects.filter((p) => p.project_id !== projectId && p.project_id !== '__system__'),
    [projects, projectId],
  )

  const [sourceProjectId, setSourceProjectId] = useState<string | null>(null)
  const [items, setItems] = useState<Material[]>([])
  const [loading, setLoading] = useState(false)
  const [cloning, setCloning] = useState(false)
  const [picked, setPicked] = useState<Set<string>>(() => new Set())
  const [err, setErr] = useState<string | null>(null)
  const [skippedCount, setSkippedCount] = useState(0)

  // 打开时拉一次最新项目列表 —— 避免用户长开 tab 后看不到新项目
  useEffect(() => {
    if (!open) return
    void refreshProjects()
  }, [open, refreshProjects])

  // 默认选中第一个候选项目；用户切项目时清掉已勾选
  useEffect(() => {
    if (!open) return
    if (sourceProjectId && candidateProjects.some((p) => p.project_id === sourceProjectId)) return
    setSourceProjectId(candidateProjects[0]?.project_id ?? null)
    setPicked(new Set())
  }, [open, candidateProjects, sourceProjectId])

  const refresh = useCallback(async (sid: string) => {
    setLoading(true)
    setErr(null)
    try {
      const list = await api.get<Material[]>(`/material?project_id=${encodeURIComponent(sid)}`)
      setItems(list)
    } catch (e) {
      setErr(e instanceof Error ? e.message : '加载源项目素材失败')
    } finally {
      setLoading(false)
    }
  }, [])

  // 切源项目 → 重新拉素材；清掉已勾
  useEffect(() => {
    if (!open || !sourceProjectId) {
      setItems([])
      return
    }
    setPicked(new Set())
    setSkippedCount(0)
    void refresh(sourceProjectId)
  }, [open, sourceProjectId, refresh])

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
    if (!projectId || !sourceProjectId || picked.size === 0) return
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
        source_project_id: sourceProjectId,
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
        if (resp.skipped.length === 0) {
          onClose()
        } else {
          setPicked(new Set())
        }
      } else {
        setErr('全部源素材都缺文件，无法克隆')
      }
    } catch (e) {
      setErr(e instanceof Error ? e.message : '克隆失败')
    } finally {
      setCloning(false)
    }
  }

  const selectedProjectName =
    candidateProjects.find((p) => p.project_id === sourceProjectId)?.name ?? sourceProjectId

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
            <h3 className="text-sm font-semibold">从我的素材库选取</h3>
            <p className="mt-0.5 text-xs text-muted-foreground">
              从你其他项目的素材库挑素材克隆到本项目；克隆生成新 material_id，与原项目独立。
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

        <div className="flex items-center gap-2 border-b border-border px-4 py-2">
          <label className="text-xs font-medium text-muted-foreground">源项目</label>
          {candidateProjects.length === 0 ? (
            <span className="text-xs text-muted-foreground">
              你只有当前一个项目；建多几个项目并上传素材后，这里能跨项目复用
            </span>
          ) : (
            <select
              value={sourceProjectId ?? ''}
              onChange={(e) => setSourceProjectId(e.target.value || null)}
              disabled={cloning}
              className="rounded border border-border bg-background px-2 py-1 text-xs disabled:opacity-50"
            >
              {candidateProjects.map((p) => (
                <option key={p.project_id} value={p.project_id}>
                  {p.name || p.project_id}
                </option>
              ))}
            </select>
          )}
          {sourceProjectId && (
            <span className="ml-auto text-[11px] text-muted-foreground">
              {items.length} 条素材
            </span>
          )}
        </div>

        <div className="flex-1 overflow-y-auto px-4 py-3">
          {!sourceProjectId ? (
            <div className="rounded-md border border-dashed border-border bg-background/30 p-8 text-center text-xs text-muted-foreground">
              请先在上方选择源项目
            </div>
          ) : loading ? (
            <div className="rounded-md border border-dashed border-border bg-background/30 p-8 text-center text-xs text-muted-foreground">
              加载中…
            </div>
          ) : items.length === 0 ? (
            <div className="rounded-md border border-dashed border-border bg-background/30 p-8 text-center text-xs text-muted-foreground">
              「{selectedProjectName}」项目下还没素材；切到别的源项目再试
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
              disabled={
                cloning || picked.size === 0 || picked.size > 20 || !projectId || !sourceProjectId
              }
              className={cn(
                'rounded bg-primary px-3 py-1 text-xs font-medium text-primary-foreground',
                (cloning ||
                  picked.size === 0 ||
                  picked.size > 20 ||
                  !projectId ||
                  !sourceProjectId) &&
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
