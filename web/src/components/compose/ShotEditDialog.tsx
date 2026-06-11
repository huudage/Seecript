import { useEffect, useMemo, useState } from 'react'

import { patchShotFields, swapSceneSource } from '@/api/plan'
import { SECTION_LABEL } from '@/lib/sections'
import type {
  AdaptedSection,
  Material,
  Plan,
  Scene,
  ShotPlan,
} from '@/types/schemas'

import { MaterialTrimPanel } from './MaterialTrimPanel'

type SourceType = 'user_material' | 'aigc_image' | 'aigc_t2v' | 'text_card'

const SOURCE_LABEL: Record<SourceType, string> = {
  user_material: '用户素材',
  aigc_image: 'AI 单图（Seedream）',
  aigc_t2v: 'AI 视频（Seedance）',
  text_card: '字卡',
}

const SOURCE_HINT: Record<SourceType, string> = {
  user_material: '从素材库挑一段切给本镜（按本镜时长切入出点）',
  aigc_image: '调 Seedream 出图（≈10s）；先用 shot.subject+visual 当 prompt，可加 hint',
  aigc_t2v: '调 Seedance 出视频（≈2-3min 同步等待）；prompt 同上',
  text_card: '字卡画面：主文案 + 副文案，时长跟随本镜',
}

/**
 * stage-37：单镜级编辑弹窗——段块展开后点小镜调出。
 * stage-39：增加「换源」面板——除了改 subject/visual/narration，
 *           还可把本镜的 source 切到 user_material / aigc_image / aigc_t2v / text_card，
 *           走 POST /plan/{plan_id}/scene/{scene_id}/swap-source。
 */
export function ShotEditDialog({
  plan,
  scene,
  section,
  materials,
  onClose,
  onSaved,
  disabled = false,
}: {
  plan: Plan
  scene: Scene | null
  section: AdaptedSection | null
  /** 素材库（user_material 换源时挑用）。无项目素材时为空数组。 */
  materials: readonly Material[]
  onClose: () => void
  onSaved: (plan: Plan) => void
  disabled?: boolean
}) {
  const open = scene !== null && section !== null

  // 当前镜在父 section 里的 ShotPlan（subject/visual/narration 的真源）
  const shot: ShotPlan | null = useMemo(() => {
    if (!scene || !section?.shots) return null
    return section.shots.find((sh) => sh.order === scene.shot_order) ?? null
  }, [scene, section])

  // 文本编辑态
  const [subject, setSubject] = useState('')
  const [visual, setVisual] = useState('')
  const [narration, setNarration] = useState('')
  const [cameraTechnique, setCameraTechnique] = useState('')
  const [saving, setSaving] = useState(false)
  const [err, setErr] = useState<string | null>(null)

  // 换源态
  const [swapSource, setSwapSource] = useState<SourceType>('user_material')
  const [swapMaterialId, setSwapMaterialId] = useState<string>('')
  const [swapMaterialShotIdx, setSwapMaterialShotIdx] = useState<number | null>(null)
  // stage-29 手动裁剪：与 swapMaterialShotIdx 互斥；用户拖手柄就走手动路径
  const [swapManualIn, setSwapManualIn] = useState<number | null>(null)
  const [swapManualOut, setSwapManualOut] = useState<number | null>(null)
  const [swapPromptHint, setSwapPromptHint] = useState('')
  const [swapMainText, setSwapMainText] = useState('')
  const [swapSubText, setSwapSubText] = useState('')
  const [swapping, setSwapping] = useState(false)
  const [swapErr, setSwapErr] = useState<string | null>(null)

  useEffect(() => {
    if (!open) return
    setSubject(shot?.subject ?? scene?.shot_subject ?? '')
    setVisual(shot?.visual ?? '')
    setNarration(shot?.narration ?? scene?.narration ?? '')
    setCameraTechnique(shot?.camera_technique ?? '')
    setErr(null)
    // 换源默认值：currentSource = scene.source；切目标默认 user_material；其它字段清空
    const curRaw = scene?.source as string | undefined
    const cur: SourceType =
      curRaw === 'user_material' ||
      curRaw === 'aigc_image' ||
      curRaw === 'aigc_t2v' ||
      curRaw === 'text_card'
        ? curRaw
        : 'user_material'
    setSwapSource(cur)
    setSwapMaterialId(materials[0]?.material_id ?? '')
    setSwapMaterialShotIdx(null)
    setSwapManualIn(null)
    setSwapManualOut(null)
    setSwapPromptHint('')
    setSwapMainText('')
    setSwapSubText('')
    setSwapErr(null)
  }, [open, shot, scene, materials])

  useEffect(() => {
    if (!open) return
    const handler = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && !saving && !swapping) onClose()
    }
    window.addEventListener('keydown', handler)
    return () => window.removeEventListener('keydown', handler)
  }, [open, onClose, saving, swapping])

  if (!open || !scene || !section) return null

  const origSubject = shot?.subject ?? scene.shot_subject ?? ''
  const origVisual = shot?.visual ?? ''
  const origNarration = shot?.narration ?? scene.narration ?? ''
  const origCameraTechnique = shot?.camera_technique ?? ''
  const dirty =
    subject !== origSubject ||
    visual !== origVisual ||
    narration !== origNarration ||
    cameraTechnique !== origCameraTechnique

  const handleSave = async () => {
    if (!dirty) {
      onClose()
      return
    }
    setSaving(true)
    setErr(null)
    try {
      const patch: { subject?: string; visual?: string; narration?: string; camera_technique?: string } = {}
      if (subject !== origSubject) patch.subject = subject.trim()
      if (visual !== origVisual) patch.visual = visual.trim()
      if (narration !== origNarration) patch.narration = narration.trim()
      if (cameraTechnique !== origCameraTechnique) patch.camera_technique = cameraTechnique.trim()
      const fresh = await patchShotFields(plan.plan_id, scene.scene_id, patch)
      onSaved(fresh)
      onClose()
    } catch (e) {
      setErr(e instanceof Error ? e.message : '保存失败')
    } finally {
      setSaving(false)
    }
  }

  // user_material 模式拆三个独立入口（手动裁剪 / 自动镜头 / 默认首镜），
  // 用户在面板里直接看到"我点这个按钮做什么"，不再让一个含糊的"切到用户素材"
  // 同时承担三种语义。aigc / text_card 仍走 handleSwap 统一入口。
  const callSwap = async (
    body: {
      source: SourceType
      material_id?: string
      material_shot_index?: number
      material_in_point?: number
      material_out_point?: number
      prompt_hint?: string
      main_text?: string
      sub_text?: string
    },
  ) => {
    setSwapping(true)
    setSwapErr(null)
    try {
      const fresh = await swapSceneSource(plan.plan_id, scene.scene_id, body)
      onSaved(fresh)
      onClose()
    } catch (e) {
      setSwapErr(e instanceof Error ? e.message : '换源失败')
    } finally {
      setSwapping(false)
    }
  }

  const applyManualTrim = () => {
    if (!swapMaterialId) {
      setSwapErr('请选择一条素材')
      return
    }
    if (swapManualIn === null || swapManualOut === null) {
      setSwapErr('请先在裁剪条上拖手柄选片段')
      return
    }
    if (swapManualOut - swapManualIn < 0.5) {
      setSwapErr('裁剪窗口太短，至少 0.5s')
      return
    }
    void callSwap({
      source: 'user_material',
      material_id: swapMaterialId,
      material_in_point: swapManualIn,
      material_out_point: swapManualOut,
    })
  }

  const applyAutoShot = () => {
    if (!swapMaterialId) {
      setSwapErr('请选择一条素材')
      return
    }
    void callSwap({
      source: 'user_material',
      material_id: swapMaterialId,
      ...(swapMaterialShotIdx !== null ? { material_shot_index: swapMaterialShotIdx } : {}),
    })
  }

  const handleSwap = async () => {
    setSwapErr(null)
    const body: {
      source: SourceType
      prompt_hint?: string
      main_text?: string
      sub_text?: string
    } = { source: swapSource }
    if (swapSource === 'aigc_image' || swapSource === 'aigc_t2v') {
      const hint = swapPromptHint.trim()
      if (hint) body.prompt_hint = hint
    } else if (swapSource === 'text_card') {
      const m = swapMainText.trim()
      const s = swapSubText.trim()
      if (m) body.main_text = m
      if (s) body.sub_text = s
    }
    await callSwap(body)
  }

  const busy = disabled || saving || swapping

  // user_material 候选材料的 shots（用于在挑材料后再挑哪段镜头）
  const selectedMaterial = materials.find((m) => m.material_id === swapMaterialId) ?? null
  const materialShots = selectedMaterial?.shots ?? []

  const longSwapHint =
    swapSource === 'aigc_t2v'
      ? '注意：Seedance 同步等待最长 ~3 分钟，期间弹窗会卡住'
      : swapSource === 'aigc_image'
        ? '约 6-15 秒同步等待'
        : ''

  return (
    <div
      role="dialog"
      aria-modal="true"
      onClick={() => {
        if (!busy) onClose()
      }}
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/55 p-4"
    >
      <div
        onClick={(e) => e.stopPropagation()}
        className="flex max-h-[90vh] w-full max-w-2xl flex-col overflow-hidden rounded-lg border border-border bg-card shadow-xl"
      >
        <div className="flex items-center justify-between border-b border-border px-4 py-2">
          <h3 className="text-sm font-semibold">
            单镜编辑 ·{' '}
            <span className="text-muted-foreground">
              {SECTION_LABEL[scene.section]} · 第 {scene.shot_order + 1} 镜（{scene.scene_id}）
            </span>
            <span className="ml-2 rounded border border-border bg-muted px-1.5 py-0.5 text-[10px] font-normal text-muted-foreground">
              当前源：
              {scene.source === 'user_material' ||
              scene.source === 'aigc_image' ||
              scene.source === 'aigc_t2v' ||
              scene.source === 'text_card'
                ? SOURCE_LABEL[scene.source]
                : scene.source}
            </span>
          </h3>
          <button
            onClick={onClose}
            disabled={busy}
            className="rounded text-muted-foreground hover:text-foreground disabled:opacity-40"
            aria-label="关闭"
          >
            ✕
          </button>
        </div>

        <div className="flex-1 space-y-4 overflow-y-auto px-4 py-3">
          {/* —— 内容编辑 —— */}
          <section className="space-y-3">
            <h4 className="text-xs font-semibold text-foreground">改内容</h4>
            <label className="block space-y-1">
              <span className="text-xs text-muted-foreground">
                画面主体（subject · ≤40 字 · 写具象名词，不要比喻）
              </span>
              <input
                autoFocus
                value={subject}
                maxLength={40}
                disabled={busy}
                onChange={(e) => setSubject(e.target.value)}
                className="w-full rounded border border-border bg-background px-2 py-1.5 text-sm disabled:opacity-60"
                placeholder="如：主播正脸 / 青铜鼎特写 / 展厅全景"
              />
            </label>
            <label className="block space-y-1">
              <span className="text-xs text-muted-foreground">
                画面描述（visual · ≤200 字 · 主体 + 动作 + 构图 + 镜头语言）
              </span>
              <textarea
                value={visual}
                maxLength={200}
                disabled={busy}
                onChange={(e) => setVisual(e.target.value)}
                rows={3}
                className="w-full resize-y rounded border border-border bg-background px-2 py-1.5 text-sm disabled:opacity-60"
                placeholder="如：主播正面手持产品，腰部以上特写，眼神看向镜头，桌面留白背景"
              />
            </label>
            <label className="block space-y-1">
              <span className="text-xs text-muted-foreground">
                口播 / 字幕（narration · ≤200 字 · 纯画面镜头可留空）
              </span>
              <textarea
                value={narration}
                maxLength={200}
                disabled={busy}
                onChange={(e) => setNarration(e.target.value)}
                rows={2}
                className="w-full resize-y rounded border border-border bg-background px-2 py-1.5 text-sm disabled:opacity-60"
                placeholder="嘿，看这块青铜鼎，三千年前就有了这种范铸工艺……"
              />
            </label>
            <label className="block space-y-1">
              <span className="text-xs text-muted-foreground">
                运镜手法（camera_technique · ≤30 字 · 同时驱动 AI 视频提示词 & Remotion 静帧动效）
              </span>
              <input
                value={cameraTechnique}
                maxLength={30}
                disabled={busy}
                onChange={(e) => setCameraTechnique(e.target.value)}
                className="w-full rounded border border-border bg-background px-2 py-1.5 text-sm disabled:opacity-60"
                placeholder="如：缓慢推近 / 左向横摇 / 固定特写 / 跟随主体侧移"
              />
            </label>
            <div className="rounded-md border border-border/60 bg-muted/30 px-2 py-1.5 text-[11px] leading-relaxed text-muted-foreground">
              本镜时长：
              <span className="font-mono text-foreground">{scene.duration.toFixed(2)}s</span>
              （改时长会牵动下游 Scene.start，目前需回到第 1 步重生 plan 才能调整）
            </div>
            {err && <p className="text-xs text-destructive">{err}</p>}
          </section>

          {/* —— 换源 —— */}
          <section className="space-y-3 rounded-md border border-dashed border-primary/30 bg-primary/[0.03] px-3 py-2.5">
            <div className="flex items-center justify-between gap-2">
              <h4 className="text-xs font-semibold text-foreground">换源（替换本镜画面来源）</h4>
              <span className="text-[10px] text-muted-foreground">{SOURCE_HINT[swapSource]}</span>
            </div>
            <div className="flex flex-wrap gap-1">
              {(['user_material', 'aigc_image', 'aigc_t2v', 'text_card'] as SourceType[]).map(
                (s) => (
                  <button
                    key={s}
                    onClick={() => setSwapSource(s)}
                    disabled={busy}
                    className={
                      'rounded-md border px-2 py-1 text-xs transition-colors disabled:opacity-50 ' +
                      (swapSource === s
                        ? 'border-primary bg-primary/10 text-primary'
                        : 'border-border bg-background hover:bg-secondary')
                    }
                  >
                    {SOURCE_LABEL[s]}
                  </button>
                ),
              )}
            </div>

            {swapSource === 'user_material' && (
              <div className="space-y-2">
                {materials.length === 0 ? (
                  <p className="text-xs text-muted-foreground">
                    项目内还没有用户素材——先到第 1 步上传素材后再回来切。
                  </p>
                ) : (
                  <>
                    <label className="block space-y-1">
                      <span className="text-xs text-muted-foreground">挑一条素材</span>
                      <select
                        value={swapMaterialId}
                        disabled={busy}
                        onChange={(e) => {
                          setSwapMaterialId(e.target.value)
                          setSwapMaterialShotIdx(null)
                          setSwapManualIn(null)
                          setSwapManualOut(null)
                        }}
                        className="w-full rounded border border-border bg-background px-2 py-1.5 text-sm disabled:opacity-60"
                      >
                        {materials.map((m) => (
                          <option key={m.material_id} value={m.material_id}>
                            {m.filename}
                            {m.duration_seconds
                              ? ` · ${m.duration_seconds.toFixed(1)}s`
                              : ''}
                          </option>
                        ))}
                      </select>
                    </label>
                    {selectedMaterial &&
                      selectedMaterial.media_type === 'video' &&
                      (selectedMaterial.duration_seconds ?? 0) > 0 && (
                        <div className="space-y-1.5 rounded-md border border-indigo-200 bg-indigo-50/40 p-2">
                          <span className="text-xs font-semibold text-indigo-700">
                            ① 手动裁剪（拖手柄选片段，时长会直接覆盖本镜的 {scene.duration.toFixed(1)}s）
                          </span>
                          <MaterialTrimPanel
                            material={selectedMaterial}
                            targetDuration={scene.duration}
                            initialIn={swapManualIn ?? 0}
                            initialOut={
                              swapManualOut ??
                              Math.min(scene.duration, selectedMaterial.duration_seconds ?? scene.duration)
                            }
                            onChange={(i, o) => {
                              setSwapManualIn(i)
                              setSwapManualOut(o)
                              setSwapMaterialShotIdx(null)
                            }}
                          />
                          <div className="flex items-center justify-end pt-1">
                            <button
                              onClick={applyManualTrim}
                              disabled={
                                busy ||
                                !swapMaterialId ||
                                swapManualIn === null ||
                                swapManualOut === null
                              }
                              className="rounded-md bg-indigo-600 px-3 py-1 text-xs font-medium text-white hover:bg-indigo-700 disabled:opacity-50"
                            >
                              {swapping ? '切源中…' : '应用此手动裁剪'}
                            </button>
                          </div>
                        </div>
                      )}
                    {materialShots.length > 0 && (
                      <div className="space-y-1.5 rounded-md border border-emerald-200 bg-emerald-50/40 p-2">
                        <span className="text-xs font-semibold text-emerald-700">
                          ② 或挑自动识别的镜头（缺省取首镜，按本镜时长 {scene.duration.toFixed(1)}s 切入出点）
                        </span>
                        <select
                          value={swapMaterialShotIdx === null ? '' : String(swapMaterialShotIdx)}
                          disabled={busy}
                          onChange={(e) => {
                            const v = e.target.value
                            setSwapMaterialShotIdx(v === '' ? null : Number(v))
                            setSwapManualIn(null)
                            setSwapManualOut(null)
                          }}
                          className="w-full rounded border border-border bg-background px-2 py-1.5 text-sm disabled:opacity-60"
                        >
                          <option value="">（默认：首镜）</option>
                          {materialShots.map((sh) => (
                            <option key={sh.index} value={sh.index}>
                              第 {sh.index + 1} 镜 · {sh.start.toFixed(1)}-{sh.end.toFixed(1)}s
                              {sh.caption ? ` · ${sh.caption.slice(0, 24)}` : ''}
                            </option>
                          ))}
                        </select>
                        <div className="flex items-center justify-end pt-1">
                          <button
                            onClick={applyAutoShot}
                            disabled={busy || !swapMaterialId}
                            className="rounded-md bg-emerald-600 px-3 py-1 text-xs font-medium text-white hover:bg-emerald-700 disabled:opacity-50"
                          >
                            {swapping
                              ? '切源中…'
                              : swapMaterialShotIdx !== null
                                ? '应用此镜头'
                                : '应用首镜（默认）'}
                          </button>
                        </div>
                      </div>
                    )}
                    {selectedMaterial &&
                      selectedMaterial.media_type !== 'video' && (
                        <div className="flex items-center justify-end pt-1">
                          <button
                            onClick={applyAutoShot}
                            disabled={busy || !swapMaterialId}
                            className="rounded-md bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
                          >
                            {swapping ? '切源中…' : '切到此素材'}
                          </button>
                        </div>
                      )}
                  </>
                )}
              </div>
            )}

            {(swapSource === 'aigc_image' || swapSource === 'aigc_t2v') && (
              <label className="block space-y-1">
                <span className="text-xs text-muted-foreground">
                  额外提示（可空 · 不写就直接用 subject + visual 当 prompt）
                </span>
                <textarea
                  value={swapPromptHint}
                  maxLength={200}
                  disabled={busy}
                  onChange={(e) => setSwapPromptHint(e.target.value)}
                  rows={2}
                  className="w-full resize-y rounded border border-border bg-background px-2 py-1.5 text-sm disabled:opacity-60"
                  placeholder="如：暖光、低饱和；或：手持微抖、街头纪实感"
                />
              </label>
            )}

            {swapSource === 'text_card' && (
              <div className="space-y-2">
                <label className="block space-y-1">
                  <span className="text-xs text-muted-foreground">
                    主文案（≤24 字 · 缺省取 subject）
                  </span>
                  <input
                    value={swapMainText}
                    maxLength={24}
                    disabled={busy}
                    onChange={(e) => setSwapMainText(e.target.value)}
                    className="w-full rounded border border-border bg-background px-2 py-1.5 text-sm disabled:opacity-60"
                    placeholder={origSubject || '（缺省取 subject）'}
                  />
                </label>
                <label className="block space-y-1">
                  <span className="text-xs text-muted-foreground">副文案（≤40 字 · 可空）</span>
                  <input
                    value={swapSubText}
                    maxLength={40}
                    disabled={busy}
                    onChange={(e) => setSwapSubText(e.target.value)}
                    className="w-full rounded border border-border bg-background px-2 py-1.5 text-sm disabled:opacity-60"
                  />
                </label>
              </div>
            )}

            {swapSource !== 'user_material' && (
              <div className="flex items-center justify-between gap-2 pt-1">
                <span className="text-[10px] text-muted-foreground">{longSwapHint}</span>
                <button
                  onClick={() => void handleSwap()}
                  disabled={busy}
                  className="rounded-md bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
                >
                  {swapping
                    ? swapSource === 'aigc_t2v'
                      ? 'Seedance 生成中…（最长 3 分钟）'
                      : swapSource === 'aigc_image'
                        ? 'Seedream 生成中…'
                        : '切源中…'
                    : `切到「${SOURCE_LABEL[swapSource]}」`}
                </button>
              </div>
            )}
            {swapErr && <p className="text-xs text-destructive">{swapErr}</p>}
          </section>
        </div>

        <div className="flex items-center justify-end gap-2 border-t border-border px-4 py-2">
          <button
            onClick={onClose}
            disabled={busy}
            className="rounded-md border border-border bg-card px-3 py-1.5 text-xs hover:bg-secondary disabled:opacity-50"
          >
            关闭
          </button>
          <button
            onClick={() => void handleSave()}
            disabled={busy || !dirty}
            className="rounded-md bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
          >
            {saving ? '保存中…' : dirty ? '保存内容修改' : '内容已最新'}
          </button>
        </div>
      </div>
    </div>
  )
}
