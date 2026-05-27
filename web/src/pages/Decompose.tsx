import { useCallback, useEffect, useRef, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { LineChart, Line, XAxis, YAxis, Tooltip, ResponsiveContainer, CartesianGrid } from 'recharts'

import { api } from '@/api/client'
import { createSSE, type SSEHandle } from '@/api/sse'
import { PageShell } from '@/components/layout/PageShell'
import { useSessionStore } from '@/stores/session'
import type {
  DecomposeRequest,
  DecomposeSubmitResponse,
  ProgressEventPayload,
  SampleManifest,
  VideoType,
} from '@/types/schemas'
import {
  SECTION_BG,
  SECTION_LABEL,
  VIDEO_TYPE_HINT,
  VIDEO_TYPE_LABEL,
} from '@/lib/sections'
import { cn } from '@/lib/utils'

const VIDEO_TYPE_OPTIONS: VideoType[] = ['marketing', 'editing', 'motion_graph']

interface DoneEvent {
  job_id: string
  payload: { sample_id: string; manifest: SampleManifest }
}

interface DecomposeUploadResponse {
  sample_id: string
  filename: string
  size_bytes: number
  video_url: string
}

export default function DecomposePage() {
  const selectedSampleId = useSessionStore((s) => s.selectedSampleId)
  const sampleSource = useSessionStore((s) => s.sampleSource)
  const videoType = useSessionStore((s) => s.videoType)
  const setVideoType = useSessionStore((s) => s.setVideoType)
  const manifest = useSessionStore((s) => s.manifest)
  const setManifest = useSessionStore((s) => s.setManifest)
  const selectSample = useSessionStore((s) => s.selectSample)
  const navigate = useNavigate()

  const [progress, setProgress] = useState<{ step: string; percent: number; note?: string }>({
    step: 'idle',
    percent: 0,
  })
  const [running, setRunning] = useState(false)
  const [uploading, setUploading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [uploadedFile, setUploadedFile] = useState<{ filename: string; size_bytes: number } | null>(null)
  const sseRef = useRef<SSEHandle | null>(null)
  const fileInputRef = useRef<HTMLInputElement | null>(null)

  // sampleSource:
  //   'system' = 从素材库挑的内置样例 → video_type 锁定，直接拆解
  //   'user'   = 用户上传到 server/var/uploads/decompose/<sample_id>/ 的视频 → video_type 可选
  //   null     = 没选/没传任何样例 → 引导用户去素材库或上传
  const isSystemSample = sampleSource === 'system'
  const isUserSample = sampleSource === 'user'

  const handlePickFile = useCallback(
    async (file: File | null) => {
      if (!file) return
      setError(null)
      setUploading(true)
      try {
        const fd = new FormData()
        fd.append('file', file)
        const resp = await api.post<DecomposeUploadResponse>('/decompose/upload', fd)
        // 切换到 user 来源；videoType 保持当前选择，让用户在 radios 里改。
        selectSample(resp.sample_id, videoType, 'user')
        setUploadedFile({ filename: resp.filename, size_bytes: resp.size_bytes })
      } catch (err) {
        setError(err instanceof Error ? err.message : '上传失败')
      } finally {
        setUploading(false)
      }
    },
    [selectSample, videoType],
  )

  const run = useCallback(async () => {
    if (!selectedSampleId) return
    setError(null)
    setRunning(true)
    setProgress({ step: 'submit', percent: 2, note: '提交任务' })
    try {
      const req: DecomposeRequest = { sample_id: selectedSampleId, video_type: videoType }
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
            setManifest(done.payload.manifest)
            setProgress({ step: 'done', percent: 100, note: '完成' })
            setRunning(false)
          },
          onError: (err) => {
            setError(err.detail || '拆解失败')
            setRunning(false)
          },
        },
      )
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
      setRunning(false)
    }
  }, [selectedSampleId, setManifest, videoType])

  useEffect(() => {
    return () => {
      sseRef.current?.close()
    }
  }, [])

  return (
    <PageShell
      title="样例拆解"
      subtitle="从素材库挑样例（已知类型直接拆），或上传一段自己的视频自选类型再拆。PySceneDetect → librosa VAD → ASR（按需）→ 多模态 LLM 帧标签 + 段落结构。"
    >
      {/* ====== 来源块：系统样例锁定卡 / 用户上传卡 / 双入口 ====== */}
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
                  selectSample(null)
                  setUploadedFile(null)
                  setManifest(null)
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
              选个起点：去素材库挑一段内置爆款样例，或者上传一段自己的视频开始拆解。
            </p>
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
              <Link
                to="/library"
                className="flex flex-col items-start gap-1 rounded-lg border border-border bg-background p-4 transition-colors hover:border-primary hover:bg-primary/5"
              >
                <span className="text-sm font-semibold">从素材库挑样例</span>
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
                  mp4 / mov / webm，单文件 ≤ 200MB；上传后选类型再拆。
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

      <div className="mb-6 flex flex-wrap items-center gap-3">
        <button
          onClick={run}
          disabled={running || !selectedSampleId}
          className={cn(
            'rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground transition-opacity',
            (running || !selectedSampleId) && 'cursor-not-allowed opacity-60',
          )}
        >
          {manifest ? '重新拆解' : '开始拆解'}
        </button>
        {manifest && (
          <button
            onClick={() => navigate('/compose')}
            className="rounded-md border border-border bg-card px-4 py-2 text-sm font-medium hover:bg-secondary"
          >
            下一步 · 上传素材 →
          </button>
        )}
        {error && (
          <span className="text-sm text-destructive">{error}</span>
        )}
      </div>

      {(running || progress.step !== 'idle') && (
        <ProgressPanel step={progress.step} percent={progress.percent} note={progress.note} />
      )}

      {manifest && <ManifestView manifest={manifest} />}
    </PageShell>
  )
}

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

function ManifestView({ manifest }: { manifest: SampleManifest }) {
  const rhythmData = manifest.rhythm.times.map((t, i) => ({
    t,
    cut: manifest.rhythm.cut_density[i] ?? 0,
    bgm: manifest.rhythm.bgm_energy[i] ?? 0,
  }))

  return (
    <div className="space-y-6">
      <div className="rounded-lg border border-border bg-card p-4">
        <div className="mb-3 flex items-center justify-between">
          <h2 className="text-sm font-semibold">段落结构 · {VIDEO_TYPE_LABEL[manifest.video_type]}</h2>
          <span className="text-xs text-muted-foreground">
            {manifest.has_voice ? '🎙 含口播' : '🎵 纯 BGM'}
          </span>
        </div>
        <SectionsBar manifest={manifest} />
      </div>

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
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
              </LineChart>
            </ResponsiveContainer>
          </div>
          {manifest.rhythm.tempo_bpm != null && (
            <p className="mt-2 text-xs text-muted-foreground">BPM ≈ {manifest.rhythm.tempo_bpm.toFixed(0)}</p>
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
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-4 xl:grid-cols-6">
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
      <div className="relative flex h-10 w-full overflow-hidden rounded-md border border-border">
        {manifest.sections.map((sec, idx) => {
          const widthPct = ((sec.end - sec.start) / total) * 100
          return (
            <div
              key={idx}
              className={cn(
                'flex items-center justify-center text-xs font-medium text-white',
                SECTION_BG[sec.kind],
              )}
              style={{ width: `${widthPct}%` }}
              title={`${SECTION_LABEL[sec.kind]}: ${sec.summary}`}
            >
              {SECTION_LABEL[sec.kind]}
            </div>
          )
        })}
      </div>
      <div className="space-y-1 text-xs text-muted-foreground">
        {manifest.sections.map((sec, idx) => (
          <div key={idx} className="flex gap-2">
            <span className="font-mono">{sec.start.toFixed(1)}–{sec.end.toFixed(1)}s</span>
            <span className="font-medium text-foreground">{SECTION_LABEL[sec.kind]}：</span>
            <span>{sec.summary}</span>
          </div>
        ))}
      </div>
    </div>
  )
}
