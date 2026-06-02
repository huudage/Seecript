import { useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'

import { api, ApiError } from '@/api/client'
import { commitStep } from '@/api/steps'
import { NewProjectDialog } from '@/components/home/NewProjectDialog'
import { PageShell } from '@/components/layout/PageShell'
import { AssetLibraryView } from '@/components/library/AssetLibraryView'
import { useProjectsStore } from '@/stores/projects'
import { useSessionStore } from '@/stores/session'
import type { LibraryItem, VideoType } from '@/types/schemas'
import { VIDEO_TYPE_LABEL } from '@/lib/sections'
import { cn } from '@/lib/utils'
import { readVideoDuration, VIDEO_UPLOAD_MAX_DURATION_SECONDS } from '@/lib/video'

type Section = 'samples' | 'assets'
type Tab = 'system' | 'user'

// 系统样例库上传响应:POST /api/library/system/upload(落到 server/samples/<sys-hex>/)
interface LibrarySystemUploadResponse {
  sample_id: string
  title: string
  video_type: VideoType
  filename: string
  size_bytes: number
  video_url: string
}

// 用户样例库上传响应:POST /api/decompose/upload(落到 var/uploads/decompose/<user-hex>/)
interface DecomposeUploadResponse {
  sample_id: string
  filename: string
  size_bytes: number
  video_url: string
}

export default function LibraryPage() {
  const [section, setSection] = useState<Section>('samples')
  const [items, setItems] = useState<LibraryItem[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [tab, setTab] = useState<Tab>('system')
  const [previewItem, setPreviewItem] = useState<LibraryItem | null>(null)
  // 新建项目弹窗：从素材库挑样例时也支持直接新建项目
  const [newProjectSampleId, setNewProjectSampleId] = useState<string | null>(null)
  // 上传:reloadTick 自增触发列表重拉(上传成功后立即看到新样例)
  const [reloadTick, setReloadTick] = useState(0)
  const [uploading, setUploading] = useState(false)
  const selectSamples = useSessionStore((s) => s.selectSamples)
  const selectedSampleIds = useSessionStore((s) => s.selectedSampleIds)
  const currentProjectId = useProjectsStore((s) => s.currentProjectId)
  const navigate = useNavigate()

  // 一次性拉合并列表（不带 ?source=）；前端按 source 字段切 tab，省一次请求。
  useEffect(() => {
    let cancelled = false
    api
      .get<LibraryItem[]>('/library')
      .then((data) => {
        if (!cancelled) setItems(data)
      })
      .catch((err: ApiError | Error) => {
        if (!cancelled) setError(err.message || '加载失败')
      })
    return () => {
      cancelled = true
    }
  }, [reloadTick])

  const counts = useMemo(() => {
    const sys = items?.filter((i) => i.source === 'system').length ?? 0
    const usr = items?.filter((i) => i.source === 'user').length ?? 0
    return { system: sys, user: usr }
  }, [items])

  const visible = useMemo(
    () => items?.filter((i) => i.source === tab) ?? null,
    [items, tab],
  )

  const handlePick = async (item: LibraryItem) => {
    selectSamples([item.id], [item.title], item.video_type, item.source)
    // 已有项目：让用户继续在该项目内（不换 sample），并提交 library 步骤快照
    if (currentProjectId) {
      try {
        await commitStep(currentProjectId, 'library', { sample_ids: [item.id] })
      } catch (err) {
        setError(err instanceof Error ? err.message : '保存步骤失败')
        return
      }
      navigate('/decompose')
      return
    }
    // 无项目：弹「新建项目」让用户起名（创建后由 NewProjectDialog 内部 commit library）
    setNewProjectSampleId(item.id)
  }

  // 上传到 system / user 样例库:两条 endpoint 都是 multipart,字段一致(file/video_type/title),
  // 仅落地路径与 sample_id 前缀不同。上传成功 → 刷新列表,新卡片立即出现在对应 tab。
  // 拆解仍走 Decompose 页:上传完不自动触发拆解,因为拆解会用配额(LLM/Seedance),
  // 用户应该有机会先确认上传内容再决定是否拆解。
  const handleUploadFile = async (target: Tab, file: File) => {
    setError(null)
    const duration = await readVideoDuration(file)
    if (duration != null && duration > VIDEO_UPLOAD_MAX_DURATION_SECONDS) {
      setError(`视频时长 ${duration.toFixed(1)}s 超过 3 分钟上限,请改用更短的素材`)
      return
    }
    setUploading(true)
    try {
      const fd = new FormData()
      fd.append('file', file)
      // video_type 上传时不让用户选;Decompose 页面有 radio 让用户改。默认 marketing。
      fd.append('video_type', 'marketing')
      if (target === 'system') {
        await api.post<LibrarySystemUploadResponse>('/library/system/upload', fd)
      } else {
        await api.post<DecomposeUploadResponse>('/decompose/upload', fd)
      }
      setTab(target)
      setReloadTick((t) => t + 1)
    } catch (err) {
      setError(err instanceof Error ? err.message : '上传失败')
    } finally {
      setUploading(false)
    }
  }

  return (
    <PageShell
      title="素材库"
      subtitle="管理样例视频与你的常用素材：BGM、参考图、参考视频。"
    >
      <div className="mb-5 inline-flex items-center gap-1 rounded-lg border border-border bg-card p-1 text-sm">
        {(['samples', 'assets'] as const).map((s) => (
          <button
            key={s}
            onClick={() => setSection(s)}
            className={cn(
              'rounded-md px-4 py-1.5 font-medium transition-colors',
              section === s
                ? 'bg-primary text-primary-foreground'
                : 'text-muted-foreground hover:bg-secondary hover:text-foreground',
            )}
          >
            {s === 'samples' ? '样例视频' : '我的素材'}
          </button>
        ))}
      </div>

      {section === 'assets' && <AssetLibraryView />}

      {section === 'samples' && (
        <>
          {error && (
            <div className="mb-4 rounded-md border border-destructive/40 bg-destructive/5 px-4 py-3 text-sm text-destructive">
              {error}
            </div>
          )}

          <div className="mb-4 inline-flex items-center gap-1 rounded-lg border border-border bg-card p-1 text-sm">
            {(['system', 'user'] as const).map((t) => (
              <button
                key={t}
                onClick={() => setTab(t)}
                className={cn(
                  'rounded-md px-3 py-1.5 transition-colors',
                  tab === t
                    ? 'bg-primary text-primary-foreground'
                    : 'text-muted-foreground hover:bg-secondary hover:text-foreground',
                )}
              >
                {t === 'system' ? '系统样例库' : '我的样例库'}
                <span className="ml-1 text-[10px] opacity-70">
                  {t === 'system' ? counts.system : counts.user}
                </span>
              </button>
            ))}
          </div>

      {items === null && !error && (
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {[0, 1, 2].map((i) => (
            <div
              key={i}
              className="h-64 animate-pulse rounded-lg border border-border bg-card"
            />
          ))}
        </div>
      )}

      {visible && (
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {/* 上传卡片永远在第一格:system tab 调 /library/system/upload,
              user tab 调 /decompose/upload。空 tab 时它也是"空状态"本身。 */}
          <UploadSampleCard
            tab={tab}
            uploading={uploading}
            onPick={(file) => void handleUploadFile(tab, file)}
          />
          {visible.map((item) => (
            <div
              key={item.id}
              role="button"
              tabIndex={0}
              onClick={() => handlePick(item)}
              onKeyDown={(e) => {
                if (e.key === 'Enter' || e.key === ' ') {
                  e.preventDefault()
                  handlePick(item)
                }
              }}
              className={cn(
                'group flex cursor-pointer flex-col overflow-hidden rounded-lg border bg-card text-left transition-all',
                'hover:shadow-lg hover:-translate-y-0.5 focus:outline-none focus:ring-2 focus:ring-primary/40',
                selectedSampleIds.includes(item.id)
                  ? 'border-primary ring-2 ring-primary/40'
                  : 'border-border',
              )}
            >
              <div
                className="relative h-40 w-full bg-gradient-to-br from-secondary to-muted"
                style={{
                  backgroundImage: item.cover_url ? `url(${item.cover_url})` : undefined,
                  backgroundSize: 'cover',
                  backgroundPosition: 'center',
                }}
              >
                <div className="absolute right-2 top-2 rounded-full bg-foreground/80 px-2 py-0.5 text-xs font-medium text-background">
                  {item.scene}
                </div>
                <div className="absolute left-2 top-2 rounded-full bg-primary/90 px-2 py-0.5 text-[10px] font-medium text-primary-foreground">
                  {VIDEO_TYPE_LABEL[item.video_type]}
                </div>
                <button
                  type="button"
                  onClick={(e) => {
                    e.stopPropagation()
                    setPreviewItem(item)
                  }}
                  aria-label={`预览 ${item.title}`}
                  title="预览样例视频"
                  className="absolute bottom-2 right-2 flex h-9 w-9 items-center justify-center rounded-full bg-foreground/85 text-background opacity-90 shadow-md transition-all hover:scale-110 hover:bg-foreground hover:opacity-100"
                >
                  <svg
                    xmlns="http://www.w3.org/2000/svg"
                    viewBox="0 0 24 24"
                    fill="currentColor"
                    className="h-4 w-4 translate-x-[1px]"
                  >
                    <path d="M8 5v14l11-7z" />
                  </svg>
                </button>
              </div>
              <div className="flex flex-1 flex-col gap-2 p-4">
                <h3 className="line-clamp-2 text-sm font-semibold leading-snug">
                  {item.title}
                </h3>
                <div className="mt-auto flex items-center justify-between text-xs text-muted-foreground">
                  <span>{item.duration_seconds.toFixed(1)}s</span>
                  <span>{item.shot_count} 镜头</span>
                </div>
              </div>
            </div>
          ))}
        </div>
      )}

      {previewItem && (
        <PreviewModal item={previewItem} onClose={() => setPreviewItem(null)} />
      )}
        </>
      )}

      {newProjectSampleId && (
        <NewProjectDialog
          onClose={() => setNewProjectSampleId(null)}
          onCreated={() => {
            setNewProjectSampleId(null)
            navigate('/decompose')
          }}
        />
      )}
    </PageShell>
  )
}

function PreviewModal({ item, onClose }: { item: LibraryItem; onClose: () => void }) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [onClose])

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/80 px-4 py-8"
      onClick={onClose}
    >
      <div
        className="relative flex w-full max-w-3xl flex-col overflow-hidden rounded-lg bg-card shadow-xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between border-b border-border px-4 py-3">
          <div className="min-w-0">
            <h3 className="truncate text-sm font-semibold">{item.title}</h3>
            <p className="text-xs text-muted-foreground">
              {VIDEO_TYPE_LABEL[item.video_type]} · {item.duration_seconds.toFixed(1)}s · {item.shot_count} 镜头
            </p>
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label="关闭预览"
            className="ml-3 flex h-8 w-8 shrink-0 items-center justify-center rounded-md text-muted-foreground hover:bg-secondary hover:text-foreground"
          >
            <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} className="h-4 w-4">
              <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
            </svg>
          </button>
        </div>
        <video
          key={item.id}
          src={`/samples/${item.id}/video.mp4`}
          controls
          autoPlay
          className="aspect-video w-full bg-black"
        >
          您的浏览器不支持视频播放。
        </video>
      </div>
    </div>
  )
}

// 上传新样例的 dashed 卡片。两个 tab 复用同一个组件,通过 tab prop 切换:
//   - system: 上传后落到 server/samples/<sys-hex>/(所有用户共享)
//   - user:   上传后落到 server/var/uploads/decompose/<user-hex>/(当前 session 私有)
// UI 风格刻意做得和样例卡同高(h-64 相当),保持 grid 视觉对齐。
function UploadSampleCard({
  tab,
  uploading,
  onPick,
}: {
  tab: Tab
  uploading: boolean
  onPick: (file: File) => void
}) {
  const inputRef = useRef<HTMLInputElement | null>(null)
  const [dragOver, setDragOver] = useState(false)

  const triggerPick = () => {
    if (!uploading) inputRef.current?.click()
  }

  const onDrop = (e: React.DragEvent<HTMLDivElement>) => {
    e.preventDefault()
    setDragOver(false)
    if (uploading) return
    const file = e.dataTransfer.files?.[0]
    if (file) onPick(file)
  }

  const tabLabel = tab === 'system' ? '系统样例库' : '我的样例库'
  const tabDesc =
    tab === 'system'
      ? '所有用户可见的公共爆款样例;上传后落到 server/samples/'
      : '只对当前 session 可见;上传后落到 var/uploads/decompose/'

  return (
    <div
      role="button"
      tabIndex={0}
      onClick={triggerPick}
      onKeyDown={(e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault()
          triggerPick()
        }
      }}
      onDragOver={(e) => {
        e.preventDefault()
        if (!uploading) setDragOver(true)
      }}
      onDragLeave={() => setDragOver(false)}
      onDrop={onDrop}
      className={cn(
        'flex min-h-[256px] cursor-pointer flex-col items-center justify-center gap-2 rounded-lg border-2 border-dashed p-6 text-center transition-all',
        'focus:outline-none focus:ring-2 focus:ring-primary/40',
        uploading
          ? 'cursor-not-allowed border-border bg-card/50 opacity-60'
          : dragOver
            ? 'border-primary bg-primary/5'
            : 'border-border bg-card/50 hover:border-primary/60 hover:bg-card',
      )}
    >
      <div className="flex h-12 w-12 items-center justify-center rounded-full bg-primary/10 text-primary">
        {uploading ? (
          <svg viewBox="0 0 24 24" className="h-6 w-6 animate-spin">
            <circle cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="3" fill="none" opacity="0.25" />
            <path d="M12 2a10 10 0 0 1 10 10" stroke="currentColor" strokeWidth="3" fill="none" />
          </svg>
        ) : (
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} className="h-6 w-6">
            <path strokeLinecap="round" strokeLinejoin="round" d="M12 4v16m8-8H4" />
          </svg>
        )}
      </div>
      <p className="text-sm font-semibold">
        {uploading ? '上传中…' : `上传到${tabLabel}`}
      </p>
      <p className="px-2 text-[11px] leading-relaxed text-muted-foreground">
        {tabDesc}
      </p>
      <p className="text-[11px] text-muted-foreground">
        mp4 / mov / webm,≤ 3 分钟、单文件 ≤ 200MB
      </p>
      <input
        ref={inputRef}
        type="file"
        hidden
        accept="video/mp4,video/quicktime,video/webm"
        onChange={(e) => {
          const file = e.target.files?.[0]
          if (file) onPick(file)
          e.target.value = ''
        }}
      />
    </div>
  )
}
