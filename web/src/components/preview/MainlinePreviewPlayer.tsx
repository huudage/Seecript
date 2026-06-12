/**
 * stage-80 (2026-06-12)：主轨预览播放器（替换 Remotion <Player> + PlanComposition <Video>）。
 *
 * 根因
 * ====
 * Remotion <Video startFrom endAt> 内部按帧重设 video.currentTime，HTMLVideoElement
 * 的 seek 不是 frame-accurate，落到关键帧附近偶发回退几帧 → 表现为「单镜头内突然
 * 复读前 0.X 秒内容」。stage-79 的 ffmpeg 修复只覆盖渲染输出，浏览器侧无能为力。
 *
 * 方案
 * ====
 * 后端把 plan.main_track 拼成单 mp4 → 前端单 <video> 顺序播。零 currentTime 抖动，
 * 零关键帧回退。代价：plan 改了要等后端 3-15s 合成；包装/字幕/BGM 暂不在预览里。
 *
 * 接口
 * ====
 * 与原 PlanPlayer 兼容：保留 `seek(seconds)` 方法、`onTimeUpdate(seconds)` 回调，
 * 调用方（Compose.tsx step2 + step3）零改动。
 *
 * 行为
 * ====
 * - 挂载 / plan.signature 变化 → 调 buildMainlinePreview() 拿 url
 * - 后端合成期间显示「正在生成预览...」覆层，结束后切到真正的 <video>
 * - <video> 用浏览器原生 controls，扣掉 download 选项
 * - section 范围 props（inSeconds/outSeconds）：仅 SectionPreviewCard 用到，本组件
 *   接到后会 currentTime=in，到 out 时暂停（实现「只看一段」体验）
 */
import { forwardRef, useCallback, useEffect, useImperativeHandle, useRef, useState } from 'react'

import type { Plan } from '@/types/schemas'
import { buildMainlinePreview, type PreviewMainlineResponse } from '@/api/plan'

export interface MainlinePreviewPlayerHandle {
  seek: (seconds: number) => void
  /** 触发后端重新合成（plan 变更后调用方手动刷新）。 */
  refresh: () => Promise<void>
}

interface Props {
  plan: Plan
  /** 每帧（实际是浏览器 timeupdate 节奏）回调当前秒，驱动外部时间轴游标。 */
  onTimeUpdate?: (seconds: number) => void
  /** 段落预览：currentTime 限制到 [inSeconds, outSeconds]，到点暂停回 inSeconds。 */
  inSeconds?: number
  outSeconds?: number
  /** 控件区域最大高度（默认 520px，section 卡用 360）。 */
  maxHeight?: number
}

export const MainlinePreviewPlayer = forwardRef<MainlinePreviewPlayerHandle, Props>(
  function MainlinePreviewPlayer(
    { plan, onTimeUpdate, inSeconds, outSeconds, maxHeight = 520 },
    ref,
  ) {
    const videoRef = useRef<HTMLVideoElement>(null)
    const [meta, setMeta] = useState<PreviewMainlineResponse | null>(null)
    const [building, setBuilding] = useState(false)
    const [error, setError] = useState<string | null>(null)

    // 仅当影响主轨视觉的字段变了，才重新拉预览。
    // 注意：这里手动列字段，避免 plan 对象引用变化（不影响主轨的 packaging/BGM 编辑）触发误刷新。
    const trackKey = plan.main_track
      .map((sc) => `${sc.scene_id}|${sc.source}|${sc.source_ref ?? ''}|${sc.in_point ?? 0}|${sc.out_point ?? 0}|${sc.duration}`)
      .join(';')

    const build = useCallback(async () => {
      setBuilding(true)
      setError(null)
      try {
        const resp = await buildMainlinePreview(plan.plan_id)
        setMeta(resp)
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e))
      } finally {
        setBuilding(false)
      }
    }, [plan.plan_id])

    // 主轨变 → 后端拉新预览
    useEffect(() => {
      if (!plan.plan_id) return
      void build()
      // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [plan.plan_id, trackKey])

    // 暴露 seek / refresh
    useImperativeHandle(
      ref,
      () => ({
        seek: (seconds: number) => {
          const v = videoRef.current
          if (!v) return
          const t = Math.max(0, seconds)
          v.currentTime = t
        },
        refresh: build,
      }),
      [build],
    )

    // 段落范围：进入区间 + 自动暂停
    useEffect(() => {
      const v = videoRef.current
      if (!v) return
      if (inSeconds == null || outSeconds == null) return
      // 用 metadata loaded 后一次性 seek 到段首
      const onLoaded = () => {
        v.currentTime = inSeconds
      }
      v.addEventListener('loadedmetadata', onLoaded)
      // 已经 ready 的话直接跳
      if (v.readyState >= 1) {
        v.currentTime = inSeconds
      }
      return () => v.removeEventListener('loadedmetadata', onLoaded)
    }, [inSeconds, outSeconds, meta?.url])

    // timeupdate 上抛 + 段落自动暂停
    useEffect(() => {
      const v = videoRef.current
      if (!v) return
      const onUpdate = () => {
        const t = v.currentTime
        onTimeUpdate?.(t)
        if (outSeconds != null && t >= outSeconds) {
          v.pause()
          // 回到段首方便重看
          if (inSeconds != null) v.currentTime = inSeconds
        }
      }
      v.addEventListener('timeupdate', onUpdate)
      return () => v.removeEventListener('timeupdate', onUpdate)
    }, [onTimeUpdate, inSeconds, outSeconds])

    const aspect = (() => {
      const r = (plan.settings.aspect_ratio ?? '9:16').toString()
      // "9:16" / "16:9" / "1:1"
      const m = r.match(/^(\d+):(\d+)$/)
      if (!m) return '9 / 16'
      return `${m[1]} / ${m[2]}`
    })()

    return (
      <div
        className="relative overflow-hidden rounded-md bg-black"
        style={{ aspectRatio: aspect, maxHeight, width: '100%' }}
      >
        {meta?.url ? (
          <video
            ref={videoRef}
            // 后端 url 是 /preview/<file>.mp4；vite dev 走 proxy；prod 同源
            src={meta.url}
            controls
            controlsList="nodownload noremoteplayback"
            playsInline
            preload="metadata"
            style={{ width: '100%', height: '100%', display: 'block', objectFit: 'contain' }}
          />
        ) : (
          <div className="absolute inset-0 flex items-center justify-center text-[11px] text-white/70">
            {error ? `预览生成失败：${error}` : building ? '正在生成预览…' : '等待预览…'}
          </div>
        )}

        {/* 顶部状态条：合成中 / 刷新 */}
        <div className="pointer-events-none absolute inset-x-0 top-0 flex items-center justify-between px-2 py-1 text-[10px] text-white/70">
          <span>{building ? '后端合成中…' : meta ? '主轨预览' : ''}</span>
          {error ? (
            <button
              type="button"
              className="pointer-events-auto rounded bg-white/10 px-2 py-0.5 text-white/80 hover:bg-white/20"
              onClick={() => void build()}
            >
              重试
            </button>
          ) : null}
        </div>
      </div>
    )
  },
)
