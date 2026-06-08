import { useState } from 'react'

import { patchPlanScene, swapSceneSource, type SceneSwapSourceRequest } from '@/api/plan'
import { SECTION_LABEL } from '@/lib/sections'
import { cn } from '@/lib/utils'
import type { PackagingItem, Plan, Scene, ShotPlan } from '@/types/schemas'

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
 * stage-26 PR-N.5：单镜行 + 换源面板。
 *
 * 三档质量 chip + 「换源」按钮，点开展开三个换源动作：
 * - 字卡占位：直接装 TextCardSpec（瞬时，无外部调用）
 * - AI 生图：Seedream 同步出图（~6-15s）
 * - AI 视频：Seedance 同步轮询（最长 ~180s，超时返 504）
 *
 * 不提供 user_material 选择器（需要项目素材列表跨组件传参）——
 * 想换素材请用 ⌘K 对话编辑或在 step1 重新上传素材后重跑分析。
 */
function ShotRow({
  shot,
  scene,
  planId,
  onSaved,
  disabled,
}: {
  shot: ShotPlan
  scene: Scene | null
  planId: string
  onSaved: (plan: Plan) => void
  disabled: boolean
}) {
  const [open, setOpen] = useState(false)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const [hintText, setHintText] = useState('')
  const [mainText, setMainText] = useState(shot.subject ?? '')
  const [subText, setSubText] = useState('')

  const quality: 'good' | 'weak' | 'missing' =
    (shot.match_quality as 'good' | 'weak' | 'missing' | undefined) ?? 'good'
  const sceneId = scene?.scene_id
  const needsFill = scene?.needs_fill === true
  const canSwap = !!sceneId && !disabled && !busy

  const promptFallback = [shot.subject, shot.visual, shot.narration]
    .filter((s) => s && s.trim())
    .join('；')

  const callSwap = async (body: SceneSwapSourceRequest) => {
    if (!sceneId) return
    setBusy(true)
    setErr(null)
    try {
      const fresh = await swapSceneSource(planId, sceneId, body)
      onSaved(fresh)
      setOpen(false)
    } catch (e) {
      setErr(e instanceof Error ? e.message : '换源失败')
    } finally {
      setBusy(false)
    }
  }

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
            {canSwap && (
              <button
                type="button"
                onClick={() => {
                  setOpen((v) => !v)
                  setErr(null)
                }}
                className="ml-auto rounded border border-border bg-card px-1.5 py-0.5 text-[9px] hover:bg-secondary"
              >
                {open ? '收起' : '换源…'}
              </button>
            )}
          </div>
          {shot.visual && <div className="text-muted-foreground">画面：{shot.visual}</div>}
          {shot.narration && <div className="text-muted-foreground">口播：{shot.narration}</div>}
        </div>
      </div>

      {open && sceneId && (
        <div className="mt-1.5 space-y-1.5 rounded border border-border bg-card/60 p-2">
          <div className="text-[10px] font-semibold text-muted-foreground">
            换素材来源 · scene <span className="font-mono">{sceneId}</span>
          </div>
          {/* 字卡占位 */}
          <div className="grid gap-1 rounded border border-border/60 bg-background/50 p-1.5">
            <div className="flex items-center justify-between">
              <span className="text-[10px] font-semibold">字卡占位（瞬时）</span>
              <button
                type="button"
                disabled={busy}
                onClick={() => void callSwap({ source: 'text_card', main_text: mainText, sub_text: subText })}
                className="rounded bg-primary px-2 py-0.5 text-[10px] text-primary-foreground disabled:opacity-50"
              >
                应用
              </button>
            </div>
            <input
              value={mainText}
              maxLength={24}
              disabled={busy}
              onChange={(e) => setMainText(e.target.value)}
              placeholder={shot.subject || '主文案（≤24 字）'}
              className="w-full rounded border border-border bg-background px-1.5 py-0.5 text-[10px]"
            />
            <input
              value={subText}
              maxLength={40}
              disabled={busy}
              onChange={(e) => setSubText(e.target.value)}
              placeholder="副文案（可空）"
              className="w-full rounded border border-border bg-background px-1.5 py-0.5 text-[10px]"
            />
          </div>
          {/* AI 生图 / 视频 共用一个 prompt_hint */}
          <div className="grid gap-1 rounded border border-border/60 bg-background/50 p-1.5">
            <span className="text-[10px] font-semibold">AI 生成（生图 ~10s / 视频 ~3min）</span>
            <textarea
              value={hintText}
              rows={2}
              maxLength={200}
              disabled={busy}
              onChange={(e) => setHintText(e.target.value)}
              placeholder={`额外提示（可空，默认用：${promptFallback || '本镜主体+画面+口播'}）`}
              className="w-full resize-y rounded border border-border bg-background px-1.5 py-0.5 text-[10px]"
            />
            <div className="flex gap-1">
              <button
                type="button"
                disabled={busy}
                onClick={() => void callSwap({ source: 'aigc_image', prompt_hint: hintText || undefined })}
                className="flex-1 rounded border border-emerald-500/60 bg-emerald-500/10 px-2 py-0.5 text-[10px] text-emerald-700 hover:bg-emerald-500/20 disabled:opacity-50 dark:text-emerald-300"
              >
                {busy ? '生成中…' : '出 AI 图'}
              </button>
              <button
                type="button"
                disabled={busy}
                onClick={() => void callSwap({ source: 'aigc_t2v', prompt_hint: hintText || undefined })}
                className="flex-1 rounded border border-primary/60 bg-primary/10 px-2 py-0.5 text-[10px] text-primary hover:bg-primary/20 disabled:opacity-50"
              >
                {busy ? '生成中…' : '出 AI 视频'}
              </button>
            </div>
          </div>
          {err && <p className="text-[10px] text-destructive">{err}</p>}
          <p className="text-[9px] text-muted-foreground">
            AI 视频同步轮询最长 180s；卡住可关闭后用 ⌘K 让小助手帮忙换源。
          </p>
        </div>
      )}
    </li>
  )
}
