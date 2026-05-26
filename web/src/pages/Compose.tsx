import { useCallback, useRef, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'

import { api } from '@/api/client'
import { PageShell } from '@/components/layout/PageShell'
import { usePlanStore } from '@/stores/plan'
import { useSessionStore } from '@/stores/session'
import type {
  FillAction,
  FillResult,
  Gap,
  GapStatus,
  MaterialUploadResponse,
  Plan,
  PlanBuildRequest,
} from '@/types/schemas'
import { SECTION_SHORT } from '@/lib/sections'
import { cn } from '@/lib/utils'

const STATUS_COLOR: Record<GapStatus, string> = {
  ok: 'bg-emerald-500/15 text-emerald-700 dark:text-emerald-300',
  warn: 'bg-amber-500/15 text-amber-700 dark:text-amber-300',
  miss: 'bg-rose-500/15 text-rose-700 dark:text-rose-300',
}
const STATUS_LABEL: Record<GapStatus, string> = { ok: '✅ 命中', warn: '⚠️ 勉强', miss: '❌ 缺口' }
const ACTIONS: { value: FillAction; label: string; hint: string }[] = [
  { value: 'rerank', label: '结构重排', hint: '从已有素材里挑一个最匹配的' },
  { value: 'copy', label: '文案补全', hint: 'LLM 写一段画外口播' },
  { value: 'aigc', label: 'AIGC 生成', hint: 'Seedance T2V 生成 5-8s 短片填补槽位' },
]

export default function ComposePage() {
  const navigate = useNavigate()
  const selectedSampleId = useSessionStore((s) => s.selectedSampleId)
  const sessionId = useSessionStore((s) => s.sessionId)
  const materials = useSessionStore((s) => s.materials)
  const setSession = useSessionStore((s) => s.setSession)
  const appendMaterials = useSessionStore((s) => s.appendMaterials)
  const removeMaterial = useSessionStore((s) => s.removeMaterial)

  const plan = usePlanStore((s) => s.plan)
  const gaps = usePlanStore((s) => s.gaps)
  const fills = usePlanStore((s) => s.fills)
  const setPlan = usePlanStore((s) => s.setPlan)
  const setGaps = usePlanStore((s) => s.setGaps)
  const upsertFill = usePlanStore((s) => s.upsertFill)

  const fileInputRef = useRef<HTMLInputElement | null>(null)
  const [uploading, setUploading] = useState(false)
  const [building, setBuilding] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const handlePickFiles = useCallback(
    async (files: FileList | null) => {
      if (!files || files.length === 0) return
      setError(null)
      setUploading(true)
      try {
        const fd = new FormData()
        Array.from(files).forEach((f) => fd.append('files', f))
        if (sessionId) fd.append('session_id', sessionId)
        const resp = await api.post<MaterialUploadResponse>('/material/upload', fd)
        setSession(resp.session_id)
        appendMaterials(resp.materials)
      } catch (err) {
        setError(err instanceof Error ? err.message : '上传失败')
      } finally {
        setUploading(false)
      }
    },
    [appendMaterials, sessionId, setSession],
  )

  const handleBuildAndDetect = useCallback(async () => {
    if (!selectedSampleId) {
      setError('请先在素材库选样例')
      return
    }
    setError(null)
    setBuilding(true)
    try {
      const planReq: PlanBuildRequest = {
        sample_id: selectedSampleId,
        session_id: sessionId ?? 'no-session',
        selected_materials: materials.map((m) => m.material_id),
        fills,
        variant: 'A',
      }
      const builtPlan = await api.post<Plan>('/plan/build', planReq)
      setPlan(builtPlan)
      const detected = await api.post<Gap[]>('/gap/detect', { plan_id: builtPlan.plan_id })
      setGaps(detected)
    } catch (err) {
      setError(err instanceof Error ? err.message : '构建失败')
    } finally {
      setBuilding(false)
    }
  }, [fills, materials, selectedSampleId, sessionId, setGaps, setPlan])

  const handleFill = useCallback(
    async (gap: Gap, action: FillAction) => {
      setError(null)
      try {
        const params: Record<string, unknown> = {}
        if (action === 'copy') params.prompt_hint = gap.requirement
        if (action === 'aigc') params.prompt = gap.requirement
        const result = await api.post<FillResult>('/gap/fill', {
          gap_id: gap.gap_id,
          action,
          params,
        })
        upsertFill(result)
      } catch (err) {
        setError(err instanceof Error ? err.message : '补全失败')
      }
    },
    [upsertFill],
  )

  if (!selectedSampleId) {
    return (
      <PageShell title="新素材 / 缺口" subtitle="先去素材库挑一个样例。">
        <div className="rounded-lg border border-dashed border-border bg-card p-8 text-sm text-muted-foreground">
          <Link to="/library" className="text-primary underline-offset-4 hover:underline">
            返回素材库 →
          </Link>
        </div>
      </PageShell>
    )
  }

  return (
    <PageShell
      title="新素材 / 缺口补全"
      subtitle="上传你的视频 / 图片 / 音频，跑通『匹配 → 缺口识别 → 三种补全』。"
    >
      {error && (
        <div className="mb-4 rounded-md border border-destructive/40 bg-destructive/5 px-4 py-3 text-sm text-destructive">
          {error}
        </div>
      )}

      <section className="mb-6 rounded-lg border border-border bg-card p-4">
        <div className="mb-3 flex items-center justify-between">
          <h2 className="text-sm font-semibold">1 · 上传素材</h2>
          <span className="text-xs text-muted-foreground">
            session: <span className="font-mono">{sessionId ?? '尚未分配'}</span>
          </span>
        </div>
        <UploadDropzone
          uploading={uploading}
          onPick={() => fileInputRef.current?.click()}
          onDrop={(files) => void handlePickFiles(files)}
        />
        <input
          ref={fileInputRef}
          type="file"
          multiple
          hidden
          accept="video/*,image/*,audio/*"
          onChange={(e) => void handlePickFiles(e.target.files)}
        />

        {materials.length > 0 && (
          <ul className="mt-4 grid grid-cols-1 gap-2 sm:grid-cols-2 lg:grid-cols-3">
            {materials.map((m) => (
              <li
                key={m.material_id}
                className="flex items-start gap-3 rounded-md border border-border bg-background/50 p-3"
              >
                <span className="rounded bg-secondary px-1.5 py-0.5 text-[10px] font-medium uppercase">
                  {m.media_type}
                </span>
                <div className="min-w-0 flex-1 text-xs">
                  <p className="truncate font-medium text-foreground" title={m.filename}>
                    {m.filename}
                  </p>
                  <p className="text-muted-foreground">
                    {m.recommended_section ? `推荐 ${SECTION_SHORT[m.recommended_section]}` : '未分类'}
                    {m.duration_seconds != null ? ` · ${m.duration_seconds.toFixed(1)}s` : ''}
                  </p>
                  {m.tags.length > 0 && (
                    <p className="mt-1 line-clamp-2 text-[10px] text-muted-foreground">
                      {m.tags.join(' · ')}
                    </p>
                  )}
                </div>
                <button
                  onClick={() => removeMaterial(m.material_id)}
                  className="text-muted-foreground hover:text-destructive"
                  title="移除"
                >
                  ×
                </button>
              </li>
            ))}
          </ul>
        )}
      </section>

      <section className="mb-6 rounded-lg border border-border bg-card p-4">
        <div className="flex items-center justify-between">
          <h2 className="text-sm font-semibold">2 · 构建 Plan + 缺口识别</h2>
          <button
            onClick={handleBuildAndDetect}
            disabled={building}
            className={cn(
              'rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground',
              building && 'cursor-not-allowed opacity-60',
            )}
          >
            {building ? '构建中…' : plan ? '重新识别' : '开始构建'}
          </button>
        </div>
        {plan && (
          <p className="mt-2 text-xs text-muted-foreground">
            plan_id <span className="font-mono">{plan.plan_id}</span> · 主轨 {plan.main_track.length} scene · 包装 {plan.packaging_track.length} item
          </p>
        )}
      </section>

      {gaps.length > 0 && (
        <section className="mb-6 rounded-lg border border-border bg-card p-4">
          <h2 className="mb-3 text-sm font-semibold">3 · 缺口与补全（{gaps.length}）</h2>
          <div className="space-y-2">
            {gaps.map((gap) => {
              const fill = fills.find((f) => f.gap_id === gap.gap_id)
              return (
                <div key={gap.gap_id} className="rounded-md border border-border bg-background/40 p-3">
                  <div className="flex flex-wrap items-center gap-2 text-xs">
                    <span className="rounded bg-secondary px-1.5 py-0.5 font-medium">
                      {SECTION_SHORT[gap.section]} · slot {gap.slot_index}
                    </span>
                    <span className={cn('rounded px-1.5 py-0.5 font-medium', STATUS_COLOR[gap.status])}>
                      {STATUS_LABEL[gap.status]}
                    </span>
                    <span className="text-muted-foreground">影响：{gap.impact}</span>
                    {gap.matched_material_id && (
                      <span className="text-muted-foreground">
                        命中 <span className="font-mono">{gap.matched_material_id}</span>
                      </span>
                    )}
                  </div>
                  <p className="mt-2 text-sm">{gap.requirement}</p>
                  {gap.note && <p className="text-xs text-muted-foreground">{gap.note}</p>}
                  <div className="mt-3 flex flex-wrap gap-2">
                    {ACTIONS.map((a) => (
                      <button
                        key={a.value}
                        onClick={() => void handleFill(gap, a.value)}
                        className={cn(
                          'rounded-md border px-2.5 py-1 text-xs transition-colors',
                          fill?.action === a.value
                            ? 'border-primary bg-primary/10 text-primary'
                            : 'border-border bg-background hover:bg-secondary',
                        )}
                        title={a.hint}
                      >
                        {a.label}
                      </button>
                    ))}
                  </div>
                  {fill && <FillSummary fill={fill} />}
                </div>
              )
            })}
          </div>
        </section>
      )}

      {plan && (
        <div className="flex gap-2">
          <button
            onClick={() => navigate('/migrate')}
            className="rounded-md border border-border bg-card px-4 py-2 text-sm font-medium hover:bg-secondary"
          >
            下一步 · 迁移映射 →
          </button>
          <button
            onClick={() => navigate('/render')}
            className="rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground"
          >
            直接生成视频 →
          </button>
        </div>
      )}
    </PageShell>
  )
}

function UploadDropzone({
  onPick,
  onDrop,
  uploading,
}: {
  onPick: () => void
  onDrop: (files: FileList) => void
  uploading: boolean
}) {
  const [hover, setHover] = useState(false)
  return (
    <div
      onDragOver={(e) => {
        e.preventDefault()
        setHover(true)
      }}
      onDragLeave={() => setHover(false)}
      onDrop={(e) => {
        e.preventDefault()
        setHover(false)
        onDrop(e.dataTransfer.files)
      }}
      onClick={onPick}
      className={cn(
        'flex h-32 cursor-pointer items-center justify-center rounded-md border-2 border-dashed text-sm transition-colors',
        hover ? 'border-primary bg-primary/5' : 'border-border bg-background/40',
        uploading && 'pointer-events-none opacity-60',
      )}
    >
      <span className="text-muted-foreground">
        {uploading ? '上传中…' : '点击或拖拽 video / image / audio（≤ 50MB / file）'}
      </span>
    </div>
  )
}

function FillSummary({ fill }: { fill: FillResult }) {
  return (
    <div className="mt-2 rounded border border-border bg-secondary/50 p-2 text-xs">
      <div className="text-muted-foreground">
        补全 · <span className="font-medium">{fill.action}</span> · 状态 {STATUS_LABEL[fill.status]}
      </div>
      {fill.narration && <p className="mt-1">{fill.narration}</p>}
      {fill.new_material_id && (
        <p className="mt-1 text-muted-foreground">
          new_material_id <span className="font-mono">{fill.new_material_id}</span>
        </p>
      )}
      {fill.note && <p className="mt-1 text-muted-foreground">{fill.note}</p>}
    </div>
  )
}
