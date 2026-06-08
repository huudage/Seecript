import { useCallback, useEffect, useMemo, useState } from 'react'

import { api, ApiError } from '@/api/client'
import { PageShell } from '@/components/layout/PageShell'
import { cn } from '@/lib/utils'

/**
 * 个性知识库管理页 —— Hermes 风格规则库的总开关 + 项目级粒度管理。
 *
 * 三块内容：
 *  1. 用户级设置：自动学习开关（关掉只写 trace 不出 KB）。
 *  2. 默认库说明：内置 prompt（不可关、不可编辑），文字告知。
 *  3. 项目 KB 列表：每个项目一个折叠卡，显示规则数 + summary；
 *     - top-10 最近完成的项目：标 "默认启用"，复选框 disabled。
 *     - 其它项目：复选框可切换，落到 enabled_extra_project_ids。
 *     - 展开能看 rules 全文（scope + text）。
 */

interface ProfileSettings {
  realtime_distill_enabled: boolean
  enabled_extra_project_ids: string[]
}

interface ProjectKBSummary {
  project_id: string
  project_title: string
  video_type: string | null
  render_committed_at: number
  summary: string
  rules_count: number
  enabled: boolean
  is_top10: boolean
  is_extra_enabled: boolean
}

interface ProfileOverview {
  settings: ProfileSettings
  default_kb_description: string
  projects: ProjectKBSummary[]
}

interface KBRule {
  id: string
  scope: string
  text: string
  evidence_trace_ids: string[]
}

interface ProjectKBFull {
  project_id: string
  project_title: string
  video_type: string | null
  render_committed_at: number
  summary: string
  rules: KBRule[]
}

const SCOPE_LABEL: Record<string, string> = {
  structure: '段落结构',
  source: '镜头来源',
  narration: '口播/文案',
  pacing: '节奏与时长',
}

function formatTs(ts: number): string {
  if (!ts) return '—'
  const d = new Date(ts * 1000)
  return d.toLocaleString('zh-CN', { hour12: false })
}

export default function KnowledgePage() {
  const [overview, setOverview] = useState<ProfileOverview | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [savingSettings, setSavingSettings] = useState(false)
  const [pendingProjectId, setPendingProjectId] = useState<string | null>(null)
  const [expanded, setExpanded] = useState<Record<string, ProjectKBFull | 'loading' | 'error'>>({})

  const reload = useCallback(async () => {
    try {
      const data = await api.get<ProfileOverview>('/profile')
      setOverview(data)
    } catch (err) {
      const msg = err instanceof ApiError ? err.message : String(err)
      setError(msg)
    }
  }, [])

  useEffect(() => {
    void reload()
  }, [reload])

  const totalActiveRules = useMemo(() => {
    if (!overview) return 0
    return overview.projects
      .filter((p) => p.enabled)
      .reduce((sum, p) => sum + p.rules_count, 0)
  }, [overview])

  const toggleRealtimeDistill = useCallback(
    async (next: boolean) => {
      if (!overview) return
      setSavingSettings(true)
      try {
        const updated = await api.patch<ProfileSettings>('/profile/settings', {
          realtime_distill_enabled: next,
        })
        setOverview({ ...overview, settings: updated })
      } catch (err) {
        const msg = err instanceof ApiError ? err.message : String(err)
        setError(msg)
      } finally {
        setSavingSettings(false)
      }
    },
    [overview],
  )

  const toggleProject = useCallback(
    async (project_id: string, next: boolean) => {
      if (!overview) return
      setPendingProjectId(project_id)
      try {
        const updated = await api.patch<ProfileSettings>(
          `/profile/projects/${encodeURIComponent(project_id)}/enabled`,
          { enabled: next },
        )
        const extraSet = new Set(updated.enabled_extra_project_ids)
        setOverview({
          ...overview,
          settings: updated,
          projects: overview.projects.map((p) =>
            p.project_id === project_id
              ? {
                  ...p,
                  is_extra_enabled: extraSet.has(p.project_id),
                  enabled: p.is_top10 || extraSet.has(p.project_id),
                }
              : p,
          ),
        })
      } catch (err) {
        const msg = err instanceof ApiError ? err.message : String(err)
        setError(msg)
      } finally {
        setPendingProjectId(null)
      }
    },
    [overview],
  )

  const handleExpand = useCallback(
    async (project_id: string) => {
      // 收起
      if (expanded[project_id]) {
        setExpanded((cur) => {
          const cp = { ...cur }
          delete cp[project_id]
          return cp
        })
        return
      }
      setExpanded((cur) => ({ ...cur, [project_id]: 'loading' }))
      try {
        const kb = await api.get<ProjectKBFull>(
          `/profile/projects/${encodeURIComponent(project_id)}`,
        )
        setExpanded((cur) => ({ ...cur, [project_id]: kb }))
      } catch {
        setExpanded((cur) => ({ ...cur, [project_id]: 'error' }))
      }
    },
    [expanded],
  )

  return (
    <PageShell
      title="我的创作偏好"
      subtitle="AI 会记住你的创作习惯。每次完成视频后自动总结你的风格偏好，下次创作时自动帮你保持一致。"
    >
      {error && (
        <div className="mb-4 rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive">
          {error}
        </div>
      )}

      {!overview ? (
        <div className="text-sm text-muted-foreground">加载中…</div>
      ) : (
        <div className="space-y-6">
          {/* ============ 1. 用户级设置 ============ */}
          <section className="rounded-xl border border-border bg-card p-4 transition-all duration-300 hover:border-primary/20">
            <h2 className="text-sm font-semibold">用户级设置</h2>
            <label className="mt-3 flex items-start gap-3 text-sm">
              <input
                type="checkbox"
                disabled={savingSettings}
                checked={overview.settings.realtime_distill_enabled}
                onChange={(e) => toggleRealtimeDistill(e.target.checked)}
                className="mt-0.5 h-4 w-4 accent-primary"
              />
              <div>
                <div className="font-medium">实时蒸馏</div>
                <div className="text-xs leading-relaxed text-muted-foreground">
                  开启后，每次导出视频时 AI 会自动总结你的创作偏好；
                  关闭后只保存记录不自动总结（后续可手动触发或重新打开）。
                </div>
              </div>
            </label>
          </section>

          {/* ============ 2. 默认库 ============ */}
          <section className="rounded-lg border border-border bg-card p-4">
            <div className="flex items-center justify-between">
              <h2 className="text-sm font-semibold">默认偏好（始终生效）</h2>
              <span className="rounded-full bg-secondary px-2 py-0.5 text-xs text-secondary-foreground">
                内置 · 不可关
              </span>
            </div>
            <p className="mt-2 text-xs leading-relaxed text-muted-foreground">
              {overview.default_kb_description}
            </p>
          </section>

          {/* ============ 3. 项目 KB 列表 ============ */}
          <section className="rounded-lg border border-border bg-card p-4">
            <div className="mb-3 flex items-center justify-between">
              <h2 className="text-sm font-semibold">项目偏好</h2>
              <span className="text-xs text-muted-foreground">
                {overview.projects.length} 个 · 当前注入 {totalActiveRules} 条偏好
              </span>
            </div>
            {overview.projects.length === 0 ? (
              <p className="text-sm text-muted-foreground">
                还没有项目偏好。完成一个视频的导出（且开启自动学习）后，AI 会帮你总结。
              </p>
            ) : (
              <ul className="space-y-2">
                {overview.projects.map((p) => {
                  const exp = expanded[p.project_id]
                  const isExpanded = exp != null
                  return (
                    <li
                      key={p.project_id}
                      className={cn(
                        'rounded-xl bg-card shadow-sm p-3 transition-all duration-300 hover:shadow-md',
                        p.enabled && 'border-primary/40 bg-primary/5',
                      )}
                    >
                      <div className="flex flex-wrap items-start gap-3">
                        <label
                          className="flex items-center gap-2 text-xs"
                          title={
                            p.is_top10
                              ? '最近 10 个已完成项目自动启用，无法手动关闭'
                              : '启用后，AI 创作时会参考这个项目的偏好'
                          }
                        >
                          <input
                            type="checkbox"
                            disabled={p.is_top10 || pendingProjectId === p.project_id}
                            checked={p.enabled}
                            onChange={(e) => toggleProject(p.project_id, e.target.checked)}
                            className="h-4 w-4 accent-primary"
                          />
                          <span
                            className={cn(
                              'rounded-full px-2 py-0.5',
                              p.enabled
                                ? 'bg-primary/20 text-primary'
                                : 'bg-secondary text-secondary-foreground',
                            )}
                          >
                            {p.is_top10 ? '自动启用（最近项目）' : p.enabled ? '已启用' : '未启用'}
                          </span>
                        </label>
                        <div className="min-w-0 flex-1">
                          <div className="flex items-baseline gap-2">
                            <span className="truncate text-sm font-medium">{p.project_title}</span>
                            <span className="text-xs text-muted-foreground">
                              {p.rules_count} 条规则
                              {p.video_type && ` · ${p.video_type}`}
                            </span>
                          </div>
                          <p className="mt-0.5 truncate text-xs text-muted-foreground" title={p.summary}>
                            {p.summary || '（暂无 summary）'}
                          </p>
                          <p className="mt-0.5 text-xs text-muted-foreground">
                            最近渲染：{formatTs(p.render_committed_at)}
                          </p>
                        </div>
                        <button
                          type="button"
                          onClick={() => handleExpand(p.project_id)}
                          className="rounded-lg border border-border bg-card px-2 py-1 text-xs font-medium hover:bg-secondary hover:-translate-y-px active:scale-[0.98] transition-all duration-200"
                        >
                          {isExpanded ? '收起' : '查看规则'}
                        </button>
                      </div>

                      {isExpanded && (
                        <div className="mt-3 rounded-md border border-border bg-background/60 p-2">
                          {exp === 'loading' && (
                            <div className="text-xs text-muted-foreground">加载规则中…</div>
                          )}
                          {exp === 'error' && (
                            <div className="text-xs text-destructive">规则加载失败，请刷新重试</div>
                          )}
                          {exp !== 'loading' && exp !== 'error' && exp != null && (
                            <ul className="space-y-1.5">
                              {exp.rules.length === 0 && (
                                <li className="text-xs text-muted-foreground">
                                  暂无偏好（信号不足，无法自动总结）
                                </li>
                              )}
                              {exp.rules.map((r) => (
                                <li
                                  key={r.id}
                                  className="rounded border border-border/60 bg-card px-2 py-1.5"
                                >
                                  <div className="flex items-center gap-2">
                                    <span className="rounded-full bg-primary/15 px-1.5 py-0.5 text-xs font-medium text-primary">
                                      {SCOPE_LABEL[r.scope] ?? r.scope}
                                    </span>
                                    <span className="text-xs text-muted-foreground">
                                      #{r.id}
                                    </span>
                                  </div>
                                  <p className="mt-1 text-xs leading-relaxed">{r.text}</p>
                                </li>
                              ))}
                            </ul>
                          )}
                        </div>
                      )}
                    </li>
                  )
                })}
              </ul>
            )}
          </section>
        </div>
      )}
    </PageShell>
  )
}
