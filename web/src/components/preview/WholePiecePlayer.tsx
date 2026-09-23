/**
 * PRD-v2 F7 / U5：整片预览不走 Remotion。
 * 按主轨顺序串播原生 video / 静帧 / 字卡，包装项用 DOM 叠在画面上。
 * 这是 PRD 写明的保底形态（WebAV 未接入时的整片预览）。
 */
import {
  forwardRef,
  useCallback,
  useEffect,
  useImperativeHandle,
  useMemo,
  useRef,
  useState,
} from 'react'

import { resolveSceneMedia } from '@/lib/scene_url'
import type { Material, Plan, Scene } from '@/types/schemas'

export interface WholePieceHandle {
  seek: (seconds: number) => void
  play: () => void
  pause: () => void
  player: null
}

interface Props {
  plan: Plan
  materials: Material[]
  /** CSS aspect-ratio, e.g. "9 / 16". */
  aspectRatio?: string
  onTimeUpdate?: (seconds: number) => void
}

function sceneAt(scenes: Scene[], seconds: number): Scene | null {
  if (scenes.length === 0) return null
  const hit = scenes.find((scene, index) => {
    const end = index === scenes.length - 1
      ? scene.start + scene.duration + 0.001
      : scenes[index + 1].start
    return seconds >= scene.start && seconds < end
  })
  return hit ?? scenes[scenes.length - 1]
}

function isStill(scene: Scene, url: string, materials: Material[]): boolean {
  const material = materials.find((item) => item.material_id === scene.source_ref)
  if (material?.media_type === 'image' || scene.source === 'aigc_image') return true
  return /\.(png|jpe?g|webp|gif)(\?|$)/i.test(url)
}

export const WholePiecePlayer = forwardRef<WholePieceHandle, Props>(function WholePiecePlayer(
  { plan, materials, aspectRatio = '9 / 16', onTimeUpdate },
  ref,
) {
  const scenes = useMemo(
    () => [...plan.main_track].sort((a, b) => a.start - b.start),
    [plan.main_track],
  )
  const [playhead, setPlayhead] = useState(0)
  const [playing, setPlaying] = useState(false)
  const videoRef = useRef<HTMLVideoElement>(null)
  const playingRef = useRef(false)
  const playheadRef = useRef(0)
  const rafRef = useRef(0)

  const publish = useCallback((seconds: number) => {
    const next = Math.max(0, seconds)
    playheadRef.current = next
    setPlayhead(next)
    onTimeUpdate?.(next)
  }, [onTimeUpdate])

  const active = sceneAt(scenes, playhead) ?? scenes[0] ?? null
  const media = active ? resolveSceneMedia(active, materials) : { kind: 'text' as const, text: '没有可播放的镜头' }
  const still = active && media.kind !== 'text' ? isStill(active, media.url, materials) : false

  const seek = useCallback((seconds: number) => {
    const scene = sceneAt(scenes, seconds)
    publish(seconds)
    const video = videoRef.current
    if (!scene || !video) return
    const resolved = resolveSceneMedia(scene, materials)
    if (resolved.kind === 'text' || isStill(scene, resolved.kind === 'video' ? resolved.url : '', materials)) {
      return
    }
    const offset = Math.max(0, scene.in_point + (seconds - scene.start))
    if (Math.abs(video.currentTime - offset) > 0.05) video.currentTime = offset
  }, [materials, publish, scenes])

  const play = useCallback(() => {
    playingRef.current = true
    setPlaying(true)
    void videoRef.current?.play().catch(() => undefined)
  }, [])

  const pause = useCallback(() => {
    playingRef.current = false
    setPlaying(false)
    videoRef.current?.pause()
  }, [])

  useImperativeHandle(ref, () => ({ seek, play, pause, player: null }), [pause, play, seek])

  useEffect(() => {
    const lastNow = { current: 0 }
    const tick = (now: number) => {
      rafRef.current = requestAnimationFrame(tick)
      if (!playingRef.current) {
        lastNow.current = now
        return
      }
      const scene = sceneAt(scenes, playheadRef.current)
      if (!scene) return
      const resolved = resolveSceneMedia(scene, materials)
      const video = videoRef.current
      const useVideoClock = resolved.kind === 'video'
        && video
        && !isStill(scene, resolved.url, materials)
        && !video.paused
      let next = playheadRef.current
      if (useVideoClock && video) {
        next = scene.start + Math.max(0, video.currentTime - scene.in_point)
      } else {
        const delta = lastNow.current ? (now - lastNow.current) / 1000 : 0
        next = playheadRef.current + delta
      }
      lastNow.current = now
      const end = scene.start + scene.duration
      if (next >= end - 0.02) {
        const index = scenes.findIndex((item) => item.scene_id === scene.scene_id)
        const following = scenes[index + 1]
        if (!following) {
          playingRef.current = false
          setPlaying(false)
          publish(end)
          video?.pause()
          return
        }
        publish(following.start)
        return
      }
      publish(next)
    }
    rafRef.current = requestAnimationFrame(tick)
    return () => cancelAnimationFrame(rafRef.current)
  }, [materials, publish, scenes])

  const activeId = active?.scene_id ?? ''
  const mediaUrl = media.kind === 'text' ? '' : media.url
  useEffect(() => {
    const video = videoRef.current
    const scene = scenes.find((item) => item.scene_id === activeId)
    if (!video || !scene || !mediaUrl || still) return
    const offset = Math.max(0, scene.in_point + (playheadRef.current - scene.start))
    if (video.getAttribute('src') !== mediaUrl) {
      video.src = mediaUrl
    }
    const onReady = () => {
      if (Math.abs(video.currentTime - offset) > 0.08) video.currentTime = offset
      if (playingRef.current) void video.play().catch(() => undefined)
    }
    video.addEventListener('loadeddata', onReady)
    if (video.readyState >= 2) onReady()
    return () => video.removeEventListener('loadeddata', onReady)
  }, [activeId, mediaUrl, scenes, still])

  const overlays = (plan.packaging_track ?? []).filter((item) => (
    playhead >= item.start
    && playhead < item.end
    && (item.kind === 'subtitle' || item.kind === 'title_bar' || item.kind === 'cover' || item.kind === 'sticker')
    && (item.text ?? '').trim().length > 0
  ))

  return (
    <div className="relative w-full bg-black text-white" style={{ aspectRatio }}>
      {media.kind === 'video' && !still ? (
        <video ref={videoRef} className="h-full w-full object-cover" playsInline />
      ) : media.kind !== 'text' ? (
        <img src={media.url} alt="" className="h-full w-full object-cover" />
      ) : (
        <div className="flex h-full w-full items-center justify-center px-6 text-center text-sm leading-relaxed">
          {[active?.text_card_spec?.main_text, active?.text_card_spec?.sub_text].filter(Boolean).join('\n') || media.text}
        </div>
      )}
      {overlays.map((item) => (
        <div
          key={item.item_id}
          className={
            item.kind === 'subtitle'
              ? 'absolute inset-x-3 bottom-4 text-center text-sm font-medium drop-shadow'
              : item.kind === 'title_bar'
                ? 'absolute inset-x-3 top-4 text-center text-base font-semibold drop-shadow'
                : 'absolute inset-x-6 top-1/3 text-center text-lg font-semibold drop-shadow'
          }
        >
          {item.text}
        </div>
      ))}
      <button
        type="button"
        onClick={() => {
          if (playing) {
            playingRef.current = false
            setPlaying(false)
            videoRef.current?.pause()
            return
          }
          play()
        }}
        className="absolute bottom-3 left-3 rounded bg-black/60 px-2 py-1 text-[11px]"
      >
        {playing ? '暂停' : '播放'}
      </button>
    </div>
  )
})
