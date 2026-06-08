import { useEffect, useRef, useState } from 'react'

import { api } from '@/api/client'
import { cn } from '@/lib/utils'
import type { Plan, PlanSnapshotCreateRequest, PlanSnapshotMeta } from '@/types/schemas'

/**
 * 版本管理下拉菜单——每个 step 的顶部右侧都挂一个，复用同一个 plan 的快照流。
 *
 * 后端：
 *   POST   /plan/{plan_id}/snapshot                 → 命名快照
 *   GET    /plan/{plan_id}/snapshots                → 列表
 *   POST   /plan/{plan_id}/snapshot/{sid}/restore   → 还原（返回 Plan）
 *   DELETE /plan/{plan_id}/snapshot/{sid}           → 删除
 *
 * 用法：
 *   <VersionMenu plan={plan} onPlanRestored={(p) => setPlanAndPush(p)} />
 */
export function VersionMenu({
  plan,
  onPlanRestored,
}: {
  plan: Plan
  onPlanRestored: (plan: Plan) => void
}) {
  const [open, setOpen] = useState(false)
  const [items, setItems] = useState<PlanSnapshotMeta[] | null>(null)
  const [loading, setLoading] = useState(false)
  const [busyId, setBusyId] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const wrapRef = useRef<HTMLDivElement | null>(null)

  // 点外部收菜单
  useEffect(() => {
    if (!open) return
    const onClick = (e: MouseEvent) => {
      if (!wrapRef.current) return
      if (!wrapRef.current.contains(e.target as Node)) setOpen(false)
    }
    window.addEventListener('mousedown', onClick)
    return () => window.removeEventListener('mousedown', onClick)
  }, [open])

  const loadList = async () => {
    setLoading(true)
    setError(null)
    try {
      const list = await api.get<PlanSnapshotMeta[]>(`/plan/${encodeURIComponent(plan.plan_id)}/snapshots`)
      setItems(list.sort((a, b) => b.ts - a.ts))
    } catch (err) {
      setError(err instanceof Error ? err.message : '加载版本列表失败')
    } finally {
      setLoading(false)
    }
  }

  // 打开时拉一次列表
  useEffect(() => {
    if (open) void loadList()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, plan.plan_id])

  const handleSave = async () => {
    const defaultName = `版本 · ${new Date().toLocaleString('zh-CN', { hour12: false })}`
    const name = window.prompt('给当前版本起个名字（可留默认）', defaultName)
    if (name === null) return
    const trimmed = name.trim() || defaultName
    setError(null)
    try {
      const body: PlanSnapshotCreateRequest = { name: trimmed.slice(0, 60) }
      await api.post<PlanSnapshotMeta>(`/plan/${encodeURIComponent(plan.plan_id)}/snapshot`, body)
      await loadList()
    } catch (err) {
      setError(err instanceof Error ? err.message : '保存失败')
    }
  }

  const handleRestore = async (snap: PlanSnapshotMeta) => {
    if (!window.confirm(`恢复到「${snap.name}」？当前未保存的改动会被覆盖。`)) return
    setBusyId(snap.snapshot_id)
    setError(null)
    try {
      const restored = await api.post<Plan>(
        `/plan/${encodeURIComponent(plan.plan_id)}/snapshot/${encodeURIComponent(snap.snapshot_id)}/restore`,
        {},
      )
      onPlanRestored(restored)
      setOpen(false)
    } catch (err) {
      setError(err instanceof Error ? err.message : '恢复失败')
    } finally {
      setBusyId(null)
    }
  }

  const handleDelete = async (snap: PlanSnapshotMeta) => {
    if (!window.confirm(`删除「${snap.name}」？此操作不可撤销。`)) return
    setBusyId(snap.snapshot_id)
    setError(null)
    try {
      await api.delete(`/plan/${encodeURIComponent(plan.plan_id)}/snapshot/${encodeURIComponent(snap.snapshot_id)}`)
      await loadList()
    } catch (err) {
      setError(err instanceof Error ? err.message : '删除失败')
    } finally {
      setBusyId(null)
    }
  }

  const formatTs = (ts: number) => {
    const d = new Date(ts * 1000)
    return d.toLocaleString('zh-CN', { hour12: false, month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })
  }

  return (
    <div ref={wrapRef} className="relative inline-flex items-center gap-1">
      <button
        type="button"
        onClick={() => void handleSave()}
        className="inline-flex items-center gap-1 rounded-md border border-emerald-500/60 bg-emerald-500/10 px-2.5 py-1 text-xs font-medium text-emerald-700 hover:bg-emerald-500/20 dark:text-emerald-300"
        title="保存当前版本——给当前 plan 起个名字、入快照库"
      >
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} className="h-3.5 w-3.5">
          <path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z" />
          <polyline points="17 21 17 13 7 13 7 21" />
          <polyline points="7 3 7 8 15 8" />
        </svg>
        保存
      </button>
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className={cn(
          'inline-flex items-center gap-1 rounded-md border border-border bg-background px-2.5 py-1 text-xs font-medium text-foreground hover:bg-secondary',
          open && 'border-primary bg-primary/10',
        )}
        title="历史版本：切回之前保存的版本"
      >
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} className="h-3.5 w-3.5">
          <circle cx="12" cy="12" r="9" />
          <path d="M12 7v5l3 3" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
        版本
        <span className="text-xs text-muted-foreground">{open ? '▴' : '▾'}</span>
      </button>

      {open && (
        <div className="absolute right-0 top-full z-30 mt-1 w-72 rounded-md border border-border bg-popover p-2 text-popover-foreground shadow-lg">
          <div className="flex items-center gap-1 border-b border-border pb-2">
            <button
              type="button"
              onClick={handleSave}
              className="flex-1 rounded-md bg-primary px-2 py-1 text-xs font-medium text-primary-foreground hover:bg-primary/90"
            >
              + 另存为新版本
            </button>
            <button
              type="button"
              onClick={() => void loadList()}
              disabled={loading}
              className="rounded-md border border-border bg-background px-2 py-1 text-xs hover:bg-secondary disabled:opacity-60"
              title="刷新列表"
            >
              {loading ? '…' : '↻'}
            </button>
          </div>

          {error && (
            <p className="mt-2 rounded bg-destructive/10 px-2 py-1 text-xs text-destructive">{error}</p>
          )}

          <div className="mt-2 max-h-72 space-y-1 overflow-y-auto">
            {items === null && loading && (
              <p className="px-2 py-3 text-center text-xs text-muted-foreground">加载中…</p>
            )}
            {items && items.length === 0 && (
              <p className="px-2 py-3 text-center text-xs text-muted-foreground">
                还没有保存的版本。点上面的「另存为新版本」开始记录你想留住的状态。
              </p>
            )}
            {items?.map((it) => {
              const busy = busyId === it.snapshot_id
              return (
                <div
                  key={it.snapshot_id}
                  className="group flex items-center gap-1 rounded-md border border-border bg-background/40 px-2 py-1.5 hover:border-primary/40"
                >
                  <div className="min-w-0 flex-1">
                    <p className="truncate text-xs font-medium" title={it.name}>{it.name}</p>
                    <p className="text-xs text-muted-foreground">{formatTs(it.ts)}</p>
                  </div>
                  <button
                    type="button"
                    onClick={() => void handleRestore(it)}
                    disabled={busy}
                    className="rounded px-1.5 py-0.5 text-xs text-primary hover:bg-primary/10 disabled:opacity-60"
                    title="恢复到这个版本"
                  >
                    切到
                  </button>
                  <button
                    type="button"
                    onClick={() => void handleDelete(it)}
                    disabled={busy}
                    className="rounded px-1.5 py-0.5 text-xs text-destructive hover:bg-destructive/10 disabled:opacity-60"
                    title="删除这个版本"
                  >
                    删
                  </button>
                </div>
              )
            })}
          </div>
        </div>
      )}
    </div>
  )
}
