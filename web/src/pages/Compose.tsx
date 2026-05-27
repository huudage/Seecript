import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'

import { api } from '@/api/client'
import { BriefInput } from '@/components/compose/BriefInput'
import { FillAigcPanel } from '@/components/compose/FillAigcPanel'
import { FillCopyPanel } from '@/components/compose/FillCopyPanel'
import { FillRerankPanel } from '@/components/compose/FillRerankPanel'
import { GapList } from '@/components/compose/GapList'
import { GapPreviewDialog } from '@/components/compose/GapPreviewDialog'
import { MaterialGrid } from '@/components/compose/MaterialGrid'
import { StoryboardPreview } from '@/components/compose/StoryboardPreview'
import { PageShell } from '@/components/layout/PageShell'
import { SECTION_BG, SECTION_SHORT } from '@/lib/sections'
import { cn } from '@/lib/utils'
import { usePlanStore } from '@/stores/plan'
import { useProjectsStore } from '@/stores/projects'
import { useSessionStore } from '@/stores/session'
import type {
  FillAction,
  FillResult,
  Gap,
  GapDetectRequest,
  GapFillRequest,
  MaterialUploadResponse,
  Plan,
  PlanBuildRequest,
} from '@/types/schemas'
import { kindsForVideoType } from '@/types/schemas'

const ACTION_TABS: { value: FillAction; label: string; hint: string }[] = [
  { value: 'rerank', label: '结构重排', hint: '从已上传素材里挑一个最匹配的填进 slot' },
  { value: 'copy', label: '文案补全', hint: 'LLM 写一段画外口播，可编辑+三选一' },
  { value: 'aigc', label: 'AIGC 生成', hint: 'Seedance T2V 生成 5-8s 短片填补 slot' },
]

export default function ComposePage() {
  const navigate = useNavigate()

  // session store
  const selectedSampleId = useSessionStore((s) => s.selectedSampleId)
  const videoType = useSessionStore((s) => s.videoType)
  const sessionId = useSessionStore((s) => s.sessionId)
  const materials = useSessionStore((s) => s.materials)
  const brief = useSessionStore((s) => s.brief)
  const setBrief = useSessionStore((s) => s.setBrief)
  const setSession = useSessionStore((s) => s.setSession)
  const appendMaterials = useSessionStore((s) => s.appendMaterials)
  const removeMaterial = useSessionStore((s) => s.removeMaterial)
  const reorderMaterials = useSessionStore((s) => s.reorderMaterials)

  // projects store（自动存档到 localStorage，首页能看到历史项目）
  const currentProjectId = useProjectsStore((s) => s.currentProjectId)
  const upsertProject = useProjectsStore((s) => s.upsertProject)

  // plan store
  const plan = usePlanStore((s) => s.plan)
  const gaps = usePlanStore((s) => s.gaps)
  const fills = usePlanStore((s) => s.fills)
  const selectedGapId = usePlanStore((s) => s.selectedGapId)
  const setPlan = usePlanStore((s) => s.setPlan)
  const setGaps = usePlanStore((s) => s.setGaps)
  const upsertFill = usePlanStore((s) => s.upsertFill)
  const setSelectedGapId = usePlanStore((s) => s.setSelectedGapId)

  // UI state
  const fileInputRef = useRef<HTMLInputElement | null>(null)
  const [uploading, setUploading] = useState(false)
  const [analyzing, setAnalyzing] = useState(false)
  const [activeAction, setActiveAction] = useState<FillAction>('rerank')
  const [filling, setFilling] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [previewGapId, setPreviewGapId] = useState<string | null>(null)
  const [briefTouched, setBriefTouched] = useState(false)

  const sortedMaterials = useMemo(
    () => materials.slice().sort((a, b) => a.sort_order - b.sort_order),
    [materials],
  )

  // 选中 gap → 拿对应的 fill（如果已经做过）
  const selectedGap = useMemo(
    () => gaps.find((g) => g.gap_id === selectedGapId) ?? null,
    [gaps, selectedGapId],
  )
  const selectedFill = useMemo(
    () => fills.find((f) => f.gap_id === selectedGapId) ?? null,
    [fills, selectedGapId],
  )
  const filledGapIds = useMemo(() => new Set(fills.map((f) => f.gap_id)), [fills])

  // gap 列表换了之后，自动选第一个 miss/warn
  useEffect(() => {
    if (gaps.length === 0) {
      setSelectedGapId(null)
      return
    }
    if (selectedGapId && gaps.some((g) => g.gap_id === selectedGapId)) return
    const first = gaps.find((g) => g.status !== 'ok') ?? gaps[0]
    setSelectedGapId(first.gap_id)
  }, [gaps, selectedGapId, setSelectedGapId])

  /* ----------------------------- 上传 ----------------------------- */

  const handlePickFiles = useCallback(
    async (files: FileList | null) => {
      if (!files || files.length === 0) return
      setError(null)
      setUploading(true)
      try {
        const fd = new FormData()
        Array.from(files).forEach((f) => fd.append('files', f))
        if (sessionId) fd.append('session_id', sessionId)
        fd.append('video_type', videoType)
        const resp = await api.post<MaterialUploadResponse>('/material/upload', fd)
        setSession(resp.session_id)
        appendMaterials(resp.materials)
      } catch (err) {
        setError(err instanceof Error ? err.message : '上传失败')
      } finally {
        setUploading(false)
      }
    },
    [appendMaterials, sessionId, setSession, videoType],
  )

  /* -------------------- 智能分析（plan/build + gap/detect） -------------------- */

  const runAnalyze = useCallback(
    async (extraFills?: FillResult[]) => {
      if (!selectedSampleId) {
        setError('请先在素材库挑一个样例')
        return null
      }
      if (brief.trim().length === 0) {
        setBriefTouched(true)
        setError('请先输入主题——LLM 需要它作为语义锚定，否则缺口推断会失真。')
        return null
      }
      setError(null)
      setAnalyzing(true)
      try {
        const planReq: PlanBuildRequest = {
          sample_id: selectedSampleId,
          session_id: sessionId ?? 'no-session',
          brief: brief.trim() || null,
          selected_materials: sortedMaterials.map((m) => m.material_id),
          fills: extraFills ?? fills,
          variant: 'A',
        }
        const builtPlan = await api.post<Plan>('/plan/build', planReq)
        setPlan(builtPlan)

        const detectReq: GapDetectRequest = {
          plan_id: builtPlan.plan_id,
          session_id: sessionId ?? null,
          // 用户没传素材时关掉 mock 回退，让所有 gap 真实地显示 miss，引导走 copy/aigc。
          allow_mock: sortedMaterials.length > 0,
        }
        const detected = await api.post<Gap[]>('/gap/detect', detectReq)
        // 把已采纳的 fill 叠加到 gap 状态上：后端 detect 只看 materials，不知道
        // 用户刚采纳的 copy/aigc/rerank。这里在前端做合并，让红色 ❌ 立刻变 ✅。
        const useFills = extraFills ?? fills
        const fillMap = new Map(useFills.map((f) => [f.gap_id, f]))
        const merged = detected.map((g): Gap => {
          const f = fillMap.get(g.gap_id)
          if (!f || f.status !== 'ok') return g
          const label =
            f.action === 'copy' ? '文案补全' : f.action === 'aigc' ? 'AIGC 生成' : '已重排'
          return {
            ...g,
            status: 'ok',
            note: f.note ?? `已采纳 ${label}`,
            matched_material_id: f.new_material_id ?? g.matched_material_id,
          }
        })
        setGaps(merged)

        // 自动存档到首页项目列表
        if (currentProjectId) {
          upsertProject({
            id: currentProjectId,
            session_id: sessionId ?? null,
            brief,
            materials: sortedMaterials,
            plan: builtPlan,
            plan_id: builtPlan.plan_id,
            gaps: merged,
            fills: useFills,
            status: 'planned',
          })
        }

        return builtPlan
      } catch (err) {
        setError(err instanceof Error ? err.message : '智能分析失败')
        return null
      } finally {
        setAnalyzing(false)
      }
    },
    [brief, currentProjectId, fills, selectedSampleId, sessionId, setGaps, setPlan, sortedMaterials, upsertProject],
  )

  const handleAnalyze = useCallback(() => void runAnalyze(), [runAnalyze])

  /* ----------------------------- 补全动作 ----------------------------- */

  const runFill = useCallback(
    async (gap: Gap, action: FillAction, params: Record<string, unknown> = {}) => {
      setFilling(true)
      setError(null)
      try {
        const body: GapFillRequest = { gap_id: gap.gap_id, action, params }
        const result = await api.post<FillResult>('/gap/fill', body)
        upsertFill(result)
        // 自动用最新 fills 重发 plan/build + gap/detect → 刷新右侧 + 底部
        const nextFills = [...fills.filter((f) => f.gap_id !== gap.gap_id), result]
        await runAnalyze(nextFills)
        return result
      } catch (err) {
        setError(err instanceof Error ? err.message : '补全失败')
        return null
      } finally {
        setFilling(false)
      }
    },
    [fills, runAnalyze, upsertFill],
  )

  const handleRerankApply = useCallback(async () => {
    if (!selectedGap) return
    await runFill(selectedGap, 'rerank')
  }, [runFill, selectedGap])

  const handleCopyAdopt = useCallback(
    async (finalNarration: string) => {
      if (!selectedGap) return
      // 用 prompt_hint 触发后端再写一次，但我们其实只要回写本地——简单走 upsertFill+rebuild
      const baseFill: FillResult = selectedFill ?? {
        gap_id: selectedGap.gap_id,
        action: 'copy',
        alternatives: [],
        status: 'ok',
      }
      const merged: FillResult = {
        ...baseFill,
        action: 'copy',
        narration: finalNarration,
        status: 'ok',
      }
      upsertFill(merged)
      const nextFills = [...fills.filter((f) => f.gap_id !== selectedGap.gap_id), merged]
      await runAnalyze(nextFills)
    },
    [fills, runAnalyze, selectedFill, selectedGap, upsertFill],
  )

  const handleCopyTrigger = useCallback(async () => {
    if (!selectedGap) return
    await runFill(selectedGap, 'copy', { prompt_hint: selectedGap.requirement })
  }, [runFill, selectedGap])

  /* ----------------------------- guard ----------------------------- */

  if (!selectedSampleId) {
    return (
      <PageShell title="新素材 / 缺口补全" subtitle="先去素材库挑一个样例。">
        <div className="rounded-lg border border-dashed border-border bg-card p-8 text-sm text-muted-foreground">
          <Link to="/library" className="text-primary underline-offset-4 hover:underline">
            返回素材库 →
          </Link>
        </div>
      </PageShell>
    )
  }

  /* ------------------------------ 渲染 ------------------------------ */

  return (
    <PageShell
      title="新素材 / 缺口补全"
      subtitle="输入主题（可选上传素材）→ 智能分析 → 在当前页一键采纳三种补全。"
    >
      {error && (
        <div className="mb-3 rounded-md border border-destructive/40 bg-destructive/5 px-4 py-2 text-sm text-destructive">
          {error}
        </div>
      )}

      {/* 三栏：左输入 / 右结构+缺口；底部 col-span-2 预览 */}
      <div className="grid gap-4 xl:grid-cols-[minmax(360px,1fr)_minmax(420px,1.4fr)]">
        {/* ===================== 左 · 输入区 ===================== */}
        <section className="space-y-3 rounded-lg border border-border bg-card p-4">
          <BriefInput
            value={brief}
            onChange={(v) => {
              setBrief(v)
              if (v.trim().length > 0) setBriefTouched(false)
            }}
            required
            showError={briefTouched}
          />

          <div className="space-y-2">
            <div className="flex items-center justify-between">
              <label className="text-xs font-semibold">
                上传素材 <span className="font-normal text-muted-foreground">（可选）</span>
              </label>
              <span className="text-[10px] text-muted-foreground">
                session{' '}
                <span className="font-mono">{sessionId ?? '尚未分配'}</span>
              </span>
            </div>
            {sortedMaterials.length === 0 && (
              <p className="rounded-md bg-muted/40 px-2 py-1 text-[11px] text-muted-foreground">
                没有素材也能跑：仅凭主题分析 → 缺口全部 miss → 用 文案 / AIGC 逐个补齐。
              </p>
            )}
            <UploadDropzone
              uploading={uploading}
              onPick={() => fileInputRef.current?.click()}
              onDrop={(f) => void handlePickFiles(f)}
            />
            <input
              ref={fileInputRef}
              type="file"
              multiple
              hidden
              accept="video/*,image/*,audio/*"
              onChange={(e) => void handlePickFiles(e.target.files)}
            />
          </div>

          <div className="space-y-2">
            <div className="flex items-center justify-between">
              <label className="text-xs font-semibold">素材库（拖拽可排序）</label>
              <span className="text-[10px] text-muted-foreground">{sortedMaterials.length} 条</span>
            </div>
            <MaterialGrid
              materials={sortedMaterials}
              onReorder={reorderMaterials}
              onRemove={removeMaterial}
            />
          </div>

          <button
            onClick={handleAnalyze}
            disabled={analyzing || brief.trim().length === 0}
            title={brief.trim().length === 0 ? '请先输入主题/卖点' : undefined}
            className={cn(
              'w-full rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground transition-colors',
              (analyzing || brief.trim().length === 0) && 'cursor-not-allowed opacity-60',
            )}
          >
            {analyzing ? '智能分析中…' : plan ? '重新分析' : '智能分析'}
          </button>
        </section>

        {/* ===================== 右 · 结构 + 缺口 ===================== */}
        <section className="space-y-3 rounded-lg border border-border bg-card p-4">
          <div className="space-y-2">
            <div className="flex items-center justify-between">
              <h2 className="text-sm font-semibold">样例结构</h2>
              <span className="text-[10px] text-muted-foreground">{videoType}</span>
            </div>
            <SectionsBar videoType={videoType} />
          </div>

          <div className="space-y-2">
            <div className="flex items-center justify-between">
              <h2 className="text-sm font-semibold">缺口清单（{gaps.length}）</h2>
              {fills.length > 0 && (
                <span className="text-[10px] text-muted-foreground">已采纳 {fills.length}</span>
              )}
            </div>
            <GapList
              gaps={gaps}
              selectedGapId={selectedGapId}
              filledGapIds={filledGapIds}
              onSelect={(id) => {
                setSelectedGapId(id)
                setPreviewGapId(id)
              }}
            />
          </div>

          {selectedGap && (
            <div className="space-y-2">
              <div className="flex flex-wrap items-center gap-1 border-t border-border pt-3 text-xs">
                {ACTION_TABS.map((tab) => (
                  <button
                    key={tab.value}
                    onClick={() => setActiveAction(tab.value)}
                    title={tab.hint}
                    className={cn(
                      'rounded-md border px-2 py-1 transition-colors',
                      activeAction === tab.value
                        ? 'border-primary bg-primary/10 text-primary'
                        : 'border-border bg-background hover:bg-secondary',
                    )}
                  >
                    {tab.label}
                  </button>
                ))}
              </div>

              {activeAction === 'rerank' && plan && (
                <>
                  {!selectedFill && (
                    <button
                      onClick={() => void runFill(selectedGap, 'rerank')}
                      disabled={filling}
                      className={cn(
                        'w-full rounded-md bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground',
                        filling && 'cursor-not-allowed opacity-60',
                      )}
                    >
                      {filling ? '生成候选中…' : '让 LLM 挑一个素材填进来'}
                    </button>
                  )}
                  {selectedFill && selectedFill.action === 'rerank' && (
                    <FillRerankPanel
                      plan={plan}
                      fill={selectedFill}
                      materials={sortedMaterials}
                      onApply={handleRerankApply}
                      loading={filling}
                    />
                  )}
                </>
              )}

              {activeAction === 'copy' && (
                <>
                  {!selectedFill || selectedFill.action !== 'copy' ? (
                    <button
                      onClick={handleCopyTrigger}
                      disabled={filling}
                      className={cn(
                        'w-full rounded-md bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground',
                        filling && 'cursor-not-allowed opacity-60',
                      )}
                    >
                      {filling ? '生成文案中…' : '让 LLM 写一段口播'}
                    </button>
                  ) : (
                    <FillCopyPanel
                      fill={selectedFill}
                      onAdopt={handleCopyAdopt}
                      loading={filling}
                    />
                  )}
                </>
              )}

              {activeAction === 'aigc' && (
                <FillAigcPanel
                  gap={selectedGap}
                  fill={selectedFill?.action === 'aigc' ? selectedFill : null}
                  onResult={(f) => {
                    upsertFill(f)
                    const nextFills = [...fills.filter((x) => x.gap_id !== f.gap_id), f]
                    void runAnalyze(nextFills)
                  }}
                />
              )}
            </div>
          )}
        </section>

        {/* ===================== 底部 · 适配结果预览 ===================== */}
        <section className="rounded-lg border border-border bg-card p-4 xl:col-span-2">
          <h2 className="mb-3 text-sm font-semibold">适配结果预览</h2>
          {plan ? (
            <StoryboardPreview plan={plan} />
          ) : (
            <div className="rounded-md border border-dashed border-border bg-background/30 p-6 text-center text-xs text-muted-foreground">
              点上方「智能分析」开始；plan 构建好后这里会显示分镜带。
            </div>
          )}
        </section>
      </div>

      {/* 底部 next steps */}
      {plan && (
        <div className="mt-4 flex gap-2">
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

      {/* 样例截图弹窗 */}
      <GapPreviewDialog
        gap={previewGapId ? (gaps.find((g) => g.gap_id === previewGapId) ?? null) : null}
        onClose={() => setPreviewGapId(null)}
      />
    </PageShell>
  )
}

/* ---------- 子组件 ---------- */

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
        'flex h-24 cursor-pointer items-center justify-center rounded-md border-2 border-dashed text-xs transition-colors',
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

function SectionsBar({ videoType }: { videoType: 'marketing' | 'editing' | 'motion_graph' }) {
  const kinds = kindsForVideoType(videoType)
  return (
    <div className="flex h-8 overflow-hidden rounded-md border border-border">
      {kinds.map((k) => (
        <div
          key={k}
          className={cn(
            'flex flex-1 items-center justify-center text-[11px] font-medium text-white',
            SECTION_BG[k],
          )}
        >
          {SECTION_SHORT[k]}
        </div>
      ))}
    </div>
  )
}
