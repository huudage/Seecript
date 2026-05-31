import { useState } from 'react'

import { patchPlanScene } from '@/api/plan'
import { SECTION_LABEL } from '@/lib/sections'
import { cn } from '@/lib/utils'
import type { Plan, Scene } from '@/types/schemas'

/**
 * 内容轨「段落内容编辑」面板。
 *
 * 来源：内容轨上的 Scene + 它联动的 AdaptedSection（拆解迁移后的结构内容）。
 * - theme / content_description 改的是 AdaptedSection（结构层，迁移产物）
 * - narration 改的是 Scene.narration（口播文案）
 *
 * 保存走 PATCH /plan/{id}/scene/{sceneId}，后端按 sc-<order> 把 theme/content
 * 联动写回 AdaptedSection。保存成功后回调父级用返回的最新 Plan 同步 store。
 *
 * 草稿初始化：父级用 `key={selectedSceneId}` 强制切段时整组件重挂，
 * useState 初值直接取该段当前值——无需 effect，避免 setState-in-effect 级联渲染。
 *
 * 设计取舍：本面板只做「改文字」，不重跑 LLM、不动缺口；用户改完结构内容后，
 * 再用左侧补全面板对该段做 rerank/copy/aigc 补缺。
 */

interface Props {
  plan: Plan
  /** 当前内容轨选中的 scene_id；null 时面板提示先点一段。 */
  selectedSceneId: string | null
  /** 保存成功后把最新 Plan 回灌父级 store。 */
  onSaved: (plan: Plan) => void
  /** 禁用（父级 busy 时）。 */
  disabled?: boolean
}

function sectionForScene(plan: Plan, sceneId: string) {
  const m = /sc-(\d+)/.exec(sceneId)
  const order = m ? Number(m[1]) : null
  if (order == null) return null
  return plan.adapted_sections.find((s) => s.order === order) ?? null
}

export function SceneEditPanel({ plan, selectedSceneId, onSaved, disabled = false }: Props) {
  const scene: Scene | null =
    plan.main_track.find((s) => s.scene_id === selectedSceneId) ?? null
  const section = selectedSceneId ? sectionForScene(plan, selectedSceneId) : null

  const [theme, setTheme] = useState(section?.theme ?? '')
  const [content, setContent] = useState(section?.content_description ?? '')
  const [narration, setNarration] = useState(scene?.narration ?? '')
  const [saving, setSaving] = useState(false)
  const [err, setErr] = useState<string | null>(null)

  if (!scene) {
    return (
      <div className="rounded-md border border-dashed border-border bg-background/30 p-4 text-center text-[11px] text-muted-foreground">
        点内容轨上任意一段，在这里编辑它的「主题 / 结构内容 / 口播」，改完再做补全。
      </div>
    )
  }

  const dirty =
    theme !== (section?.theme ?? '') ||
    content !== (section?.content_description ?? '') ||
    narration !== (scene.narration ?? '')

  const handleSave = async () => {
    if (!selectedSceneId || !dirty) return
    setSaving(true)
    setErr(null)
    try {
      const fresh = await patchPlanScene(plan.plan_id, selectedSceneId, {
        theme: section ? theme : undefined,
        content_description: section ? content : undefined,
        narration,
      })
      onSaved(fresh)
    } catch (e) {
      setErr(e instanceof Error ? e.message : '保存失败')
    } finally {
      setSaving(false)
    }
  }

  const busy = disabled || saving

  return (
    <div className="space-y-2 rounded-md border border-border bg-card p-3">
      <div className="flex items-center justify-between">
        <h3 className="text-xs font-semibold">
          段落内容编辑 ·{' '}
          <span className="text-muted-foreground">
            {SECTION_LABEL[scene.section]} · {scene.scene_id}
          </span>
        </h3>
        {dirty && <span className="text-[10px] text-amber-500">未保存</span>}
      </div>

      {section ? (
        <>
          <label className="block space-y-0.5">
            <span className="text-[10px] text-muted-foreground">段主题（迁移结构标题）</span>
            <input
              value={theme}
              maxLength={80}
              disabled={busy}
              onChange={(e) => setTheme(e.target.value)}
              className="w-full rounded border border-border bg-background px-2 py-1 text-xs disabled:opacity-60"
              placeholder="如：痛点放大 / 卖点展示"
            />
          </label>
          <label className="block space-y-0.5">
            <span className="text-[10px] text-muted-foreground">结构内容（迁移后的段落描述）</span>
            <textarea
              value={content}
              maxLength={400}
              disabled={busy}
              onChange={(e) => setContent(e.target.value)}
              rows={3}
              className="w-full resize-y rounded border border-border bg-background px-2 py-1 text-xs disabled:opacity-60"
              placeholder="这段画面 / 节奏要表达什么——LLM 补全和缺口推断以此为锚定"
            />
          </label>
        </>
      ) : (
        <p className="rounded bg-muted/40 px-2 py-1 text-[10px] text-muted-foreground">
          该段无关联 AdaptedSection（老数据），仅可改口播。
        </p>
      )}

      <label className="block space-y-0.5">
        <span className="text-[10px] text-muted-foreground">口播文案（TTS 合成走这一行）</span>
        <textarea
          value={narration}
          maxLength={2000}
          disabled={busy}
          onChange={(e) => setNarration(e.target.value)}
          rows={2}
          className="w-full resize-y rounded border border-border bg-background px-2 py-1 text-xs disabled:opacity-60"
          placeholder="留空则保持当前；改完保存后可在口播轨重新合成"
        />
      </label>

      {err && <p className="text-[10px] text-destructive">{err}</p>}

      <div className="flex items-center gap-2">
        <button
          onClick={() => void handleSave()}
          disabled={busy || !dirty}
          className={cn(
            'rounded-md bg-primary px-3 py-1 text-xs font-medium text-primary-foreground',
            (busy || !dirty) && 'cursor-not-allowed opacity-60',
          )}
        >
          {saving ? '保存中…' : '保存改动'}
        </button>
        {dirty && !saving && (
          <button
            onClick={() => {
              setTheme(section?.theme ?? '')
              setContent(section?.content_description ?? '')
              setNarration(scene.narration ?? '')
              setErr(null)
            }}
            disabled={busy}
            className="rounded-md border border-border bg-background px-3 py-1 text-xs text-muted-foreground hover:bg-secondary disabled:opacity-60"
          >
            还原
          </button>
        )}
      </div>
    </div>
  )
}
