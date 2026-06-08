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
  // assetsDir = 'static' 是为了避开和后端 /assets/ 静态挂载（用户素材库 BGM/参考图）
  // 同名路径的冲突。生产 nginx 用 /static/ 长缓存前端 bundle，/assets/ 代理给后端。
  build: {
    assetsDir: 'static',
  },
  server: {
    port: 5173,
    // 后端：./run.ps1 默认 127.0.0.1:8090，所有 /api 与 SSE 走代理。
    // 5 个后端 StaticFiles 挂载点（main.py:166-184）全部需要代理给后端，
    // 否则本地 dev 模式下视频/音频/缩略图会 404。
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
      '/assets': {
        target: 'http://127.0.0.1:8090',
        changeOrigin: true,
      },
      '/voiceovers': {
        target: 'http://127.0.0.1:8090',
        changeOrigin: true,
      },
      '/aigc-videos': {
        target: 'http://127.0.0.1:8090',
        changeOrigin: true,
      },
      '/aigc-images': {
        target: 'http://127.0.0.1:8090',
        changeOrigin: true,
      },
    },
  },
})
