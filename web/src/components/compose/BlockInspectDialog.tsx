import { useEffect, useRef, useState } from 'react'

import { swapSceneSource } from '@/api/plan'
import type { Material, Plan, Scene } from '@/types/schemas'

/**
 * 左键视频块：弹窗播放 + 时间轴裁剪。
 * 字卡不播画面，只改主副文案和时长。
 * 不滚动页面，避免把画布顶出视口。
 */

interface Props {
  planId: string
  scene: Scene
  materials: Material[]
  onClose: () => void
  onPlan: (plan: Plan) => void
}

function playbackUrl(scene: Scene, materials: Material[]): string | null {
  if (scene.aigc_video_urls?.[0]) return scene.aigc_video_urls[0]
  if (scene.source === 'user_material' || scene.source === 'sample') {
    const material = materials.find((item) => item.material_id === scene.source_ref)
    return material?.file_url ?? null
  }
  return null
}

export function BlockInspectDialog({ planId, scene, materials, onClose, onPlan }: Props) {
  const isCard = scene.source === 'text_card' || scene.text_card_spec != null
  const videoRef = useRef<HTMLVideoElement>(null)
  const [mainText, setMainText] = useState(scene.text_card_spec?.main_text ?? '')
  const [subText, setSubText] = useState(scene.text_card_spec?.sub_text ?? '')
  const [cardDuration, setCardDuration] = useState(scene.text_card_spec?.duration_seconds || scene.duration)
  const [inPoint, setInPoint] = useState(scene.in_point ?? 0)
  const [outPoint, setOutPoint] = useState(scene.out_point ?? (scene.in_point ?? 0) + scene.duration)
  const [pending, setPending] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const src = playbackUrl(scene, materials)
  const material = materials.find((item) => item.material_id === scene.source_ref)
  const mediaDuration = Math.max(material?.duration_seconds ?? 0, outPoint, scene.duration, 1)
  const canTrim = scene.source === 'user_material' && !!scene.source_ref && !scene.source_ref.includes('empty')

  useEffect(() => {
    const node = videoRef.current
    if (!node || isCard) return
    const onTime = () => {
      if (node.currentTime >= outPoint) {
        node.pause()
        node.currentTime = inPoint
      }
    }
    node.addEventListener('timeupdate', onTime)
    node.currentTime = inPoint
    void node.play().catch(() => undefined)
    return () => node.removeEventListener('timeupdate', onTime)
  }, [inPoint, outPoint, isCard, src])

  const saveCard = async () => {
    setPending(true)
    setError(null)
    try {
      const next = await swapSceneSource(planId, scene.scene_id, {
        source: 'text_card',
        main_text: mainText.trim(),
        sub_text: subText.trim(),
        duration_seconds: cardDuration,
      })
      onPlan(next)
      onClose()
    } catch (err) {
      setError(err instanceof Error ? err.message : '字卡保存失败')
    } finally {
      setPending(false)
    }
  }

  const saveTrim = async () => {
    setPending(true)
    setError(null)
    try {
      const next = await swapSceneSource(planId, scene.scene_id, {
        source: 'user_material',
        material_id: scene.source_ref,
        material_in_point: inPoint,
        material_out_point: outPoint,
      })
      onPlan(next)
      onClose()
    } catch (err) {
      setError(err instanceof Error ? err.message : '裁剪失败')
    } finally {
      setPending(false)
    }
  }

  return (
    <div className="fixed inset-0 z-[70] flex items-center justify-center bg-black/45 p-4" onClick={onClose}>
      <div
        className="w-full max-w-md rounded-lg border border-border bg-card p-4 shadow-xl"
        onClick={(event) => event.stopPropagation()}
      >
        <header className="mb-3 flex items-center justify-between">
          <h3 className="text-sm font-semibold">{isCard ? '字卡参数' : '播放与裁剪'}</h3>
          <button type="button" onClick={onClose} className="text-muted-foreground hover:text-foreground" aria-label="关闭">
            ×
          </button>
        </header>

        {isCard ? (
          <div className="space-y-2 text-[12px]">
            <p className="text-[11px] text-muted-foreground">字卡不预览画面，只改参数。</p>
            <label className="block">
              <span className="mb-1 block text-muted-foreground">主文案</span>
              <input
                value={mainText}
                maxLength={24}
                onChange={(event) => setMainText(event.target.value)}
                className="w-full rounded border border-border bg-background px-2 py-1"
              />
            </label>
            <label className="block">
              <span className="mb-1 block text-muted-foreground">副文案</span>
              <input
                value={subText}
                maxLength={40}
                onChange={(event) => setSubText(event.target.value)}
                className="w-full rounded border border-border bg-background px-2 py-1"
              />
            </label>
            <label className="block">
              <span className="mb-1 block text-muted-foreground">时长（秒）</span>
              <input
                type="number"
                min={0.5}
                step={0.5}
                value={cardDuration}
                onChange={(event) => setCardDuration(Number(event.target.value))}
                className="w-full rounded border border-border bg-background px-2 py-1"
              />
            </label>
            <button
              type="button"
              disabled={pending || !mainText.trim()}
              onClick={() => void saveCard()}
              className="rounded bg-primary px-3 py-1 text-primary-foreground disabled:opacity-50"
            >
              {pending ? '保存中…' : '保存'}
            </button>
          </div>
        ) : (
          <div className="space-y-2 text-[12px]">
            {src ? (
              scene.aigc_image_url && !scene.aigc_video_urls?.[0] && scene.source === 'aigc_image' ? (
                <img src={scene.aigc_image_url} alt="" className="max-h-56 w-full rounded bg-black object-contain" />
              ) : (
                <video ref={videoRef} src={src} className="max-h-56 w-full rounded bg-black" controls playsInline />
              )
            ) : scene.aigc_image_url ? (
              <img src={scene.aigc_image_url} alt="" className="max-h-56 w-full rounded bg-black object-contain" />
            ) : (
              <p className="text-[11px] text-muted-foreground">这段还没有可播放的画面。</p>
            )}
            <label className="block">
              <span className="mb-1 flex justify-between text-muted-foreground">
                <span>入点 {inPoint.toFixed(1)}s</span>
                <span>出点 {outPoint.toFixed(1)}s</span>
              </span>
              <input
                type="range"
                min={0}
                max={mediaDuration}
                step={0.1}
                value={inPoint}
                onChange={(event) => {
                  const next = Number(event.target.value)
                  setInPoint(Math.min(next, outPoint - 0.5))
                  if (videoRef.current) videoRef.current.currentTime = Math.min(next, outPoint - 0.5)
                }}
                className="w-full"
              />
              <input
                type="range"
                min={0}
                max={mediaDuration}
                step={0.1}
                value={outPoint}
                onChange={(event) => setOutPoint(Math.max(Number(event.target.value), inPoint + 0.5))}
                className="mt-1 w-full"
              />
            </label>
            {!canTrim && (
              <p className="text-[10px] text-muted-foreground">时间轴裁剪目前作用在用户上传的素材上。</p>
            )}
            {canTrim && (
              <button
                type="button"
                disabled={pending || outPoint - inPoint < 0.5}
                onClick={() => void saveTrim()}
                className="rounded bg-primary px-3 py-1 text-primary-foreground disabled:opacity-50"
              >
                {pending ? '裁剪中…' : '按时间轴裁剪'}
              </button>
            )}
          </div>
        )}
        {error && <p className="mt-2 text-[11px] text-rose-600">{error}</p>}
      </div>
    </div>
  )
}
