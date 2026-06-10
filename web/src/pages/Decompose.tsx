import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { LineChart, Line, XAxis, YAxis, Tooltip, ResponsiveContainer, CartesianGrid, ReferenceLine, ReferenceDot } from 'recharts'

import { api, ApiError } from '@/api/client'
import { commitStep, getStepSnapshot } from '@/api/steps'
import { createSSE, type SSEHandle } from '@/api/sse'
import { PageShell } from '@/components/layout/PageShell'
import { BgmAnalysisCard } from '@/components/compose/BgmAnalysisCard'
import { AnalysisCard } from '@/components/decompose/AnalysisCard'
import { DecomposeTable } from '@/components/decompose/DecomposeTable'
import { useProjectsStore } from '@/stores/projects'
import { useSessionStore } from '@/stores/session'
import type {
  DecomposeRequest,
  DecomposeSubmitResponse,
  LibraryItem,
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
  /**
   * stage-15: persist=false 时 slot_id=null + manifest 含完整草稿;
   * persist=true 时 slot_id 为新写入的槽 id, manifest 也回吐(老协议兼容)
   */
  payload: { sample_id: string; manifest: SampleManifest; slot_id?: string | null }
}

const DRAFT_SLOT = '__draft__' as const

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
  const draftManifests = useSessionStore((s) => s.draftManifests)
  const setDraft = useSessionStore((s) => s.setDraft)
  const clearDraft = useSessionStore((s) => s.clearDraft)
  const currentProjectId = useProjectsStore((s) => s.currentProjectId)
  const projects = useProjectsStore((s) => s.projects)
  const updateProject = useProjectsStore((s) => s.updateProject)
  const currentProject = useMemo(
    () => projects.find((p) => p.project_id === currentProjectId) ?? null,
    [projects, currentProjectId],
  )
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

  // 内嵌的系统样例 picker：空态下展示（!selectedSampleId 时）。
  // 拉一次 /library 全量，按 source==='system' 过滤，再按 chip 二次过滤。
  // chip 初值优先 currentProject.video_type → session.videoType → 'all'
  const [librarySamples, setLibrarySamples] = useState<LibraryItem[] | null>(null)
  const [libraryLoading, setLibraryLoading] = useState(false)
  const [libraryError, setLibraryError] = useState<string | null>(null)
  const [pickerChip, setPickerChip] = useState<VideoType | 'all'>(
    currentProject?.video_type ?? videoType ?? 'all',
  )

  // sampleSource:
  //   'system' = 从素材库挑的内置样例 → video_type 锁定，直接拆解
  //   'user'   = 用户上传到 server/var/uploads/decompose/<sample_id>/ 的视频 → video_type 可选
  //   null     = 没选/没传任何样例 → 引导用户去素材库或上传
  const isSystemSample = sampleSource === 'system'
  const isUserSample = sampleSource === 'user'

  // viewSlot 没显式指定时跟随 active；versions=[] 时也回 null。
  // stage-15: viewSlot === DRAFT_SLOT 时显示前端 zustand 草稿(未保存到资产库)
  const draftManifest = selectedSampleId ? draftManifests[selectedSampleId] ?? null : null
  const hasDraft = draftManifest !== null
  const effectiveViewSlot = useMemo(() => {
    if (viewSlot === DRAFT_SLOT && hasDraft) return DRAFT_SLOT
    if (versions.length === 0) return hasDraft ? DRAFT_SLOT : null
    if (viewSlot && versions.some((v) => v.slot_id === viewSlot)) return viewSlot
    return activeSlot ?? versions[versions.length - 1].slot_id
  }, [versions, viewSlot, activeSlot, hasDraft])

  const isViewingDraft = effectiveViewSlot === DRAFT_SLOT
  const currentManifest = isViewingDraft
    ? draftManifest
    : effectiveViewSlot
      ? manifestCache[effectiveViewSlot] ?? null
      : null
  // 对比模式：左旧 v1 / 右新 v2
  const compareLeft = versions[0]
  const compareRight = versions[1]
  const compareLeftManifest = compareLeft ? manifestCache[compareLeft.slot_id] ?? null : null
  const compareRightManifest = compareRight ? manifestCache[compareRight.slot_id] ?? null : null

  // session.manifest 同步——给 Compose 入口 / step snapshot 用
  useEffect(() => {
    setManifest(currentManifest)
  }, [currentManifest, setManifest])

  // 拉一次 /library 全量，按 source==='system' 过滤后存内存；chip 在前端切
  useEffect(() => {
    if (selectedSampleId) return
    let cancelled = false
    setLibraryLoading(true)
    setLibraryError(null)
    api
      .get<LibraryItem[]>('/library')
      .then((items) => {
        if (cancelled) return
        setLibrarySamples(items.filter((it) => it.source === 'system'))
        setLibraryLoading(false)
      })
      .catch((err: Error) => {
        if (cancelled) return
        setLibraryError(err.message || '加载系统样例失败')
        setLibraryLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [selectedSampleId])

  // 项目切换时根据 project.video_type 重置 chip
  const projectVideoType = currentProject?.video_type
  useEffect(() => {
    if (projectVideoType) setPickerChip(projectVideoType)
  }, [projectVideoType])

  const filteredSamples = useMemo(() => {
    if (!librarySamples) return []
    if (pickerChip === 'all') return librarySamples
    return librarySamples.filter((it) => it.video_type === pickerChip)
  }, [librarySamples, pickerChip])

  const handlePickSystemSample = useCallback(
    async (item: LibraryItem) => {
      // 进入"已选样例"态：填 session + 把 project.reference_versions 占位回写
      // slot_id 此时未知（用户还没拆解）→ 用 active_slot 或占位 'pending'
      selectSamples([item.id], [item.title], item.video_type, 'system')
      if (currentProjectId) {
        try {
          const slot = item.active_slot ?? 'pending'
          await updateProject(currentProjectId, {
            reference_versions: [{ sample_id: item.id, slot_id: slot }],
          })
        } catch {
          /* 占位回写失败不阻断；后续 commit-decompose 会重写 */
        }
      }
    },
    [currentProjectId, selectSamples, updateProject],
  )

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
        // 显式传 video_type:不传后端默认 marketing,会把所有上传归到营销分类。
        // 用 session.videoType(就是当前 Decompose 页面顶部 chip 的值)。
        fd.append('video_type', videoType)
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

  const run = useCallback(async (opts?: { nl_prompt?: string }) => {
    if (!selectedSampleId) return
    setError(null)
    // 关掉上一轮可能残留的 SSE：onDone 后浏览器仍可能投递晚到的 'error' 事件，
    // 把当前的 setError('SSE connection closed') 写脏；新一轮开始时统一清掉。
    sseRef.current?.close()
    sseRef.current = null
    // 立刻进入"拆解中"视觉态——清掉旧草稿、切到 DRAFT_SLOT 占位，
    // 否则 SSE done 来之前用户视野里仍是上一版结果，体感"按了没反应"。
    clearDraft(selectedSampleId)
    setViewSlot(DRAFT_SLOT)
    setCompareMode(false)
    setRunning(true)
    setProgress({ step: 'submit', percent: 2, note: '提交任务' })
    // stage-15:默认草稿模式;不写盘,SSE done 把完整 manifest 推回前端
    const req: DecomposeRequest = {
      sample_id: selectedSampleId,
      video_type: videoType,
      nl_prompt: opts?.nl_prompt?.trim() || null,
      persist: false,
    }
    try {
      const { job_id } = await api.post<DecomposeSubmitResponse>('/decompose', req)
      const source = createSSE<DoneEvent, ProgressEventPayload>(
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
            setProgress({ step: 'done', percent: 100, note: '完成（未保存）' })
            setRunning(false)
            // 草稿:写入 zustand,切到 DRAFT_SLOT 视图;不再 refreshStatus
            setDraft(selectedSampleId, done.payload.manifest)
            setViewSlot(DRAFT_SLOT)
            setCompareMode(false)
            // done 之后浏览器仍可能投递 'error' 事件——我们手动 close 并把
            // sseRef 清空,onError 用 ref guard 忽略晚到的关闭事件。
            sseRef.current = null
          },
          onError: (err) => {
            // sseRef 已被 onDone 清空 → 晚到的 close 事件,忽略不弄脏 UI。
            if (!sseRef.current) return
            setError(err.detail || '拆解失败')
            setRunning(false)
            sseRef.current = null
          },
        },
      )
      sseRef.current = source
    } catch (err) {
      setRunning(false)
      setError(err instanceof Error ? err.message : String(err))
    }
  }, [clearDraft, selectedSampleId, setDraft, videoType])

  /**
   * 保存当前草稿到资产库版本槽。
   * - 槽未满 → 直接 POST /save 创建新槽
   * - 槽满 → 后端返 409 slots_full,前端弹 SlotsFullDialog 让用户挑覆盖目标
   */
  const saveDraft = useCallback(
    async (replace_slot?: string) => {
      if (!selectedSampleId || !draftManifest) return
      setError(null)
      setBusy(true)
      try {
        const res = await api.post<VersionMutationResponse>(
          `/sample/${selectedSampleId}/manifest/save`,
          { manifest: draftManifest, replace_slot: replace_slot ?? null },
        )
        applyMutation(res)
        // 拉新写槽的 manifest 进缓存,然后清掉草稿
        const newSlot = res.active_slot ?? res.versions[res.versions.length - 1]?.slot_id ?? null
        if (newSlot) {
          setManifestCache((prev) => ({
            ...prev,
            [newSlot]: draftManifest,
          }))
          setViewSlot(newSlot)
        }
        clearDraft(selectedSampleId)
        setSlotsFullDialog(null)
      } catch (err) {
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
              pendingNlPrompt: '',
            })
            return
          }
        }
        setError(err instanceof Error ? err.message : '保存失败')
      } finally {
        setBusy(false)
      }
    },
    // applyMutation 在下方定义,引用 hook 顺序不影响,但要同步
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [clearDraft, draftManifest, selectedSampleId],
  )

  useEffect(() => {
    return () => {
      sseRef.current?.close()
    }
  }, [])

  // stage-15: 浏览器关闭/刷新时,若存在草稿弹原生确认框(zustand 在内存里,刷新即丢)
  useEffect(() => {
    if (!hasDraft) return
    const onBeforeUnload = (e: BeforeUnloadEvent) => {
      e.preventDefault()
      e.returnValue = ''
    }
    window.addEventListener('beforeunload', onBeforeUnload)
    return () => window.removeEventListener('beforeunload', onBeforeUnload)
  }, [hasDraft])

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

  // stage-15: 不再自动跑拆解。用户必须主动点「开始拆解」。系统样例也走草稿流程。

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

  // stage-15: 槽满弹窗的「挑要覆盖的槽」入口——保存流程触发(不再是拆解流程)
  const resumeSaveWithReplace = useCallback(
    async (slot: string) => {
      setSlotsFullDialog(null)
      await saveDraft(slot)
    },
    [saveDraft],
  )

  const handleNext = useCallback(async () => {
    if (!currentProjectId || !selectedSampleId) {
      navigate('/workshop')
      return
    }
    try {
      await commitStep(currentProjectId, 'decompose', { sample_id: selectedSampleId })
    } catch (err) {
      setError(err instanceof Error ? err.message : '保存步骤失败')
      return
    }
    navigate('/workshop')
  }, [currentProjectId, navigate, selectedSampleId])

  const hasAnyVersion = versions.length > 0
  const canCompare = versions.length === 2

  return (
    <PageShell
      title="样例拆解"
      subtitle="把一支爆款视频拆成结构，看清它怎么开场、怎么推进、怎么收尾。最多保留 2 个版本，可以并排对比。"
    >
      {/* ====== 来源块 ====== */}
      <div className="mb-6 rounded-lg border border-border bg-card p-4">
        {isSystemSample && selectedSampleId && (
          <div className="space-y-2">
            <div className="flex items-center justify-between text-xs text-muted-foreground">
              <span className="font-semibold text-foreground">来源 · 官方样例（类型已锁定）</span>
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
              <span className="font-semibold text-foreground">来源 · 你上传的视频</span>
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
            <div className="flex items-center justify-between gap-3">
              <p className="text-xs text-muted-foreground">
                从内置样例库挑一支爆款开始拆，或者上传自己的视频。
              </p>
              <button
                onClick={() => fileInputRef.current?.click()}
                disabled={uploading}
                className={cn(
                  'shrink-0 rounded-md border border-dashed border-border bg-background px-3 py-1.5 text-xs hover:border-primary hover:bg-primary/5',
                  uploading && 'cursor-not-allowed opacity-60',
                )}
              >
                {uploading ? '上传中…' : '上传自己的视频'}
              </button>
            </div>

            {/* video_type chip 过滤栏 */}
            <div className="flex flex-wrap items-center gap-1.5">
              <span className="text-[11px] text-muted-foreground">按种类筛选：</span>
              {(['all', ...VIDEO_TYPE_OPTIONS] as const).map((chip) => (
                <button
                  key={chip}
                  onClick={() => setPickerChip(chip)}
                  className={cn(
                    'rounded-full border px-2.5 py-1 text-[11px] font-medium transition-colors',
                    pickerChip === chip
                      ? 'border-primary bg-primary text-primary-foreground'
                      : 'border-border bg-card text-muted-foreground hover:bg-secondary',
                  )}
                >
                  {chip === 'all' ? '全部' : VIDEO_TYPE_LABEL[chip]}
                </button>
              ))}
              {currentProject?.video_type && (
                <span className="ml-2 text-[11px] text-muted-foreground">
                  · 项目预选「{VIDEO_TYPE_LABEL[currentProject.video_type]}」
                </span>
              )}
            </div>

            {/* 系统样例网格 */}
            {libraryLoading && (
              <p className="text-xs text-muted-foreground">加载样例库…</p>
            )}
            {libraryError && (
              <p className="text-xs text-destructive">{libraryError}</p>
            )}
            {!libraryLoading && !libraryError && filteredSamples.length === 0 && (
              <p className="rounded-md border border-dashed border-border bg-background/50 px-4 py-6 text-center text-xs text-muted-foreground">
                该种类下暂无系统样例。试试切到其它 chip，或直接上传自己的视频。
              </p>
            )}
            {filteredSamples.length > 0 && (
              <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-4">
                {filteredSamples.map((item) => (
                  <button
                    key={item.id}
                    onClick={() => void handlePickSystemSample(item)}
                    className="group flex flex-col overflow-hidden rounded-lg border border-border bg-background text-left transition-all hover:-translate-y-0.5 hover:border-primary hover:shadow-md"
                  >
                    <div
                      className="aspect-video w-full bg-gradient-to-br from-secondary to-muted"
                      style={{
                        backgroundImage: item.cover_url ? `url(${item.cover_url})` : undefined,
                        backgroundSize: 'cover',
                        backgroundPosition: 'center',
                      }}
                    />
                    <div className="space-y-1 p-2.5">
                      <div className="line-clamp-1 text-xs font-semibold">{item.title}</div>
                      <div className="flex items-center gap-1.5 text-[10px] text-muted-foreground">
                        <span className="rounded-full bg-primary/10 px-1.5 py-0.5 font-medium text-primary">
                          {VIDEO_TYPE_LABEL[item.video_type]}
                        </span>
                        <span>{item.duration_seconds.toFixed(0)}s · {item.shot_count} 镜头</span>
                      </div>
                    </div>
                  </button>
                ))}
              </div>
            )}

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
        {selectedSampleId && !isEditing && (
          <button
            onClick={() => {
              if (hasAnyVersion || hasDraft) openRegenerate()
              else void run()
            }}
            disabled={running || busy}
            className={cn(
              'rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground transition-opacity',
              (running || busy) && 'cursor-not-allowed opacity-60',
            )}
          >
            {hasAnyVersion || hasDraft ? '重新拆解' : '开始拆解'}
          </button>
        )}

        {/* stage-15: 草稿存在 → 显式保存 / 丢弃入口 */}
        {hasDraft && !isEditing && (
          <>
            <button
              onClick={() => void saveDraft()}
              disabled={running || busy}
              className="rounded-md border border-emerald-400/60 bg-emerald-500 px-4 py-2 text-sm font-medium text-white hover:bg-emerald-400 disabled:opacity-60"
              title="把当前草稿写入资产库"
            >
              {busy ? '保存中…' : '保存到资产库'}
            </button>
            <button
              onClick={() => {
                if (!selectedSampleId) return
                if (window.confirm('丢弃当前草稿？此版本将不会保留。')) {
                  clearDraft(selectedSampleId)
                  setViewSlot(null)
                }
              }}
              disabled={running || busy}
              className="rounded-md border border-border px-3 py-2 text-xs text-muted-foreground hover:bg-destructive/10 hover:text-destructive disabled:opacity-60"
            >
              丢弃草稿
            </button>
          </>
        )}

        {hasAnyVersion && !isEditing && !isViewingDraft && (
          <>
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
            {hasDraft && (
              <span className="ml-2 rounded-full bg-amber-500/15 px-2 py-0.5 text-[10px] font-semibold text-amber-700 dark:text-amber-300">
                有未保存草稿
              </span>
            )}
          </span>
        )}

        {hasAnyVersion && !isEditing && !compareMode && !hasDraft && (
          <button
            onClick={() => void handleNext()}
            className="ml-auto rounded-md border border-border bg-card px-4 py-2 text-sm font-medium hover:bg-secondary"
          >
            下一步 · 去视频工坊 →
          </button>
        )}

        {error && (
          <span className="text-sm text-destructive">{error}</span>
        )}
      </div>

      {/* ====== 版本 tabs ====== */}
      {(hasAnyVersion || hasDraft) && !isEditing && (
        <VersionTabs
          versions={versions}
          activeSlot={activeSlot}
          viewSlot={effectiveViewSlot}
          compareMode={compareMode}
          canCompare={canCompare}
          hasDraft={hasDraft}
          onPick={(slot) => {
            setCompareMode(false)
            setViewSlot(slot)
          }}
          onToggleCompare={() => setCompareMode((v) => !v)}
        />
      )}

      {isViewingDraft && (
        <div className="mb-3 flex flex-wrap items-center gap-2 rounded-md border border-amber-400/40 bg-amber-50/50 px-3 py-2 text-xs text-amber-800 dark:bg-amber-950/30 dark:text-amber-200">
          <span className="rounded-full bg-amber-500/20 px-2 py-0.5 text-[10px] font-semibold">未保存</span>
          <span>当前结果只在浏览器里;关掉页面或换样例就会丢失。点「保存到资产库」入版本。</span>
        </div>
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
          onPick={(slot) => void resumeSaveWithReplace(slot)}
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
  hasDraft,
  onPick,
  onToggleCompare,
}: {
  versions: SampleVersionInfo[]
  activeSlot: string | null
  viewSlot: string | null
  compareMode: boolean
  canCompare: boolean
  hasDraft: boolean
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
      {hasDraft && (
        <button
          onClick={() => onPick(DRAFT_SLOT)}
          className={cn(
            'inline-flex items-center gap-1.5 rounded-md px-3 py-1.5 font-medium transition-colors',
            !compareMode && viewSlot === DRAFT_SLOT
              ? 'bg-amber-500 text-white'
              : 'text-amber-700 hover:bg-amber-100 dark:text-amber-300 dark:hover:bg-amber-900/40',
          )}
        >
          草稿
          <span
            className={cn(
              'rounded-full px-1.5 py-0.5 text-[9px] font-semibold',
              !compareMode && viewSlot === DRAFT_SLOT
                ? 'bg-white/30 text-white'
                : 'bg-amber-500/20 text-amber-800 dark:text-amber-200',
            )}
          >
            未保存
          </span>
        </button>
      )}
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
          可选：用一两句话告诉 AI 你想强调什么（比如「更看重开场」「压短结尾」「多关注产品特写」），
          它会照着这个偏好重新切段、写视频画像。留空也行——直接按默认重新跑一次。
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
        <h3 className="text-base font-semibold">版本已满</h3>
        <p className="mt-1 text-xs text-muted-foreground">
          每个样例最多保留 {detail.max_versions} 个版本，请选一个覆盖：
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
        <span>决定段落怎么写（营销 / 剪辑 / 动画）</span>
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

function nearestIndex(times: number[], t: number): number {
  if (!times.length) return -1
  let best = 0
  let bestD = Math.abs(times[0] - t)
  for (let i = 1; i < times.length; i++) {
    const d = Math.abs(times[i] - t)
    if (d < bestD) {
      bestD = d
      best = i
    }
  }
  return best
}

function ManifestView({ manifest, compact = false }: { manifest: SampleManifest; compact?: boolean }) {
  const emotion = manifest.rhythm.emotion ?? null
  const moodCurve = manifest.rhythm.mood_curve ?? []
  // 优先用 stage-28 LLM emotion.points；fallback mood_curve（老 manifest 兼容）
  const useEmotion = emotion && emotion.points.length > 0
  const rhythmData = useEmotion
    ? emotion!.points.map((pt) => {
        // 按 t 在 rhythm.times 里近邻找一根 bgm 能量参考
        const i = nearestIndex(manifest.rhythm.times, pt.t)
        return {
          t: pt.t,
          mood: pt.intensity,
          bgm: i >= 0 ? manifest.rhythm.bgm_energy[i] ?? 0 : 0,
        }
      })
    : manifest.rhythm.times.map((t, i) => ({
        t,
        mood: moodCurve[i] ?? 0,
        bgm: manifest.rhythm.bgm_energy[i] ?? 0,
      }))
  const fitScore = manifest.rhythm.bgm_fit_score
  const fitNote = manifest.rhythm.bgm_fit_note ?? ''
  const hasMood = useEmotion || moodCurve.length > 0

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
          <div className="mb-3 flex items-center justify-between">
            <h2 className="text-sm font-semibold">情绪走势 · BGM 契合度</h2>
            {fitScore != null && (
              <span
                className={cn(
                  'rounded-full px-2 py-0.5 text-[11px] font-medium',
                  fitScore >= 0.65
                    ? 'bg-emerald-500/15 text-emerald-700 dark:text-emerald-300'
                    : fitScore >= 0.45
                      ? 'bg-amber-500/15 text-amber-700 dark:text-amber-300'
                      : 'bg-rose-500/15 text-rose-700 dark:text-rose-300',
                )}
              >
                契合度 {Math.round(fitScore * 100)}%
              </span>
            )}
          </div>
          <div className="h-56 w-full">
            <ResponsiveContainer width="100%" height="100%">
              <LineChart data={rhythmData} margin={{ top: 8, right: 12, bottom: 8, left: 0 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="hsl(240 6% 90%)" />
                <XAxis
                  dataKey="t"
                  tickFormatter={(v: number) => `${v.toFixed(1)}s`}
                  tick={{ fontSize: 10, fill: 'hsl(240 4% 46%)' }}
                />
                <YAxis
                  domain={[0, 1]}
                  tickFormatter={(v: number) => `${Math.round(v * 100)}%`}
                  tick={{ fontSize: 10, fill: 'hsl(240 4% 46%)' }}
                />
                <Tooltip
                  formatter={(value, name) => {
                    if (typeof value !== 'number') return String(value ?? '')
                    return [`${Math.round(value * 100)}%`, name === 'mood' ? '情绪走势' : 'BGM 能量']
                  }}
                  labelFormatter={(label) => (typeof label === 'number' ? `t=${label.toFixed(2)}s` : String(label))}
                  contentStyle={{ fontSize: 12 }}
                />
                {hasMood && (
                  <Line
                    type="monotone"
                    dataKey="mood"
                    name="mood"
                    stroke={useEmotion ? 'hsl(265 87% 56%)' : 'hsl(217 91% 60%)'}
                    dot={false}
                    strokeWidth={useEmotion ? 3 : 2.5}
                    isAnimationActive={false}
                  />
                )}
                <Line
                  type="monotone"
                  dataKey="bgm"
                  name="bgm"
                  stroke="hsl(240 5% 65%)"
                  dot={false}
                  strokeWidth={1.5}
                  strokeOpacity={0.6}
                  strokeDasharray={useEmotion ? '4 3' : undefined}
                  isAnimationActive={false}
                />
                {manifest.climax_position != null && (
                  <ReferenceLine
                    x={manifest.climax_position}
                    stroke="hsl(0 84% 60%)"
                    strokeDasharray="4 2"
                    label={{ value: `高潮 ${manifest.climax_position.toFixed(1)}s`, position: 'top', fill: 'hsl(0 84% 60%)', fontSize: 10 }}
                  />
                )}
                {useEmotion &&
                  emotion!.peaks.map((pk, i) => (
                    <ReferenceDot
                      key={`peak-${i}`}
                      x={pk.t}
                      y={pk.intensity}
                      r={5}
                      fill="hsl(0 84% 55%)"
                      stroke="white"
                      strokeWidth={1.5}
                    >
                      <title>{`高潮 t=${pk.t.toFixed(1)}s · ${(pk.intensity * 100).toFixed(0)}%${pk.reason ? ` · ${pk.reason}` : ''}`}</title>
                    </ReferenceDot>
                  ))}
                {useEmotion &&
                  emotion!.valleys.map((vy, i) => (
                    <ReferenceDot
                      key={`valley-${i}`}
                      x={vy.t}
                      y={vy.intensity}
                      r={4}
                      fill="hsl(240 5% 50%)"
                      stroke="white"
                      strokeWidth={1.5}
                    >
                      <title>{`低谷 t=${vy.t.toFixed(1)}s · ${(vy.intensity * 100).toFixed(0)}%${vy.reason ? ` · ${vy.reason}` : ''}`}</title>
                    </ReferenceDot>
                  ))}
              </LineChart>
            </ResponsiveContainer>
          </div>
          <div className="mt-2 flex flex-wrap items-center gap-3 text-[11px] text-muted-foreground">
            <span className="inline-flex items-center gap-1">
              <span
                className={cn(
                  'inline-block h-0.5 w-4',
                  useEmotion ? 'bg-violet-600' : 'bg-blue-500',
                )}
              />
              {useEmotion ? '综合情绪强度（LLM 多信号打分）' : '情绪走势（按段落结构低频平滑）'}
            </span>
            <span className="inline-flex items-center gap-1">
              <span className={cn('inline-block h-0.5 w-4 bg-slate-400/70', useEmotion && 'border-b border-dashed')} /> BGM 能量（参考）
            </span>
            {useEmotion && emotion!.peaks.length > 0 && (
              <span className="inline-flex items-center gap-1">
                <span className="inline-block h-2 w-2 rounded-full bg-rose-600" /> 高潮 ×{emotion!.peaks.length}
              </span>
            )}
            {useEmotion && emotion!.valleys.length > 0 && (
              <span className="inline-flex items-center gap-1">
                <span className="inline-block h-2 w-2 rounded-full bg-slate-500" /> 低谷 ×{emotion!.valleys.length}
              </span>
            )}
          </div>
          {useEmotion && emotion!.summary && (
            <p className="mt-2 rounded-md bg-violet-500/10 px-2 py-1.5 text-xs leading-relaxed text-violet-900 dark:text-violet-200">
              {emotion!.summary}
            </p>
          )}
          {useEmotion && (emotion!.signals_used?.length ?? 0) > 0 && (
            <div className="mt-2 flex flex-wrap items-center gap-1">
              <span className="text-[10px] text-muted-foreground">参与打分信号：</span>
              {emotion!.signals_used!.map((s) => (
                <span
                  key={s}
                  className="rounded-full bg-secondary/60 px-1.5 py-0.5 text-[10px] text-muted-foreground"
                >
                  {s}
                </span>
              ))}
              {emotion!.backend === 'rule_fallback' && (
                <span className="rounded-full bg-amber-500/15 px-1.5 py-0.5 text-[10px] text-amber-700 dark:text-amber-300">
                  规则兜底
                </span>
              )}
            </div>
          )}
          {fitNote && (
            <p className="mt-2 rounded-md bg-secondary/40 px-2 py-1.5 text-xs leading-relaxed text-muted-foreground">
              {fitNote}
            </p>
          )}
          {manifest.audio_understanding && (
            <div className="mt-3 border-t border-border/60 pt-3">
              <BgmAnalysisCard
                analysis={manifest.audio_understanding}
                leftTitle="音轨理解"
                leftSubtitle="AI 听完整段音轨的解读"
                fitHint="AI 判断音轨能量与视频题材的契合度（0-100%）"
                variant="sample"
              />
            </div>
          )}
        </div>

        <div className="rounded-lg border border-border bg-card p-4">
          <h2 className="mb-3 text-sm font-semibold">画面包装</h2>
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
        <h2 className="mb-3 text-sm font-semibold">全片复盘</h2>
        <AnalysisCard analysis={manifest.analysis} />
      </div>

      <div className="rounded-lg border border-border bg-card p-4">
        <h2 className="mb-3 text-sm font-semibold">
          拆解表（{manifest.shots.length} 分镜 · {manifest.sections.length} 段）
        </h2>
        <DecomposeTable manifest={manifest} />
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
  // 每段镜头数:用 main_track shot.section_id 关联回不来,这里直接按 sec.start/end 数 shots
  const shotCountFor = (sec: { start: number; end: number }): number =>
    manifest.shots.filter(
      (sh) => sh.start < sec.end - 0.01 && sh.end > sec.start + 0.01,
    ).length

  return (
    <div className="space-y-3">
      {/* 时间比例条:按段时长比例的色块,极短段只画色条不写文字（写了也溢出） */}
      <div className="relative flex h-11 w-full gap-[2px] overflow-hidden rounded-md">
        {manifest.sections.map((sec, idx) => {
          const widthPct = ((sec.end - sec.start) / total) * 100
          // 宽度 < 7% 几乎没空间放任何文字,只显示色块 + tooltip
          const hasRoom = widthPct >= 7
          return (
            <div
              key={idx}
              className={cn(
                'flex min-w-0 flex-col items-center justify-center px-1.5 text-[11px] leading-tight text-white',
                SECTION_BG[sec.role],
              )}
              style={{ width: `${widthPct}%` }}
              title={`${SECTION_LABEL[sec.role]} · ${sec.theme}（${sec.start.toFixed(1)}–${sec.end.toFixed(1)}s）：${sec.summary}`}
            >
              {hasRoom ? (
                <>
                  <span className="truncate text-[10px] opacity-75">
                    {SECTION_LABEL[sec.role]}
                  </span>
                  <span className="w-full truncate text-center font-semibold">
                    {sec.theme}
                  </span>
                </>
              ) : (
                <span className="text-[10px] font-bold opacity-90">{idx + 1}</span>
              )}
            </div>
          )
        })}
      </div>

      {/* 段落详情列表:网格化对齐——色点 / 时间窗 / 角色·主题 / summary / 镜头数 */}
      <ul className="divide-y divide-border/60 overflow-hidden rounded-md border border-border/60">
        {manifest.sections.map((sec, idx) => {
          const dur = sec.end - sec.start
          const shots = shotCountFor(sec)
          return (
            <li
              key={idx}
              className="grid grid-cols-[auto_minmax(7rem,auto)_minmax(8rem,1fr)_auto] items-start gap-x-3 gap-y-1 px-3 py-2 text-xs hover:bg-muted/30"
            >
              {/* 1) 色点 + 序号 */}
              <div className="flex items-center gap-2 pt-0.5">
                <span
                  className={cn('h-2.5 w-2.5 shrink-0 rounded-full', SECTION_BG[sec.role])}
                  aria-hidden
                />
                <span className="font-mono text-[10px] text-muted-foreground">
                  #{idx + 1}
                </span>
              </div>
              {/* 2) 时间窗 */}
              <div className="font-mono text-[11px] text-muted-foreground tabular-nums">
                {sec.start.toFixed(1)}–{sec.end.toFixed(1)}s
                <span className="ml-1 text-muted-foreground/60">·{dur.toFixed(1)}s</span>
              </div>
              {/* 3) 角色徽标 + 主题 + summary（同列折叠展开） */}
              <div className="min-w-0">
                <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
                  <span className="rounded bg-muted px-1.5 py-0.5 font-mono text-[10px] uppercase text-muted-foreground">
                    {SECTION_LABEL[sec.role]}
                  </span>
                  <span className="font-medium text-foreground">{sec.theme}</span>
                </div>
                {sec.summary && (
                  <p className="mt-0.5 leading-relaxed text-muted-foreground">
                    {sec.summary}
                  </p>
                )}
              </div>
              {/* 4) 镜头数右对齐 */}
              <span className="rounded-full bg-muted/60 px-2 py-0.5 text-[10px] font-medium text-muted-foreground">
                {shots} 镜头
              </span>
            </li>
          )
        })}
      </ul>
    </div>
  )
}

function UnderstandingCard({ u }: { u: VideoUnderstanding }) {
  return (
    <div className="rounded-lg border border-border bg-card p-4">
      <div className="mb-2 flex items-center justify-between">
        <h2 className="text-sm font-semibold">视频画像</h2>
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
        编辑模式 · 改动会直接覆盖当前查看的版本，不会新开一个版本。点「保存编辑」提交，「取消编辑」放弃。
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
                <input
                  placeholder="对象（具象名词，如『青铜器残片』）"
                  value={shot.subject ?? ''}
                  onChange={(e) => updateShot(idx, { subject: e.target.value.slice(0, 40) })}
                  className="w-full rounded-md border border-sky-500/40 bg-background px-2 py-1 text-[11px] font-semibold focus:border-sky-500 focus:outline-none"
                  title="本镜画面主体——禁比喻 / 上位词 / 营销词；下游 AIGC 会原样使用"
                />
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
