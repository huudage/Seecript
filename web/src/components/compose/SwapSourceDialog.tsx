import { useEffect, useMemo, useState } from 'react'

import {
  getMaterialShotFitScores,
  swapSceneSource,
  type SceneSwapSourceRequest,
  type ShotFitScoreItem,
} from '@/api/plan'
import { cn } from '@/lib/utils'
import type { Material, Plan, Scene, ShotPlan } from '@/types/schemas'

import { MaterialTrimPanel } from './MaterialTrimPanel'

/**
 * stage-26 PR-N.5 重构：单镜换源弹窗。
 *
 * 4 个 tab 平级：真实素材 / 字卡占位 / AI 生图 / AI 视频。
 * 弹窗时按 initialMode 落到指定 tab；用户可在 tab 间切换无需关闭。
 *
 * 真实素材 tab 需要项目素材列表（materials prop），点击素材卡选中；
 * 视频素材若已预处理出 shots>1，再选具体哪一镜（material_shot_index）。
 */
export type SwapSourceMode = 'user_material' | 'text_card' | 'aigc_image' | 'aigc_t2v'

const MODE_LABEL: Record<SwapSourceMode, string> = {
  user_material: '真实素材',
  text_card: '字卡占位',
  aigc_image: 'AI 生图',
  aigc_t2v: 'AI 视频',
}

const MODE_HINT: Record<SwapSourceMode, string> = {
  user_material: '从项目素材库选一段画面/视频',
  text_card: '即时生成字卡占位（无外部调用）',
  aigc_image: 'Seedream 同步出图，~6-15s',
  aigc_t2v: 'Seedance 同步轮询，最长 ~180s',
}

export function SwapSourceDialog({
  open,
  initialMode,
  shot,
  scene,
  planId,
  materials,
  onClose,
  onSaved,
}: {
  open: boolean
  initialMode: SwapSourceMode
  shot: ShotPlan
  scene: Scene | null
  planId: string
  materials: Material[]
  onClose: () => void
  onSaved: (plan: Plan) => void
}) {
  const [mode, setMode] = useState<SwapSourceMode>(initialMode)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)

  // 字卡占位
  const [mainText, setMainText] = useState(shot.subject ?? '')
  const [subText, setSubText] = useState('')

  // AI 生图 / 视频共用提示
  const [hintText, setHintText] = useState('')

  // 真实素材选择
  const [pickedMaterial, setPickedMaterial] = useState<string | null>(null)
  const [pickedShotIdx, setPickedShotIdx] = useState<number | null>(null)
  // stage-29 手动裁剪：与 pickedShotIdx 互斥
  const [manualIn, setManualIn] = useState<number | null>(null)
  const [manualOut, setManualOut] = useState<number | null>(null)
  // stage-77 切片适配度评分：选中 material 后异步拉，按 shot_index 映射好给 picker 展示
  const [shotScores, setShotScores] = useState<Record<number, ShotFitScoreItem>>({})
  const [scoresLoading, setScoresLoading] = useState(false)

  const sceneId = scene?.scene_id ?? null
  const targetMaterial = pickedMaterial
    ? materials.find((m) => m.material_id === pickedMaterial) ?? null
    : null

  useEffect(() => {
    if (!open) return
    setMode(initialMode)
    setErr(null)
    setMainText(shot.subject ?? '')
    setSubText('')
    setHintText('')
    setPickedMaterial(null)
    setPickedShotIdx(null)
    setManualIn(null)
    setManualOut(null)
    setShotScores({})
  }, [open, initialMode, shot.subject])

  // stage-77：选中 material 时拉每个 shot 对当前 scene 的适配度评分
  useEffect(() => {
    if (!open) return
    if (!sceneId) return
    if (!pickedMaterial) {
      setShotScores({})
      return
    }
    let cancelled = false
    setScoresLoading(true)
    void getMaterialShotFitScores(planId, sceneId, pickedMaterial)
      .then((resp) => {
        if (cancelled) return
        const map: Record<number, ShotFitScoreItem> = {}
        for (const s of resp.scores) map[s.shot_index] = s
        setShotScores(map)
      })
      .catch(() => {
        if (!cancelled) setShotScores({})
      })
      .finally(() => {
        if (!cancelled) setScoresLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [open, planId, sceneId, pickedMaterial])

  const promptFallback = useMemo(
    () =>
      [shot.subject, shot.visual, shot.narration]
        .filter((s) => s && s.trim())
        .join('；'),
    [shot.subject, shot.visual, shot.narration],
  )

  if (!open) return null

  const callSwap = async (body: SceneSwapSourceRequest) => {
    if (!sceneId) {
      setErr('当前镜头未关联 Scene，无法换源')
      return
    }
    setBusy(true)
    setErr(null)
    try {
      const fresh = await swapSceneSource(planId, sceneId, body)
      onSaved(fresh)
      onClose()
    } catch (e) {
      setErr(e instanceof Error ? e.message : '换源失败')
    } finally {
      setBusy(false)
    }
  }

  const apply = () => {
    if (busy) return
    if (mode === 'text_card') {
      if (!mainText.trim()) {
        setErr('请先填主文案——空文案会落『（待补全）』占位')
        return
      }
      void callSwap({ source: 'text_card', main_text: mainText, sub_text: subText })
    } else if (mode === 'aigc_image') {
      void callSwap({ source: 'aigc_image', prompt_hint: hintText || undefined })
    } else if (mode === 'aigc_t2v') {
      void callSwap({ source: 'aigc_t2v', prompt_hint: hintText || undefined })
    } else if (mode === 'user_material') {
      if (!pickedMaterial) {
        setErr('请先在下方选一个素材')
        return
      }
      // 手动裁剪优先：in+out 同时存在 → 让后端按用户窗口写 scene.in_point/out_point/duration
      if (manualIn !== null && manualOut !== null) {
        if (manualOut - manualIn < 0.5) {
          setErr('裁剪窗口太短，至少 0.5s')
          return
        }
        void callSwap({
          source: 'user_material',
          material_id: pickedMaterial,
          material_in_point: manualIn,
          material_out_point: manualOut,
        })
        return
      }
      void callSwap({
        source: 'user_material',
        material_id: pickedMaterial,
        material_shot_index: pickedShotIdx ?? undefined,
      })
    }
  }

  const applyDisabled =
    busy ||
    (mode === 'text_card' && !mainText.trim()) ||
    (mode === 'user_material' && !pickedMaterial)

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4"
      onClick={busy ? undefined : onClose}
    >
      <div
        className="flex w-full max-w-2xl flex-col rounded-lg border border-border bg-card shadow-xl"
        style={{ maxHeight: '85vh' }}
        onClick={(e) => e.stopPropagation()}
      >
        <header className="flex items-start justify-between border-b border-border px-4 py-3">
          <div>
            <h3 className="text-sm font-semibold">
              换源 ·{' '}
              <span className="font-mono text-xs text-muted-foreground">
                {sceneId ?? '（未关联 Scene）'}
              </span>
            </h3>
            <p className="mt-0.5 text-xs text-muted-foreground">
              第 {shot.order + 1} 镜 · {shot.duration_seconds.toFixed(1)}s ·{' '}
              {shot.subject || '（无主体）'}
            </p>
          </div>
          <button
            onClick={onClose}
            disabled={busy}
            className="rounded text-lg leading-none text-muted-foreground hover:text-foreground disabled:opacity-50"
            aria-label="关闭"
          >
            ×
          </button>
        </header>

        {/* tab bar */}
        <div className="flex shrink-0 border-b border-border bg-background/40 px-2 py-1.5">
          {(Object.keys(MODE_LABEL) as SwapSourceMode[]).map((m) => (
            <button
              key={m}
              onClick={() => {
                setMode(m)
                setErr(null)
              }}
              disabled={busy}
              className={cn(
                'flex-1 rounded px-2 py-1 text-xs font-medium transition-colors disabled:opacity-50',
                mode === m
                  ? 'bg-primary text-primary-foreground'
                  : 'text-muted-foreground hover:bg-secondary',
              )}
            >
              {MODE_LABEL[m]}
            </button>
          ))}
        </div>

        <div className="flex-1 overflow-y-auto px-4 py-3">
          <p className="mb-2 text-xs text-muted-foreground">{MODE_HINT[mode]}</p>

          {mode === 'text_card' && (
            <div className="space-y-2">
              <label className="block space-y-0.5">
                <span className="text-xs text-muted-foreground">主文案（≤24 字，必填）</span>
                <input
                  value={mainText}
                  maxLength={24}
                  disabled={busy}
                  onChange={(e) => setMainText(e.target.value)}
                  placeholder={shot.subject || '主文案'}
                  className="w-full rounded border border-border bg-background px-2 py-1 text-xs disabled:opacity-60"
                  autoFocus
                />
              </label>
              <label className="block space-y-0.5">
                <span className="text-xs text-muted-foreground">副文案（≤40 字，可空）</span>
                <input
                  value={subText}
                  maxLength={40}
                  disabled={busy}
                  onChange={(e) => setSubText(e.target.value)}
                  placeholder="副文案"
                  className="w-full rounded border border-border bg-background px-2 py-1 text-xs disabled:opacity-60"
                />
              </label>
              <p className="rounded bg-muted/40 px-2 py-1 text-xs text-muted-foreground">
                字卡画面由 ffmpeg 即烧——背景色 / 字号 / 字体走包装样板，瞬时落入轨道。
              </p>
            </div>
          )}

          {(mode === 'aigc_image' || mode === 'aigc_t2v') && (
            <div className="space-y-2">
              <label className="block space-y-0.5">
                <span className="text-xs text-muted-foreground">
                  额外提示词（可空，默认用本镜画面 + 口播 + 主体拼接）
                </span>
                <textarea
                  value={hintText}
                  rows={3}
                  maxLength={200}
                  disabled={busy}
                  onChange={(e) => setHintText(e.target.value)}
                  placeholder={`默认提示：${promptFallback || '本镜主体 + 画面 + 口播'}`}
                  className="w-full resize-y rounded border border-border bg-background px-2 py-1 text-xs disabled:opacity-60"
                />
              </label>
              {shot.targets && shot.targets.length > 0 && (
                <div className="rounded bg-muted/40 px-2 py-1.5">
                  <div className="mb-1 text-xs text-muted-foreground">本镜目标（自动注入 prompt）</div>
                  <ul className="space-y-0.5">
                    {shot.targets.map((t, i) => (
                      <li key={i} className="text-xs">
                        <span className="font-mono text-xs text-muted-foreground">{t.kind}</span>{' '}
                        <span className="font-semibold">{t.name}</span>
                        {t.visual_hint && (
                          <span className="ml-1 text-muted-foreground">· {t.visual_hint}</span>
                        )}
                      </li>
                    ))}
                  </ul>
                </div>
              )}
              {mode === 'aigc_t2v' && (
                <p className="rounded bg-amber-500/10 px-2 py-1 text-xs text-amber-700 dark:text-amber-300">
                  ⚠ AI 视频同步轮询最长 180s；生成期间请勿关闭页面。
                </p>
              )}
            </div>
          )}

          {mode === 'user_material' && (
            <UserMaterialPicker
              materials={materials}
              pickedMaterial={pickedMaterial}
              pickedShotIdx={pickedShotIdx}
              onPickMaterial={(id) => {
                setPickedMaterial(id)
                setPickedShotIdx(null)
                setManualIn(null)
                setManualOut(null)
              }}
              onPickShot={(idx) => {
                setPickedShotIdx(idx)
                setManualIn(null)
                setManualOut(null)
              }}
              targetMaterial={targetMaterial}
              targetDuration={scene?.duration ?? shot.duration_seconds}
              manualIn={manualIn}
              manualOut={manualOut}
              onTrimChange={(i, o) => {
                setManualIn(i)
                setManualOut(o)
                setPickedShotIdx(null)
              }}
              shotScores={shotScores}
              scoresLoading={scoresLoading}
            />
          )}
        </div>

        {err && (
          <p className="border-t border-destructive/30 bg-destructive/10 px-4 py-1.5 text-xs text-destructive">
            {err}
          </p>
        )}

        <footer className="flex shrink-0 items-center justify-end gap-2 border-t border-border px-4 py-2">
          <button
            onClick={onClose}
            disabled={busy}
            className="rounded border border-border bg-background px-3 py-1 text-xs hover:bg-secondary disabled:opacity-50"
          >
            取消
          </button>
          <button
            onClick={apply}
            disabled={applyDisabled}
            className={cn(
              'rounded bg-primary px-3 py-1 text-xs font-medium text-primary-foreground',
              applyDisabled && 'cursor-not-allowed opacity-60',
            )}
          >
            {busy ? (mode === 'aigc_t2v' ? '生成中…（最长 3 分钟）' : '生成中…') : '应用'}
          </button>
        </footer>
      </div>
    </div>
  )
}

function UserMaterialPicker({
  materials,
  pickedMaterial,
  pickedShotIdx,
  onPickMaterial,
  onPickShot,
  targetMaterial,
  targetDuration,
  manualIn,
  manualOut,
  onTrimChange,
  shotScores,
  scoresLoading,
}: {
  materials: Material[]
  pickedMaterial: string | null
  pickedShotIdx: number | null
  onPickMaterial: (id: string) => void
  onPickShot: (idx: number | null) => void
  targetMaterial: Material | null
  targetDuration: number
  manualIn: number | null
  manualOut: number | null
  onTrimChange: (inPt: number, outPt: number) => void
  shotScores: Record<number, ShotFitScoreItem>
  scoresLoading: boolean
}) {
  if (materials.length === 0) {
    return (
      <div className="rounded-md border border-dashed border-border bg-background/30 p-6 text-center text-xs text-muted-foreground">
        项目素材库还是空的——回到上方上传一些图/视频，或从系统素材库添加。
      </div>
    )
  }
  return (
    <div className="space-y-2">
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-3">
        {materials.map((m) => {
          const active = m.material_id === pickedMaterial
          return (
            <button
              key={m.material_id}
              type="button"
              onClick={() => onPickMaterial(m.material_id)}
              className={cn(
                'overflow-hidden rounded border bg-background text-left transition-colors',
                active
                  ? 'border-primary ring-2 ring-primary/40'
                  : 'border-border hover:border-primary/60',
              )}
            >
              <div className="aspect-video bg-muted">
                {m.thumbnail_url ? (
                  <img
                    src={m.thumbnail_url}
                    alt={m.filename}
                    className="h-full w-full object-cover"
                    loading="lazy"
                  />
                ) : (
                  <div className="flex h-full w-full items-center justify-center text-xs text-muted-foreground">
                    {m.media_type === 'audio' ? '🎵' : m.media_type === 'video' ? '🎬' : '🖼'}
                  </div>
                )}
              </div>
              <div className="px-1.5 py-1">
                <div className="truncate text-xs font-medium">{m.filename}</div>
                <div className="flex items-center gap-1 text-xs text-muted-foreground">
                  <span className="font-mono">{m.media_type}</span>
                  {m.shots && m.shots.length > 1 && <span>· {m.shots.length} 镜</span>}
                </div>
              </div>
            </button>
          )
        })}
      </div>

      {/* stage-29 视频素材手动裁剪：仅 video + 已识别时长才出 */}
      {targetMaterial &&
        targetMaterial.media_type === 'video' &&
        (targetMaterial.duration_seconds ?? 0) > 0 && (
          <div className="space-y-1.5">
            <div className="flex items-center justify-between">
              <span className="text-xs font-semibold text-muted-foreground">
                手动裁剪（直接覆盖此分镜的时长）
              </span>
              {(manualIn !== null || manualOut !== null) && (
                <button
                  type="button"
                  onClick={() => onTrimChange(NaN, NaN)}
                  className="hidden text-xs text-muted-foreground hover:text-foreground"
                >
                  清除裁剪
                </button>
              )}
            </div>
            <MaterialTrimPanel
              material={targetMaterial}
              targetDuration={targetDuration}
              initialIn={manualIn ?? 0}
              initialOut={
                manualOut ??
                Math.min(targetDuration, targetMaterial.duration_seconds ?? targetDuration)
              }
              onChange={onTrimChange}
            />
          </div>
        )}

      {/* shot index picker for video with multiple shots */}
      {targetMaterial && targetMaterial.shots && targetMaterial.shots.length > 1 && (
        <div className="rounded border border-border bg-background/40 p-2">
          <div className="mb-1.5 flex items-center justify-between">
            <span className="text-xs font-semibold text-muted-foreground">
              选具体哪一镜
              <span className="ml-1 text-muted-foreground/70">
                （右上角分数 = 与本分镜的适配度；越高越搭）
              </span>
            </span>
            {pickedShotIdx != null && (
              <button
                type="button"
                onClick={() => onPickShot(null)}
                className="text-xs text-muted-foreground hover:text-foreground"
              >
                清除
              </button>
            )}
          </div>
          <div className="grid grid-cols-3 gap-1 sm:grid-cols-4">
            {targetMaterial.shots.map((sh) => {
              const active = sh.index === pickedShotIdx
              const fit = shotScores[sh.index]
              return (
                <button
                  key={sh.index}
                  type="button"
                  onClick={() => onPickShot(sh.index)}
                  className={cn(
                    'relative overflow-hidden rounded border text-left transition-colors',
                    active
                      ? 'border-primary ring-1 ring-primary/40'
                      : 'border-border hover:border-primary/60',
                  )}
                  title={fit ? `适配度 ${fit.score_pct}/100 · ${fit.quality}` : undefined}
                >
                  <div className="aspect-video bg-muted">
                    {sh.thumbnail_url ? (
                      <img
                        src={sh.thumbnail_url}
                        alt={`shot ${sh.index}`}
                        className="h-full w-full object-cover"
                        loading="lazy"
                      />
                    ) : (
                      <div className="flex h-full w-full items-center justify-center text-xs text-muted-foreground">
                        无图
                      </div>
                    )}
                    {/* stage-77 适配度徽章：good=绿 / weak=黄 / missing=灰 */}
                    {fit && (
                      <span
                        className={cn(
                          'absolute right-1 top-1 rounded px-1 py-px text-[10px] font-bold leading-none shadow',
                          fit.quality === 'good'
                            ? 'bg-emerald-500/95 text-white'
                            : fit.quality === 'weak'
                              ? 'bg-amber-500/95 text-white'
                              : 'bg-zinc-600/85 text-white',
                        )}
                      >
                        {fit.score_pct}
                      </span>
                    )}
                    {!fit && scoresLoading && (
                      <span className="absolute right-1 top-1 rounded bg-zinc-500/70 px-1 py-px text-[10px] font-bold leading-none text-white">
                        …
                      </span>
                    )}
                  </div>
                  <div className="px-1 py-0.5 text-xs">
                    <span className="font-mono">#{sh.index}</span>{' '}
                    <span className="text-muted-foreground">{sh.duration.toFixed(1)}s</span>
                  </div>
                </button>
              )
            })}
          </div>
        </div>
      )}
    </div>
  )
}
