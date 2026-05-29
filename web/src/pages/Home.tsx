import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'

import { NewProjectDialog } from '@/components/home/NewProjectDialog'
import { PageShell } from '@/components/layout/PageShell'
import { cn } from '@/lib/utils'
import { useProjectsStore } from '@/stores/projects'
import type { Project, ProjectStatus } from '@/types/schemas'

const STATUS_LABEL: Record<ProjectStatus, string> = {
  draft: '草稿',
  planned: '已规划',
  rendered: '已渲染',
}

const STATUS_COLOR: Record<ProjectStatus, string> = {
  draft: 'bg-slate-500/15 text-slate-700 dark:text-slate-300',
  planned: 'bg-sky-500/15 text-sky-700 dark:text-sky-300',
  rendered: 'bg-emerald-500/15 text-emerald-700 dark:text-emerald-300',
}

export default function HomePage() {
  const navigate = useNavigate()
  const projects = useProjectsStore((s) => s.projects)
  const loading = useProjectsStore((s) => s.loading)
  const error = useProjectsStore((s) => s.error)
  const refresh = useProjectsStore((s) => s.refresh)
  const resumeProject = useProjectsStore((s) => s.resumeProject)
  const deleteProject = useProjectsStore((s) => s.deleteProject)
  const updateProject = useProjectsStore((s) => s.updateProject)
  const [renamingId, setRenamingId] = useState<string | null>(null)
  const [renameInput, setRenameInput] = useState('')
  const [showNew, setShowNew] = useState(false)
  const [enteringId, setEnteringId] = useState<string | null>(null)

  // 首页挂载即拉一次列表
  useEffect(() => {
    void refresh()
  }, [refresh])

  const handleEnter = async (proj: Project) => {
    if (enteringId) return
    setEnteringId(proj.project_id)
    try {
      const loaded = await resumeProject(proj.project_id)
      if (!loaded) return
      if (loaded.status === 'rendered') navigate('/render')
      else if (loaded.status === 'planned') navigate('/compose')
      else navigate('/decompose')
    } finally {
      setEnteringId(null)
    }
  }

  const handleRename = (proj: Project) => {
    setRenamingId(proj.project_id)
    setRenameInput(proj.name)
  }

  const commitRename = async () => {
    const id = renamingId
    const next = renameInput.trim()
    setRenamingId(null)
    setRenameInput('')
    if (id && next) {
      await updateProject(id, { name: next })
    }
  }

  return (
    <PageShell title="首页" subtitle="管理你的历史项目，或新建一个开始创作。">
      <div className="mb-4 flex items-center justify-between">
        <div className="text-sm text-muted-foreground">
          共 {projects.length} 个项目
          {loading && <span className="ml-2 text-xs">· 刷新中…</span>}
        </div>
        <button
          onClick={() => setShowNew(true)}
          className="rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground transition-colors hover:bg-primary/90"
        >
          + 新建项目
        </button>
      </div>

      {error && (
        <div className="mb-4 rounded-md border border-destructive/40 bg-destructive/5 px-4 py-2 text-sm text-destructive">
          {error}
        </div>
      )}

      {projects.length === 0 && !loading ? (
        <div className="rounded-lg border border-dashed border-border bg-card p-12 text-center">
          <p className="text-sm text-muted-foreground">
            还没有项目。点右上「新建项目」从素材库挑一个爆款样例开始。
          </p>
        </div>
      ) : (
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {projects.map((proj) => (
            <div
              key={proj.project_id}
              className="group flex flex-col overflow-hidden rounded-lg border border-border bg-card transition-all hover:-translate-y-0.5 hover:shadow-lg"
            >
              <button
                onClick={() => void handleEnter(proj)}
                disabled={enteringId === proj.project_id}
                className="relative h-40 w-full bg-gradient-to-br from-secondary to-muted disabled:opacity-60"
              >
                <div className="absolute right-2 top-2 flex items-center gap-1">
                  <span
                    className={cn(
                      'rounded-full px-2 py-0.5 text-[10px] font-medium',
                      STATUS_COLOR[proj.status],
                    )}
                  >
                    {STATUS_LABEL[proj.status]}
                  </span>
                </div>
                <div className="absolute inset-0 flex items-center justify-center text-xs text-muted-foreground">
                  {enteringId === proj.project_id
                    ? '加载中…'
                    : proj.status === 'draft'
                      ? '尚未生成预览'
                      : proj.status === 'planned'
                        ? '已规划，未渲染'
                        : '渲染完成'}
                </div>
              </button>

              <div className="flex flex-1 flex-col gap-2 p-4">
                {renamingId === proj.project_id ? (
                  <input
                    autoFocus
                    value={renameInput}
                    onChange={(e) => setRenameInput(e.target.value.slice(0, 60))}
                    onBlur={() => void commitRename()}
                    onKeyDown={(e) => {
                      if (e.key === 'Enter') void commitRename()
                      if (e.key === 'Escape') {
                        setRenamingId(null)
                        setRenameInput('')
                      }
                    }}
                    className="rounded-md border border-primary bg-background px-2 py-1 text-sm outline-none"
                  />
                ) : (
                  <h3
                    className="line-clamp-1 cursor-text text-sm font-semibold leading-snug"
                    onDoubleClick={() => handleRename(proj)}
                    title="双击重命名"
                  >
                    {proj.name}
                  </h3>
                )}

                <div className="flex items-center justify-between text-[11px] text-muted-foreground">
                  <span className="truncate">样例 {proj.sample_id}</span>
                  <span className="font-mono">{formatTime(proj.updated_at)}</span>
                </div>

                {proj.brief && (
                  <p className="line-clamp-2 text-xs text-muted-foreground" title={proj.brief}>
                    {proj.brief}
                  </p>
                )}

                <div className="mt-auto flex items-center gap-2 pt-2">
                  <button
                    onClick={() => void handleEnter(proj)}
                    disabled={enteringId === proj.project_id}
                    className="flex-1 rounded-md bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-60"
                  >
                    {enteringId === proj.project_id ? '加载…' : '进入'}
                  </button>
                  <button
                    onClick={() => handleRename(proj)}
                    className="rounded-md border border-border bg-background px-3 py-1.5 text-xs hover:bg-secondary"
                  >
                    改名
                  </button>
                  <button
                    onClick={() => {
                      if (confirm(`删除项目「${proj.name}」？\n\n这会清空该项目的素材、资产库和所有 plan/gap/fill 数据，且不可撤销。`)) {
                        void deleteProject(proj.project_id)
                      }
                    }}
                    className="rounded-md border border-destructive/40 px-3 py-1.5 text-xs text-destructive hover:bg-destructive/10"
                  >
                    删除
                  </button>
                </div>
              </div>
            </div>
          ))}
        </div>
      )}

      {showNew && (
        <NewProjectDialog
          onClose={() => setShowNew(false)}
          onCreated={(id) => {
            setShowNew(false)
            void (async () => {
              // 拿详情灌进 session，跳到 decompose
              const loaded = await resumeProject(id)
              if (loaded) navigate('/decompose')
            })()
          }}
        />
      )}
    </PageShell>
  )
}

// 后端时间戳为秒（unix timestamp，float），统一乘 1000 给 Date
function formatTime(tsSeconds: number): string {
  const d = new Date(tsSeconds * 1000)
  const now = new Date()
  const sameDay =
    d.getFullYear() === now.getFullYear() &&
    d.getMonth() === now.getMonth() &&
    d.getDate() === now.getDate()
  const hh = d.getHours().toString().padStart(2, '0')
  const mm = d.getMinutes().toString().padStart(2, '0')
  if (sameDay) return `今天 ${hh}:${mm}`
  return `${d.getMonth() + 1}/${d.getDate()} ${hh}:${mm}`
}
