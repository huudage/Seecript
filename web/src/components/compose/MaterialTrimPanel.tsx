import { useEffect, useRef, useState } from 'react'

import { cn } from '@/lib/utils'
import type { Material } from '@/types/schemas'

/**
 * stage-29 视频素材手动裁剪面板。
 *
 * 嵌在 SwapSourceDialog 真实素材 tab 内：用户挑选 video 素材后展开，给一个
 * `<video>` 预览 + 双手柄裁剪条 + 数值输入。提交时 SwapSourceDialog 把 in/out
 * 通过 swap-source 接口的 material_in_point / material_out_point 字段提交，
 * 后端写 scene.in_point/out_point/duration 并重铺时间轴。
 *
 * 用户原话："分镜时长要跟着用户裁剪结果走，完全听用户的"——所以这里不强制
 * 吸附到 targetDuration，仅作差值提示。
 */

const MIN_WINDOW_S = 0.5

export function MaterialTrimPanel({
  material,
  targetDuration,
  initialIn,
  initialOut,
  onChange,
}: {
  material: Material
  /** 当前 scene 的 duration，仅用于 hint，不强制约束 */
  targetDuration: number
  initialIn?: number
  initialOut?: number
  onChange: (inPt: number, outPt: number) => void
}) {
  const matDur = Number(material.duration_seconds || 0)
  const fileUrl = material.file_url || ''

  const safeInitialIn = clamp(initialIn ?? 0, 0, Math.max(0, matDur - MIN_WINDOW_S))
  const safeInitialOut = clamp(
    initialOut ?? Math.min(targetDuration || matDur, matDur),
    safeInitialIn + MIN_WINDOW_S,
    matDur,
  )

  const [inPt, setInPt] = useState(safeInitialIn)
  const [outPt, setOutPt] = useState(safeInitialOut)
  const [previewing, setPreviewing] = useState(false)
  const [touched, setTouched] = useState(false)

  const videoRef = useRef<HTMLVideoElement | null>(null)
  const trackRef = useRef<HTMLDivElement | null>(null)
  const draggingRef = useRef<'in' | 'out' | null>(null)
  const stopPreviewRafRef = useRef<number | null>(null)

  useEffect(() => {
    if (!touched) return
    onChange(round(inPt), round(outPt))
  }, [inPt, outPt, touched, onChange])

  useEffect(() => () => {
    if (stopPreviewRafRef.current !== null) cancelAnimationFrame(stopPreviewRafRef.current)
  }, [])

  if (!matDur || matDur <= 0 || !fileUrl) {
    return (
      <div className="rounded-md border border-amber-200 bg-amber-50 p-3 text-xs text-amber-700">
        该素材未识别到时长信息，无法手动裁剪。请改用「自动分镜」选项，或重新上传素材。
      </div>
    )
  }

  const winLen = outPt - inPt
  const diff = winLen - targetDuration

  const onPointerDown = (handle: 'in' | 'out') => (e: React.PointerEvent) => {
    e.preventDefault()
    e.stopPropagation()
    draggingRef.current = handle
    setTouched(true)
    ;(e.target as HTMLElement).setPointerCapture(e.pointerId)
  }
  const onPointerMove = (e: React.PointerEvent) => {
    if (!draggingRef.current || !trackRef.current) return
    const rect = trackRef.current.getBoundingClientRect()
    const ratio = clamp((e.clientX - rect.left) / rect.width, 0, 1)
    const t = ratio * matDur
    if (draggingRef.current === 'in') {
      const next = clamp(t, 0, outPt - MIN_WINDOW_S)
      setInPt(next)
      seek(next)
    } else {
      const next = clamp(t, inPt + MIN_WINDOW_S, matDur)
      setOutPt(next)
      seek(next - 0.05)
    }
  }
  const onPointerUp = (e: React.PointerEvent) => {
    if (!draggingRef.current) return
    ;(e.target as HTMLElement).releasePointerCapture?.(e.pointerId)
    draggingRef.current = null
  }

  const seek = (t: number) => {
    const v = videoRef.current
    if (!v) return
    try {
      v.pause()
      v.currentTime = clamp(t, 0, matDur)
    } catch {
      /* video not ready, ignore */
    }
  }

  const playSelection = () => {
    const v = videoRef.current
    if (!v) return
    try {
      v.currentTime = inPt
      void v.play()
      setPreviewing(true)
      const tick = () => {
        if (!v || v.paused) {
          setPreviewing(false)
          return
        }
        if (v.currentTime >= outPt - 0.02) {
          v.pause()
          setPreviewing(false)
          return
        }
        stopPreviewRafRef.current = requestAnimationFrame(tick)
      }
      stopPreviewRafRef.current = requestAnimationFrame(tick)
    } catch {
      setPreviewing(false)
    }
  }

  const reset = () => {
    setInPt(0)
    setOutPt(Math.min(targetDuration || matDur, matDur))
    setTouched(true)
    seek(0)
  }

  const inPct = (inPt / matDur) * 100
  const outPct = (outPt / matDur) * 100

  return (
    <div className="rounded-md border border-slate-200 bg-white p-3 space-y-2">
      <video
        ref={videoRef}
        src={fileUrl}
        className="w-full max-h-[260px] rounded-md bg-black"
        controls
        preload="metadata"
        onLoadedMetadata={() => seek(inPt)}
      />

      <div
        ref={trackRef}
        className="relative h-7 rounded bg-slate-100 select-none touch-none"
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
        onPointerCancel={onPointerUp}
      >
        {/* 选中区间 */}
        <div
          className="absolute top-0 bottom-0 bg-indigo-100 border-y border-indigo-400 pointer-events-none"
          style={{ left: `${inPct}%`, width: `${Math.max(0.5, outPct - inPct)}%` }}
        />
        {/* 左手柄 */}
        <button
          type="button"
          aria-label="裁剪起点"
          onPointerDown={onPointerDown('in')}
          className="absolute top-0 bottom-0 w-3 -translate-x-1/2 rounded-sm bg-indigo-600 cursor-ew-resize hover:bg-indigo-700"
          style={{ left: `${inPct}%` }}
        />
        {/* 右手柄 */}
        <button
          type="button"
          aria-label="裁剪终点"
          onPointerDown={onPointerDown('out')}
          className="absolute top-0 bottom-0 w-3 -translate-x-1/2 rounded-sm bg-indigo-600 cursor-ew-resize hover:bg-indigo-700"
          style={{ left: `${outPct}%` }}
        />
      </div>

      <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-slate-600">
        <NumField label="起点" value={inPt} step={0.1} min={0} max={outPt - MIN_WINDOW_S}
          onChange={(v) => { setInPt(v); setTouched(true); seek(v) }} />
        <NumField label="终点" value={outPt} step={0.1} min={inPt + MIN_WINDOW_S} max={matDur}
          onChange={(v) => { setOutPt(v); setTouched(true); seek(v - 0.05) }} />
        <span className="text-slate-700">时长 <b>{winLen.toFixed(2)}s</b></span>
        <span className={cn(
          'text-[11px]',
          Math.abs(diff) < 0.05 ? 'text-emerald-600' :
          diff > 0 ? 'text-amber-600' : 'text-rose-600',
        )}>
          目标 {targetDuration.toFixed(1)}s · 差 {diff >= 0 ? '+' : ''}{diff.toFixed(2)}s
        </span>
        <span className="ml-auto flex gap-2">
          <button
            type="button"
            onClick={playSelection}
            className="rounded border border-indigo-300 bg-white px-2 py-1 text-[11px] text-indigo-700 hover:bg-indigo-50"
          >
            {previewing ? '播放中…' : '预览所选'}
          </button>
          <button
            type="button"
            onClick={reset}
            className="rounded border border-slate-300 bg-white px-2 py-1 text-[11px] text-slate-600 hover:bg-slate-50"
          >
            重置
          </button>
        </span>
      </div>

      <p className="text-[11px] text-slate-500 leading-snug">
        手动裁剪会直接覆盖此分镜的时长，后续分镜会自动顺移、整轨总长伸缩；BGM/字幕/包装项会同步重铺。
      </p>
    </div>
  )
}

function NumField({
  label, value, min, max, step, onChange,
}: {
  label: string
  value: number
  min: number
  max: number
  step: number
  onChange: (v: number) => void
}) {
  return (
    <label className="flex items-center gap-1">
      <span>{label}</span>
      <input
        type="number"
        className="w-16 rounded border border-slate-300 px-1 py-0.5 text-right text-xs"
        value={Number.isFinite(value) ? value.toFixed(1) : 0}
        min={min}
        max={max}
        step={step}
        onChange={(e) => {
          const next = Number(e.target.value)
          if (!Number.isFinite(next)) return
          onChange(clamp(next, min, max))
        }}
      />
      <span className="text-slate-400">s</span>
    </label>
  )
}

function clamp(v: number, lo: number, hi: number): number {
  if (hi < lo) return lo
  return Math.max(lo, Math.min(hi, v))
}
function round(v: number): number {
  return Math.round(v * 1000) / 1000
}
