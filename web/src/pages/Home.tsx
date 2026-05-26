import { useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'

import { PageShell } from '@/components/layout/PageShell'
import { VIDEO_TYPE_LABEL } from '@/lib/sections'
import { cn } from '@/lib/utils'
import { useProjectsStore, type Project, type ProjectStatus } from '@/stores/projects'

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
  const resumeProject = useProjectsStore((s) => s.resumeProject)
  const removeProject = useProjectsStore((s) => s.removeProject)
  const upsertProject = useProjectsStore((s) => s.upsertProject)
  const [renamingId, setRenamingId] = useState<string | null>(null)
  const [renameInput, setRenameInput] = useState('')

  const sorted = useMemo(
    () => projects.slice().sort((a, b) => b.updated_at - a.updated_at),
    [projects],
  )

  const handleEnter = (proj: Project) => {
    const loaded = resumeProject(proj.id)
    if (!loaded) return
    if (proj.status === 'rendered') navigate('/render')
    else if (proj.status === 'planned') navigate('/compose')
    else navigate('/decompose')
  }

  const handleRename = (proj: Project) => {
    setRenamingId(proj.id)
    setRenameInput(proj.name)
  }
  const commitRename = () => {
    if (renamingId && renameInput.trim()) {
      upsertProject({ id: renamingId, name: renameInput.trim() })
    }
    setRenamingId(null)
    setRenameInput('')
  }

  return (
    <PageShell title="首页" subtitle="管理你的历史项目，或新建一个开始创作。">
      <div className="mb-4 flex items-center justify-between">
        <div className="text-sm text-muted-foreground">
          共 {projects.length} 个项目
        </div>
        <button
          onClick={() => navigate('/library')}
          className="rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground transition-colors hover:bg-primary/90"
        >
          + 新建项目
        </button>
      </div>

      {projects.length === 0 ? (
        <div className="rounded-lg border border-dashed border-border bg-card p-12 text-center">
          <p className="text-sm text-muted-foreground">
            还没有项目。点右上「新建项目」从素材库挑一个爆款样例开始。
          </p>
        </div>
      ) : (
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {sorted.map((proj) => (
            <div
              key={proj.id}
              className="group flex flex-col overflow-hidden rounded-lg border border-border bg-card transition-all hover:-translate-y-0.5 hover:shadow-lg"
            >
              <button
                onClick={() => handleEnter(proj)}
                className="relative h-40 w-full bg-gradient-to-br from-secondary to-muted"
                style={{
                  backgroundImage: proj.last_cover_url
                    ? `url(${proj.last_cover_url})`
                    : undefined,
                  backgroundSize: 'cover',
                  backgroundPosition: 'center',
                }}
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
                <div className="absolute left-2 top-2 rounded-full bg-primary/90 px-2 py-0.5 text-[10px] font-medium text-primary-foreground">
                  {VIDEO_TYPE_LABEL[proj.video_type]}
                </div>
                {!proj.last_cover_url && (
                  <div className="absolute inset-0 flex items-center justify-center text-xs text-muted-foreground">
                    {proj.status === 'draft' ? '尚未生成预览' : '渲染后显示封面'}
                  </div>
                )}
              </button>

              <div className="flex flex-1 flex-col gap-2 p-4">
                {renamingId === proj.id ? (
                  <input
                    autoFocus
                    value={renameInput}
                    onChange={(e) => setRenameInput(e.target.value.slice(0, 60))}
                    onBlur={commitRename}
                    onKeyDown={(e) => {
                      if (e.key === 'Enter') commitRename()
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
                  <span className="truncate">{proj.sample_title}</span>
                  <span className="font-mono">{formatTime(proj.updated_at)}</span>
                </div>

                {proj.brief && (
                  <p className="line-clamp-2 text-xs text-muted-foreground" title={proj.brief}>
                    {proj.brief}
                  </p>
                )}

                <div className="mt-auto flex items-center gap-2 pt-2">
                  <button
                    onClick={() => handleEnter(proj)}
                    className="flex-1 rounded-md bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground hover:bg-primary/90"
                  >
                    进入
                  </button>
                  <button
                    onClick={() => handleRename(proj)}
                    className="rounded-md border border-border bg-background px-3 py-1.5 text-xs hover:bg-secondary"
                  >
                    改名
                  </button>
                  <button
                    onClick={() => {
                      if (confirm(`删除项目「${proj.name}」？此操作不可撤销。`)) {
                        removeProject(proj.id)
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
    </PageShell>
  )
}

function formatTime(ts: number): string {
  const d = new Date(ts)
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
