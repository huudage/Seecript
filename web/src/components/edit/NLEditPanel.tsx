/**
 * 自然语言编辑 · 三轨 tab 版。
 *
 * - 顶 3 个 tab：内容轨 / 包装轨 / 字幕 & 口播轨
 * - lockedTracks 包含 'main' 时（Render 页），内容轨 tab 禁用 + 提示语
 * - 每个 tab 对应不同的 placeholder + LLM tools 子集（后端按 req.track 分流）
 * - 提交完成回调 onApplied(newPlan)，由父组件 push 到 undo 栈
 *
 * Marks（可选区段）UI 内置；parent 通过 selectedSceneId 触发预填，
 * setMarksHint 自动算出 start/end 写入本组件本地 state。
 */
import { useCallback, useEffect, useMemo, useState } from 'react'

import { api } from '@/api/client'
import { cn } from '@/lib/utils'
import type { EditApplyRequest, EditMark, Plan, Scene } from '@/types/schemas'

export type EditTrack = 'main' | 'packaging' | 'voice'

interface Props {
  plan: Plan
  /** 当前所在步骤；'render' 时若 lockedTracks 含 'main'，主轨 tab 禁用并显示锁定提示 */
  projectStep?: 'library' | 'decompose' | 'compose' | 'render'
  /** 强制禁用的轨道（视觉禁用 + 后端 409 双保险）。常用：Render 页传 ['main'] */
  lockedTracks?: EditTrack[]
  onApplied: (plan: Plan) => void
  /** 父组件传入"用户在主轨点中了哪个 scene"，本组件自动预填 marks */
  selectedSceneId?: string | null
  /** 默认 tab；不传时按页面：compose 默认 main，render 默认 packaging */
  defaultTrack?: EditTrack
}

const TRACK_META: Record<EditTrack, { label: string; placeholder: string; hint: string }> = {
  main: {
    label: '镜头',
    placeholder:
      '例：把第 1 段缩短到 3 秒；第 2 段换成另一个素材；第 3 段加个溶解转场',
    hint: '只改镜头本身：时长 / 用的素材 / 转场。不动字幕、口播、背景音乐。',
  },
  packaging: {
    label: '包装',
    placeholder: '例：把第 1 条字幕改成「限时 5 折」；背景音乐音量调到 0.3',
    hint: '只改包装层：字幕 / 标题 / 贴纸文字 + 背景音乐音量。',
  },
  voice: {
    label: '字幕 / 口播',
    placeholder: '例：第 1 段字幕改得更口语化一点；第 2 段改成「现在下单立减 99」',
    hint: '只改字幕文案。字幕轨立刻刷新；如果开启了口播，配音也会自动重合成。',
  },
}

const TRACKS: EditTrack[] = ['main', 'packaging', 'voice']

export function NLEditPanel({
  plan,
  projectStep,
  lockedTracks = [],
  onApplied,
  selectedSceneId,
  defaultTrack,
}: Props) {
  const lockedSet = useMemo(() => new Set(lockedTracks), [lockedTracks])
  const initialTrack: EditTrack = useMemo(() => {
    if (defaultTrack && !lockedSet.has(defaultTrack)) return defaultTrack
    // Render 页（main 锁住）默认 packaging；其他默认 main
    if (lockedSet.has('main')) return 'packaging'
    return 'main'
  }, [defaultTrack, lockedSet])

  const [activeTrack, setActiveTrack] = useState<EditTrack>(initialTrack)
  const [instruction, setInstruction] = useState('')
  const [applying, setApplying] = useState(false)
  const [editError, setEditError] = useState<string | null>(null)
  const [markStart, setMarkStart] = useState('')
  const [markEnd, setMarkEnd] = useState('')

  // 选中 scene 变化时预填 marks（仅主轨/口播轨语义下有用，但保留行为统一）
  useEffect(() => {
    if (!selectedSceneId) return
    const scene: Scene | undefined = plan.main_track.find((s) => s.scene_id === selectedSceneId)
    if (!scene) return
    setMarkStart(scene.start.toFixed(1))
    setMarkEnd((scene.start + scene.duration).toFixed(1))
  }, [plan.main_track, selectedSceneId])

  const renderLocked = projectStep === 'render' && lockedSet.has('main') && activeTrack === 'main'

  const handleSubmit = useCallback(async () => {
    if (!instruction.trim()) return
    if (lockedSet.has(activeTrack)) {
      setEditError(`「${TRACK_META[activeTrack].label}」当前步骤改不了。`)
      return
    }
    setApplying(true)
    setEditError(null)
    try {
      const marks: EditMark[] = []
      const s = parseFloat(markStart)
      const e = parseFloat(markEnd)
      if (!Number.isNaN(s) && !Number.isNaN(e) && e > s) {
        // EditMark.track 仅支持 main/packaging（schemas）；voice 编辑也归到 main 区段定位
        const markTrack: EditMark['track'] = activeTrack === 'packaging' ? 'packaging' : 'main'
        marks.push({ track: markTrack, start: s, end: e, target_id: selectedSceneId ?? null })
      }
      const body: EditApplyRequest = {
        plan_id: plan.plan_id,
        track: activeTrack,
        instruction: instruction.trim(),
        marks,
      }
      const newPlan = await api.post<Plan>('/edit/apply', body)
      onApplied(newPlan)
      setInstruction('')
    } catch (err) {
      setEditError(err instanceof Error ? err.message : '编辑失败')
    } finally {
      setApplying(false)
    }
  }, [activeTrack, instruction, lockedSet, markEnd, markStart, onApplied, plan.plan_id, selectedSceneId])

  return (
    <section className="rounded-lg border border-border bg-card p-4">
      <div className="mb-3 flex items-center gap-2">
        <h2 className="text-sm font-semibold">用一句话改这段</h2>
        <span className="text-xs text-muted-foreground">AI 会按你说的话改对应内容</span>
      </div>

      {/* tabs */}
      <div className="mb-3 flex gap-1 rounded-md border border-border bg-background p-1">
        {TRACKS.map((t) => {
          const locked = lockedSet.has(t)
          const active = t === activeTrack
          return (
            <button
              key={t}
              type="button"
              disabled={locked}
              onClick={() => !locked && setActiveTrack(t)}
              className={cn(
                'flex-1 rounded-md px-3 py-1.5 text-xs transition-colors',
                active
                  ? 'bg-primary text-primary-foreground'
                  : locked
                  ? 'cursor-not-allowed text-muted-foreground opacity-50'
                  : 'hover:bg-secondary',
              )}
              title={locked ? '当前步骤无法改这个' : TRACK_META[t].hint}
            >
              {TRACK_META[t].label}
              {locked && <span className="ml-1">🔒</span>}
            </button>
          )
        })}
      </div>

      {renderLocked && (
        <div className="mb-3 rounded-md border border-amber-400/40 bg-amber-50 px-3 py-2 text-xs text-amber-900 dark:bg-amber-950/30 dark:text-amber-200">
          已进入出片流程，镜头改动请回上一步；当前可改：包装 / 字幕 & 口播。
        </div>
      )}

      <div className="grid grid-cols-1 gap-3 lg:grid-cols-[1fr_240px]">
        <div>
          <textarea
            value={instruction}
            onChange={(e) => setInstruction(e.target.value)}
            placeholder={TRACK_META[activeTrack].placeholder}
            rows={3}
            disabled={lockedSet.has(activeTrack)}
            className={cn(
              'w-full rounded-md border border-border bg-background px-3 py-2 text-sm placeholder:text-muted-foreground focus:outline-none focus:ring-1 focus:ring-primary',
              lockedSet.has(activeTrack) && 'cursor-not-allowed opacity-50',
            )}
          />
          <p className="mt-1 text-xs text-muted-foreground">{TRACK_META[activeTrack].hint}</p>
        </div>
        <div className="space-y-2 text-xs">
          <div>
            <label className="text-muted-foreground">作用区间（可选）</label>
            <div className="mt-1 flex gap-2">
              <input
                type="number"
                placeholder="起"
                value={markStart}
                onChange={(e) => setMarkStart(e.target.value)}
                className="w-20 rounded-md border border-border bg-background px-2 py-1"
              />
              <span className="self-center text-muted-foreground">–</span>
              <input
                type="number"
                placeholder="止"
                value={markEnd}
                onChange={(e) => setMarkEnd(e.target.value)}
                className="w-20 rounded-md border border-border bg-background px-2 py-1"
              />
              <span className="self-center text-muted-foreground">秒</span>
            </div>
            {selectedSceneId && (
              <p className="mt-1 text-muted-foreground">已选中 {selectedSceneId}</p>
            )}
          </div>
          <button
            type="button"
            onClick={handleSubmit}
            disabled={applying || !instruction.trim() || lockedSet.has(activeTrack)}
            className={cn(
              'mt-2 w-full rounded-md bg-primary px-3 py-2 text-sm font-medium text-primary-foreground',
              (applying || !instruction.trim() || lockedSet.has(activeTrack)) && 'cursor-not-allowed opacity-60',
            )}
          >
            {applying ? '应用中…' : '应用编辑'}
          </button>
        </div>
      </div>
      {editError && <p className="mt-2 text-xs text-destructive">{editError}</p>}
    </section>
  )
}
