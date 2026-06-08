import { useEffect, useRef } from 'react'

import { api, ApiError } from '@/api/client'
import { useSessionStore } from '@/stores/session'
import type { Material, SessionId } from '@/types/schemas'

const POLL_INTERVAL_MS = 3000

/**
 * 视频预处理轮询：对所有 preprocess_status ∈ {pending, running} 的素材，
 * 每 3s 拉一次 GET /material/{id}/preprocess?project_id=...，写回 store。
 *
 * 设计：
 * - 同一进程内单实例足够（视频量通常 < 20）；不做 per-id channel
 * - 状态稳定（ready/failed/skipped）后停止该 id 的轮询
 * - 组件卸载或 sessionId 变化时清空所有 timer
 * - 网络/4xx 错误：log warn，不阻断其它 id 的轮询；下一轮继续重试
 */
export function useMaterialPreprocessPolling() {
  const sessionId = useSessionStore((s) => s.sessionId)
  const materials = useSessionStore((s) => s.materials)
  const updateMaterial = useSessionStore((s) => s.updateMaterial)

  // 用 ref 持有 timers，避免每次 materials 变化重建所有 interval
  const timersRef = useRef<Map<string, ReturnType<typeof setInterval>>>(new Map())

  useEffect(() => {
    if (!sessionId) {
      // 没 session：清掉所有
      for (const t of timersRef.current.values()) clearInterval(t)
      timersRef.current.clear()
      return
    }

    const activeIds = new Set(
      materials
        .filter((m) => m.preprocess_status === 'pending' || m.preprocess_status === 'running')
        .map((m) => m.material_id),
    )

    // 1) 清理已不再活跃（变 ready/failed/skipped 或被删除）的 timer
    for (const [id, timer] of timersRef.current.entries()) {
      if (!activeIds.has(id)) {
        clearInterval(timer)
        timersRef.current.delete(id)
      }
    }

    // 2) 给新增的活跃 id 起 timer
    for (const id of activeIds) {
      if (timersRef.current.has(id)) continue
      const timer = setInterval(() => {
        void pollOne(sessionId, id, updateMaterial, () => {
          const t = timersRef.current.get(id)
          if (t) {
            clearInterval(t)
            timersRef.current.delete(id)
          }
        })
      }, POLL_INTERVAL_MS)
      timersRef.current.set(id, timer)
    }
  }, [sessionId, materials, updateMaterial])

  // 卸载兜底
  useEffect(() => {
    return () => {
      for (const t of timersRef.current.values()) clearInterval(t)
      timersRef.current.clear()
    }
  }, [])
}

async function pollOne(
  sessionId: SessionId,
  materialId: string,
  updateMaterial: (id: string, patch: Partial<Material>) => void,
  stopSelf: () => void,
) {
  try {
    const m = await api.get<Material>(
      `/material/${encodeURIComponent(materialId)}/preprocess?project_id=${encodeURIComponent(sessionId)}`,
    )
    updateMaterial(materialId, m)
    if (
      m.preprocess_status === 'ready'
      || m.preprocess_status === 'failed'
      || m.preprocess_status === 'skipped'
    ) {
      stopSelf()
    }
  } catch (exc) {
    if (exc instanceof ApiError && exc.status === 404) {
      // 素材不在 store（被删？项目切了？）— 停掉自身
      stopSelf()
      return
    }
    // 其它错误：保持 timer，下一轮再试
    // eslint-disable-next-line no-console
    console.warn('[preprocess] poll', materialId, exc)
  }
}
