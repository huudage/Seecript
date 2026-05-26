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

export default function DecomposePage() {
  const selectedSampleId = useSessionStore((s) => s.selectedSampleId)
  const videoType = useSessionStore((s) => s.videoType)
  const setVideoType = useSessionStore((s) => s.setVideoType)
  const manifest = useSessionStore((s) => s.manifest)
  const setManifest = useSessionStore((s) => s.setManifest)
  const navigate = useNavigate()

  const [progress, setProgress] = useState<{ step: string; percent: number; note?: string }>({
    step: 'idle',
    percent: 0,
  })
  const [running, setRunning] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const sseRef = useRef<SSEHandle | null>(null)

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

  if (!selectedSampleId) {
    return (
      <PageShell title="样例拆解" subtitle="先去素材库挑一个样例。">
        <div className="rounded-lg border border-dashed border-border bg-card p-8 text-sm text-muted-foreground">
          尚未选中样例。
          <Link to="/library" className="ml-2 text-primary underline-offset-4 hover:underline">
            返回素材库
          </Link>
        </div>
      </PageShell>
    )
  }

  return (
    <PageShell
      title="样例拆解"
      subtitle={`样例 ${selectedSampleId} · PySceneDetect → librosa VAD → ASR（按需）→ 多模态 LLM 帧标签 + 段落结构`}
    >
      <div className="mb-6 rounded-lg border border-border bg-card p-4">
        <div className="mb-2 flex items-center justify-between text-xs text-muted-foreground">
          <span className="font-semibold text-foreground">视频类型</span>
          <span>决定段落 prompt（marketing / editing / motion_graph）</span>
        </div>
        <div className="grid grid-cols-1 gap-2 sm:grid-cols-3">
          {VIDEO_TYPE_OPTIONS.map((vt) => (
            <label
              key={vt}
              className={cn(
                'flex cursor-pointer flex-col gap-1 rounded-md border px-3 py-2 transition-colors',
                videoType === vt
                  ? 'border-primary bg-primary/5'
                  : 'border-border bg-background hover:bg-secondary/50',
                running && 'pointer-events-none opacity-60',
              )}
            >
              <div className="flex items-center gap-2 text-sm font-medium">
                <input
                  type="radio"
                  name="video_type"
                  value={vt}
                  checked={videoType === vt}
                  onChange={() => setVideoType(vt)}
                  disabled={running}
                  className="accent-primary"
                />
                <span>{VIDEO_TYPE_LABEL[vt]}</span>
              </div>
              <span className="text-[11px] text-muted-foreground">{VIDEO_TYPE_HINT[vt]}</span>
            </label>
          ))}
        </div>
      </div>

      <div className="mb-6 flex flex-wrap items-center gap-3">
        <button
          onClick={run}
          disabled={running}
          className={cn(
            'rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground transition-opacity',
            running && 'cursor-not-allowed opacity-60',
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
