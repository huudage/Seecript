import { useEffect, useMemo, useRef, useState } from 'react'

import { api, ApiError } from '@/api/client'
import { commitStep } from '@/api/steps'
import { VIDEO_TYPE_HINT, VIDEO_TYPE_LABEL } from '@/lib/sections'
import { cn } from '@/lib/utils'
import { useProjectsStore } from '@/stores/projects'
import type { LibraryItem, SampleId, VideoType } from '@/types/schemas'

/**
 * 新建项目向导（两步）：
 * 1. 先选视频类型（marketing / editing / motion_graph）—— 类型决定后续的样例池 / 上传默认风格
 * 2. 再选样例：从系统样例 / 我的样例库里挑，或上传一段自己的视频（后台自动注册为 user 样例）
 *    → 起项目名 → POST /api/project
 *
 * 为什么先类型再样例：用户的思维路径是「我要做哪种视频 → 找一个差不多的参考」，
 * 而不是「我先翻一遍样例库再决定方向」。前者点击路径更短，也方便上传分支的 video_type 默认锁定。
 */

interface DecomposeUploadResponse {
  sample_id: string
  filename: string
  size_bytes: number
  video_url: string
}

type SourceTab = 'system' | 'user' | 'upload'

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
  const [sampleId, setSampleId] = useState<SampleId | null>(null)
  const [name, setName] = useState('')

  // 上传 tab 状态
  const [uploading, setUploading] = useState(false)
  const [uploadError, setUploadError] = useState<string | null>(null)
  const [uploadedSample, setUploadedSample] = useState<{ sample_id: string; filename: string } | null>(null)
  const fileInputRef = useRef<HTMLInputElement | null>(null)

  // 提交项目
  const [submitting, setSubmitting] = useState(false)
  const [submitError, setSubmitError] = useState<string | null>(null)

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

  // 切 tab 时自动选第一项 / 清空选择
  useEffect(() => {
    if (step !== 2) return
    if (tab === 'system') {
      setSampleId(filteredSystem[0]?.id ?? null)
    } else if (tab === 'user') {
      setSampleId(filteredUser[0]?.id ?? null)
    } else {
      setSampleId(uploadedSample?.sample_id ?? null)
    }
  }, [tab, filteredSystem, filteredUser, uploadedSample, step])

  // 选中样例时自动填项目名（用户没改过的话）
  const userTouchedName = useRef(false)
  const selectedItem = useMemo(() => {
    if (tab === 'upload') {
      if (!uploadedSample) return null
      return (items ?? []).find((it) => it.id === uploadedSample.sample_id) ?? null
    }
    return (items ?? []).find((it) => it.id === sampleId) ?? null
  }, [items, sampleId, tab, uploadedSample])

  useEffect(() => {
    if (!userTouchedName.current && selectedItem) {
      setName(selectedItem.title.slice(0, 80))
    }
  }, [selectedItem])

  // ===== Handlers =====
  const onPickType = (vt: VideoType) => {
    setVideoType(vt)
    setStep(2)
  }

  const onBackToStep1 = () => {
    setStep(1)
    setSampleId(null)
    setUploadedSample(null)
    setUploadError(null)
    setTab('system')
    userTouchedName.current = false
    setName('')
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
      setUploadedSample({ sample_id: resp.sample_id, filename: resp.filename })
      // 上传后刷新样例列表，让 _scan_user_library 返回的新条目进入 items
      try {
        const data = await api.get<LibraryItem[]>('/library')
        setItems(data)
      } catch {
        // 失败不阻断，sampleId 已经能用
      }
      setSampleId(resp.sample_id)
    } catch (err) {
      setUploadError(err instanceof Error ? err.message : '上传失败')
    } finally {
      setUploading(false)
    }
  }

  const onConfirm = async () => {
    setSubmitError(null)
    if (!sampleId) {
      setSubmitError('请挑一个样例或先上传视频')
      return
    }
    const finalName = name.trim()
    if (!finalName) {
      setSubmitError('请输入项目名')
      return
    }
    setSubmitting(true)
    try {
      const created = await createProject(finalName, sampleId)
      // 创建后立即把 library 步骤打 saved——用户已经做出了「选样例」的决定，
      // 顶部导航 nav 才会显示 library=saved + current_step=decompose
      try {
        await commitStep(created.project_id, 'library', { sample_id: sampleId })
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
                {step === 1 ? '步骤 1 / 2 · 选类型' : '步骤 2 / 2 · 选样例'}
              </span>
            </h3>
            <p className="text-xs text-muted-foreground">
              {step === 1
                ? '类型决定后续样例池 + 上传视频的默认风格'
                : `已选：${VIDEO_TYPE_LABEL[videoType!]}　·　从素材库挑一个样例，或上传一段自己的视频`}
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
              {/* Tabs */}
              <div className="flex gap-1 rounded-md border border-border bg-background/40 p-1 text-xs">
                {(['system', 'user', 'upload'] as const).map((t) => (
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
                    {t === 'system' && '系统样例'}
                    {t === 'user' && '我的样例'}
                    {t === 'upload' && '上传新视频'}
                  </button>
                ))}
              </div>

              {/* Tab content */}
              {tab === 'system' && (
                <SamplePicker
                  items={filteredSystem}
                  loading={items === null}
                  error={loadError}
                  sampleId={sampleId}
                  onPick={setSampleId}
                  emptyHint={`暂无 ${VIDEO_TYPE_LABEL[videoType!]} 类型的系统样例`}
                />
              )}

              {tab === 'user' && (
                <SamplePicker
                  items={filteredUser}
                  loading={items === null}
                  error={loadError}
                  sampleId={sampleId}
                  onPick={setSampleId}
                  emptyHint={`你还没有 ${VIDEO_TYPE_LABEL[videoType!]} 类型的样例，可以切到「上传新视频」加一个`}
                />
              )}

              {tab === 'upload' && (
                <div className="space-y-2">
                  <input
                    ref={fileInputRef}
                    type="file"
                    accept="video/mp4,video/quicktime,video/webm"
                    className="hidden"
                    onChange={(e) => onUploadFile(e.target.files?.[0] ?? null)}
                  />
                  {!uploadedSample && (
                    <button
                      type="button"
                      onClick={() => fileInputRef.current?.click()}
                      disabled={uploading}
                      className={cn(
                        'flex h-32 w-full flex-col items-center justify-center gap-2 rounded-md border-2 border-dashed border-border bg-background/40 text-xs transition-colors',
                        !uploading && 'hover:border-primary hover:bg-primary/5',
                        uploading && 'cursor-wait opacity-60',
                      )}
                    >
                      <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.5} className="h-6 w-6 text-muted-foreground">
                        <path strokeLinecap="round" strokeLinejoin="round" d="M3 16.5v2.25A2.25 2.25 0 005.25 21h13.5A2.25 2.25 0 0021 18.75V16.5m-13.5-9L12 3m0 0l4.5 4.5M12 3v13.5" />
                      </svg>
                      <div className="font-medium">
                        {uploading ? '上传中…' : '点击选择视频'}
                      </div>
                      <div className="text-[10px] text-muted-foreground">
                        支持 mp4 / mov / webm · 单文件 ≤ 200MB · 类型默认 {VIDEO_TYPE_LABEL[videoType!]}
                      </div>
                    </button>
                  )}
                  {uploadedSample && (
                    <div className="space-y-2 rounded-md border border-primary/40 bg-primary/5 px-3 py-3 text-xs">
                      <div className="flex items-center justify-between">
                        <span className="font-semibold text-primary">已上传</span>
                        <button
                          type="button"
                          onClick={() => {
                            setUploadedSample(null)
                            setSampleId(null)
                          }}
                          className="text-muted-foreground hover:text-foreground"
                        >
                          换一个 →
                        </button>
                      </div>
                      <div>
                        <span className="text-muted-foreground">文件：</span>
                        <span className="font-mono">{uploadedSample.filename}</span>
                      </div>
                      <div>
                        <span className="text-muted-foreground">sample_id：</span>
                        <span className="font-mono">{uploadedSample.sample_id}</span>
                      </div>
                      <p className="text-[11px] text-muted-foreground">
                        提示：项目创建后可在「样例拆解」页跑一次 decompose，否则后续 plan/gap 会用等分 stub。
                      </p>
                    </div>
                  )}
                  {uploadError && (
                    <p className="rounded-md border border-destructive/40 bg-destructive/5 px-3 py-2 text-xs text-destructive">
                      {uploadError}
                    </p>
                  )}
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
            {!submitError && step === 2 && selectedItem && (
              <p className="text-[11px] text-muted-foreground">
                已选样例：<span className="font-medium text-foreground">{selectedItem.title}</span>
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
                disabled={submitting || uploading || !sampleId || !name.trim()}
                className={cn(
                  'rounded-md bg-primary px-4 py-1.5 text-sm font-medium text-primary-foreground hover:bg-primary/90',
                  (submitting || uploading || !sampleId || !name.trim()) && 'cursor-not-allowed opacity-60',
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
// 内部组件：样例网格选择器
// ============================================================================
function SamplePicker({
  items,
  loading,
  error,
  sampleId,
  onPick,
  emptyHint,
}: {
  items: LibraryItem[]
  loading: boolean
  error: string | null
  sampleId: SampleId | null
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
      {items.map((item) => (
        <button
          key={item.id}
          type="button"
          onClick={() => onPick(item.id)}
          className={cn(
            'flex flex-col gap-1 overflow-hidden rounded-md border p-2 text-left transition-all',
            sampleId === item.id
              ? 'border-primary bg-primary/5 ring-1 ring-primary/40'
              : 'border-border bg-card hover:border-primary/40',
          )}
        >
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
      ))}
    </div>
  )
}
