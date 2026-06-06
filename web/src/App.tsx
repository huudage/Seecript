import { useEffect } from 'react'
import { NavLink, Navigate, Route, Routes, useLocation } from 'react-router-dom'
import { cn } from '@/lib/utils'

import HomePage from '@/pages/Home'
import LibraryPage from '@/pages/Library'
import DecomposePage from '@/pages/Decompose'
import ComposePage from '@/pages/Compose'
import KnowledgePage from '@/pages/Knowledge'
import { useProjectsStore } from '@/stores/projects'

/**
 * 顶栏 = 4 模块平铺导航。
 *
 * - 首页：项目列表
 * - 资产库：收藏的爆款样例 + 上传素材
 * - 样例拆解：把样例拆出结构
 * - 视频工坊：写主题 → 配素材 → 对照结构 → 出片（吸收原 Compose + Migrate）
 *
 * 旧路由 /compose、/migrate 全部重定向到 /workshop。
 * 项目工作流的 step_states 仍由后端跟踪,但不再决定导航可见性 —— 用户自己点。
 */
const NAV_ITEMS: { to: string; label: string; requireProject: boolean }[] = [
  { to: '/', label: '首页', requireProject: false },
  { to: '/library', label: '资产库', requireProject: false },
  { to: '/decompose', label: '样例拆解', requireProject: true },
  { to: '/workshop', label: '视频工坊', requireProject: true },
  { to: '/knowledge', label: '个性知识库', requireProject: false },
]

// 需要先选项目才能进的 path。
const PROJECT_REQUIRED_PATHS = new Set<string>(['/decompose', '/workshop'])

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
  useEffect(() => {
    void refreshProjects()
  }, [refreshProjects])

  return (
    <div className="flex min-h-screen flex-col bg-background text-foreground">
      <header className="border-b border-border bg-card">
        <div className="mx-auto flex max-w-screen-2xl items-center gap-6 px-6 py-3">
          <span className="font-semibold tracking-tight">
            Seecript<span className="text-muted-foreground"> · 短视频结构借鉴助手</span>
          </span>
          <MainNav />
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
            path="/workshop"
            element={
              <ProjectGuard>
                <ComposePage />
              </ProjectGuard>
            }
          />
          <Route path="/knowledge" element={<KnowledgePage />} />
          {/* 旧路由全部重定向到 /workshop */}
          <Route path="/compose" element={<Navigate to="/workshop" replace />} />
          <Route path="/migrate" element={<Navigate to="/workshop?tab=migrate" replace />} />
          <Route path="/render" element={<Navigate to="/workshop" replace />} />
        </Routes>
      </main>
    </div>
  )
}

function MainNav() {
  const currentProjectId = useProjectsStore((s) => s.currentProjectId)

  return (
    <nav className="flex items-center gap-1 text-sm">
      {NAV_ITEMS.map((item) => {
        const disabled = item.requireProject && !currentProjectId
        return (
          <NavLink
            key={item.to}
            to={item.to}
            end={item.to === '/'}
            onClick={(e) => {
              if (disabled) e.preventDefault()
            }}
            className={({ isActive }) =>
              cn(
                'rounded-md px-3 py-1.5 transition-colors',
                isActive
                  ? 'bg-accent text-accent-foreground'
                  : 'text-muted-foreground hover:bg-secondary hover:text-foreground',
                disabled && 'cursor-not-allowed opacity-40 hover:bg-transparent hover:text-muted-foreground',
              )
            }
            title={disabled ? '先在首页选一个项目再进来' : undefined}
          >
            {item.label}
          </NavLink>
        )
      })}
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
      title={`当前项目 · ${proj.name}`}
    >
      {proj.name}
    </span>
  )
}
