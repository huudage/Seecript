import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'
import './index.css'
import App from './App.tsx'

// 演示模式（?demo=1）：无后端时用仓库样例素材撑起画布全链路（见 src/dev/demo.ts）。
// 必须在首渲染前同步 seed（否则 Compose 的 ?step=2 降级 effect 会先抢跑）；
// 动态 import 挂在 import.meta.env.DEV 门下，生产构建整条链路被 tree-shake。
async function bootstrap() {
  if (import.meta.env.DEV && new URLSearchParams(location.search).has('demo')) {
    const { installDemo } = await import('./dev/demo')
    installDemo()
  }
  createRoot(document.getElementById('root')!).render(
    <StrictMode>
      <BrowserRouter>
        <App />
      </BrowserRouter>
    </StrictMode>,
  )
}

void bootstrap()
