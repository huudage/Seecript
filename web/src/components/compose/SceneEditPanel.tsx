import { useState } from 'react'

import { SwapSourceDialog, type SwapSourceMode } from './SwapSourceDialog'
import { patchPlanScene } from '@/api/plan'
import { SECTION_LABEL } from '@/lib/sections'
import { cn } from '@/lib/utils'
import type { Material, PackagingItem, Plan, Scene, ShotPlan } from '@/types/schemas'

const PKG_KIND_LABEL: Record<PackagingItem['kind'], string> = {
  subtitle: '字幕',
  title_bar: '标题条',
  sticker: '贴纸/水印',
  transition: '切换',
  cover: '封面',
}

/** stage-26 PR-N.5：单镜换源 chip 颜色 + 文案。 */
const QUALITY_TONE: Record<'good' | 'weak' | 'missing', string> = {
  good: 'bg-emerald-500/20 text-emerald-700 dark:text-emerald-300',
  weak: 'bg-amber-500/20 text-amber-700 dark:text-amber-300',
  missing: 'bg-slate-500/30 text-slate-600 dark:text-slate-300',
}
const QUALITY_LABEL: Record<'good' | 'weak' | 'missing', string> = {
  good: '✓ 精准',
  weak: '⚠ 待修补',
  missing: '○ 缺匹配',
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
  /** 项目素材库（用于换源弹窗 user_material 模式）。 */
  materials?: Material[]
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
  materials = [],
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
      materials={materials}
      onSaved={onSaved}
      disabled={disabled}
    />
  )
}

function ScenePanel({
  plan,
  selectedSceneId,
  materials,
  onSaved,
  disabled,
}: {
  plan: Plan
  selectedSceneId: string | null
  materials: Material[]
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

      {/* stage-24 / stage-26：分镜清单 + 单镜换源（PR-N.5）。
          每条 shot 旁挂 match_quality chip + 「换源」按钮——good 段也可换源（用户可能要主动替素材）。 */}
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
            {section.shots.map((sh) => {
              const sceneOfShot = plan.main_track.find(
                (sc) =>
                  sc.parent_section_id === section.section_id && sc.shot_order === sh.order,
              )
              return (
                <ShotRow
                  key={sh.order}
                  shot={sh}
                  scene={sceneOfShot ?? null}
                  planId={plan.plan_id}
                  materials={materials}
                  onSaved={onSaved}
                  disabled={busy}
                />
              )
            })}
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

/**
 * stage-26 PR-N.5 v2：单镜行 + 4 按钮换源（弹窗化）。
 *
 * 不再 inline 展开换源面板（可读性差）；改为 4 个按钮：
 * 真实素材 / 字卡占位 / AI 生图 / AI 视频，每个点击都打开 SwapSourceDialog 落到对应 tab。
 *
 * targets（stage-25）显示在主体行下方——一镜可能多目标（人/物/字），
 * 让用户看到 LLM 拆分意图，便于判断要不要换源。
 */
function ShotRow({
  shot,
  scene,
  planId,
  materials,
  onSaved,
  disabled,
}: {
  shot: ShotPlan
  scene: Scene | null
  planId: string
  materials: Material[]
  onSaved: (plan: Plan) => void
  disabled: boolean
}) {
  const [dialogMode, setDialogMode] = useState<SwapSourceMode | null>(null)

  const quality: 'good' | 'weak' | 'missing' =
    (shot.match_quality as 'good' | 'weak' | 'missing' | undefined) ?? 'good'
  const sceneId = scene?.scene_id
  const needsFill = scene?.needs_fill === true
  const canSwap = !!sceneId && !disabled

  const SWAP_BUTTONS: { mode: SwapSourceMode; label: string; tone: string }[] = [
    {
      mode: 'user_material',
      label: '🎬 真实素材',
      tone: 'border-sky-500/60 bg-sky-500/10 text-sky-700 hover:bg-sky-500/20 dark:text-sky-300',
    },
    {
      mode: 'text_card',
      label: '🅰 字卡占位',
      tone: 'border-slate-500/60 bg-slate-500/10 text-slate-700 hover:bg-slate-500/20 dark:text-slate-300',
    },
    {
      mode: 'aigc_image',
      label: '🖼 AI 生图',
      tone: 'border-emerald-500/60 bg-emerald-500/10 text-emerald-700 hover:bg-emerald-500/20 dark:text-emerald-300',
    },
    {
      mode: 'aigc_t2v',
      label: '🎞 AI 视频',
      tone: 'border-primary/60 bg-primary/10 text-primary hover:bg-primary/20',
    },
  ]

  return (
    <li className="rounded bg-background/40 px-1.5 py-1 text-[10px]">
      <div className="flex items-start gap-2">
        <span className="mt-0.5 inline-flex h-3.5 w-5 shrink-0 items-center justify-center rounded bg-violet-500/30 font-mono text-[9px] font-bold text-violet-100">
          #{shot.order + 1}
        </span>
        <div className="flex-1 space-y-0.5">
          <div className="flex flex-wrap items-center gap-1.5">
            <span className="font-semibold text-foreground">{shot.subject || '（无主体）'}</span>
            <span className="font-mono text-[9px] text-muted-foreground">
              {shot.duration_seconds.toFixed(1)}s
            </span>
            <span
              className={cn('rounded px-1 py-0.5 text-[9px] font-medium', QUALITY_TONE[quality])}
              title={
                shot.match_score != null
                  ? `匹配分 ${shot.match_score.toFixed(2)} · ${
                      quality === 'good'
                        ? '≥0.30 精准对齐'
                        : quality === 'weak'
                          ? '0.10-0.30 待修补'
                          : '<0.10 缺匹配（已用字卡占位）'
                    }`
                  : '本镜质量等级'
              }
            >
              {QUALITY_LABEL[quality]}
            </span>
            {scene?.source && (
              <span className="rounded bg-secondary/60 px-1 font-mono text-[9px] text-muted-foreground">
                {scene.source}
              </span>
            )}
            {needsFill && (
              <span className="rounded bg-amber-500/30 px-1 text-[9px] font-medium text-amber-700 dark:text-amber-300">
                待修补
              </span>
            )}
          </div>
          {shot.visual && <div className="text-muted-foreground">画面：{shot.visual}</div>}
          {shot.narration && <div className="text-muted-foreground">口播：{shot.narration}</div>}
          {shot.targets && shot.targets.length > 0 && (
            <div className="flex flex-wrap items-center gap-1 pt-0.5">
              <span className="text-[9px] text-muted-foreground">目标：</span>
              {shot.targets.map((t, i) => (
                <span
                  key={i}
                  className="rounded bg-violet-500/15 px-1 py-0.5 text-[9px] text-violet-700 dark:text-violet-300"
                  title={t.visual_hint || undefined}
                >
                  <span className="font-mono opacity-60">{t.kind}</span> {t.name}
                </span>
              ))}
            </div>
          )}
          {canSwap && (
            <div className="flex flex-wrap items-center gap-1 pt-1">
              {SWAP_BUTTONS.map((b) => (
                <button
                  key={b.mode}
                  type="button"
                  onClick={() => setDialogMode(b.mode)}
                  className={cn(
                    'rounded border px-1.5 py-0.5 text-[10px] font-medium transition-colors',
                    b.tone,
                  )}
                >
                  {b.label}
                </button>
              ))}
            </div>
          )}
        </div>
      </div>

      {dialogMode && sceneId && (
        <SwapSourceDialog
          open={true}
          initialMode={dialogMode}
          shot={shot}
          scene={scene}
          planId={planId}
          materials={materials}
          onClose={() => setDialogMode(null)}
          onSaved={onSaved}
        />
      )}
    </li>
  )
}
