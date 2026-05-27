import path from 'node:path'
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
  server: {
    port: 5173,
    // 后端：./run.ps1 默认 127.0.0.1:8090，所有 /api 与 SSE 走代理。
    // /samples 与 /outputs 是后端 StaticFiles 挂的目录（样例视频、渲染产物），同样要代理给后端。
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8090',
        changeOrigin: true,
        // SSE 必须保持长连接，关闭 ws upgrade。
        ws: false,
      },
      '/samples': {
        target: 'http://127.0.0.1:8090',
        changeOrigin: true,
      },
      '/uploads': {
        target: 'http://127.0.0.1:8090',
        changeOrigin: true,
      },
      '/outputs': {
        target: 'http://127.0.0.1:8090',
        changeOrigin: true,
      },
    },
  },
})
