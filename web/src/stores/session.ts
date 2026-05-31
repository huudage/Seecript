import { create } from 'zustand'

import {
  DEFAULT_COMPOSE_SETTINGS,
  type ComposeSettings,
  type Material,
  type SampleId,
  type SampleManifest,
  type SessionId,
  type VideoType,
} from '@/types/schemas'

/**
 * 当前会话级状态。
 * - selectedSampleIds / videoType / manifest：素材库 → 拆解 的产物
 *   selectedSampleIds 是 1-2 个样例 ID 的有序数组（A/B 位）；多样例时后端合并段落参考池
 * - selectedSampleTitles：与 selectedSampleIds 一一对应的样例标题，仅 UI 展示用
 * - sessionId / materials：上传素材后由后端分配；plan 构建时透传
 * - brief：Compose 页用户输入的主题/卖点；plan/build 时透传给后端
 * - videoGoal：Compose 页用户输入的视频要求与目的；plan/build 时透传给后端驱动结构改编
 * - settings：Compose 页用户配置（目标总时长 / 平台 / 调性 / CTA / 关键词），全部带默认值
 */
interface SessionState {
  selectedSampleIds: SampleId[]
  selectedSampleTitles: string[]
  /** 当前样例来源：system=从内置素材库挑的（video_type 锁定），user=用户自传的视频（待选 type）。多样例时取首个的来源。 */
  sampleSource: 'system' | 'user' | null
  videoType: VideoType
  manifest: SampleManifest | null
  sessionId: SessionId | null
  materials: Material[]
  brief: string
  videoGoal: string
  settings: ComposeSettings

  /**
   * 选样例：ids 长度 0-2。
   * - 长度 0 等价于清空（manifest 同时置 null）
   * - 长度 1-2 会更新 sampleSource，videoType 用首个样例的（如传入）
   * - titles 与 ids 一一对应，纯展示
   */
  selectSamples: (
    ids: SampleId[],
    titles?: string[],
    videoType?: VideoType,
    source?: 'system' | 'user',
  ) => void
  setVideoType: (videoType: VideoType) => void
  setManifest: (manifest: SampleManifest | null) => void
  setSession: (sessionId: SessionId | null) => void
  appendMaterials: (items: Material[]) => void
  removeMaterial: (materialId: string) => void
  /** 拖拽完成后传新的 material_id 顺序，store 内更新每条的 sort_order。 */
  reorderMaterials: (orderedIds: string[]) => void
  setBrief: (brief: string) => void
  setVideoGoal: (videoGoal: string) => void
  setSettings: (patch: Partial<ComposeSettings>) => void
  reset: () => void
}

export const useSessionStore = create<SessionState>((set) => ({
  selectedSampleIds: [],
  selectedSampleTitles: [],
  sampleSource: null,
  videoType: 'marketing',
  manifest: null,
  sessionId: null,
  materials: [],
  brief: '',
  videoGoal: '',
  settings: { ...DEFAULT_COMPOSE_SETTINGS },

  selectSamples: (ids, titles, videoType, source) =>
    set((state) => {
      const sameHead = ids.length > 0 && state.selectedSampleIds[0] === ids[0]
      return {
        selectedSampleIds: ids.slice(0, 2),
        selectedSampleTitles: (titles ?? []).slice(0, 2),
        sampleSource: ids.length > 0 ? (source ?? state.sampleSource ?? 'system') : null,
        videoType: videoType ?? state.videoType,
        // 主样例（A 位）变化时丢掉 manifest 缓存（manifest 始终是 A 位的）
        manifest: sameHead ? state.manifest : null,
      }
    }),
  setVideoType: (videoType) => set({ videoType }),
  setManifest: (manifest) => set({ manifest }),
  setSession: (sessionId) => set({ sessionId }),
  appendMaterials: (items) =>
    set((state) => {
      const baseOrder = state.materials.length
      const withOrder = items.map((m, i) => ({
        ...m,
        sort_order: m.sort_order ?? baseOrder + i,
      }))
      return { materials: [...state.materials, ...withOrder] }
    }),
  removeMaterial: (materialId) =>
    set((state) => ({
      materials: state.materials
        .filter((m) => m.material_id !== materialId)
        .map((m, i) => ({ ...m, sort_order: i })),
    })),
  reorderMaterials: (orderedIds) =>
    set((state) => {
      const idx = new Map(orderedIds.map((id, i) => [id, i]))
      const next = state.materials
        .slice()
        .sort((a, b) => (idx.get(a.material_id) ?? 0) - (idx.get(b.material_id) ?? 0))
        .map((m, i) => ({ ...m, sort_order: i }))
      return { materials: next }
    }),
  setBrief: (brief) => set({ brief }),
  setVideoGoal: (videoGoal) => set({ videoGoal }),
  setSettings: (patch) =>
    set((state) => ({ settings: { ...state.settings, ...patch } })),
  reset: () =>
    set({
      selectedSampleIds: [],
      selectedSampleTitles: [],
      sampleSource: null,
      videoType: 'marketing',
      manifest: null,
      sessionId: null,
      materials: [],
      brief: '',
      videoGoal: '',
      settings: { ...DEFAULT_COMPOSE_SETTINGS },
    }),
}))
