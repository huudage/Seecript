import fs from 'node:fs'
import path from 'node:path'
import { defineConfig, type Plugin } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

// 本地 dev 直接从 ../server/samples 出静态文件（与后端 main.py 挂载的是同一目录），
// 不走 8090 代理——本地没有 Python 后端时代理只会 502，样例缩略图/视频全挂。
const SAMPLES_ROOT = path.resolve(__dirname, '../server/samples')

const SAMPLE_MIME: Record<string, string> = {
  '.jpg': 'image/jpeg',
  '.jpeg': 'image/jpeg',
  '.png': 'image/png',
  '.mp4': 'video/mp4',
  '.json': 'application/json',
  '.active': 'text/plain',
}

function serveSamples(): Plugin {
  return {
    name: 'serve-samples',
    configureServer(server) {
      server.middlewares.use((req, res, next) => {
        const raw = req.url ?? ''
        const m = /^\/samples\/([^?]+)/.exec(raw)
        if (!m) return next()
        const rel = decodeURIComponent(m[1])
        const file = path.normalize(path.join(SAMPLES_ROOT, rel))
        if (!file.startsWith(SAMPLES_ROOT + path.sep)) {
          res.statusCode = 403
          res.end()
          return
        }
        fs.stat(file, (err, st) => {
          if (err || !st.isFile()) {
            res.statusCode = 404
            res.end()
            return
          }
          const type = SAMPLE_MIME[path.extname(file).toLowerCase()] ?? 'application/octet-stream'
          const range = /^bytes=(\d*)-(\d*)$/.exec(String(req.headers.range ?? '').trim())
          let start = 0
          let end = st.size - 1
          if (range) {
            if (range[1]) start = Number.parseInt(range[1], 10)
            if (range[2]) end = Number.parseInt(range[2], 10)
            if (!range[1] && range[2]) start = Math.max(0, st.size - Number.parseInt(range[2], 10))
            end = Math.min(end, st.size - 1)
          }
          if (start > end || start >= st.size) {
            res.writeHead(416, { 'Content-Range': `bytes */${st.size}` })
            res.end()
            return
          }
          if (range) {
            res.writeHead(206, {
              'Content-Type': type,
              'Content-Range': `bytes ${start}-${end}/${st.size}`,
              'Content-Length': end - start + 1,
              'Accept-Ranges': 'bytes',
            })
          } else {
            res.writeHead(200, {
              'Content-Type': type,
              'Content-Length': st.size,
              'Accept-Ranges': 'bytes',
            })
          }
          fs.createReadStream(file, { start, end }).pipe(res)
        })
      })
    },
  }
}

// https://vite.dev/config/
export default defineConfig({
  plugins: [serveSamples(), react(), tailwindcss()],
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
    // /samples 由上方 serveSamples() 直出仓库样例目录，不走代理。
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8090',
        changeOrigin: true,
        // SSE 必须保持长连接，关闭 ws upgrade。
        ws: false,
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
