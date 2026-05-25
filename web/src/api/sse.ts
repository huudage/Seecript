/**
 * SSE 进度推送工具。统一封装 EventSource：
 * - 自动加 /api 前缀（与 client.ts 一致，走 vite proxy）。
 * - onProgress / onDone / onError 三类回调。
 * - 返回的 close() 用于组件卸载时收尾。
 *
 * 后端事件契约：
 *   event: progress    data: { step: string, percent: number, payload?: any }
 *   event: done        data: { ...任务结果 }
 *   event: error       data: { detail: string, code?: string }
 */

export interface SSEHandlers<TDone = unknown, TProgress = unknown> {
  onProgress?: (payload: { step: string; percent: number; payload?: TProgress }) => void
  onDone?: (payload: TDone) => void
  onError?: (err: { detail: string; code?: string }) => void
}

export interface SSEHandle {
  close: () => void
}

export function createSSE<TDone = unknown, TProgress = unknown>(
  path: string,
  handlers: SSEHandlers<TDone, TProgress>,
): SSEHandle {
  const source = new EventSource(`/api${path}`)

  source.addEventListener('progress', (ev) => {
    try {
      const data = JSON.parse((ev as MessageEvent).data)
      handlers.onProgress?.(data)
    } catch {
      /* 服务端不该发非 JSON，但兜底防崩 */
    }
  })

  source.addEventListener('done', (ev) => {
    try {
      const data = JSON.parse((ev as MessageEvent).data) as TDone
      handlers.onDone?.(data)
    } catch {
      handlers.onError?.({ detail: 'invalid done payload' })
    } finally {
      source.close()
    }
  })

  source.addEventListener('error', (ev) => {
    let detail = 'SSE connection closed'
    try {
      const data = JSON.parse((ev as MessageEvent).data ?? '{}')
      if (data?.detail) detail = data.detail
      handlers.onError?.({ detail, code: data?.code })
    } catch {
      handlers.onError?.({ detail })
    }
    source.close()
  })

  return {
    close: () => source.close(),
  }
}
