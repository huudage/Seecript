import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { LineChart, Line, XAxis, YAxis, Tooltip, ResponsiveContainer, CartesianGrid, ReferenceLine } from 'recharts'

import { api, ApiError } from '@/api/client'
import { commitStep, getStepSnapshot } from '@/api/steps'
import { createSSE, type SSEHandle } from '@/api/sse'
import { PageShell } from '@/components/layout/PageShell'
import { useProjectsStore } from '@/stores/projects'
import { useSessionStore } from '@/stores/session'
import type {
  DecomposeRequest,
  DecomposeSubmitResponse,
  ManifestStatusResponse,
  ProgressEventPayload,
  SampleManifest,
  SampleVersionInfo,
  Section,
  Shot,
  VersionMutationResponse,
  VideoType,
  VideoUnderstanding,
} from '@/types/schemas'
import {
  SECTION_BG,
  SECTION_LABEL,
  VIDEO_TYPE_HINT,
  VIDEO_TYPE_LABEL,
} from '@/lib/sections'
import { cn } from '@/lib/utils'
import { readVideoDuration, VIDEO_UPLOAD_MAX_DURATION_SECONDS } from '@/lib/video'

const VIDEO_TYPE_OPTIONS: VideoType[] = ['marketing', 'editing', 'motion_graph']
const NL_PROMPT_MAX = 500

interface DoneEvent {
  job_id: string
  payload: { sample_id: string; manifest: SampleManifest; slot_id?: string | null }
}

interface DecomposeUploadResponse {
  sample_id: string
  filename: string
  size_bytes: number
  video_url: string
}

// slots_full 409 body: { error: "slots_full", versions: [{slot_id, updated_at, is_active}], ... }
interface SlotsFullDetail {
  error: 'slots_full'
  message: string
  max_versions: number
  versions: { slot_id: string; updated_at: number; is_active: boolean }[]
}

// 把后端 versions 数组 + viewSlot 解析成"当前正在看哪个"的 manifest 缓存。
// key=slot_id, value=manifest。重新拉 / 编辑后更新对应 key。
type ManifestCache = Record<string, SampleManifest>

export default function DecomposePage() {
  const selectedSampleIds = useSessionStore((s) => s.selectedSampleIds)
  const selectedSampleId = selectedSampleIds[0] ?? null
  const sampleSource = useSessionStore((s) => s.sampleSource)
  const videoType = useSessionStore((s) => s.videoType)
  const setVideoType = useSessionStore((s) => s.setVideoType)
  const setManifest = useSessionStore((s) => s.setManifest)
  const selectSamples = useSessionStore((s) => s.selectSamples)
  const currentProjectId = useProjectsStore((s) => s.currentProjectId)
  const navigate = useNavigate()

  const [progress, setProgress] = useState<{ step: string; percent: number; note?: string }>({
    step: 'idle',
    percent: 0,
  })
  const [running, setRunning] = useState(false)
  const [uploading, setUploading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [uploadedFile, setUploadedFile] = useState<{ filename: string; size_bytes: number } | null>(null)

  // 版本槽模型：versions[] 顺序由后端给（按 mtime 升序，v1 最旧 / v2 最新）
  const [versions, setVersions] = useState<SampleVersionInfo[]>([])
  const [activeSlot, setActiveSlot] = useState<string | null>(null)
  // viewSlot：用户当前正在看的 slot（独立于 active）。null = 等同 active。
  const [viewSlot, setViewSlot] = useState<string | null>(null)
  // 同 slot_id → manifest 缓存。切 tab 不重拉。重新拆 / 编辑后增删对应 key。
  const [manifestCache, setManifestCache] = useState<ManifestCache>({})
  // 对比模式（仅 versions.length===2 时可用）
  const [compareMode, setCompareMode] = useState(false)

  // 编辑态
  const [isEditing, setIsEditing] = useState(false)
  const [draftBuffer, setDraftBuffer] = useState<SampleManifest | null>(null)
  // 编辑目标的 slot_id——viewSlot 在开编辑时锁定，避免用户切 tab 引起 PUT 错槽
  const [editingSlot, setEditingSlot] = useState<string | null>(null)

  // 重新拆解对话框
  const [regenOpen, setRegenOpen] = useState(false)
  const [nlPrompt, setNlPrompt] = useState('')
  // 槽满弹窗：拿到 409 后，把 nlPrompt 暂存到这里，让用户挑要替换的 slot 后再发
  const [slotsFullDialog, setSlotsFullDialog] = useState<{
    detail: SlotsFullDetail
    pendingNlPrompt: string
  } | null>(null)

  const [busy, setBusy] = useState(false)
  const sseRef = useRef<SSEHandle | null>(null)
  const fileInputRef = useRef<HTMLInputElement | null>(null)

  // sampleSource:
  //   'system' = 从素材库挑的内置样例 → video_type 锁定，直接拆解
  //   'user'   = 用户上传到 server/var/uploads/decompose/<sample_id>/ 的视频 → video_type 可选
  //   null     = 没选/没传任何样例 → 引导用户去素材库或上传
  const isSystemSample = sampleSource === 'system'
  const isUserSample = sampleSource === 'user'

  // viewSlot 没显式指定时跟随 active；versions=[] 时也回 null
  const effectiveViewSlot = useMemo(() => {
    if (versions.length === 0) return null
    if (viewSlot && versions.some((v) => v.slot_id === viewSlot)) return viewSlot
    return activeSlot ?? versions[versions.length - 1].slot_id
  }, [versions, viewSlot, activeSlot])

  const currentManifest = effectiveViewSlot ? manifestCache[effectiveViewSlot] ?? null : null
  // 对比模式：左旧 v1 / 右新 v2
  const compareLeft = versions[0]
  const compareRight = versions[1]
  const compareLeftManifest = compareLeft ? manifestCache[compareLeft.slot_id] ?? null : null
  const compareRightManifest = compareRight ? manifestCache[compareRight.slot_id] ?? null : null

  // session.manifest 同步——给 Compose 入口 / step snapshot 用
  useEffect(() => {
    setManifest(currentManifest)
  }, [currentManifest, setManifest])

  const handlePickFile = useCallback(
    async (file: File | null) => {
      if (!file) return
      setError(null)
      const duration = await readVideoDuration(file)
      if (duration != null && duration > VIDEO_UPLOAD_MAX_DURATION_SECONDS) {
        setError(`视频时长 ${duration.toFixed(1)}s 超过 3 分钟上限，请改用更短的素材`)
        return
      }
      setUploading(true)
      try {
        const fd = new FormData()
        fd.append('file', file)
        const resp = await api.post<DecomposeUploadResponse>('/decompose/upload', fd)
        selectSamples([resp.sample_id], [resp.filename], videoType, 'user')
        setUploadedFile({ filename: resp.filename, size_bytes: resp.size_bytes })
      } catch (err) {
        setError(err instanceof Error ? err.message : '上传失败')
      } finally {
        setUploading(false)
      }
    },
    [selectSamples, videoType],
  )

  const refreshStatus = useCallback(async (sampleId: string) => {
    try {
      const status = await api.get<ManifestStatusResponse>(`/sample/${sampleId}/manifest/status`)
      setVersions(status.versions)
      setActiveSlot(status.active_slot)
      // 预热缓存：把所有现存槽 manifest 拉回（最多 2 次，可以忽略并发开销）
      const fetched = await Promise.all(
        status.versions.map(async (v) => {
          try {
            const mf = await api.get<SampleManifest>(`/sample/${sampleId}/manifest?slot=${v.slot_id}`)
            return [v.slot_id, mf] as const
          } catch {
            return null
          }
        }),
      )
      const next: ManifestCache = {}
      for (const entry of fetched) {
        if (entry) next[entry[0]] = entry[1]
      }
      setManifestCache(next)
      // viewSlot 兜底：若旧 viewSlot 不存在则清空（effectiveViewSlot 会回退到 active）
      setViewSlot((prev) => (prev && status.versions.some((v) => v.slot_id === prev) ? prev : null))
      // 没两个槽就关掉 compare
      if (status.versions.length < 2) setCompareMode(false)
    } catch {
      // sample_id 不存在 / 拉失败：当作没拆解
      setVersions([])
      setActiveSlot(null)
      setManifestCache({})
      setViewSlot(null)
      setCompareMode(false)
    }
  }, [])

  const run = useCallback(async (opts?: { nl_prompt?: string; replace_slot?: string }) => {
    if (!selectedSampleId) return
    setError(null)
    setRunning(true)
    setProgress({ step: 'submit', percent: 2, note: '提交任务' })
    const req: DecomposeRequest = {
      sample_id: selectedSampleId,
      video_type: videoType,
      nl_prompt: opts?.nl_prompt?.trim() || null,
      replace_slot: opts?.replace_slot ?? null,
    }
    try {
      const { job_id } = await api.post<DecomposeSubmitResponse>('/decompose', req)
      sseRef.current = createSSE<DoneEvent, ProgressEventPayload>(
        `/decompose/stream?job_id=${job_id}`,
        {
          onProgress: (ev) => {
            setProgress({
              step: ev.step,
              percent: ev.percent,
              note: (ev.payload as { note?: string } | undefined)?.note,
            })
          },
          onDone: (done) => {
            setProgress({ step: 'done', percent: 100, note: '完成' })
            setRunning(false)
            // 写入对应 slot 缓存 + 切换 viewSlot 到新槽
            const newSlot = done.payload.slot_id
            if (newSlot) {
              setManifestCache((prev) => ({ ...prev, [newSlot]: done.payload.manifest }))
              setViewSlot(newSlot)
            }
            // 重拉 versions 拿最新 active + 顺序
            void refreshStatus(selectedSampleId)
          },
          onError: (err) => {
            setError(err.detail || '拆解失败')
            setRunning(false)
          },
        },
      )
    } catch (err) {
      setRunning(false)
      // 409 slots_full → 弹"挑要替换的版本"对话框
      if (err instanceof ApiError && err.status === 409) {
        const payload = err.payload
        if (
          payload &&
          typeof payload === 'object' &&
          'detail' in payload &&
          (payload as { detail?: { error?: string } }).detail?.error === 'slots_full'
        ) {
          setSlotsFullDialog({
            detail: (payload as { detail: SlotsFullDetail }).detail,
            pendingNlPrompt: opts?.nl_prompt ?? '',
          })
          return
        }
      }
      setError(err instanceof Error ? err.message : String(err))
    }
  }, [refreshStatus, selectedSampleId, videoType])

  useEffect(() => {
    return () => {
      sseRef.current?.close()
    }
  }, [])

  useEffect(() => {
    if (!selectedSampleId) {
      setVersions([])
      setActiveSlot(null)
      setManifestCache({})
      setViewSlot(null)
      setCompareMode(false)
      return
    }
    void refreshStatus(selectedSampleId)
  }, [selectedSampleId, refreshStatus])

  // 系统样例自动开跑：只在选样例后 status 显示无任何版本时触发一次
  const autoRunFiredRef = useRef<string | null>(null)
  useEffect(() => {
    if (!isSystemSample || !selectedSampleId) return
    if (autoRunFiredRef.current === selectedSampleId) return
    if (running) return
    if (versions.length > 0) return
    autoRunFiredRef.current = selectedSampleId
    void run()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isSystemSample, selectedSampleId, versions.length, running])

  // mount：拉一次 decompose 步骤快照——同步 sample_id 到 session
  useEffect(() => {
    if (!currentProjectId) return
    let cancelled = false
    void (async () => {
      try {
        const snap = await getStepSnapshot(currentProjectId, 'decompose')
        if (cancelled || !snap) return
        const savedSampleId = snap.payload?.sample_id as string | undefined
        if (savedSampleId && savedSampleId !== selectedSampleId) {
          selectSamples([savedSampleId])
        }
      } catch {
        /* 没快照或网络抖动不影响主流程 */
      }
    })()
    return () => {
      cancelled = true
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [currentProjectId])

  // ---- 版本动作 ----
  const applyMutation = useCallback((res: VersionMutationResponse) => {
    setVersions(res.versions)
    setActiveSlot(res.active_slot)
    if (res.versions.length < 2) setCompareMode(false)
  }, [])

  const startEdit = useCallback(() => {
    const slot = effectiveViewSlot
    if (!slot) return
    const mf = manifestCache[slot]
    if (!mf) return
    setDraftBuffer(JSON.parse(JSON.stringify(mf)) as SampleManifest)
    setEditingSlot(slot)
    setIsEditing(true)
  }, [effectiveViewSlot, manifestCache])

  const cancelEdit = useCallback(() => {
    setDraftBuffer(null)
    setEditingSlot(null)
    setIsEditing(false)
  }, [])

  const saveEdit = useCallback(async () => {
    if (!selectedSampleId || !draftBuffer || !editingSlot) return
    setError(null)
    setBusy(true)
    try {
      const res = await api.put<VersionMutationResponse>(
        `/sample/${selectedSampleId}/manifest?slot=${editingSlot}`,
        draftBuffer,
      )
      applyMutation(res)
      // 本地缓存立即覆盖，免去再 GET 一次
      setManifestCache((prev) => ({ ...prev, [editingSlot]: draftBuffer }))
      setIsEditing(false)
      setDraftBuffer(null)
      setEditingSlot(null)
    } catch (err) {
      setError(err instanceof Error ? err.message : '保存失败')
    } finally {
      setBusy(false)
    }
  }, [applyMutation, draftBuffer, editingSlot, selectedSampleId])

  const activateSlot = useCallback(async (slot: string) => {
    if (!selectedSampleId) return
    setError(null)
    setBusy(true)
    try {
      const res = await api.post<VersionMutationResponse>(
        `/sample/${selectedSampleId}/versions/${slot}/activate`,
        {},
      )
      applyMutation(res)
    } catch (err) {
      setError(err instanceof Error ? err.message : '激活失败')
    } finally {
      setBusy(false)
    }
  }, [applyMutation, selectedSampleId])

  const deleteSlot = useCallback(async (slot: string) => {
    if (!selectedSampleId) return
    if (!window.confirm('确定删除该版本？此操作不可撤销。')) return
    setError(null)
    setBusy(true)
    try {
      const res = await api.delete<VersionMutationResponse>(
        `/sample/${selectedSampleId}/versions/${slot}`,
      )
      applyMutation(res)
      setManifestCache((prev) => {
        const next = { ...prev }
        delete next[slot]
        return next
      })
      // 删的是当前查看槽 → 回到 active（effectiveViewSlot 会自动兜底）
      if (viewSlot === slot) setViewSlot(null)
    } catch (err) {
      setError(err instanceof Error ? err.message : '删除失败')
    } finally {
      setBusy(false)
    }
  }, [applyMutation, selectedSampleId, viewSlot])

  const openRegenerate = useCallback(() => {
    setNlPrompt('')
    setRegenOpen(true)
  }, [])

  const submitRegenerate = useCallback(async () => {
    const text = nlPrompt
    setRegenOpen(false)
    await run({ nl_prompt: text })
  }, [nlPrompt, run])

  const resumeRegenerateWithReplace = useCallback(async (slot: string) => {
    const dialog = slotsFullDialog
    if (!dialog) return
    setSlotsFullDialog(null)
    await run({ nl_prompt: dialog.pendingNlPrompt, replace_slot: slot })
    // 此处不能直接 setViewSlot(slot)——slot_id 是稳定的，run 完会 setViewSlot 到 new slot
    // 但 manifest_store.create_version with replace_slot 会复用同一个 slot_id（见 manifest_store），所以效果是一致的
  }, [run, slotsFullDialog])

  const handleNext = useCallback(async () => {
    if (!currentProjectId || !selectedSampleId) {
      navigate('/compose')
      return
    }
    try {
      await commitStep(currentProjectId, 'decompose', { sample_id: selectedSampleId })
    } catch (err) {
      setError(err instanceof Error ? err.message : '保存步骤失败')
      return
    }
    navigate('/compose')
  }, [currentProjectId, navigate, selectedSampleId])

  const hasAnyVersion = versions.length > 0
  const canCompare = versions.length === 2

  return (
    <PageShell
      title="视频拆解"
      subtitle="对一个样例视频做结构拆解，结果按版本槽存到资产库（最多 2 个版本可对比）。支持手动编辑、自然语言提示重新拆解。"
    >
      {/* ====== 来源块 ====== */}
      <div className="mb-6 rounded-lg border border-border bg-card p-4">
        {isSystemSample && selectedSampleId && (
          <div className="space-y-2">
            <div className="flex items-center justify-between text-xs text-muted-foreground">
              <span className="font-semibold text-foreground">来源 · 系统样例（已锁定类型）</span>
              <Link to="/library" className="text-primary underline-offset-4 hover:underline">
                换一个样例 →
              </Link>
            </div>
            <div className="flex items-center gap-2 text-sm">
              <span className="font-mono text-xs text-muted-foreground">{selectedSampleId}</span>
              <span className="rounded-full border border-primary bg-primary/10 px-2 py-0.5 text-xs font-medium text-primary">
                {VIDEO_TYPE_LABEL[videoType]}
              </span>
              <span className="text-[11px] text-muted-foreground">{VIDEO_TYPE_HINT[videoType]}</span>
            </div>
          </div>
        )}

        {isUserSample && selectedSampleId && (
          <div className="space-y-3">
            <div className="flex items-center justify-between text-xs text-muted-foreground">
              <span className="font-semibold text-foreground">来源 · 用户上传</span>
              <button
                onClick={() => {
                  selectSamples([])
                  setUploadedFile(null)
                }}
                className="text-primary underline-offset-4 hover:underline"
                disabled={running}
              >
                重新上传 →
              </button>
            </div>
            <div className="flex items-center gap-2 text-xs">
              <span className="rounded-md bg-secondary/50 px-2 py-1 font-mono">{selectedSampleId}</span>
              {uploadedFile && (
                <span className="text-muted-foreground">
                  {uploadedFile.filename} · {(uploadedFile.size_bytes / 1024 / 1024).toFixed(1)}MB
                </span>
              )}
            </div>
            <VideoTypePicker
              value={videoType}
              onChange={setVideoType}
              disabled={running}
            />
          </div>
        )}

        {!selectedSampleId && (
          <div className="space-y-4">
            <p className="text-xs text-muted-foreground">
              选个起点：去资产库挑一段内置爆款样例，或者上传一段自己的视频开始拆解。
            </p>
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
              <Link
                to="/library"
                className="flex flex-col items-start gap-1 rounded-lg border border-border bg-background p-4 transition-colors hover:border-primary hover:bg-primary/5"
              >
                <span className="text-sm font-semibold">从资产库挑样例</span>
                <span className="text-[11px] text-muted-foreground">
                  内置 3 类爆款样例（营销 / 剪辑 / Motion Graph），点选即拆解。
                </span>
              </Link>
              <button
                onClick={() => fileInputRef.current?.click()}
                disabled={uploading}
                className={cn(
                  'flex flex-col items-start gap-1 rounded-lg border border-dashed border-border bg-background p-4 text-left transition-colors hover:border-primary hover:bg-primary/5',
                  uploading && 'cursor-not-allowed opacity-60',
                )}
              >
                <span className="text-sm font-semibold">
                  {uploading ? '上传中…' : '上传自己的视频'}
                </span>
                <span className="text-[11px] text-muted-foreground">
                  mp4 / mov / webm，≤ 3 分钟、单文件 ≤ 200MB；上传后选类型再拆。
                </span>
              </button>
            </div>
            <input
              ref={fileInputRef}
              type="file"
              hidden
              accept="video/mp4,video/quicktime,video/webm"
              onChange={(e) => void handlePickFile(e.target.files?.[0] ?? null)}
            />
          </div>
        )}
      </div>

      {/* ====== 操作栏 ====== */}
      <div className="mb-4 flex flex-wrap items-center gap-3">
        {!hasAnyVersion && (
          <button
            onClick={() => void run()}
            disabled={running || busy || !selectedSampleId}
            className={cn(
              'rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground transition-opacity',
              (running || busy || !selectedSampleId) && 'cursor-not-allowed opacity-60',
            )}
          >
            开始拆解
          </button>
        )}

        {hasAnyVersion && !isEditing && (
          <>
            <button
              onClick={openRegenerate}
              disabled={running || busy}
              className="rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground disabled:opacity-60"
            >
              重新拆解
            </button>
            <button
              onClick={startEdit}
              disabled={running || busy || compareMode}
              title={compareMode ? '请先退出对比模式' : ''}
              className="rounded-md border border-border bg-card px-4 py-2 text-sm font-medium hover:bg-secondary disabled:cursor-not-allowed disabled:opacity-60"
            >
              手动编辑
            </button>
            {effectiveViewSlot && effectiveViewSlot !== activeSlot && (
              <button
                onClick={() => void activateSlot(effectiveViewSlot)}
                disabled={running || busy}
                className="rounded-md border border-emerald-400/60 bg-emerald-50 px-4 py-2 text-sm font-medium text-emerald-700 hover:bg-emerald-100 disabled:opacity-60 dark:bg-emerald-950/40 dark:text-emerald-300"
              >
                设为当前
              </button>
            )}
            {effectiveViewSlot && versions.length > 1 && (
              <button
                onClick={() => void deleteSlot(effectiveViewSlot)}
                disabled={running || busy}
                className="rounded-md border border-border px-3 py-2 text-xs text-muted-foreground hover:bg-destructive/10 hover:text-destructive disabled:opacity-60"
              >
                删除该版本
              </button>
            )}
          </>
        )}

        {isEditing && (
          <>
            <button
              onClick={() => void saveEdit()}
              disabled={busy}
              className="rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground disabled:opacity-60"
            >
              {busy ? '保存中…' : '保存编辑'}
            </button>
            <button
              onClick={cancelEdit}
              disabled={busy}
              className="rounded-md border border-border bg-card px-4 py-2 text-sm font-medium hover:bg-secondary disabled:opacity-60"
            >
              取消编辑
            </button>
          </>
        )}

        {hasAnyVersion && !isEditing && (
          <span className="text-xs text-muted-foreground">
            版本 {versions.length}/2 · 当前 {activeSlot ? versions.find((v) => v.slot_id === activeSlot)?.label ?? '—' : '—'}
          </span>
        )}

        {hasAnyVersion && !isEditing && !compareMode && (
          <button
            onClick={() => void handleNext()}
            className="ml-auto rounded-md border border-border bg-card px-4 py-2 text-sm font-medium hover:bg-secondary"
          >
            下一步 · 上传素材 →
          </button>
        )}

        {error && (
          <span className="text-sm text-destructive">{error}</span>
        )}
      </div>

      {/* ====== 版本 tabs ====== */}
      {hasAnyVersion && !isEditing && (
        <VersionTabs
          versions={versions}
          activeSlot={activeSlot}
          viewSlot={effectiveViewSlot}
          compareMode={compareMode}
          canCompare={canCompare}
          onPick={(slot) => {
            setCompareMode(false)
            setViewSlot(slot)
          }}
          onToggleCompare={() => setCompareMode((v) => !v)}
        />
      )}

      {(running || progress.step !== 'idle') && (
        <ProgressPanel step={progress.step} percent={progress.percent} note={progress.note} />
      )}

      {/* ====== 主体 ====== */}
      {isEditing && draftBuffer ? (
        <ManifestEditor manifest={draftBuffer} onChange={setDraftBuffer} />
      ) : compareMode && compareLeftManifest && compareRightManifest ? (
        <CompareView
          left={compareLeftManifest}
          right={compareRightManifest}
          leftLabel={compareLeft?.label ?? 'v1'}
          rightLabel={compareRight?.label ?? 'v2'}
          activeSlot={activeSlot}
          leftSlot={compareLeft?.slot_id}
          rightSlot={compareRight?.slot_id}
        />
      ) : (
        currentManifest && <ManifestView manifest={currentManifest} />
      )}

      {/* ====== 重新拆解对话框 ====== */}
      {regenOpen && (
        <RegenerateDialog
          nlPrompt={nlPrompt}
          onNlPromptChange={setNlPrompt}
          onSubmit={() => void submitRegenerate()}
          onCancel={() => setRegenOpen(false)}
        />
      )}

      {/* ====== 槽满弹窗 ====== */}
      {slotsFullDialog && (
        <SlotsFullDialog
          detail={slotsFullDialog.detail}
          versions={versions}
          onPick={(slot) => void resumeRegenerateWithReplace(slot)}
          onCancel={() => setSlotsFullDialog(null)}
        />
      )}
    </PageShell>
  )
}

// ----------------------- 版本 tabs -----------------------
function VersionTabs({
  versions,
  activeSlot,
  viewSlot,
  compareMode,
  canCompare,
  onPick,
  onToggleCompare,
}: {
  versions: SampleVersionInfo[]
  activeSlot: string | null
  viewSlot: string | null
  compareMode: boolean
  canCompare: boolean
  onPick: (slot: string) => void
  onToggleCompare: () => void
}) {
  return (
    <div className="mb-4 inline-flex items-center gap-1 rounded-lg border border-border bg-card p-1 text-sm">
      {versions.map((v) => (
        <button
          key={v.slot_id}
          onClick={() => onPick(v.slot_id)}
          className={cn(
            'inline-flex items-center gap-1.5 rounded-md px-3 py-1.5 font-medium transition-colors',
            !compareMode && viewSlot === v.slot_id
              ? 'bg-primary text-primary-foreground'
              : 'text-muted-foreground hover:bg-secondary hover:text-foreground',
          )}
        >
          {v.label}
          {v.slot_id === activeSlot && (
            <span
              className={cn(
                'rounded-full px-1.5 py-0.5 text-[9px] font-semibold',
                !compareMode && viewSlot === v.slot_id
                  ? 'bg-primary-foreground/20 text-primary-foreground'
                  : 'bg-emerald-500/15 text-emerald-700 dark:text-emerald-300',
              )}
            >
              当前
            </span>
          )}
        </button>
      ))}
      {canCompare && (
        <button
          onClick={onToggleCompare}
          className={cn(
            'rounded-md px-3 py-1.5 font-medium transition-colors',
            compareMode
              ? 'bg-primary text-primary-foreground'
              : 'text-muted-foreground hover:bg-secondary hover:text-foreground',
          )}
        >
          对比
        </button>
      )}
    </div>
  )
}

// ----------------------- 对比视图（左右并排）-----------------------
function CompareView({
  left,
  right,
  leftLabel,
  rightLabel,
  activeSlot,
  leftSlot,
  rightSlot,
}: {
  left: SampleManifest
  right: SampleManifest
  leftLabel: string
  rightLabel: string
  activeSlot: string | null
  leftSlot?: string
  rightSlot?: string
}) {
  return (
    <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
      <div className="rounded-lg border border-border bg-card p-3">
        <CompareHeader label={leftLabel} isActive={leftSlot === activeSlot} />
        <ManifestView manifest={left} compact />
      </div>
      <div className="rounded-lg border border-border bg-card p-3">
        <CompareHeader label={rightLabel} isActive={rightSlot === activeSlot} />
        <ManifestView manifest={right} compact />
      </div>
    </div>
  )
}

function CompareHeader({ label, isActive }: { label: string; isActive: boolean }) {
  return (
    <div className="mb-3 flex items-center justify-between">
      <span className="text-sm font-semibold">{label}</span>
      {isActive && (
        <span className="rounded-full bg-emerald-500/15 px-2 py-0.5 text-[10px] font-semibold text-emerald-700 dark:text-emerald-300">
          当前
        </span>
      )}
    </div>
  )
}

// ----------------------- 重新拆解对话框 -----------------------
function RegenerateDialog({
  nlPrompt,
  onNlPromptChange,
  onSubmit,
  onCancel,
}: {
  nlPrompt: string
  onNlPromptChange: (s: string) => void
  onSubmit: () => void
  onCancel: () => void
}) {
  const remaining = NL_PROMPT_MAX - nlPrompt.length
  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 px-4"
      onClick={onCancel}
    >
      <div
        className="w-full max-w-lg rounded-lg border border-border bg-card p-5 shadow-xl"
        onClick={(e) => e.stopPropagation()}
      >
        <h3 className="text-base font-semibold">重新拆解</h3>
        <p className="mt-1 text-xs text-muted-foreground">
          可选：写一段自然语言提示（如『更看重开场』『压短结尾』『多关注产品特写』），
          LLM 视频画像和段落切分会参考它。留空也可以——直接重跑一次默认流水线。
        </p>
        <textarea
          value={nlPrompt}
          onChange={(e) => onNlPromptChange(e.target.value.slice(0, NL_PROMPT_MAX))}
          rows={5}
          placeholder="可选 · 描述你想强调什么、希望段落结构如何调整……"
          className="mt-3 w-full resize-none rounded-md border border-border bg-background px-3 py-2 text-sm"
        />
        <div className="mt-1 text-right text-[11px] text-muted-foreground">
          剩余 {remaining} 字
        </div>
        <div className="mt-4 flex justify-end gap-2">
          <button
            onClick={onCancel}
            className="rounded-md border border-border bg-card px-4 py-2 text-sm font-medium hover:bg-secondary"
          >
            取消
          </button>
          <button
            onClick={onSubmit}
            className="rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground"
          >
            开始拆解
          </button>
        </div>
      </div>
    </div>
  )
}

// ----------------------- 槽满弹窗 -----------------------
function SlotsFullDialog({
  detail,
  versions,
  onPick,
  onCancel,
}: {
  detail: SlotsFullDetail
  // 用前端 versions（含 v1/v2 label）展示，detail.versions 只用于兜底
  versions: SampleVersionInfo[]
  onPick: (slot: string) => void
  onCancel: () => void
}) {
  // 优先用前端缓存的 versions（带 v1/v2 label）；后端 detail.versions 没 label
  const list = versions.length > 0
    ? versions
    : detail.versions.map((v, i) => ({
        slot_id: v.slot_id,
        label: `v${i + 1}`,
        updated_at: v.updated_at,
        is_active: v.is_active,
      }))
  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 px-4"
      onClick={onCancel}
    >
      <div
        className="w-full max-w-lg rounded-lg border border-border bg-card p-5 shadow-xl"
        onClick={(e) => e.stopPropagation()}
      >
        <h3 className="text-base font-semibold">版本槽已满</h3>
        <p className="mt-1 text-xs text-muted-foreground">
          每个样例最多保留 {detail.max_versions} 个版本，请选一个版本替换：
        </p>
        <ul className="mt-3 space-y-2">
          {list.map((v) => (
            <li key={v.slot_id}>
              <button
                onClick={() => onPick(v.slot_id)}
                className="flex w-full items-center justify-between rounded-md border border-border bg-background px-3 py-2 text-left text-sm hover:border-primary hover:bg-primary/5"
              >
                <span className="flex items-center gap-2">
                  <span className="font-semibold">{v.label}</span>
                  {v.is_active && (
                    <span className="rounded-full bg-emerald-500/15 px-1.5 py-0.5 text-[10px] font-semibold text-emerald-700 dark:text-emerald-300">
                      当前
                    </span>
                  )}
                </span>
                <span className="text-[11px] text-muted-foreground">
                  {new Date(v.updated_at * 1000).toLocaleString()}
                </span>
              </button>
            </li>
          ))}
        </ul>
        <p className="mt-3 text-[11px] text-amber-700 dark:text-amber-300">
          所选版本会被新拆解结果覆盖，操作不可撤销。
        </p>
        <div className="mt-4 flex justify-end">
          <button
            onClick={onCancel}
            className="rounded-md border border-border bg-card px-4 py-2 text-sm font-medium hover:bg-secondary"
          >
            取消
          </button>
        </div>
      </div>
    </div>
  )
}

// ----------------------- 其他子组件（未变）-----------------------
function VideoTypePicker({
  value,
  onChange,
  disabled,
}: {
  value: VideoType
  onChange: (v: VideoType) => void
  disabled?: boolean
}) {
  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between text-xs text-muted-foreground">
        <span className="font-semibold text-foreground">视频类型</span>
        <span>决定段落 prompt（marketing / editing / motion_graph）</span>
      </div>
      <div className="grid grid-cols-1 gap-2 sm:grid-cols-3">
        {VIDEO_TYPE_OPTIONS.map((vt) => (
          <label
            key={vt}
            className={cn(
              'flex cursor-pointer flex-col gap-1 rounded-md border px-3 py-2 transition-colors',
              value === vt
                ? 'border-primary bg-primary/5'
                : 'border-border bg-background hover:bg-secondary/50',
              disabled && 'pointer-events-none opacity-60',
            )}
          >
            <div className="flex items-center gap-2 text-sm font-medium">
              <input
                type="radio"
                name="video_type"
                value={vt}
                checked={value === vt}
                onChange={() => onChange(vt)}
                disabled={disabled}
                className="accent-primary"
              />
              <span>{VIDEO_TYPE_LABEL[vt]}</span>
            </div>
            <span className="text-[11px] text-muted-foreground">{VIDEO_TYPE_HINT[vt]}</span>
          </label>
        ))}
      </div>
    </div>
  )
}

function ProgressPanel({ step, percent, note }: { step: string; percent: number; note?: string }) {
  return (
    <div className="mb-6 rounded-lg border border-border bg-card p-4">
      <div className="mb-2 flex items-center justify-between text-xs text-muted-foreground">
        <span className="font-mono">{step}</span>
        <span>{Math.round(percent)}%</span>
      </div>
      <div className="h-2 overflow-hidden rounded-full bg-secondary">
        <div
          className="h-full bg-primary transition-all duration-500"
          style={{ width: `${Math.min(100, Math.max(0, percent))}%` }}
        />
      </div>
      {note && <p className="mt-2 text-xs text-muted-foreground">{note}</p>}
    </div>
  )
}

function ManifestView({ manifest, compact = false }: { manifest: SampleManifest; compact?: boolean }) {
  const rhythmData = manifest.rhythm.times.map((t, i) => ({
    t,
    cut: manifest.rhythm.cut_density[i] ?? 0,
    bgm: manifest.rhythm.bgm_energy[i] ?? 0,
  }))

  return (
    <div className="space-y-6">
      {manifest.video_url && (
        <div className="rounded-lg border border-border bg-card p-4">
          <div className="mb-3 flex items-center justify-between">
            <h2 className="text-sm font-semibold">原始视频</h2>
            <span className="text-xs text-muted-foreground">
              {manifest.duration_seconds.toFixed(1)}s · {manifest.shots.length} 镜头
            </span>
          </div>
          <video
            src={manifest.video_url}
            controls
            preload="metadata"
            className="aspect-video w-full max-h-[420px] rounded-md bg-black"
          >
            您的浏览器不支持视频播放。
          </video>
        </div>
      )}

      {manifest.understanding && <UnderstandingCard u={manifest.understanding} />}

      <div className="rounded-lg border border-border bg-card p-4">
        <div className="mb-3 flex items-center justify-between">
          <h2 className="text-sm font-semibold">段落结构 · {VIDEO_TYPE_LABEL[manifest.video_type]}</h2>
          <span className="text-xs text-muted-foreground">
            {manifest.has_voice ? '🎙 含口播' : '🎵 纯 BGM'}
          </span>
        </div>
        <SectionsBar manifest={manifest} />
      </div>

      <div className={cn('grid grid-cols-1 gap-6', !compact && 'lg:grid-cols-2')}>
        <div className="rounded-lg border border-border bg-card p-4">
          <h2 className="mb-3 text-sm font-semibold">节奏曲线</h2>
          <div className="h-56 w-full">
            <ResponsiveContainer width="100%" height="100%">
              <LineChart data={rhythmData} margin={{ top: 8, right: 12, bottom: 8, left: 0 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="hsl(240 6% 90%)" />
                <XAxis
                  dataKey="t"
                  tickFormatter={(v: number) => `${v.toFixed(1)}s`}
                  tick={{ fontSize: 10, fill: 'hsl(240 4% 46%)' }}
                />
                <YAxis tick={{ fontSize: 10, fill: 'hsl(240 4% 46%)' }} />
                <Tooltip
                  formatter={(value) => (typeof value === 'number' ? value.toFixed(2) : String(value ?? ''))}
                  labelFormatter={(label) => (typeof label === 'number' ? `t=${label.toFixed(2)}s` : String(label))}
                  contentStyle={{ fontSize: 12 }}
                />
                <Line type="monotone" dataKey="cut" name="切镜密度" stroke="hsl(262 83% 58%)" dot={false} strokeWidth={2} />
                <Line type="monotone" dataKey="bgm" name="BGM 能量" stroke="hsl(38 92% 50%)" dot={false} strokeWidth={2} />
                {manifest.climax_position != null && (
                  <ReferenceLine
                    x={manifest.climax_position}
                    stroke="hsl(0 84% 60%)"
                    strokeDasharray="4 2"
                    label={{ value: `高潮 ${manifest.climax_position.toFixed(1)}s`, position: 'top', fill: 'hsl(0 84% 60%)', fontSize: 10 }}
                  />
                )}
              </LineChart>
            </ResponsiveContainer>
          </div>
          {manifest.rhythm.tempo_bpm != null && (
            <p className="mt-2 text-xs text-muted-foreground">
              BPM ≈ {manifest.rhythm.tempo_bpm.toFixed(0)}
              {manifest.climax_position != null && (
                <span className="ml-3 text-destructive">· 高潮约在 {manifest.climax_position.toFixed(1)}s</span>
              )}
            </p>
          )}
        </div>

        <div className="rounded-lg border border-border bg-card p-4">
          <h2 className="mb-3 text-sm font-semibold">画面包装画像</h2>
          <dl className="space-y-2 text-sm">
            <Row label="字幕样式" value={manifest.packaging.subtitle_style} />
            <Row label="标题条" value={manifest.packaging.has_title_bar ? '有' : '无'} />
            <Row label="转场" value={manifest.packaging.transition_types.join(' · ') || '—'} />
            <Row label="封面风格" value={manifest.packaging.cover_style ?? '—'} />
            <Row label="贴纸密度" value={`${(manifest.packaging.sticker_density * 100).toFixed(0)}%`} />
          </dl>
        </div>
      </div>

      <div className="rounded-lg border border-border bg-card p-4">
        <h2 className="mb-3 text-sm font-semibold">镜头切片（{manifest.shots.length}）</h2>
        <div className={cn(
          'grid gap-3',
          compact
            ? 'grid-cols-2 sm:grid-cols-3'
            : 'grid-cols-2 sm:grid-cols-3 lg:grid-cols-4 xl:grid-cols-6',
        )}>
          {manifest.shots.map((shot) => (
            <div key={shot.index} className="overflow-hidden rounded-md border border-border bg-secondary/40">
              <div
                className="aspect-video w-full bg-gradient-to-br from-secondary to-muted"
                style={{
                  backgroundImage: shot.thumbnail_url ? `url(${shot.thumbnail_url})` : undefined,
                  backgroundSize: 'cover',
                }}
              />
              <div className="space-y-1 p-2 text-[11px] leading-tight">
                <div className="flex items-center justify-between text-muted-foreground">
                  <span>#{shot.index + 1}</span>
                  <span>{shot.duration.toFixed(1)}s</span>
                </div>
                <p className="line-clamp-2 text-foreground">{shot.transcript || '（无口播）'}</p>
                {shot.tags.length > 0 && (
                  <div className="flex flex-wrap gap-1">
                    {shot.tags.slice(0, 3).map((tag) => (
                      <span key={tag} className="rounded bg-secondary px-1 py-0.5 text-[10px] text-muted-foreground">
                        {tag}
                      </span>
                    ))}
                  </div>
                )}
              </div>
            </div>
          ))}
        </div>
      </div>
    </div>
  )
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex justify-between gap-3 text-xs">
      <dt className="text-muted-foreground">{label}</dt>
      <dd className="font-medium text-foreground">{value}</dd>
    </div>
  )
}

function SectionsBar({ manifest }: { manifest: SampleManifest }) {
  const total = manifest.duration_seconds || 1
  return (
    <div className="space-y-2">
      <div className="relative flex h-12 w-full overflow-hidden rounded-md border border-border">
        {manifest.sections.map((sec, idx) => {
          const widthPct = ((sec.end - sec.start) / total) * 100
          return (
            <div
              key={idx}
              className={cn(
                'flex flex-col items-center justify-center px-1 text-[11px] font-medium leading-tight text-white',
                SECTION_BG[sec.role],
              )}
              style={{ width: `${widthPct}%` }}
              title={`${SECTION_LABEL[sec.role]} · ${sec.theme}: ${sec.summary}`}
            >
              <span className="opacity-80">{SECTION_LABEL[sec.role]}</span>
              <span className="truncate font-semibold">{sec.theme}</span>
            </div>
          )
        })}
      </div>
      <div className="space-y-1 text-xs text-muted-foreground">
        {manifest.sections.map((sec, idx) => (
          <div key={idx} className="flex gap-2">
            <span className="font-mono">{sec.start.toFixed(1)}–{sec.end.toFixed(1)}s</span>
            <span className="font-medium text-foreground">
              {SECTION_LABEL[sec.role]} · {sec.theme}：
            </span>
            <span>{sec.summary}</span>
          </div>
        ))}
      </div>
    </div>
  )
}

function UnderstandingCard({ u }: { u: VideoUnderstanding }) {
  return (
    <div className="rounded-lg border border-border bg-card p-4">
      <div className="mb-2 flex items-center justify-between">
        <h2 className="text-sm font-semibold">LLM 视频画像</h2>
        <span className="text-[11px] text-muted-foreground">建议切 {u.suggested_segments} 段</span>
      </div>
      <div className="flex flex-wrap gap-2 text-xs">
        <span className="rounded-full bg-primary/10 px-2 py-0.5 font-medium text-primary">
          {u.archetype}
        </span>
        <span className="rounded-full border border-border px-2 py-0.5 text-muted-foreground">
          基调：{u.tone}
        </span>
      </div>
      <p className="mt-3 text-sm leading-relaxed text-foreground">{u.narrative_summary}</p>
    </div>
  )
}

// 编辑态：整段 PUT 到当前 viewSlot，允许任意修改不验证（Pydantic 只做格式校验）。
function ManifestEditor({
  manifest,
  onChange,
}: {
  manifest: SampleManifest
  onChange: (next: SampleManifest) => void
}) {
  const update = (patch: Partial<SampleManifest>) => onChange({ ...manifest, ...patch })

  const updateSection = (idx: number, patch: Partial<Section>) => {
    const next = manifest.sections.map((s, i) => (i === idx ? { ...s, ...patch } : s))
    onChange({ ...manifest, sections: next })
  }

  const updateShot = (idx: number, patch: Partial<Shot>) => {
    const next = manifest.shots.map((s, i) => (i === idx ? { ...s, ...patch } : s))
    onChange({ ...manifest, shots: next })
  }

  return (
    <div className="space-y-6">
      <div className="rounded-lg border border-amber-400/40 bg-amber-50/40 p-4 text-xs text-amber-700 dark:bg-amber-950/30 dark:text-amber-300">
        编辑模式 · 改动只会写入当前查看的版本槽（就地编辑），不开新版本。点「保存编辑」提交，「取消编辑」放弃。
      </div>

      <div className="grid grid-cols-1 gap-3 rounded-lg border border-border bg-card p-4 md:grid-cols-2">
        <FieldRow label="标题">
          <input
            value={manifest.title}
            onChange={(e) => update({ title: e.target.value })}
            className="w-full rounded-md border border-border bg-background px-2 py-1 text-sm"
          />
        </FieldRow>
        <FieldRow label="视频类型">
          <select
            value={manifest.video_type}
            onChange={(e) => update({ video_type: e.target.value as VideoType })}
            className="w-full rounded-md border border-border bg-background px-2 py-1 text-sm"
          >
            {VIDEO_TYPE_OPTIONS.map((vt) => (
              <option key={vt} value={vt}>{VIDEO_TYPE_LABEL[vt]}</option>
            ))}
          </select>
        </FieldRow>
        <FieldRow label="高潮位置 (秒)">
          <input
            type="number"
            step="0.1"
            value={manifest.climax_position ?? ''}
            onChange={(e) => {
              const v = e.target.value
              update({ climax_position: v === '' ? null : Number(v) })
            }}
            className="w-full rounded-md border border-border bg-background px-2 py-1 text-sm"
          />
        </FieldRow>
        <FieldRow label="有口播">
          <input
            type="checkbox"
            checked={manifest.has_voice}
            onChange={(e) => update({ has_voice: e.target.checked })}
            className="accent-primary"
          />
        </FieldRow>
      </div>

      <div className="rounded-lg border border-border bg-card p-4">
        <h2 className="mb-3 text-sm font-semibold">段落 ({manifest.sections.length})</h2>
        <div className="space-y-3">
          {manifest.sections.map((sec, idx) => (
            <div key={idx} className="grid grid-cols-1 gap-2 rounded-md border border-border bg-background p-3 md:grid-cols-6">
              <div className="md:col-span-1">
                <Label>角色</Label>
                <span className="text-xs text-muted-foreground">{SECTION_LABEL[sec.role]}</span>
              </div>
              <div className="md:col-span-1">
                <Label>主题</Label>
                <input
                  value={sec.theme}
                  onChange={(e) => updateSection(idx, { theme: e.target.value })}
                  className="w-full rounded-md border border-border bg-background px-2 py-1 text-sm"
                />
              </div>
              <div className="md:col-span-1">
                <Label>开始 (s)</Label>
                <input
                  type="number"
                  step="0.1"
                  value={sec.start}
                  onChange={(e) => updateSection(idx, { start: Number(e.target.value) })}
                  className="w-full rounded-md border border-border bg-background px-2 py-1 text-sm"
                />
              </div>
              <div className="md:col-span-1">
                <Label>结束 (s)</Label>
                <input
                  type="number"
                  step="0.1"
                  value={sec.end}
                  onChange={(e) => updateSection(idx, { end: Number(e.target.value) })}
                  className="w-full rounded-md border border-border bg-background px-2 py-1 text-sm"
                />
              </div>
              <div className="md:col-span-2">
                <Label>摘要</Label>
                <input
                  value={sec.summary}
                  onChange={(e) => updateSection(idx, { summary: e.target.value })}
                  className="w-full rounded-md border border-border bg-background px-2 py-1 text-sm"
                />
              </div>
            </div>
          ))}
        </div>
      </div>

      <div className="grid grid-cols-1 gap-3 rounded-lg border border-border bg-card p-4 md:grid-cols-2">
        <h2 className="text-sm font-semibold md:col-span-2">画面包装</h2>
        <FieldRow label="字幕样式">
          <input
            value={manifest.packaging.subtitle_style}
            onChange={(e) => update({ packaging: { ...manifest.packaging, subtitle_style: e.target.value } })}
            className="w-full rounded-md border border-border bg-background px-2 py-1 text-sm"
          />
        </FieldRow>
        <FieldRow label="封面风格">
          <input
            value={manifest.packaging.cover_style ?? ''}
            onChange={(e) => update({
              packaging: { ...manifest.packaging, cover_style: e.target.value || null }
            })}
            className="w-full rounded-md border border-border bg-background px-2 py-1 text-sm"
          />
        </FieldRow>
        <FieldRow label="转场 (逗号分隔)">
          <input
            value={manifest.packaging.transition_types.join(', ')}
            onChange={(e) => update({
              packaging: {
                ...manifest.packaging,
                transition_types: e.target.value.split(',').map((s) => s.trim()).filter(Boolean),
              }
            })}
            className="w-full rounded-md border border-border bg-background px-2 py-1 text-sm"
          />
        </FieldRow>
        <FieldRow label="贴纸密度 (0-1)">
          <input
            type="number"
            min={0}
            max={1}
            step={0.05}
            value={manifest.packaging.sticker_density}
            onChange={(e) => update({
              packaging: { ...manifest.packaging, sticker_density: Math.max(0, Math.min(1, Number(e.target.value))) }
            })}
            className="w-full rounded-md border border-border bg-background px-2 py-1 text-sm"
          />
        </FieldRow>
        <FieldRow label="有标题条">
          <input
            type="checkbox"
            checked={manifest.packaging.has_title_bar}
            onChange={(e) => update({ packaging: { ...manifest.packaging, has_title_bar: e.target.checked } })}
            className="accent-primary"
          />
        </FieldRow>
      </div>

      <div className="rounded-lg border border-border bg-card p-4">
        <h2 className="mb-3 text-sm font-semibold">镜头 ({manifest.shots.length})</h2>
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
          {manifest.shots.map((shot, idx) => (
            <div key={shot.index} className="overflow-hidden rounded-md border border-border bg-secondary/30">
              <div
                className="aspect-video w-full bg-gradient-to-br from-secondary to-muted"
                style={{
                  backgroundImage: shot.thumbnail_url ? `url(${shot.thumbnail_url})` : undefined,
                  backgroundSize: 'cover',
                }}
              />
              <div className="space-y-2 p-2 text-[11px]">
                <div className="flex items-center justify-between text-muted-foreground">
                  <span>#{shot.index + 1}</span>
                  <span>{shot.duration.toFixed(1)}s</span>
                </div>
                <textarea
                  placeholder="口播文本（无口播留空）"
                  value={shot.transcript ?? ''}
                  onChange={(e) => updateShot(idx, { transcript: e.target.value || null })}
                  rows={2}
                  className="w-full resize-none rounded-md border border-border bg-background px-2 py-1 text-[11px]"
                />
                <input
                  placeholder="tags（逗号分隔）"
                  value={shot.tags.join(', ')}
                  onChange={(e) => updateShot(idx, {
                    tags: e.target.value.split(',').map((s) => s.trim()).filter(Boolean),
                  })}
                  className="w-full rounded-md border border-border bg-background px-2 py-1 text-[11px]"
                />
              </div>
            </div>
          ))}
        </div>
      </div>
    </div>
  )
}

function FieldRow({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="space-y-1">
      <Label>{label}</Label>
      {children}
    </div>
  )
}

function Label({ children }: { children: React.ReactNode }) {
  return <div className="text-[11px] font-medium text-muted-foreground">{children}</div>
}
