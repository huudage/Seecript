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
  // 状态机:onDone 或 onError 触发后,后续 'error'(浏览器关连接时也会发)直接吞掉,
  // 否则会把"任务成功完成"的 UI 立刻覆盖成"SSE connection closed"假报错。
  let settled = false

  source.addEventListener('progress', (ev) => {
    if (settled) return
    try {
      const data = JSON.parse((ev as MessageEvent).data)
      handlers.onProgress?.(data)
    } catch {
      /* 服务端不该发非 JSON,但兜底防崩 */
    }
  })

  source.addEventListener('done', (ev) => {
    if (settled) return
    settled = true
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
    if (settled) {
      // done 已经收尾,本次 error 是浏览器对关连接的回声,忽略不报。
      source.close()
      return
    }
    settled = true
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
    close: () => {
      settled = true
      source.close()
    },
  }
}
