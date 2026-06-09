import { useEffect, useMemo, useState } from 'react'

import { patchShotFields } from '@/api/plan'
import { SECTION_LABEL } from '@/lib/sections'
import type { AdaptedSection, Plan, Scene, ShotPlan } from '@/types/schemas'

/**
 * stage-37：单镜级编辑弹窗——段块展开后点小镜调出。
 *
 * 编辑字段：subject / visual / narration。后端 patch_shot_fields 双写 Scene + ShotPlan。
 * duration_seconds 这次不开放——改时长要重排下游 Scene.start，留给独立路由 / Plan rebuild 做。
 */
export function ShotEditDialog({
  plan,
  scene,
  section,
  onClose,
  onSaved,
  disabled = false,
}: {
  plan: Plan
  scene: Scene | null
  section: AdaptedSection | null
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

  const [subject, setSubject] = useState('')
  const [visual, setVisual] = useState('')
  const [narration, setNarration] = useState('')
  const [saving, setSaving] = useState(false)
  const [err, setErr] = useState<string | null>(null)

  useEffect(() => {
    if (!open) return
    setSubject(shot?.subject ?? scene?.shot_subject ?? '')
    setVisual(shot?.visual ?? '')
    setNarration(shot?.narration ?? scene?.narration ?? '')
    setErr(null)
  }, [open, shot, scene])

  useEffect(() => {
    if (!open) return
    const handler = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && !saving) onClose()
    }
    window.addEventListener('keydown', handler)
    return () => window.removeEventListener('keydown', handler)
  }, [open, onClose, saving])

  if (!open || !scene || !section) return null

  const origSubject = shot?.subject ?? scene.shot_subject ?? ''
  const origVisual = shot?.visual ?? ''
  const origNarration = shot?.narration ?? scene.narration ?? ''
  const dirty =
    subject !== origSubject ||
    visual !== origVisual ||
    narration !== origNarration

  const handleSave = async () => {
    if (!dirty) {
      onClose()
      return
    }
    setSaving(true)
    setErr(null)
    try {
      const patch: { subject?: string; visual?: string; narration?: string } = {}
      if (subject !== origSubject) patch.subject = subject.trim()
      if (visual !== origVisual) patch.visual = visual.trim()
      if (narration !== origNarration) patch.narration = narration.trim()
      const fresh = await patchShotFields(plan.plan_id, scene.scene_id, patch)
      onSaved(fresh)
      onClose()
    } catch (e) {
      setErr(e instanceof Error ? e.message : '保存失败')
    } finally {
      setSaving(false)
    }
  }

  const busy = disabled || saving

  return (
    <div
      role="dialog"
      aria-modal="true"
      onClick={() => {
        if (!saving) onClose()
      }}
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/55 p-4"
    >
      <div
        onClick={(e) => e.stopPropagation()}
        className="w-full max-w-lg overflow-hidden rounded-lg border border-border bg-card shadow-xl"
      >
        <div className="flex items-center justify-between border-b border-border px-4 py-2">
          <h3 className="text-sm font-semibold">
            单镜编辑 ·{' '}
            <span className="text-muted-foreground">
              {SECTION_LABEL[scene.section]} · 第 {scene.shot_order + 1} 镜（{scene.scene_id}）
            </span>
          </h3>
          <button
            onClick={onClose}
            disabled={saving}
            className="rounded text-muted-foreground hover:text-foreground disabled:opacity-40"
            aria-label="关闭"
          >
            ✕
          </button>
        </div>

        <div className="space-y-3 px-4 py-3">
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
          <div className="rounded-md border border-border/60 bg-muted/30 px-2 py-1.5 text-[11px] leading-relaxed text-muted-foreground">
            <div>
              本镜时长：
              <span className="font-mono text-foreground">{scene.duration.toFixed(2)}s</span>
              （改时长会牵动下游 Scene.start，目前需回到第 1 步重生 plan 才能调整）
            </div>
          </div>
          {err && <p className="text-xs text-destructive">{err}</p>}
        </div>

        <div className="flex items-center justify-end gap-2 border-t border-border px-4 py-2">
          <button
            onClick={onClose}
            disabled={saving}
            className="rounded-md border border-border bg-card px-3 py-1.5 text-xs hover:bg-secondary disabled:opacity-50"
          >
            取消
          </button>
          <button
            onClick={() => void handleSave()}
            disabled={busy || !dirty}
            className="rounded-md bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
          >
            {saving ? '保存中…' : dirty ? '保存' : '已是最新'}
          </button>
        </div>
      </div>
    </div>
  )
}
