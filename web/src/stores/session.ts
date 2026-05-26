import { create } from 'zustand'

import type {
  Material,
  SampleId,
  SampleManifest,
  SessionId,
  VideoType,
} from '@/types/schemas'

/**
 * 当前会话级状态。
 * - selectedSampleId / videoType / manifest：素材库 → 拆解 的产物
 * - sessionId / materials：上传素材后由后端分配；plan 构建时透传
 */
interface SessionState {
  selectedSampleId: SampleId | null
  videoType: VideoType
  manifest: SampleManifest | null
  sessionId: SessionId | null
  materials: Material[]

  selectSample: (id: SampleId | null, videoType?: VideoType) => void
  setVideoType: (videoType: VideoType) => void
  setManifest: (manifest: SampleManifest | null) => void
  setSession: (sessionId: SessionId | null) => void
  appendMaterials: (items: Material[]) => void
  removeMaterial: (materialId: string) => void
  reset: () => void
}

export const useSessionStore = create<SessionState>((set) => ({
  selectedSampleId: null,
  videoType: 'marketing',
  manifest: null,
  sessionId: null,
  materials: [],

  selectSample: (id, videoType) =>
    set((state) => ({
      selectedSampleId: id,
      videoType: videoType ?? state.videoType,
    })),
  setVideoType: (videoType) => set({ videoType }),
  setManifest: (manifest) => set({ manifest }),
  setSession: (sessionId) => set({ sessionId }),
  appendMaterials: (items) =>
    set((state) => ({
      materials: [...state.materials, ...items],
    })),
  removeMaterial: (materialId) =>
    set((state) => ({ materials: state.materials.filter((m) => m.material_id !== materialId) })),
  reset: () =>
    set({
      selectedSampleId: null,
      videoType: 'marketing',
      manifest: null,
      sessionId: null,
      materials: [],
    }),
}))
