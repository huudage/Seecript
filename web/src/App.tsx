import { NavLink, Route, Routes } from 'react-router-dom'
import { cn } from '@/lib/utils'

import HomePage from '@/pages/Home'
import LibraryPage from '@/pages/Library'
import DecomposePage from '@/pages/Decompose'
import ComposePage from '@/pages/Compose'
import MigratePage from '@/pages/Migrate'
import RenderPage from '@/pages/Render'

const navItems = [
  { to: '/', label: '首页', end: true },
  { to: '/library', label: '素材库', end: false },
  { to: '/decompose', label: '样例拆解', end: false },
  { to: '/compose', label: '新素材 / 缺口', end: false },
  { to: '/migrate', label: '迁移映射', end: false },
  { to: '/render', label: '生成 / 编辑', end: false },
] as const

export default function App() {
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
        </div>
      </header>

      <main className="flex-1">
        <Routes>
          <Route index element={<HomePage />} />
          <Route path="/library" element={<LibraryPage />} />
          <Route path="/decompose" element={<DecomposePage />} />
          <Route path="/compose" element={<ComposePage />} />
          <Route path="/migrate" element={<MigratePage />} />
          <Route path="/render" element={<RenderPage />} />
        </Routes>
      </main>
    </div>
  )
}
