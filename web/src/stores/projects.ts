import { create } from 'zustand'
import { persist } from 'zustand/middleware'

import { api, ApiError } from '@/api/client'
import type {
  Project,
  ProjectListResponse,
  ProjectUpdateRequest,
  SampleId,
} from '@/types/schemas'

import { usePlanStore } from './plan'
import { useSessionStore } from './session'
import { useEditStore } from './edit'

export type { Project, ProjectStatus } from '@/types/schemas'

/**
 * 项目列表 / 当前项目状态 —— 后端 /api/project 是唯一来源。
 *
 * 设计取舍：
 * - 列表/详情走后端 GET；本地不再 partial 保存项目快照（之前 localStorage 持久化已删）
 * - 仅 currentProjectId 落 localStorage：跨页面/刷新时记得用户停在哪个项目；
 *   真实数据由 refresh()/resumeProject() 拉回
 * - resumeProject(id) 把 project.brief / video_goal / settings 灌回 session store，
 *   然后清空 plan store —— 实际 plan/gaps/fills 在 Compose 页点"智能分析"重新触发
 *   即可（后端 plan_store/gap_store 已落盘，重启不丢；如果用户进项目想直接看
 *   上次结果，需要拉 plan 来恢复，这一步后续按需扩展）
 */
interface ProjectsState {
  projects: Project[]
  currentProjectId: string | null
  loading: boolean
  error: string | null

  /** 从后端拉全量项目列表（覆盖本地 projects）。 */
  refresh: () => Promise<void>
  /** 切换当前项目；传 null 清空。 */
  setCurrentProject: (id: string | null) => void
  /** 新建项目 → 后端落盘 → 自动 setCurrent。 sampleIds 长度必须 1-2。 */
  createProject: (name: string, sampleIds: SampleId[]) => Promise<Project>
  /** 切到该项目并同步进 session 上下文（brief/goal/settings）；清空 plan 残留。 */
  resumeProject: (id: string) => Promise<Project | null>
  /** PATCH 单项目字段。 */
  updateProject: (id: string, patch: ProjectUpdateRequest) => Promise<Project | null>
  /** DELETE 项目（后端级联清盘）+ 本地 projects 同步移除。 */
  deleteProject: (id: string) => Promise<void>
}

const STORAGE_KEY = 'seecript:projects:current'

function applyProjectToSession(proj: Project) {
  // 把项目内已保存的 brief / video_goal / settings 灌回 session store；
  // 不动 selectedSampleIds / videoType（让用户进 Compose 时再选；samples 由 Library/Decompose 驱动）
  const session = useSessionStore.getState()
  // 切项目时必须清掉上一个项目的 manifest / materials；否则 Compose 页会显示别人的素材
  useSessionStore.setState({ manifest: null, materials: [] })
  session.setBrief(proj.brief ?? '')
  session.setVideoGoal(proj.video_goal ?? '')
  if (proj.settings) {
    useSessionStore.setState({ settings: { ...proj.settings } })
  }
  // session_id 与 project_id 等价：后端把 project_id 当 session 路由键
  session.setSession(proj.project_id)
  // 选中样例（视频类型在样例加载后补；标题先用 sample_id 兜底，Library 加载到 LibraryItem 后会刷新）
  session.selectSamples(proj.sample_ids, proj.sample_ids, undefined, 'system')
  // 切项目时清空残留的 plan/gaps/fills + 编辑历史，避免误以为是本项目的产物
  usePlanStore.getState().reset()
  useEditStore.getState().reset()
}

export const useProjectsStore = create<ProjectsState>()(
  persist(
    (set, get) => ({
      projects: [],
      currentProjectId: null,
      loading: false,
      error: null,

      refresh: async () => {
        set({ loading: true, error: null })
        try {
          const resp = await api.get<ProjectListResponse>('/project')
          // 后端默认按 updated_at 倒序；这里也按时间倒序排一遍，前端无需再 sort
          const sorted = resp.items.slice().sort((a, b) => b.updated_at - a.updated_at)
          set({ projects: sorted, loading: false })
        } catch (err) {
          const msg = err instanceof ApiError ? err.message : err instanceof Error ? err.message : '加载项目列表失败'
          set({ loading: false, error: msg })
        }
      },

      setCurrentProject: (id) => set({ currentProjectId: id }),

      createProject: async (name, sampleIds) => {
        set({ error: null })
        const created = await api.post<Project>('/project', { name, sample_ids: sampleIds })
        set((state) => ({
          projects: [created, ...state.projects.filter((p) => p.project_id !== created.project_id)],
          currentProjectId: created.project_id,
        }))
        applyProjectToSession(created)
        return created
      },

      resumeProject: async (id) => {
        set({ error: null })
        try {
          const proj = await api.get<Project>(`/project/${id}`)
          // 把详情合并回列表，刷新 updated_at 排序
          set((state) => {
            const others = state.projects.filter((p) => p.project_id !== proj.project_id)
            return {
              projects: [proj, ...others].sort((a, b) => b.updated_at - a.updated_at),
              currentProjectId: proj.project_id,
            }
          })
          applyProjectToSession(proj)
          return proj
        } catch (err) {
          if (err instanceof ApiError && err.status === 404) {
            // 后端没了 → 本地清掉
            set((state) => ({
              projects: state.projects.filter((p) => p.project_id !== id),
              currentProjectId: state.currentProjectId === id ? null : state.currentProjectId,
              error: '项目不存在（可能已删除）',
            }))
            return null
          }
          const msg = err instanceof Error ? err.message : '加载项目失败'
          set({ error: msg })
          return null
        }
      },

      updateProject: async (id, patch) => {
        try {
          const updated = await api.patch<Project>(`/project/${id}`, patch)
          set((state) => ({
            projects: state.projects
              .map((p) => (p.project_id === id ? updated : p))
              .sort((a, b) => b.updated_at - a.updated_at),
          }))
          return updated
        } catch (err) {
          const msg = err instanceof Error ? err.message : '更新失败'
          set({ error: msg })
          return null
        }
      },

      deleteProject: async (id) => {
        try {
          await api.delete(`/project/${id}`)
        } catch (err) {
          // 404 视为已不存在；其它错误暴露给 UI
          if (!(err instanceof ApiError && err.status === 404)) {
            set({ error: err instanceof Error ? err.message : '删除失败' })
            throw err
          }
        }
        set((state) => ({
          projects: state.projects.filter((p) => p.project_id !== id),
          currentProjectId: state.currentProjectId === id ? null : state.currentProjectId,
        }))
        // 当前项目被删 → 清 session/plan 残留
        if (get().currentProjectId === null) {
          useSessionStore.getState().reset()
          usePlanStore.getState().reset()
        }
      },
    }),
    {
      name: STORAGE_KEY,
      version: 2,
      partialize: (state) => ({ currentProjectId: state.currentProjectId }),
    },
  ),
)
