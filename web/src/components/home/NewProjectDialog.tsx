import { useEffect, useMemo, useRef, useState } from 'react'

import { VIDEO_TYPE_HINT, VIDEO_TYPE_LABEL } from '@/lib/sections'
import { cn } from '@/lib/utils'
import { useProjectsStore } from '@/stores/projects'
import type { VideoType } from '@/types/schemas'

/**
 * 新建项目向导（单步）：
 *   选视频类型 + 起项目名 → POST /api/project（reference_versions 留空）
 *
 * 样例选择从这里下沉到「样例拆解」页：建好项目后进 Decompose，从系统库按 video_type 过滤后
 * 挑一个样例，跑拆解时 commit references 回写。
 */

const VIDEO_TYPES: VideoType[] = ['marketing', 'editing', 'motion_graph']

function defaultName(vt: VideoType | null): string {
  if (!vt) return ''
  const now = new Date()
  const pad = (n: number) => String(n).padStart(2, '0')
  const stamp = `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())} ${pad(now.getHours())}:${pad(now.getMinutes())}`
  return `${VIDEO_TYPE_LABEL[vt]}项目 · ${stamp}`
}

export function NewProjectDialog({
  onClose,
  onCreated,
}: {
  onClose: () => void
  onCreated: (projectId: string) => void
}) {
  const createProject = useProjectsStore((s) => s.createProject)

  const [videoType, setVideoType] = useState<VideoType | null>(null)
  const [name, setName] = useState('')
  const userTouchedName = useRef(false)

  const [submitting, setSubmitting] = useState(false)
  const [submitError, setSubmitError] = useState<string | null>(null)

  // 切类型时自动同步默认项目名（除非用户改过）
  useEffect(() => {
    if (videoType && !userTouchedName.current) {
      setName(defaultName(videoType))
    }
  }, [videoType])

  const canSubmit = useMemo(
    () => !!videoType && !!name.trim() && !submitting,
    [videoType, name, submitting],
  )

  const onConfirm = async () => {
    setSubmitError(null)
    if (!videoType) {
      setSubmitError('请选择视频种类')
      return
    }
    const finalName = name.trim()
    if (!finalName) {
      setSubmitError('请输入项目名')
      return
    }
    setSubmitting(true)
    try {
      const created = await createProject(finalName, [], videoType)
      onCreated(created.project_id)
    } catch (err) {
      setSubmitError(err instanceof Error ? err.message : '创建失败')
    } finally {
      setSubmitting(false)
    }
  }

  // ESC 关闭
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && !submitting) onClose()
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [onClose, submitting])

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 px-4 py-8"
      onClick={() => {
        if (!submitting) onClose()
      }}
    >
      <div
        className="relative flex w-full max-w-2xl flex-col overflow-hidden rounded-lg border border-border bg-card shadow-xl"
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header */}
        <header className="flex items-center justify-between border-b border-border px-5 py-3">
          <div>
            <h3 className="text-sm font-semibold">新建项目</h3>
            <p className="text-xs text-muted-foreground">
              选个种类与名字就行，样例在「样例拆解」页里挑
            </p>
          </div>
          <button
            type="button"
            onClick={onClose}
            disabled={submitting}
            aria-label="关闭"
            className="ml-3 flex h-8 w-8 items-center justify-center rounded-md text-muted-foreground hover:bg-secondary hover:text-foreground disabled:opacity-50"
          >
            <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} className="h-4 w-4">
              <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
            </svg>
          </button>
        </header>

        {/* Body */}
        <div className="flex-1 space-y-5 overflow-y-auto px-5 py-4">
          {/* 视频种类卡片网格 */}
          <div className="space-y-2">
            <label className="text-xs font-semibold">视频种类</label>
            <div className="grid gap-3 sm:grid-cols-3">
              {VIDEO_TYPES.map((vt) => {
                const selected = videoType === vt
                return (
                  <button
                    key={vt}
                    type="button"
                    onClick={() => setVideoType(vt)}
                    className={cn(
                      'flex flex-col gap-2 rounded-lg border p-4 text-left transition-all',
                      selected
                        ? 'border-primary bg-primary/10 ring-1 ring-primary/40'
                        : 'border-border bg-card hover:border-primary/50 hover:bg-primary/5',
                    )}
                  >
                    <div className="text-sm font-semibold">{VIDEO_TYPE_LABEL[vt]}</div>
                    <div className="text-xs leading-snug text-muted-foreground">
                      {VIDEO_TYPE_HINT[vt]}
                    </div>
                  </button>
                )
              })}
            </div>
          </div>

          {/* 项目名 */}
          <div className="space-y-1.5">
            <label className="text-xs font-semibold">项目名</label>
            <input
              type="text"
              value={name}
              onChange={(e) => {
                userTouchedName.current = true
                setName(e.target.value.slice(0, 80))
              }}
              placeholder="例如：博物馆冬令营推广"
              className="w-full rounded-md border border-border bg-background px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-primary"
            />
            <p className="text-xs text-muted-foreground">
              最长 80 字。选种类后会自动给个默认名，按需修改即可。
            </p>
          </div>
        </div>

        {/* Footer */}
        <footer className="flex items-center justify-between gap-3 border-t border-border bg-card/50 px-5 py-3">
          <div className="flex-1">
            {submitError && <p className="text-xs text-destructive">{submitError}</p>}
            {!submitError && (
              <p className="text-xs text-muted-foreground">
                创建后进入「样例拆解」页选样例，再进「视频工坊」生成内容轨
              </p>
            )}
          </div>
          <div className="flex gap-2">
            <button
              type="button"
              onClick={onClose}
              disabled={submitting}
              className="rounded-md border border-border bg-background px-4 py-1.5 text-sm hover:bg-secondary disabled:opacity-50"
            >
              取消
            </button>
            <button
              type="button"
              onClick={onConfirm}
              disabled={!canSubmit}
              className={cn(
                'rounded-md bg-primary px-4 py-1.5 text-sm font-medium text-primary-foreground hover:bg-primary/90',
                !canSubmit && 'cursor-not-allowed opacity-60',
              )}
            >
              {submitting ? '创建中…' : '创建并进入'}
            </button>
          </div>
        </footer>
      </div>
    </div>
  )
}
