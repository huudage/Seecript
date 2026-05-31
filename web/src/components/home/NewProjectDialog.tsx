import { useEffect, useMemo, useRef, useState } from 'react'

import { api, ApiError } from '@/api/client'
import { commitStep } from '@/api/steps'
import { VIDEO_TYPE_HINT, VIDEO_TYPE_LABEL } from '@/lib/sections'
import { cn } from '@/lib/utils'
import { useProjectsStore } from '@/stores/projects'
import type { LibraryItem, SampleId, VideoType } from '@/types/schemas'

/**
 * 新建项目向导（两步）：
 * 1. 先选视频类型（marketing / editing / motion_graph）—— 类型决定后续的样例池
 * 2. 再选 1-2 个参考样例：跨『系统 / 我的样例库』多选；我的样例库支持 `+ 上传到样例库`
 *    内联上传新视频。选满 2 个再点会 FIFO 替换最早选的那个。
 *    → 起项目名 → POST /api/project
 *
 * 多样例语义：两个样例的段落结构会被合并成对等参考池喂给 LLM 改编，
 * 让创作者借两种节奏的灵感组合出第三种结构。
 */

interface DecomposeUploadResponse {
  sample_id: string
  filename: string
  size_bytes: number
  video_url: string
}

type SourceTab = 'system' | 'user'

const MAX_SAMPLES = 2

export function NewProjectDialog({
  onClose,
  onCreated,
}: {
  onClose: () => void
  onCreated: (projectId: string) => void
}) {
  const createProject = useProjectsStore((s) => s.createProject)

  // ===== Step state =====
  const [step, setStep] = useState<1 | 2>(1)
  const [videoType, setVideoType] = useState<VideoType | null>(null)

  // ===== Step 2 state =====
  const [tab, setTab] = useState<SourceTab>('system')
  const [items, setItems] = useState<LibraryItem[] | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)
  // 已选样例 ID 列表（最多 2 个），顺序就是 A/B 位（先选 A，再选 B）
  const [sampleIds, setSampleIds] = useState<SampleId[]>([])
  const [name, setName] = useState('')

  // 内联上传 state（挂在『我的样例库』tab 顶部）
  const [uploading, setUploading] = useState(false)
  const [uploadError, setUploadError] = useState<string | null>(null)
  const fileInputRef = useRef<HTMLInputElement | null>(null)

  // 提交项目
  const [submitting, setSubmitting] = useState(false)
  const [submitError, setSubmitError] = useState<string | null>(null)

  const reloadLibrary = async () => {
    try {
      const data = await api.get<LibraryItem[]>('/library')
      setItems(data)
    } catch (err) {
      setLoadError(err instanceof Error ? err.message : '加载样例失败')
    }
  }

  // 进入 step 2 后拉样例列表
  useEffect(() => {
    if (step !== 2) return
    let cancelled = false
    setItems(null)
    setLoadError(null)
    api
      .get<LibraryItem[]>('/library')
      .then((data) => {
        if (cancelled) return
        setItems(data)
      })
      .catch((err: ApiError | Error) => {
        if (!cancelled) setLoadError(err.message || '加载样例失败')
      })
    return () => {
      cancelled = true
    }
  }, [step])

  // 按 video_type 过滤
  const filteredSystem = useMemo(
    () => (items ?? []).filter((it) => it.source === 'system' && it.video_type === videoType),
    [items, videoType],
  )
  const filteredUser = useMemo(
    () => (items ?? []).filter((it) => it.source === 'user' && it.video_type === videoType),
    [items, videoType],
  )

  // 选中样例时自动填项目名（用户没改过的话）—— 用『A 位』样例标题
  const userTouchedName = useRef(false)
  const selectedItems = useMemo(() => {
    const byId = new Map((items ?? []).map((it) => [it.id, it]))
    return sampleIds.map((id) => byId.get(id)).filter((x): x is LibraryItem => !!x)
  }, [items, sampleIds])

  useEffect(() => {
    if (!userTouchedName.current && selectedItems[0]) {
      setName(selectedItems[0].title.slice(0, 80))
    }
  }, [selectedItems])

  // ===== Handlers =====
  const onPickType = (vt: VideoType) => {
    setVideoType(vt)
    setStep(2)
  }

  const onBackToStep1 = () => {
    setStep(1)
    setSampleIds([])
    setUploadError(null)
    setTab('system')
    userTouchedName.current = false
    setName('')
  }

  // 多选切换：已选则移除；未选则追加；追加后超过 2 个时 FIFO 替换队首
  const togglePick = (id: SampleId) => {
    setSampleIds((prev) => {
      if (prev.includes(id)) {
        return prev.filter((x) => x !== id)
      }
      const next = [...prev, id]
      if (next.length > MAX_SAMPLES) {
        return next.slice(next.length - MAX_SAMPLES)
      }
      return next
    })
  }

  const onUploadFile = async (file: File | null) => {
    if (!file || !videoType) return
    setUploadError(null)
    setUploading(true)
    try {
      const fd = new FormData()
      fd.append('file', file)
      fd.append('video_type', videoType)
      fd.append('title', file.name.replace(/\.[^.]+$/, ''))
      const resp = await api.post<DecomposeUploadResponse>('/decompose/upload', fd)
      // 上传成功后刷新样例列表，新条目自动并入 user tab；不自动选中（让用户自己点）
      await reloadLibrary()
      // 切到 user tab 让用户能看见自己刚传的
      setTab('user')
      // 自动把刚上传的纳入选择（替代 FIFO 行为），让用户少点一下
      togglePick(resp.sample_id)
    } catch (err) {
      setUploadError(err instanceof Error ? err.message : '上传失败')
    } finally {
      setUploading(false)
    }
  }

  const onConfirm = async () => {
    setSubmitError(null)
    if (sampleIds.length < 1) {
      setSubmitError('请至少挑一个参考样例')
      return
    }
    if (sampleIds.length > MAX_SAMPLES) {
      setSubmitError(`最多挑 ${MAX_SAMPLES} 个参考样例`)
      return
    }
    const finalName = name.trim()
    if (!finalName) {
      setSubmitError('请输入项目名')
      return
    }
    setSubmitting(true)
    try {
      const created = await createProject(finalName, sampleIds)
      // 创建后立即把 library 步骤打 saved——用户已经做出了「选样例」的决定，
      // 顶部导航 nav 才会显示 library=saved + current_step=decompose
      try {
        await commitStep(created.project_id, 'library', { sample_ids: sampleIds })
      } catch (err) {
        // commit 失败不阻断进入项目（项目已建好）；nav 顶栏会显示 library=pending，
        // 用户回 library 再点一次样例可再触发 commit
        console.warn('[NewProjectDialog] commit library step failed:', err)
      }
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
      if (e.key === 'Escape' && !submitting && !uploading) onClose()
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [onClose, submitting, uploading])

  // ===== Render =====
  const VIDEO_TYPES: VideoType[] = ['marketing', 'editing', 'motion_graph']

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 px-4 py-8"
      onClick={() => {
        if (!submitting && !uploading) onClose()
      }}
    >
      <div
        className="relative flex w-full max-w-3xl flex-col overflow-hidden rounded-lg border border-border bg-card shadow-xl"
        onClick={(e) => e.stopPropagation()}
      >
        {/* ========== Header ========== */}
        <header className="flex items-center justify-between border-b border-border px-5 py-3">
          <div>
            <h3 className="text-sm font-semibold">
              新建项目
              <span className="ml-2 text-xs font-normal text-muted-foreground">
                {step === 1 ? '步骤 1 / 2 · 选类型' : '步骤 2 / 2 · 选样例（1-2 个）'}
              </span>
            </h3>
            <p className="text-xs text-muted-foreground">
              {step === 1
                ? '类型决定后续样例池 + 上传视频的默认风格'
                : `已选：${VIDEO_TYPE_LABEL[videoType!]}　·　最多挑 ${MAX_SAMPLES} 个样例（A/B 位会被合并成对等参考池喂给 LLM）`}
            </p>
          </div>
          <button
            type="button"
            onClick={onClose}
            disabled={submitting || uploading}
            aria-label="关闭"
            className="ml-3 flex h-8 w-8 items-center justify-center rounded-md text-muted-foreground hover:bg-secondary hover:text-foreground disabled:opacity-50"
          >
            <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} className="h-4 w-4">
              <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
            </svg>
          </button>
        </header>

        {/* ========== Body ========== */}
        <div className="flex-1 space-y-4 overflow-y-auto px-5 py-4">
          {step === 1 && (
            <div className="grid gap-3 sm:grid-cols-3">
              {VIDEO_TYPES.map((vt) => (
                <button
                  key={vt}
                  type="button"
                  onClick={() => onPickType(vt)}
                  className={cn(
                    'flex flex-col gap-2 rounded-lg border p-4 text-left transition-all',
                    'border-border bg-card hover:border-primary hover:bg-primary/5',
                  )}
                >
                  <div className="text-sm font-semibold">{VIDEO_TYPE_LABEL[vt]}</div>
                  <div className="text-[11px] leading-snug text-muted-foreground">
                    {VIDEO_TYPE_HINT[vt]}
                  </div>
                </button>
              ))}
            </div>
          )}

          {step === 2 && (
            <div className="space-y-3">
              {/* 多选提示条 */}
              <div className="flex items-center justify-between rounded-md border border-border bg-background/40 px-3 py-2 text-[11px] text-muted-foreground">
                <span>
                  已选 <span className="font-semibold text-foreground">{sampleIds.length} / {MAX_SAMPLES}</span> 个样例
                  {sampleIds.length === 2 && <span className="ml-1">· 再点会替换最早选的</span>}
                </span>
                <span className="text-[10px]">提交后两份结构会被整合到 LLM 改编</span>
              </div>

              {/* Tabs（只剩 system / user 两个，移除 upload tab） */}
              <div className="flex gap-1 rounded-md border border-border bg-background/40 p-1 text-xs">
                {(['system', 'user'] as const).map((t) => (
                  <button
                    key={t}
                    type="button"
                    onClick={() => setTab(t)}
                    className={cn(
                      'flex-1 rounded px-3 py-1.5 transition-colors',
                      tab === t
                        ? 'bg-primary text-primary-foreground'
                        : 'text-muted-foreground hover:bg-secondary hover:text-foreground',
                    )}
                  >
                    {t === 'system' ? '系统样例' : '我的样例库'}
                  </button>
                ))}
              </div>

              {/* Tab content */}
              {tab === 'system' && (
                <SamplePicker
                  items={filteredSystem}
                  loading={items === null}
                  error={loadError}
                  sampleIds={sampleIds}
                  onPick={togglePick}
                  emptyHint={`暂无 ${VIDEO_TYPE_LABEL[videoType!]} 类型的系统样例`}
                />
              )}

              {tab === 'user' && (
                <div className="space-y-2">
                  {/* 用户库顶部内联上传按钮 */}
                  <div className="flex items-center justify-between">
                    <span className="text-[11px] text-muted-foreground">
                      只能从已入库的样例里选；新视频请先用『+ 上传到样例库』入库
                    </span>
                    <input
                      ref={fileInputRef}
                      type="file"
                      accept="video/mp4,video/quicktime,video/webm"
                      className="hidden"
                      onChange={(e) => onUploadFile(e.target.files?.[0] ?? null)}
                    />
                    <button
                      type="button"
                      onClick={() => fileInputRef.current?.click()}
                      disabled={uploading}
                      className={cn(
                        'rounded-md border border-primary/40 bg-primary/5 px-3 py-1 text-[11px] font-medium text-primary hover:bg-primary/10',
                        uploading && 'cursor-wait opacity-60',
                      )}
                    >
                      {uploading ? '上传中…' : '+ 上传到样例库'}
                    </button>
                  </div>
                  {uploadError && (
                    <p className="rounded-md border border-destructive/40 bg-destructive/5 px-3 py-2 text-xs text-destructive">
                      {uploadError}
                    </p>
                  )}
                  <SamplePicker
                    items={filteredUser}
                    loading={items === null}
                    error={loadError}
                    sampleIds={sampleIds}
                    onPick={togglePick}
                    emptyHint={`你还没有 ${VIDEO_TYPE_LABEL[videoType!]} 类型的样例，点上方『+ 上传到样例库』加一个`}
                  />
                </div>
              )}

              {/* 项目名 */}
              <div className="space-y-1.5 pt-1">
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
                <p className="text-[11px] text-muted-foreground">最长 80 字。后续可在首页双击改名。</p>
              </div>
            </div>
          )}
        </div>

        {/* ========== Footer ========== */}
        <footer className="flex items-center justify-between gap-3 border-t border-border bg-card/50 px-5 py-3">
          <div className="flex-1">
            {submitError && <p className="text-xs text-destructive">{submitError}</p>}
            {!submitError && step === 2 && selectedItems.length > 0 && (
              <p className="text-[11px] text-muted-foreground">
                已选样例：
                {selectedItems.map((it, i) => (
                  <span key={it.id} className="ml-1">
                    <span className="rounded-sm bg-primary/15 px-1 py-0.5 text-[10px] font-bold text-primary">
                      {String.fromCharCode(65 + i)}
                    </span>
                    <span className="ml-1 font-medium text-foreground">{it.title}</span>
                    {i < selectedItems.length - 1 && <span className="text-muted-foreground">、</span>}
                  </span>
                ))}
              </p>
            )}
          </div>
          <div className="flex gap-2">
            {step === 2 && (
              <button
                type="button"
                onClick={onBackToStep1}
                disabled={submitting || uploading}
                className="rounded-md border border-border bg-background px-4 py-1.5 text-sm hover:bg-secondary disabled:opacity-50"
              >
                ← 上一步
              </button>
            )}
            <button
              type="button"
              onClick={onClose}
              disabled={submitting || uploading}
              className="rounded-md border border-border bg-background px-4 py-1.5 text-sm hover:bg-secondary disabled:opacity-50"
            >
              取消
            </button>
            {step === 2 && (
              <button
                type="button"
                onClick={onConfirm}
                disabled={submitting || uploading || sampleIds.length < 1 || !name.trim()}
                className={cn(
                  'rounded-md bg-primary px-4 py-1.5 text-sm font-medium text-primary-foreground hover:bg-primary/90',
                  (submitting || uploading || sampleIds.length < 1 || !name.trim()) && 'cursor-not-allowed opacity-60',
                )}
              >
                {submitting ? '创建中…' : '创建并进入'}
              </button>
            )}
          </div>
        </footer>
      </div>
    </div>
  )
}

// ============================================================================
// 内部组件：样例网格选择器 —— 多选 + A/B 徽章
// ============================================================================
function SamplePicker({
  items,
  loading,
  error,
  sampleIds,
  onPick,
  emptyHint,
}: {
  items: LibraryItem[]
  loading: boolean
  error: string | null
  sampleIds: SampleId[]
  onPick: (id: SampleId) => void
  emptyHint: string
}) {
  if (error) {
    return (
      <p className="rounded-md border border-destructive/40 bg-destructive/5 px-3 py-2 text-xs text-destructive">
        {error}
      </p>
    )
  }
  if (loading) {
    return (
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-3">
        {[0, 1, 2, 3].map((i) => (
          <div key={i} className="h-24 animate-pulse rounded-md border border-border bg-background" />
        ))}
      </div>
    )
  }
  if (items.length === 0) {
    return (
      <p className="rounded-md border border-dashed border-border bg-background/30 px-3 py-4 text-center text-xs text-muted-foreground">
        {emptyHint}
      </p>
    )
  }
  return (
    <div className="grid max-h-64 grid-cols-2 gap-2 overflow-y-auto rounded-md border border-border bg-background/40 p-2 sm:grid-cols-3">
      {items.map((item) => {
        const idx = sampleIds.indexOf(item.id)
        const selected = idx >= 0
        const badge = selected ? String.fromCharCode(65 + idx) : null
        return (
          <button
            key={item.id}
            type="button"
            onClick={() => onPick(item.id)}
            className={cn(
              'relative flex flex-col gap-1 overflow-hidden rounded-md border p-2 text-left transition-all',
              selected
                ? 'border-primary bg-primary/5 ring-1 ring-primary/40'
                : 'border-border bg-card hover:border-primary/40',
            )}
          >
            {badge && (
              <span className="absolute right-1 top-1 z-10 flex h-5 w-5 items-center justify-center rounded-full bg-primary text-[11px] font-bold text-primary-foreground shadow">
                {badge}
              </span>
            )}
            <div
              className="h-16 w-full rounded bg-gradient-to-br from-secondary to-muted"
              style={{
                backgroundImage: item.cover_url ? `url(${item.cover_url})` : undefined,
                backgroundSize: 'cover',
                backgroundPosition: 'center',
              }}
            />
            <div className="flex items-center gap-1 text-[10px] text-muted-foreground">
              <span className="rounded-sm bg-primary/10 px-1 py-0.5 text-primary">
                {VIDEO_TYPE_LABEL[item.video_type]}
              </span>
              <span className="truncate">{item.scene}</span>
            </div>
            <div className="line-clamp-2 text-[11px] font-medium leading-snug">{item.title}</div>
          </button>
        )
      })}
    </div>
  )
}
