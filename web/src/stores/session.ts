import { create } from 'zustand'

import type { SampleId, SampleManifest } from '@/types/schemas'

/**
 * 当前会话级状态：选中样例 / 拆解 manifest / 上传素材列表。
 * 阶段 1 仅落 selectedSampleId + manifest 的占位；详细字段随 #8 后端契约补全。
 */
interface SessionState {
  selectedSampleId: SampleId | null
  manifest: SampleManifest | null

  selectSample: (id: SampleId | null) => void
  setManifest: (manifest: SampleManifest | null) => void
  reset: () => void
}

export const useSessionStore = create<SessionState>((set) => ({
  selectedSampleId: null,
  manifest: null,

  selectSample: (id) => set({ selectedSampleId: id }),
  setManifest: (manifest) => set({ manifest }),
  reset: () => set({ selectedSampleId: null, manifest: null }),
}))
