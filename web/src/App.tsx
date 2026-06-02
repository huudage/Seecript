import { useEffect } from 'react'
import { NavLink, Navigate, Route, Routes, useLocation } from 'react-router-dom'
import { cn } from '@/lib/utils'

import HomePage from '@/pages/Home'
import LibraryPage from '@/pages/Library'
import DecomposePage from '@/pages/Decompose'
import ComposePage from '@/pages/Compose'
import MigratePage from '@/pages/Migrate'
import { StepIndicator } from '@/components/nav/StepIndicator'
import { useProjectsStore } from '@/stores/projects'
import type { ProjectStepState, StepName, StepStatus } from '@/types/schemas'

/**
 * 顶部导航 = 项目工作流的 3 步状态机指示器。
 *
 * 渲染（'render'）不在前端 STEP_ORDER 里——后端仍把 render 作为 step_states 的一项
 * 跟踪（编辑锁、status==rendered 标记都依赖它），但 UI 上不再单独成页：渲染流水线
 * 内联在 compose 长页底部，结果视频在同页展示。
 *
 * 步骤可达规则（canEnterStep）：
 * - `saved` / `dirty` → 可点（回看 / 编辑——已有产物，回去改不会丢）
 * - 当前 `current_step` → 可点（正在做的那一步）
 * - `current_step` 之后的第一步 → 可点（线性推进；UX 上当成"下一步在哪"提示）
 * - 其它 `pending` → 禁点（防止用户跳过中间步骤导致产物对不上）
 */
const STEP_ORDER: StepName[] = ['library', 'decompose', 'compose']
const ROUTE_OF: Record<StepName, string> = {
  library: '/library',
  decompose: '/decompose',
  compose: '/compose',
  render: '/compose', // render 不再独立成页；保留映射避免类型缺项
}
const LABEL_OF: Record<StepName, string> = {
  library: '选样例',
  decompose: '样例拆解',
  compose: '新素材 / 缺口 / 渲染',
  render: '渲染', // 不出现在导航里；仅类型完整
}

function canEnterStep(
  step: StepName,
  states: ProjectStepState | undefined,
  current: StepName | undefined,
): boolean {
  if (!states || !current) return false
  const status: StepStatus = states[step]
  if (status === 'saved' || status === 'dirty') return true
  if (step === current) return true
  const currentIdx = STEP_ORDER.indexOf(current)
  const stepIdx = STEP_ORDER.indexOf(step)
  // 当前步之后的第一步（且未开始）允许点进，让用户正向推进
  if (stepIdx === currentIdx + 1 && status === 'pending') return true
  return false
}

// 需要 currentProjectId 才能访问的 path —— 没有项目时回首页。
// 素材库不强制（用户可以浏览样例后再决定建项目）。
const PROJECT_REQUIRED_PATHS = new Set<string>(['/decompose', '/compose', '/migrate'])

function ProjectGuard({ children }: { children: React.ReactNode }) {
  const location = useLocation()
  const currentProjectId = useProjectsStore((s) => s.currentProjectId)
  if (PROJECT_REQUIRED_PATHS.has(location.pathname) && !currentProjectId) {
    return <Navigate to="/" replace />
  }
  return <>{children}</>
}

export default function App() {
  const refreshProjects = useProjectsStore((s) => s.refresh)
  // 应用启动时拉一次项目列表，让 nav 顶栏 + 首页都能直接拿到
  useEffect(() => {
    void refreshProjects()
  }, [refreshProjects])

  return (
    <div className="flex min-h-screen flex-col bg-background text-foreground">
      <header className="border-b border-border bg-card">
        <div className="mx-auto flex max-w-screen-2xl items-center gap-6 px-6 py-3">
          <span className="font-semibold tracking-tight">
            Seecript<span className="text-muted-foreground"> · 爆款结构迁移引擎</span>
          </span>
          <WorkflowNav />
          <CurrentProjectBadge />
        </div>
      </header>

      <main className="flex-1">
        <Routes>
          <Route index element={<HomePage />} />
          <Route path="/library" element={<LibraryPage />} />
          <Route
            path="/decompose"
            element={
              <ProjectGuard>
                <DecomposePage />
              </ProjectGuard>
            }
          />
          <Route
            path="/compose"
            element={
              <ProjectGuard>
                <ComposePage />
              </ProjectGuard>
            }
          />
          <Route
            path="/migrate"
            element={
              <ProjectGuard>
                <MigratePage />
              </ProjectGuard>
            }
          />
          {/* /render 路由已并入 /compose；旧链接重定向 */}
          <Route path="/render" element={<Navigate to="/compose" replace />} />
        </Routes>
      </main>
    </div>
  )
}

function WorkflowNav() {
  const currentProjectId = useProjectsStore((s) => s.currentProjectId)
  const project = useProjectsStore((s) =>
    s.currentProjectId ? s.projects.find((p) => p.project_id === s.currentProjectId) : null,
  )
  const stepStates = project?.step_states
  const currentStep = project?.current_step

  return (
    <nav className="flex items-center gap-1 text-sm">
      {/* 首页始终可点 */}
      <NavLink
        to="/"
        end
        className={({ isActive }) =>
          cn(
            'rounded-md px-3 py-1.5 transition-colors',
            isActive
              ? 'bg-accent text-accent-foreground'
              : 'text-muted-foreground hover:bg-secondary hover:text-foreground',
          )
        }
      >
        首页
      </NavLink>
      {STEP_ORDER.map((step) => {
        const status: StepStatus = stepStates?.[step] ?? 'pending'
        // 没项目时只允许进 library（用户先浏览样例）
        const enabled = currentProjectId
          ? canEnterStep(step, stepStates, currentStep)
          : step === 'library'
        return (
          <NavLink
            key={step}
            to={ROUTE_OF[step]}
            onClick={(e) => {
              if (!enabled) e.preventDefault()
            }}
            className={({ isActive }) =>
              cn(
                'flex items-center gap-2 rounded-md px-3 py-1.5 transition-colors',
                isActive
                  ? 'bg-accent text-accent-foreground'
                  : 'text-muted-foreground hover:bg-secondary hover:text-foreground',
                !enabled && 'cursor-not-allowed opacity-40 hover:bg-transparent hover:text-muted-foreground',
              )
            }
          >
            <StepIndicator status={status} />
            <span>{LABEL_OF[step]}</span>
          </NavLink>
        )
      })}
      {/* Migrate 是 view-only，不在步骤序列里，独立放最后 */}
      <NavLink
        to="/migrate"
        className={({ isActive }) =>
          cn(
            'rounded-md px-3 py-1.5 transition-colors',
            isActive
              ? 'bg-accent text-accent-foreground'
              : 'text-muted-foreground hover:bg-secondary hover:text-foreground',
            !currentProjectId && 'cursor-not-allowed opacity-40 hover:bg-transparent hover:text-muted-foreground',
          )
        }
        onClick={(e) => {
          if (!currentProjectId) e.preventDefault()
        }}
      >
        迁移映射
      </NavLink>
    </nav>
  )
}

function CurrentProjectBadge() {
  const currentProjectId = useProjectsStore((s) => s.currentProjectId)
  const projects = useProjectsStore((s) => s.projects)
  const proj = currentProjectId ? projects.find((p) => p.project_id === currentProjectId) : null
  if (!proj) return null
  return (
    <span
      className="ml-auto truncate rounded-md border border-border bg-background px-2 py-1 text-xs text-muted-foreground"
      title={`当前项目 · ${proj.name}（${proj.project_id}）`}
    >
      {proj.name}
    </span>
  )
}
