import { useEffect, useState } from 'react'

import { patchPlanScene } from '@/api/plan'
import { SECTION_LABEL } from '@/lib/sections'
import type { AdaptedSection, Plan, Scene } from '@/types/schemas'

/**
 * stage-37：段级编辑弹窗——替换 step2 原来内嵌的 SceneEditPanel。
 *
 * 编辑字段：theme + content_description（与 patchPlanScene 一致；narration 留在
 * 单镜级 ShotEditDialog 改，避免「在段级写口播」的歧义）。
 *
 * 关闭时机：保存成功后关 / 点遮罩 / Esc。
 */
export function SectionEditDialog({
  plan,
  section,
  firstScene,
  onClose,
  onSaved,
  disabled = false,
}: {
  plan: Plan
  section: AdaptedSection | null
  firstScene: Scene | null
  onClose: () => void
  onSaved: (plan: Plan) => void
  disabled?: boolean
}) {
  const open = section !== null && firstScene !== null
  const [theme, setTheme] = useState('')
  const [content, setContent] = useState('')
  const [saving, setSaving] = useState(false)
  const [err, setErr] = useState<string | null>(null)

  useEffect(() => {
    if (!open) return
    setTheme(section.theme ?? '')
    setContent(section.content_description ?? '')
    setErr(null)
  }, [open, section])

  useEffect(() => {
    if (!open) return
    const handler = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && !saving) onClose()
    }
    window.addEventListener('keydown', handler)
    return () => window.removeEventListener('keydown', handler)
  }, [open, onClose, saving])

  if (!open || !section || !firstScene) return null

  const dirty =
    theme !== (section.theme ?? '') ||
    content !== (section.content_description ?? '')

  const handleSave = async () => {
    if (!dirty) {
      onClose()
      return
    }
    setSaving(true)
    setErr(null)
    try {
      const fresh = await patchPlanScene(plan.plan_id, firstScene.scene_id, {
        theme,
        content_description: content,
      })
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
            段落编辑 ·{' '}
            <span className="text-muted-foreground">
              {SECTION_LABEL[section.role]} · {section.section_id}
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
            <span className="text-xs text-muted-foreground">段主题</span>
            <input
              autoFocus
              value={theme}
              maxLength={80}
              disabled={busy}
              onChange={(e) => setTheme(e.target.value)}
              className="w-full rounded border border-border bg-background px-2 py-1.5 text-sm disabled:opacity-60"
              placeholder="如：痛点放大 / 卖点展示"
            />
          </label>
          <label className="block space-y-1">
            <span className="text-xs text-muted-foreground">段落描述</span>
            <textarea
              value={content}
              maxLength={400}
              disabled={busy}
              onChange={(e) => setContent(e.target.value)}
              rows={4}
              className="w-full resize-y rounded border border-border bg-background px-2 py-1.5 text-sm disabled:opacity-60"
              placeholder="这段画面 / 节奏想表达什么——AI 会照这个写字幕文案和找素材"
            />
          </label>
          {err && <p className="text-xs text-destructive">{err}</p>}
          <p className="text-[11px] text-muted-foreground">
            提示：单镜的 subject / visual / narration 在段块展开后点小镜编辑；段时长改动请回到第 1 步重生 plan。
          </p>
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
