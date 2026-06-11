import { useCallback, useEffect, useMemo, useState } from 'react'

import { api } from '@/api/client'
import { cn } from '@/lib/utils'
import { useProjectsStore } from '@/stores/projects'
import type {
  Asset,
  AssetListResponse,
  Material,
  MaterialCloneFromAssetRequest,
  MaterialCloneFromAssetResponse,
  MaterialCloneFromSystemRequest,
  MaterialCloneFromSystemResponse,
} from '@/types/schemas'

/**
 * 跨项目素材库选择器（"+ 从素材库选取"）。
 *
 * 用户原话（2026-06-11）："从素材库选取看的是我的素材，不是样例视频"——
 * stage-67 改成枚举用户自己 projects 列表（排除当前项目）。
 *
 * stage-75 追加（2026-06-12）："还是不支持选取我之前截图给你的'我的素材'区域中的素材"——
 * 顶部下拉新增 "📁 我的素材库" 虚拟源（assets），列出当前项目 Asset Library 里的
 * reference_image / reference_video，克隆走 POST /material/clone-from-asset
 * （bgm 后端拒绝；不展示）。其余跨项目克隆走原 /material/clone-from-system。
 *
 * 文件名沿用 SystemLibraryPicker 是历史包袱（路由名也叫 clone-from-system）。
 */

type SourceMode = 'project' | 'assets'

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

  // mode='assets' → 拉当前 project 的 Asset Library；mode='project' → 拉源项目 Material
  const [mode, setMode] = useState<SourceMode>('project')
  const [sourceProjectId, setSourceProjectId] = useState<string | null>(null)
  const [materials, setMaterials] = useState<Material[]>([])
  const [assets, setAssets] = useState<Asset[]>([])
  const [loading, setLoading] = useState(false)
  const [cloning, setCloning] = useState(false)
  const [picked, setPicked] = useState<Set<string>>(() => new Set())
  const [err, setErr] = useState<string | null>(null)
  const [skippedCount, setSkippedCount] = useState(0)

  useEffect(() => {
    if (!open) return
    void refreshProjects()
  }, [open, refreshProjects])

  useEffect(() => {
    if (!open) return
    if (mode !== 'project') return
    if (sourceProjectId && candidateProjects.some((p) => p.project_id === sourceProjectId)) return
    setSourceProjectId(candidateProjects[0]?.project_id ?? null)
    setPicked(new Set())
  }, [open, mode, candidateProjects, sourceProjectId])

  const refreshMaterials = useCallback(async (sid: string) => {
    setLoading(true)
    setErr(null)
    try {
      const list = await api.get<Material[]>(`/material?project_id=${encodeURIComponent(sid)}`)
      setMaterials(list)
    } catch (e) {
      setErr(e instanceof Error ? e.message : '加载源项目素材失败')
    } finally {
      setLoading(false)
    }
  }, [])

  const refreshAssets = useCallback(async (pid: string) => {
    setLoading(true)
    setErr(null)
    try {
      // 一次拉本项目所有资产，前端再过滤 reference_image / reference_video
      const resp = await api.get<AssetListResponse>(
        `/asset/library?project_id=${encodeURIComponent(pid)}`,
      )
      setAssets(resp.items.filter((a) => a.kind !== 'bgm'))
    } catch (e) {
      setErr(e instanceof Error ? e.message : '加载我的素材库失败')
    } finally {
      setLoading(false)
    }
  }, [])

  // 切 mode/源 → 重新拉数据；清掉已勾
  useEffect(() => {
    if (!open) return
    setPicked(new Set())
    setSkippedCount(0)
    if (mode === 'assets') {
      setMaterials([])
      if (projectId) void refreshAssets(projectId)
    } else {
      setAssets([])
      if (sourceProjectId) void refreshMaterials(sourceProjectId)
    }
  }, [open, mode, sourceProjectId, projectId, refreshMaterials, refreshAssets])

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
      let cloned: Material[] = []
      let skipped: string[] = []
      if (mode === 'assets') {
        const body: MaterialCloneFromAssetRequest = {
          project_id: projectId,
          source_asset_ids: Array.from(picked),
        }
        const resp = await api.post<MaterialCloneFromAssetResponse>(
          '/material/clone-from-asset',
          body,
        )
        cloned = resp.materials
        skipped = resp.skipped
      } else {
        if (!sourceProjectId) return
        const body: MaterialCloneFromSystemRequest = {
          project_id: projectId,
          source_project_id: sourceProjectId,
          source_material_ids: Array.from(picked),
        }
        const resp = await api.post<MaterialCloneFromSystemResponse>(
          '/material/clone-from-system',
          body,
        )
        cloned = resp.materials
        skipped = resp.skipped
      }
      if (cloned.length > 0) {
        onCloned(cloned)
      }
      setSkippedCount(skipped.length)
      if (cloned.length > 0) {
        if (skipped.length === 0) {
          onClose()
        } else {
          setPicked(new Set())
        }
      } else {
        setErr(mode === 'assets' ? '全部源资产无法克隆（缺文件或为 BGM）' : '全部源素材都缺文件，无法克隆')
      }
    } catch (e) {
      setErr(e instanceof Error ? e.message : '克隆失败')
    } finally {
      setCloning(false)
    }
  }

  const selectedProjectName =
    candidateProjects.find((p) => p.project_id === sourceProjectId)?.name ?? sourceProjectId
  const itemsCount = mode === 'assets' ? assets.length : materials.length

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
              「我的素材库」= 本项目长期资产库（参考图/参考视频）；其余 = 你别的项目的内容素材；任一来源都会克隆为本项目新素材。
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
          <label className="text-xs font-medium text-muted-foreground">来源</label>
          <select
            value={mode === 'assets' ? '__assets__' : (sourceProjectId ?? '')}
            onChange={(e) => {
              const v = e.target.value
              if (v === '__assets__') {
                setMode('assets')
              } else {
                setMode('project')
                setSourceProjectId(v || null)
              }
            }}
            disabled={cloning}
            className="rounded border border-border bg-background px-2 py-1 text-xs disabled:opacity-50"
          >
            <option value="__assets__">📁 我的素材库（参考图/参考视频）</option>
            {candidateProjects.length === 0 ? (
              <option value="" disabled>
                （还没有其他项目）
              </option>
            ) : (
              candidateProjects.map((p) => (
                <option key={p.project_id} value={p.project_id}>
                  📂 {p.name || p.project_id}
                </option>
              ))
            )}
          </select>
          <span className="ml-auto text-[11px] text-muted-foreground">{itemsCount} 条</span>
        </div>

        <div className="flex-1 overflow-y-auto px-4 py-3">
          {loading ? (
            <div className="rounded-md border border-dashed border-border bg-background/30 p-8 text-center text-xs text-muted-foreground">
              加载中…
            </div>
          ) : mode === 'assets' ? (
            assets.length === 0 ? (
              <div className="rounded-md border border-dashed border-border bg-background/30 p-8 text-center text-xs text-muted-foreground">
                本项目「我的素材」里还没有参考图或参考视频；先去左侧 库 → 我的素材 里上传一些
              </div>
            ) : (
              <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-4">
                {assets.map((a) => {
                  const active = picked.has(a.asset_id)
                  const thumb =
                    a.kind === 'reference_image'
                      ? a.file_url
                      : (a.metadata?.thumbnail_url as string | undefined)
                  return (
                    <button
                      key={a.asset_id}
                      type="button"
                      onClick={() => togglePick(a.asset_id)}
                      disabled={cloning}
                      className={cn(
                        'overflow-hidden rounded border bg-background text-left transition-colors disabled:opacity-50',
                        active
                          ? 'border-primary ring-2 ring-primary/40'
                          : 'border-border hover:border-primary/60',
                      )}
                    >
                      <div className="relative aspect-video bg-muted">
                        {thumb ? (
                          <img
                            src={thumb}
                            alt={a.title || a.file_name}
                            className="h-full w-full object-cover"
                            loading="lazy"
                          />
                        ) : (
                          <div className="flex h-full w-full items-center justify-center text-base text-muted-foreground">
                            {a.kind === 'reference_video' ? '🎬' : '🖼'}
                          </div>
                        )}
                        {active && (
                          <span className="absolute right-1 top-1 rounded bg-primary px-1 text-xs text-primary-foreground">
                            ✓
                          </span>
                        )}
                      </div>
                      <div className="px-2 py-1.5">
                        <div className="truncate text-xs font-medium" title={a.title || a.file_name}>
                          {a.title || a.file_name}
                        </div>
                        <div className="flex items-center gap-1 text-xs text-muted-foreground">
                          <span className="font-mono">
                            {a.kind === 'reference_video' ? 'video' : 'image'}
                          </span>
                          {typeof a.metadata?.duration_seconds === 'number' && (
                            <span>· {(a.metadata.duration_seconds as number).toFixed(1)}s</span>
                          )}
                        </div>
                        {a.tags.length > 0 && (
                          <div className="mt-0.5 line-clamp-1 text-xs text-muted-foreground">
                            {a.tags.slice(0, 3).join(' · ')}
                          </div>
                        )}
                      </div>
                    </button>
                  )
                })}
              </div>
            )
          ) : !sourceProjectId ? (
            <div className="rounded-md border border-dashed border-border bg-background/30 p-8 text-center text-xs text-muted-foreground">
              请先在上方选择源项目，或切换到「我的素材库」
            </div>
          ) : materials.length === 0 ? (
            <div className="rounded-md border border-dashed border-border bg-background/30 p-8 text-center text-xs text-muted-foreground">
              「{selectedProjectName}」项目下还没素材；切到别的源或「我的素材库」再试
            </div>
          ) : (
            <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-4">
              {materials.map((m) => {
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
                有 {skippedCount} 个源{mode === 'assets' ? '资产' : '素材'}缺文件或不支持，已跳过；其余已成功克隆。
              </p>
            )}
          </div>
        )}

        <footer className="flex shrink-0 items-center justify-between gap-2 border-t border-border px-4 py-2">
          <span className="text-xs text-muted-foreground">
            已选 {picked.size}/{itemsCount} 个
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
                cloning ||
                picked.size === 0 ||
                picked.size > 20 ||
                !projectId ||
                (mode === 'project' && !sourceProjectId)
              }
              className={cn(
                'rounded bg-primary px-3 py-1 text-xs font-medium text-primary-foreground',
                (cloning ||
                  picked.size === 0 ||
                  picked.size > 20 ||
                  !projectId ||
                  (mode === 'project' && !sourceProjectId)) &&
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
