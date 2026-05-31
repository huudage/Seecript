import { api } from '@/api/client'
import { useProjectsStore } from '@/stores/projects'
import type { Project, StepName, StepSnapshot } from '@/types/schemas'

/**
 * 「下一步」= 把当前步产物快照写后端 → 前端 projects store 重新拉一次让 nav 状态更新。
 *
 * 这是工作流唯一的「保存」语义：未点过此函数对应步骤的产物对后端视角等同于草稿。
 * commit 返回最新 Project；这里把它合并回 projects store，保持顶部 nav 状态与后端一致。
 *
 * 失败会抛出 ApiError——调用方应该 try/catch 提示用户并**不要** navigate，
 * 让前端进度不要领先于后端状态机。
 */
export async function commitStep(
  projectId: string,
  step: StepName,
  payload: Record<string, unknown>,
): Promise<Project> {
  const snapshot: StepSnapshot = {
    step,
    saved_at: Date.now() / 1000,
    payload,
  }
  const updated = await api.post<Project>(
    `/project/${projectId}/step/${step}/commit`,
    snapshot,
  )
  // 把最新 Project 合并回 store，让顶部 nav 立刻看到新的 step_states / current_step
  useProjectsStore.setState((state) => ({
    projects: state.projects
      .map((p) => (p.project_id === projectId ? updated : p))
      .sort((a, b) => b.updated_at - a.updated_at),
  }))
  return updated
}

/** 拉某一步的 snapshot（mount 时回填本地 store 用）。无快照返回 null。 */
export async function getStepSnapshot(
  projectId: string,
  step: StepName,
): Promise<StepSnapshot | null> {
  return await api.get<StepSnapshot | null>(`/project/${projectId}/step/${step}`)
}
