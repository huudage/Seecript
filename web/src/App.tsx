import { useEffect } from 'react'
import { NavLink, Navigate, Route, Routes, useLocation } from 'react-router-dom'
import { cn } from '@/lib/utils'

import HomePage from '@/pages/Home'
import LibraryPage from '@/pages/Library'
import DecomposePage from '@/pages/Decompose'
import ComposePage from '@/pages/Compose'
import MigratePage from '@/pages/Migrate'
import RenderPage from '@/pages/Render'
import { useProjectsStore } from '@/stores/projects'

const navItems = [
  { to: '/', label: '首页', end: true },
  { to: '/library', label: '素材库', end: false },
  { to: '/decompose', label: '样例拆解', end: false },
  { to: '/compose', label: '新素材 / 缺口', end: false },
  { to: '/migrate', label: '迁移映射', end: false },
  { to: '/render', label: '生成 / 编辑', end: false },
] as const

// 需要 currentProjectId 才能访问的 path —— 没有项目时回首页。
// 素材库不强制（用户可以浏览样例后再决定建项目）。
const PROJECT_REQUIRED_PATHS = new Set<string>(['/decompose', '/compose', '/migrate', '/render'])

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
          <nav className="flex items-center gap-1 text-sm">
            {navItems.map((item) => (
              <NavLink
                key={item.to}
                to={item.to}
                end={item.end}
                className={({ isActive }) =>
                  cn(
                    'rounded-md px-3 py-1.5 transition-colors',
                    isActive
                      ? 'bg-accent text-accent-foreground'
                      : 'text-muted-foreground hover:bg-secondary hover:text-foreground',
                  )
                }
              >
                {item.label}
              </NavLink>
            ))}
          </nav>
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
          <Route
            path="/render"
            element={
              <ProjectGuard>
                <RenderPage />
              </ProjectGuard>
            }
          />
        </Routes>
      </main>
    </div>
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
