import { create } from 'zustand'
import { persist } from 'zustand/middleware'

import type {
  FillResult,
  Gap,
  Material,
  Plan,
  PlanId,
  SampleId,
  SessionId,
  VideoType,
} from '@/types/schemas'

import { usePlanStore } from './plan'
import { useSessionStore } from './session'

/**
 * 项目状态：草稿（刚选样例）/ 已规划（plan/build 跑过）/ 已渲染（拿到视频）。
 */
export type ProjectStatus = 'draft' | 'planned' | 'rendered'

/**
 * 一个"项目"是用户从素材库挑某个样例后产生的工作上下文快照：
 * - sample_id + video_type 决定结构
 * - session_id + materials + brief 是用户在 Compose 页的输入
 * - plan + gaps + fills 是分析产物
 * - last_video_url / last_cover_url 是最终渲染结果
 *
 * 进入「首页」点"进入"会把该快照灌回 session + plan store。
 */
export interface Project {
  id: string
  name: string
  sample_id: SampleId
  sample_title: string
  video_type: VideoType
  session_id: SessionId | null
  brief: string
  materials: Material[]
  plan: Plan | null
  plan_id: PlanId | null
  gaps: Gap[]
  fills: FillResult[]
  last_video_url: string | null
  last_cover_url: string | null
  status: ProjectStatus
  created_at: number
  updated_at: number
}

interface ProjectsState {
  projects: Project[]
  currentProjectId: string | null

  /** 局部合并；如果 id 不存在则创建。每次调用都更新 updated_at。 */
  upsertProject: (patch: Partial<Project> & { id: string }) => void
  removeProject: (id: string) => void
  setCurrentProject: (id: string | null) => void
  /** 把项目快照灌回 session + plan store，让 Compose / Render 页能从断点续作。 */
  resumeProject: (id: string) => Project | null
  /** 用当前 session + plan store 的状态新建一个项目并返回 id。 */
  createFromCurrent: (init: { sample_id: SampleId; sample_title: string; video_type: VideoType }) => string
}

const STORAGE_KEY = 'seecript:projects'

function newId(): string {
  return `proj-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`
}

export const useProjectsStore = create<ProjectsState>()(
  persist(
    (set, get) => ({
      projects: [],
      currentProjectId: null,

      upsertProject: (patch) =>
        set((state) => {
          const idx = state.projects.findIndex((p) => p.id === patch.id)
          const now = Date.now()
          if (idx < 0) {
            const created: Project = {
              id: patch.id,
              name: patch.name ?? '未命名项目',
              sample_id: patch.sample_id ?? '',
              sample_title: patch.sample_title ?? '',
              video_type: patch.video_type ?? 'marketing',
              session_id: patch.session_id ?? null,
              brief: patch.brief ?? '',
              materials: patch.materials ?? [],
              plan: patch.plan ?? null,
              plan_id: patch.plan_id ?? null,
              gaps: patch.gaps ?? [],
              fills: patch.fills ?? [],
              last_video_url: patch.last_video_url ?? null,
              last_cover_url: patch.last_cover_url ?? null,
              status: patch.status ?? 'draft',
              created_at: now,
              updated_at: now,
            }
            return { projects: [created, ...state.projects] }
          }
          const next = state.projects.slice()
          next[idx] = { ...next[idx], ...patch, updated_at: now }
          return { projects: next }
        }),

      removeProject: (id) =>
        set((state) => ({
          projects: state.projects.filter((p) => p.id !== id),
          currentProjectId: state.currentProjectId === id ? null : state.currentProjectId,
        })),

      setCurrentProject: (id) => set({ currentProjectId: id }),

      resumeProject: (id) => {
        const proj = get().projects.find((p) => p.id === id) ?? null
        if (!proj) return null
        // 灌回 session
        const session = useSessionStore.getState()
        session.selectSample(proj.sample_id, proj.video_type)
        session.setSession(proj.session_id)
        session.setBrief(proj.brief)
        // 重置后批量追加（appendMaterials 会自动补 sort_order）
        useSessionStore.setState({ materials: proj.materials })
        // 灌回 plan
        const planStore = usePlanStore.getState()
        planStore.setPlan(proj.plan)
        planStore.setGaps(proj.gaps)
        // upsertFill 是逐条的；批量直接写 fills
        usePlanStore.setState({ fills: proj.fills, selectedGapId: null })
        set({ currentProjectId: id })
        return proj
      },

      createFromCurrent: ({ sample_id, sample_title, video_type }) => {
        const id = newId()
        const now = Date.now()
        const created: Project = {
          id,
          name: sample_title || '未命名项目',
          sample_id,
          sample_title,
          video_type,
          session_id: null,
          brief: '',
          materials: [],
          plan: null,
          plan_id: null,
          gaps: [],
          fills: [],
          last_video_url: null,
          last_cover_url: null,
          status: 'draft',
          created_at: now,
          updated_at: now,
        }
        set((state) => ({
          projects: [created, ...state.projects],
          currentProjectId: id,
        }))
        return id
      },
    }),
    {
      name: STORAGE_KEY,
      version: 1,
      partialize: (state) => ({
        projects: state.projects,
        currentProjectId: state.currentProjectId,
      }),
    },
  ),
)
