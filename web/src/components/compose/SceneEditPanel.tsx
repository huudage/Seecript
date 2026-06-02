import { useState } from 'react'

import { api } from '@/api/client'
import { patchPlanScene } from '@/api/plan'
import { SECTION_LABEL } from '@/lib/sections'
import { cn } from '@/lib/utils'
import type {
  EditApplyRequest,
  PackagingItem,
  Plan,
  Scene,
} from '@/types/schemas'

type NLIntent = 'main' | 'voice'

const NL_META: Record<NLIntent, { label: string; placeholder: string; hint: string }> = {
  main: {
    label: '改本段（时长/转场/素材）',
    placeholder: '例：把这段压到 3 秒；换成 mat-xxx；前面加个 dissolve 转场',
    hint: 'LLM 会按本段定位（已自动 mark 起止）改 main 轨。',
  },
  voice: {
    label: '改口播（自动重合成）',
    placeholder: '例：口播改得更口语化；改成「现在下单立减 99」',
    hint: '保存后系统会自动重新跑 TTS。',
  },
}

const PKG_KIND_LABEL: Record<PackagingItem['kind'], string> = {
  subtitle: '字幕',
  title_bar: '标题条',
  sticker: '贴纸/水印',
  transition: '转场',
  cover: '封面',
}

/**
 * 轨道段编辑面板（内容/口播/包装三轨共用入口）。
 *
 * 调度：
 * - 优先 packagingItem：包装轨被选中，渲染包装编辑面板（NL 限定 track=packaging）
 * - 否则 scene：内容/口播轨被选中（口播段视觉上属于同一 scene_id），渲染段落编辑
 * - 否则：占位提示
 *
 * 草稿初始化：父级用 `key={selection}` 强制切段时整组件重挂，
 * useState 初值直接取当前值——无需 effect，避免 setState-in-effect 级联渲染。
 */

interface Props {
  plan: Plan
  /** 当前内容/口播轨选中的 scene_id。 */
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
    return (
      <PackagingPanel
        plan={plan}
        item={selectedPackagingItem}
        onSaved={onSaved}
        disabled={disabled}
      />
    )
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
  const [narration, setNarration] = useState(scene?.narration ?? '')
  const [saving, setSaving] = useState(false)
  const [err, setErr] = useState<string | null>(null)

  const [nlIntent, setNlIntent] = useState<NLIntent>('main')
  const [nlInstruction, setNlInstruction] = useState('')
  const [nlApplying, setNlApplying] = useState(false)
  const [nlErr, setNlErr] = useState<string | null>(null)

  if (!scene) {
    return (
      <div className="rounded-md border border-dashed border-border bg-background/30 p-4 text-center text-[11px] text-muted-foreground">
        点四轨中任意片段（内容 / 口播 / 包装），在这里编辑文字、改口播或对该段做自然语言编辑。
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

  const handleNLApply = async () => {
    if (!scene || !nlInstruction.trim()) return
    setNlApplying(true)
    setNlErr(null)
    try {
      const body: EditApplyRequest = {
        plan_id: plan.plan_id,
        track: nlIntent,
        instruction: nlInstruction.trim(),
        marks: [
          {
            track: 'main',
            start: Number(scene.start.toFixed(1)),
            end: Number((scene.start + scene.duration).toFixed(1)),
            target_id: scene.scene_id,
          },
        ],
      }
      const fresh = await api.post<Plan>('/edit/apply', body)
      onSaved(fresh)
      setNlInstruction('')
    } catch (e) {
      setNlErr(e instanceof Error ? e.message : 'NL 编辑失败')
    } finally {
      setNlApplying(false)
    }
  }
  const nlBusy = disabled || nlApplying

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

      {/* NL 编辑：针对当前段、marks 自动取 scene.start ~ +duration */}
      <div className="space-y-1.5 border-t border-border pt-2">
        <div className="flex items-center justify-between">
          <span className="text-[11px] font-medium">自然语言编辑（仅作用于本段）</span>
          <span className="font-mono text-[10px] text-muted-foreground">
            mark {scene.start.toFixed(1)}–{(scene.start + scene.duration).toFixed(1)}s
          </span>
        </div>
        <div className="flex gap-1 rounded-md border border-border bg-background p-0.5">
          {(['main', 'voice'] as NLIntent[]).map((t) => (
            <button
              key={t}
              type="button"
              disabled={nlBusy}
              onClick={() => setNlIntent(t)}
              className={cn(
                'flex-1 rounded px-2 py-1 text-[10px] transition-colors',
                nlIntent === t
                  ? 'bg-primary text-primary-foreground'
                  : 'text-muted-foreground hover:bg-secondary',
                nlBusy && 'cursor-not-allowed opacity-60',
              )}
              title={NL_META[t].hint}
            >
              {NL_META[t].label}
            </button>
          ))}
        </div>
        <textarea
          value={nlInstruction}
          onChange={(e) => setNlInstruction(e.target.value)}
          placeholder={NL_META[nlIntent].placeholder}
          rows={2}
          disabled={nlBusy}
          maxLength={500}
          className="w-full resize-y rounded border border-border bg-background px-2 py-1 text-xs disabled:opacity-60"
        />
        <p className="text-[10px] text-muted-foreground">{NL_META[nlIntent].hint}</p>
        {nlErr && <p className="text-[10px] text-destructive">{nlErr}</p>}
        <button
          onClick={() => void handleNLApply()}
          disabled={nlBusy || !nlInstruction.trim()}
          className={cn(
            'rounded-md border border-border bg-background px-3 py-1 text-xs hover:bg-secondary',
            (nlBusy || !nlInstruction.trim()) && 'cursor-not-allowed opacity-60',
          )}
        >
          {nlApplying ? '应用中…' : '应用 NL 编辑'}
        </button>
      </div>
    </div>
  )
}

function PackagingPanel({
  plan,
  item,
  onSaved,
  disabled,
}: {
  plan: Plan
  item: PackagingItem
  onSaved: (plan: Plan) => void
  disabled: boolean
}) {
  const [nlInstruction, setNlInstruction] = useState('')
  const [nlApplying, setNlApplying] = useState(false)
  const [nlErr, setNlErr] = useState<string | null>(null)

  const handleNLApply = async () => {
    if (!nlInstruction.trim()) return
    setNlApplying(true)
    setNlErr(null)
    try {
      const body: EditApplyRequest = {
        plan_id: plan.plan_id,
        track: 'packaging',
        instruction: nlInstruction.trim(),
        marks: [
          {
            track: 'packaging',
            start: Number(item.start.toFixed(1)),
            end: Number(item.end.toFixed(1)),
            target_id: item.item_id,
          },
        ],
      }
      const fresh = await api.post<Plan>('/edit/apply', body)
      onSaved(fresh)
      setNlInstruction('')
    } catch (e) {
      setNlErr(e instanceof Error ? e.message : 'NL 编辑失败')
    } finally {
      setNlApplying(false)
    }
  }
  const nlBusy = disabled || nlApplying

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
          <summary className="cursor-pointer text-muted-foreground">样式 JSON</summary>
          <pre className="mt-1 overflow-x-auto rounded bg-background/60 px-2 py-1 font-mono">
            {JSON.stringify(item.style, null, 2)}
          </pre>
        </details>
      )}

      <div className="space-y-1.5 border-t border-border pt-2">
        <div className="flex items-center justify-between">
          <span className="text-[11px] font-medium">自然语言编辑（仅作用于该包装段）</span>
          <span className="font-mono text-[10px] text-muted-foreground">
            mark {item.start.toFixed(1)}–{item.end.toFixed(1)}s
          </span>
        </div>
        <textarea
          value={nlInstruction}
          onChange={(e) => setNlInstruction(e.target.value)}
          placeholder={
            item.kind === 'subtitle'
              ? '例：把字幕改成「现在下单立减 99」；字体放大；放屏幕底部'
              : item.kind === 'title_bar'
                ? '例：标题改成「夏季大促」；底色换成红色'
                : item.kind === 'transition'
                  ? '例：换成 dissolve；持续时间改 0.5 秒'
                  : item.kind === 'cover'
                    ? '例：封面文案改成「3 步搞定」；右下加 logo'
                    : '例：贴纸改成 emoji 火焰；右上角'
          }
          rows={3}
          disabled={nlBusy}
          maxLength={500}
          className="w-full resize-y rounded border border-border bg-background px-2 py-1 text-xs disabled:opacity-60"
        />
        <p className="text-[10px] text-muted-foreground">
          LLM 按本包装段定位（已自动 mark 起止），改完即时回写 packaging_track。
        </p>
        {nlErr && <p className="text-[10px] text-destructive">{nlErr}</p>}
        <button
          onClick={() => void handleNLApply()}
          disabled={nlBusy || !nlInstruction.trim()}
          className={cn(
            'rounded-md border border-border bg-background px-3 py-1 text-xs hover:bg-secondary',
            (nlBusy || !nlInstruction.trim()) && 'cursor-not-allowed opacity-60',
          )}
        >
          {nlApplying ? '应用中…' : '应用 NL 编辑'}
        </button>
      </div>
    </div>
  )
}
