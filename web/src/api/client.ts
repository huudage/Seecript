/**
 * 统一 HTTP 客户端。后端走 /api 前缀（vite proxy 转 8090）。
 *
 * 设计取舍：
 * - 不引 axios / ky：fetch + 一个薄封装够用，省一个 dep。
 * - 错误统一抛 ApiError；UI 侧用 try/catch 渲染 toast。
 * - SSE 单独走 createSSE（见 ./sse.ts），不复用 request()。
 */

export class ApiError extends Error {
  status: number
  code?: string
  trace_id?: string
  payload?: unknown

  constructor(
    message: string,
    opts: { status: number; code?: string; trace_id?: string; payload?: unknown },
  ) {
    super(message)
    this.name = 'ApiError'
    this.status = opts.status
    this.code = opts.code
    this.trace_id = opts.trace_id
    this.payload = opts.payload
  }
}

interface RequestOptions extends Omit<RequestInit, 'body'> {
  body?: unknown
  signal?: AbortSignal
}

async function request<T>(path: string, opts: RequestOptions = {}): Promise<T> {
  const { body, headers, ...rest } = opts
  const isFormData = body instanceof FormData
  const init: RequestInit = {
    ...rest,
    headers: {
      ...(isFormData ? {} : { 'Content-Type': 'application/json' }),
      ...headers,
    },
    body: body == null ? undefined : isFormData ? body : JSON.stringify(body),
  }

  const res = await fetch(`/api${path}`, init)
  const trace_id = res.headers.get('X-Trace-Id') ?? undefined

  if (!res.ok) {
    let detail: string = res.statusText
    let code: string | undefined
    let payload: unknown
    try {
      payload = await res.json()
      const d = payload as { detail?: string; code?: string }
      if (d?.detail) detail = d.detail
      if (d?.code) code = d.code
    } catch {
      /* body 不是 JSON，沿用 statusText */
    }
    throw new ApiError(detail, { status: res.status, code, trace_id, payload })
  }

  // 204 / 空 body
  if (res.status === 204) return undefined as T
  const ct = res.headers.get('Content-Type') ?? ''
  if (ct.includes('application/json')) return (await res.json()) as T
  return (await res.text()) as unknown as T
}

export const api = {
  get: <T>(path: string, opts?: RequestOptions) => request<T>(path, { ...opts, method: 'GET' }),
  post: <T>(path: string, body?: unknown, opts?: RequestOptions) =>
    request<T>(path, { ...opts, method: 'POST', body }),
  put: <T>(path: string, body?: unknown, opts?: RequestOptions) =>
    request<T>(path, { ...opts, method: 'PUT', body }),
  patch: <T>(path: string, body?: unknown, opts?: RequestOptions) =>
    request<T>(path, { ...opts, method: 'PATCH', body }),
  delete: <T>(path: string, opts?: RequestOptions) =>
    request<T>(path, { ...opts, method: 'DELETE' }),
}
