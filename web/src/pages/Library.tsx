import { useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'

import { api, ApiError } from '@/api/client'
import { PageShell } from '@/components/layout/PageShell'
import { AssetLibraryView } from '@/components/library/AssetLibraryView'
import { useSessionStore } from '@/stores/session'
import type { LibraryItem, VideoType } from '@/types/schemas'
import { VIDEO_TYPE_LABEL, VIDEO_TYPE_HINT } from '@/lib/sections'
import { cn } from '@/lib/utils'
import { readVideoDuration, VIDEO_UPLOAD_MAX_DURATION_SECONDS } from '@/lib/video'

type Section = 'samples' | 'assets'
type Tab = 'system' | 'user'
type VideoTypeFilter = 'all' | VideoType

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
  const [videoTypeFilter, setVideoTypeFilter] = useState<VideoTypeFilter>('all')
  const [previewItem, setPreviewItem] = useState<LibraryItem | null>(null)
  // 上传:reloadTick 自增触发列表重拉(上传成功后立即看到新样例)
  const [reloadTick, setReloadTick] = useState(0)
  const [uploading, setUploading] = useState(false)
  const selectSamples = useSessionStore((s) => s.selectSamples)
  const selectedSampleIds = useSessionStore((s) => s.selectedSampleIds)
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

  // 按当前 tab（system/user）算每种 video_type 的数量，用于分类按钮上的徽标。
  const videoTypeCounts = useMemo(() => {
    const base: Record<VideoTypeFilter, number> = { all: 0, marketing: 0, editing: 0, motion_graph: 0 }
    if (!items) return base
    for (const it of items) {
      if (it.source !== tab) continue
      base.all += 1
      base[it.video_type] = (base[it.video_type] ?? 0) + 1
    }
    return base
  }, [items, tab])

  const visible = useMemo(
    () =>
      items
        ?.filter((i) => i.source === tab)
        .filter((i) => videoTypeFilter === 'all' || i.video_type === videoTypeFilter) ?? null,
    [items, tab, videoTypeFilter],
  )

  // stage-15:卡片点击只引导到 Decompose 页(查看 / 编辑该样例的拆解版本)。
  // 资产库与 Compose 链路彻底解耦——结构参考由 Compose 顶部 ReferencePicker 单独挑。
  // selectSamples 仅用于在 Decompose 页知道"现在在拆/看哪条",不再 commit library step。
  const handlePick = (item: LibraryItem) => {
    selectSamples([item.id], [item.title], item.video_type, item.source)
    navigate(`/decompose?sample=${encodeURIComponent(item.id)}`)
  }

  // 上传到 system / user 样例库:两条 endpoint 都是 multipart,字段一致(file/video_type/title),
  // 仅落地路径与 sample_id 前缀不同。上传成功 → 刷新列表,新卡片立即出现在对应 tab。
  // 拆解仍走 Decompose 页:上传完不自动触发拆解,因为拆解会用配额(LLM/Seedance),
  // 用户应该有机会先确认上传内容再决定是否拆解。
  // video_type: 用上传卡片内的 select 显式选择(默认跟随当前筛选 chip;'all' 时回退 marketing),
  // 否则会出现"在 Vlog 筛选下上传却被归入营销"的脏数据。
  const handleUploadFile = async (target: Tab, file: File, videoType: VideoType) => {
    setError(null)
    const duration = await readVideoDuration(file)
    if (duration != null && duration > VIDEO_UPLOAD_MAX_DURATION_SECONDS) {
      setError(`视频时长 ${duration.toFixed(1)} 秒超过了 3 分钟上限，请换一个更短的视频`)
      return
    }
    setUploading(true)
    try {
      const fd = new FormData()
      fd.append('file', file)
      fd.append('video_type', videoType)
      if (target === 'system') {
        await api.post<LibrarySystemUploadResponse>('/library/system/upload', fd)
      } else {
        await api.post<DecomposeUploadResponse>('/decompose/upload', fd)
      }
      setTab(target)
      // 上传成功后把筛选切到刚选的类型,新卡片立即可见(不然停在 'all' 也行,但
      // 用户多半希望看到自己刚上传的那张)
      setVideoTypeFilter(videoType)
      setReloadTick((t) => t + 1)
    } catch (err) {
      setError(err instanceof Error ? err.message : '上传失败')
    } finally {
      setUploading(false)
    }
  }

  return (
    <PageShell
      title="素材与灵感"
      subtitle="热门样例和你的素材都在这里。选中一个热门视频，分析它的结构，找到创作灵感。"
    >
      <div className="mb-5 inline-flex items-center gap-1 rounded-lg border border-border bg-card p-1 text-sm">
        {(['samples', 'assets'] as const).map((s) => (
          <button
            key={s}
            onClick={() => setSection(s)}
            className={cn(
              'rounded-lg px-4 py-1.5 font-medium transition-all duration-200',
              section === s
                ? 'bg-primary text-primary-foreground shadow-sm'
                : 'text-muted-foreground hover:bg-secondary hover:text-foreground',
            )}
          >
            {s === 'samples' ? '爆款样例' : '我的素材'}
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
                  'rounded-lg px-3 py-1.5 transition-all duration-200',
                  tab === t
                    ? 'bg-primary text-primary-foreground shadow-sm'
                    : 'text-muted-foreground hover:bg-secondary hover:text-foreground',
                )}
              >
                {t === 'system' ? '官方样例' : '我上传的'}
                <span className="ml-1 text-xs opacity-70">
                  {t === 'system' ? counts.system : counts.user}
                </span>
              </button>
            ))}
          </div>

          {/* 视频类型分类筛选——backend LibraryItem.video_type 长存（marketing/editing/motion_graph），
              前端按 system/user tab 内再做二次过滤。这里把分类暴露出来，否则用户只看到一种贴在
              卡片角的小徽标，永远找不到"只看 Vlog 样例"。 */}
          <div className="mb-4 flex flex-wrap items-center gap-2">
            <span className="text-xs font-medium text-muted-foreground">视频类型</span>
            <div className="inline-flex flex-wrap items-center gap-1 rounded-lg border border-border bg-card p-1 text-xs">
              {(['all', 'marketing', 'editing', 'motion_graph'] as const).map((f) => {
                const label = f === 'all' ? '全部' : VIDEO_TYPE_LABEL[f]
                const active = videoTypeFilter === f
                const count = videoTypeCounts[f]
                return (
                  <button
                    key={f}
                    onClick={() => setVideoTypeFilter(f)}
                    title={f === 'all' ? '不过滤视频类型' : VIDEO_TYPE_HINT[f]}
                    className={cn(
                      'rounded-lg px-2.5 py-1 transition-all duration-200',
                      active
                        ? 'bg-primary text-primary-foreground shadow-sm'
                        : 'text-muted-foreground hover:bg-secondary hover:text-foreground',
                    )}
                  >
                    {label}
                    <span className="ml-1 text-xs opacity-70">{count}</span>
                  </button>
                )
              })}
            </div>
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
            defaultVideoType={videoTypeFilter === 'all' ? 'marketing' : videoTypeFilter}
            onPick={(file, videoType) => void handleUploadFile(tab, file, videoType)}
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
                'group flex cursor-pointer flex-col overflow-hidden rounded-xl bg-card shadow-sm text-left transition-all duration-300 hover:shadow-md',
                'hover:shadow-[var(--shadow-md)] hover:-translate-y-0.5 focus:outline-none focus:ring-2 focus:ring-primary/40',
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
                <div className="absolute left-2 top-2 rounded-full bg-primary/90 px-2 py-0.5 text-xs font-medium text-primary-foreground">
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
                <ManifestStatusBadge versionCount={item.version_count} />
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
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/80 backdrop-blur-sm px-4 py-8"
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
            className="ml-3 flex h-8 w-8 shrink-0 items-center justify-center rounded-lg text-muted-foreground hover:bg-secondary hover:text-foreground transition-colors duration-200"
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

function ManifestStatusBadge({ versionCount }: { versionCount: number }) {
  if (versionCount === 0) {
    return (
      <span className="inline-flex items-center gap-1.5 rounded-full border border-border bg-muted px-2 py-0.5 text-xs font-medium text-muted-foreground">
        <span className="h-1.5 w-1.5 rounded-full bg-muted-foreground/60" />
        还没分析
      </span>
    )
  }
  return (
    <span className="inline-flex items-center gap-1.5 rounded-full border border-emerald-400/40 bg-emerald-400/10 px-2 py-0.5 text-xs font-medium text-emerald-600 dark:text-emerald-300">
      <span className="h-1.5 w-1.5 rounded-full bg-emerald-500" />
      已拆解 · {versionCount} 个版本
    </span>
  )
}

// 上传新样例的 dashed 卡片。两个 tab 复用同一个组件,通过 tab prop 切换:
//   - system: 上传后落到 server/samples/<sys-hex>/(所有用户共享)
//   - user:   上传后落到 server/var/uploads/decompose/<user-hex>/(当前 session 私有)
// UI 风格刻意做得和样例卡同高(h-64 相当),保持 grid 视觉对齐。
// defaultVideoType: 由父级传入(跟随当前筛选 chip),用户可在 select 内显式改。
function UploadSampleCard({
  tab,
  uploading,
  defaultVideoType,
  onPick,
}: {
  tab: Tab
  uploading: boolean
  defaultVideoType: VideoType
  onPick: (file: File, videoType: VideoType) => void
}) {
  const inputRef = useRef<HTMLInputElement | null>(null)
  const [dragOver, setDragOver] = useState(false)
  const [videoType, setVideoType] = useState<VideoType>(defaultVideoType)
  // 父级筛选 chip 切换时同步 select(用户大概率想"跟着筛选走"),
  // 但不用 useEffect 强制覆盖——已手动改过的状态不该被父级一刷又拉回。
  // 这里用 ref 跟踪上一次默认值变化即可。
  const lastDefaultRef = useRef(defaultVideoType)
  if (lastDefaultRef.current !== defaultVideoType) {
    lastDefaultRef.current = defaultVideoType
    setVideoType(defaultVideoType)
  }

  const triggerPick = () => {
    if (!uploading) inputRef.current?.click()
  }

  const onDrop = (e: React.DragEvent<HTMLDivElement>) => {
    e.preventDefault()
    setDragOver(false)
    if (uploading) return
    const file = e.dataTransfer.files?.[0]
    if (file) onPick(file, videoType)
  }

  const tabLabel = tab === 'system' ? '官方样例' : '我上传的样例'
  const tabDesc =
    tab === 'system'
      ? '所有用户都能看到的公开样例'
      : '只有当前账号能看到的私人样例'

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
        'focus:outline-none focus:ring-2 focus:ring-primary/40 transition-all duration-300',
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
      <p className="px-2 text-xs leading-relaxed text-muted-foreground">
        {tabDesc}
      </p>
      {/* 视频类型选择——上传时显式定类,避免被默认归入营销分类导致筛选错位 */}
      <label
        className="mt-1 flex items-center gap-1.5 text-xs text-muted-foreground"
        onClick={(e) => e.stopPropagation()}
      >
        <span>视频类型</span>
        <select
          value={videoType}
          disabled={uploading}
          onChange={(e) => setVideoType(e.target.value as VideoType)}
          onClick={(e) => e.stopPropagation()}
          className="rounded-lg border border-input bg-background px-2 py-0.5 text-xs text-foreground transition-shadow duration-200 focus:ring-2 focus:ring-primary/20 focus:border-primary focus:outline-none"
        >
          <option value="marketing">{VIDEO_TYPE_LABEL.marketing}</option>
          <option value="editing">{VIDEO_TYPE_LABEL.editing}</option>
          <option value="motion_graph">{VIDEO_TYPE_LABEL.motion_graph}</option>
        </select>
      </label>
      <p className="text-xs text-muted-foreground">
        mp4 / mov / webm，单个文件 ≤ 200MB、≤ 3 分钟
      </p>
      <input
        ref={inputRef}
        type="file"
        hidden
        accept="video/mp4,video/quicktime,video/webm"
        onChange={(e) => {
          const file = e.target.files?.[0]
          if (file) onPick(file, videoType)
          e.target.value = ''
        }}
      />
    </div>
  )
}
