import { useEffect } from 'react'
import { NavLink, Navigate, Route, Routes } from 'react-router-dom'
import { useLocation } from 'react-router-dom'
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
 * - 我的项目：项目列表
 * - 素材与灵感：热门样例 + 上传素材
 * - 分析热门结构：拆出热门视频结构
 * - 创作工作台：写主题 → 配素材 → 对照结构 → 出片
 *
 * 旧路由 /compose、/migrate 全部重定向到 /workshop。
 * 项目工作流的 step_states 仍由后端跟踪,但不再决定导航可见性 —— 用户自己点。
 */
const NAV_ITEMS: { to: string; label: string; requireProject: boolean; external?: boolean }[] = [
  { to: '/intro.html', label: '首页', requireProject: false, external: true },
  { to: '/', label: '我的项目', requireProject: false },
  { to: '/library', label: '素材与灵感', requireProject: false },
  { to: '/decompose', label: '分析热门结构', requireProject: true },
  { to: '/workshop', label: '创作工作台', requireProject: true },
  { to: '/knowledge', label: '我的创作偏好', requireProject: false },
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
  const location = useLocation()
  useEffect(() => {
    void refreshProjects()
  }, [refreshProjects])

  return (
    <div className="flex min-h-screen flex-col bg-background text-foreground">
      <header className="border-b border-border bg-card/80 backdrop-blur-xl">
        <div className="mx-auto flex max-w-screen-2xl items-center gap-6 px-6 py-3">
          <span className="font-semibold tracking-tight select-none">
            <span className="bg-gradient-to-r from-cyan-400 to-violet-500 bg-clip-text text-transparent">Seecript</span>
            <span className="text-muted-foreground"> · AI 视频创作助手</span>
          </span>
          <MainNav />
          <CurrentProjectBadge />
        </div>
      </header>

      <main className="flex-1" key={location.pathname}>
        <div className="animate-fade-up">
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
        </div>
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
        if (item.external) {
          return (
            <a
              key={item.to}
              href={item.to}
              className="relative rounded-md px-3 py-1.5 text-sm text-muted-foreground transition-all duration-200 hover:text-foreground"
            >
              {item.label}
            </a>
          )
        }
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
                'relative rounded-md px-3 py-1.5 transition-all duration-200',
                isActive
                  ? 'text-foreground font-medium after:absolute after:bottom-0 after:left-1/2 after:-translate-x-1/2 after:h-0.5 after:w-4 after:rounded-full after:bg-primary after:shadow-[0_0_8px_var(--color-primary)]'
                  : 'text-muted-foreground hover:text-foreground',
                disabled && 'cursor-not-allowed opacity-40 hover:text-muted-foreground',
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
