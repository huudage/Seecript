import { useState } from 'react'

import { patchPlanScene } from '@/api/plan'
import { SECTION_LABEL } from '@/lib/sections'
import { cn } from '@/lib/utils'
import type { PackagingItem, Plan, Scene } from '@/types/schemas'

const PKG_KIND_LABEL: Record<PackagingItem['kind'], string> = {
  subtitle: '字幕',
  title_bar: '标题条',
  sticker: '贴纸/水印',
  transition: '切换',
  cover: '封面',
}

/**
 * 轨道段编辑面板（内容/字幕/口播/包装多轨共用入口）。
 *
 * 自然语言编辑统一收敛到 ⌘K 对话编辑小助手 agent，本面板只保留逐字段直改。
 *
 * 调度：
 * - 优先 packagingItem：包装轨被选中，渲染包装编辑面板（只读 + 样式详情）
 * - 否则 scene：内容/字幕/口播轨被选中（共用同一 scene_id），渲染段落编辑
 * - 否则：占位提示
 *
 * 草稿初始化：父级用 `key={selection}` 强制切段时整组件重挂，
 * useState 初值直接取当前值——无需 effect，避免 setState-in-effect 级联渲染。
 */

interface Props {
  plan: Plan
  /** 当前内容/字幕/口播轨选中的 scene_id。 */
  selectedSceneId: string | null
  /** 当前包装轨选中的 PackagingItem（互斥于 scene）。 */
  selectedPackagingItem?: PackagingItem | null
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

export function SceneEditPanel({
  plan,
  selectedSceneId,
  selectedPackagingItem = null,
  onSaved,
  disabled = false,
}: Props) {
  if (selectedPackagingItem) {
    return <PackagingPanel item={selectedPackagingItem} />
  }
  return (
    <ScenePanel
      plan={plan}
      selectedSceneId={selectedSceneId}
      onSaved={onSaved}
      disabled={disabled}
    />
  )
}

function ScenePanel({
  plan,
  selectedSceneId,
  onSaved,
  disabled,
}: {
  plan: Plan
  selectedSceneId: string | null
  onSaved: (plan: Plan) => void
  disabled: boolean
}) {
  const scene: Scene | null =
    plan.main_track.find((s) => s.scene_id === selectedSceneId) ?? null
  const section = selectedSceneId ? sectionForScene(plan, selectedSceneId) : null

  const [theme, setTheme] = useState(section?.theme ?? '')
  const [content, setContent] = useState(section?.content_description ?? '')
  const [saving, setSaving] = useState(false)
  const [err, setErr] = useState<string | null>(null)

  if (!scene) {
    return (
      <div className="rounded-md border border-dashed border-border bg-background/30 p-4 text-center text-[11px] text-muted-foreground">
        点四轨中任意片段（镜头 / 字幕 / 口播 / 包装），在这里编辑文字；
        要批量改或用一句话改请按 <kbd className="rounded bg-secondary px-1">⌘K</kbd> 找对话编辑小助手。
      </div>
    )
  }

  const dirty =
    theme !== (section?.theme ?? '') ||
    content !== (section?.content_description ?? '')

  const handleSave = async () => {
    if (!selectedSceneId || !dirty || !section) return
    setSaving(true)
    setErr(null)
    try {
      const fresh = await patchPlanScene(plan.plan_id, selectedSceneId, {
        theme,
        content_description: content,
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
          段落编辑 ·{' '}
          <span className="text-muted-foreground">
            {SECTION_LABEL[scene.section]} · {scene.scene_id}
          </span>
        </h3>
        {dirty && <span className="text-[10px] text-amber-500">未保存</span>}
      </div>

      {section ? (
        <>
          <label className="block space-y-0.5">
            <span className="text-[10px] text-muted-foreground">段主题</span>
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
            <span className="text-[10px] text-muted-foreground">段落描述</span>
            <textarea
              value={content}
              maxLength={400}
              disabled={busy}
              onChange={(e) => setContent(e.target.value)}
              rows={3}
              className="w-full resize-y rounded border border-border bg-background px-2 py-1 text-xs disabled:opacity-60"
              placeholder="这段画面 / 节奏想表达什么——AI 会照这个写字幕文案和找素材"
            />
          </label>
        </>
      ) : (
        <p className="rounded bg-muted/40 px-2 py-1 text-[10px] text-muted-foreground">
          该段无关联 AdaptedSection（老数据），无法在此直改；请按 ⌘K 找对话编辑小助手。
        </p>
      )}

      {err && <p className="text-[10px] text-destructive">{err}</p>}

      {/* stage-24：分镜清单。section.shots 非空时显示 LLM 拆分的分镜，
          帮助创作者理解「这一段会被拆成 N 段渲染」的事实；只读，进一步编辑通过 ⌘K。 */}
      {section?.shots && section.shots.length > 0 && (
        <div className="rounded-md border border-violet-500/30 bg-violet-500/5 px-2 py-1.5">
          <div className="mb-1 flex items-center justify-between">
            <span className="text-[10px] font-semibold text-violet-400">
              分镜清单 · 本段拆为 {section.shots.length} 镜
            </span>
            <span className="font-mono text-[9px] text-muted-foreground">
              共 {section.shots.reduce((a, sh) => a + sh.duration_seconds, 0).toFixed(1)}s
            </span>
          </div>
          <ul className="space-y-1">
            {section.shots.map((sh) => (
              <li
                key={sh.order}
                className="flex items-start gap-2 rounded bg-background/40 px-1.5 py-1 text-[10px]"
              >
                <span className="mt-0.5 inline-flex h-3.5 w-5 shrink-0 items-center justify-center rounded bg-violet-500/30 font-mono text-[9px] font-bold text-violet-100">
                  #{sh.order + 1}
                </span>
                <div className="flex-1 space-y-0.5">
                  <div className="flex items-center gap-1.5">
                    <span className="font-semibold text-foreground">{sh.subject || '（无主体）'}</span>
                    <span className="font-mono text-[9px] text-muted-foreground">
                      {sh.duration_seconds.toFixed(1)}s
                    </span>
                    {sh.matched_material_id && (
                      <span className="rounded bg-emerald-400/20 px-1 text-[9px] text-emerald-300" title={`已匹配素材 ${sh.matched_material_id} #${sh.matched_material_shot_index}`}>
                        ✓ 匹配素材
                      </span>
                    )}
                    {sh.source_hint && (
                      <span className="rounded bg-secondary/60 px-1 font-mono text-[9px] text-muted-foreground">
                        {sh.source_hint}
                      </span>
                    )}
                  </div>
                  {sh.visual && (
                    <div className="text-muted-foreground">画面：{sh.visual}</div>
                  )}
                  {sh.narration && (
                    <div className="text-muted-foreground">口播：{sh.narration}</div>
                  )}
                </div>
              </li>
            ))}
          </ul>
          <p className="mt-1 text-[9px] text-muted-foreground">
            想改具体某镜的画面/口播/时长？按 ⌘K 告诉对话编辑小助手「
            <code className="rounded bg-secondary/60 px-1">{section.section_id}</code>第 N 镜」。
          </p>
        </div>
      )}

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
              setErr(null)
            }}
            disabled={busy}
            className="rounded-md border border-border bg-background px-3 py-1 text-xs text-muted-foreground hover:bg-secondary disabled:opacity-60"
          >
            还原
          </button>
        )}
      </div>

      <p className="border-t border-border pt-2 text-[10px] text-muted-foreground">
        想改字幕文案 / BGM / 调性，或用一句话批量改（"删除 sec-2"、"BGM 推迟 2 秒"）？按{' '}
        <kbd className="rounded bg-secondary px-1">⌘K</kbd> 找对话编辑小助手。
      </p>
    </div>
  )
}

function PackagingPanel({ item }: { item: PackagingItem }) {
  return (
    <div className="space-y-2 rounded-md border border-border bg-card p-3">
      <div className="flex items-center justify-between">
        <h3 className="text-xs font-semibold">
          包装段编辑 ·{' '}
          <span className="text-muted-foreground">
            {PKG_KIND_LABEL[item.kind]} · {item.item_id}
          </span>
        </h3>
        <span className="font-mono text-[10px] text-muted-foreground">
          {item.start.toFixed(1)}–{item.end.toFixed(1)}s
        </span>
      </div>

      {item.text && (
        <div className="rounded bg-muted/40 px-2 py-1.5 text-[11px]">
          <div className="mb-0.5 text-[10px] text-muted-foreground">当前文案</div>
          <div className="whitespace-pre-wrap break-words">{item.text}</div>
        </div>
      )}

      {Object.keys(item.style).length > 0 && (
        <details className="text-[10px]">
          <summary className="cursor-pointer text-muted-foreground">样式详情</summary>
          <pre className="mt-1 overflow-x-auto rounded bg-background/60 px-2 py-1 font-mono">
            {JSON.stringify(item.style, null, 2)}
          </pre>
        </details>
      )}

      <p className="border-t border-border pt-2 text-[10px] text-muted-foreground">
        要改文字 / BGM 偏移 / 调性等，按{' '}
        <kbd className="rounded bg-secondary px-1">⌘K</kbd> 找对话编辑小助手——
        告诉它 item_id「{item.item_id}」就行。
      </p>
    </div>
  )
}
