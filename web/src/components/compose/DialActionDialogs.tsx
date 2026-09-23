import { useEffect, useState, type ReactNode } from 'react'

import { api } from '@/api/client'
import {
  fetchShotBrief,
  fetchTrimSuggestion,
  swapSceneSource,
  type SceneShotBrief,
  type SceneTrimSuggestion,
} from '@/api/plan'
import type { ComposeEditDiff, ComposeEditRequest, ComposeEditResponse, Plan, PlanId } from '@/types/schemas'

/**
 * 功能盘里需要确认才落盘的三个动作（PRD-v2 F6）。
 * AI 理解贴纸不走这里：它不写 plan，由画布本地态摘除。
 */

export function TrimDiffDialog({
  planId,
  sceneId,
  materialId,
  onClose,
  onApplied,
}: {
  planId: PlanId
  sceneId: string
  materialId: string
  onClose: () => void
  onApplied: (plan: Plan) => void
}) {
  const [loading, setLoading] = useState(true)
  const [applying, setApplying] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [suggestion, setSuggestion] = useState<SceneTrimSuggestion | null>(null)

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError(null)
    void fetchTrimSuggestion(planId, sceneId)
      .then((res) => {
        if (!cancelled) setSuggestion(res)
      })
      .catch((err: unknown) => {
        if (!cancelled) setError(err instanceof Error ? err.message : '裁剪建议失败')
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [planId, sceneId])

  const apply = async () => {
    if (!suggestion) return
    setApplying(true)
    setError(null)
    try {
      const next = await swapSceneSource(planId, sceneId, {
        source: 'user_material',
        material_id: materialId,
        material_in_point: suggestion.suggested_in,
        material_out_point: suggestion.suggested_out,
      })
      onApplied(next)
    } catch (err) {
      setError(err instanceof Error ? err.message : '应用裁剪失败')
    } finally {
      setApplying(false)
    }
  }

  return (
    <DialShell title="AI 裁剪" hint="建议先给人看。确认前不改入出点。" onClose={onClose}>
      {loading && <p className="text-xs text-muted-foreground">正在看这段素材的入出点…</p>}
      {error && <p className="text-xs text-destructive">{error}</p>}
      {suggestion && (
        <div className="space-y-1 text-xs">
          <p>
            现在 {suggestion.current_in.toFixed(1)}s → {suggestion.current_out.toFixed(1)}s
          </p>
          <p className="font-medium">
            建议 {suggestion.suggested_in.toFixed(1)}s → {suggestion.suggested_out.toFixed(1)}s
          </p>
          <p className="text-muted-foreground">{suggestion.reason}</p>
        </div>
      )}
      <DialActions
        onClose={onClose}
        confirmLabel={applying ? '写入中…' : '应用这段裁剪'}
        confirmDisabled={loading || applying || !suggestion}
        onConfirm={() => void apply()}
      />
    </DialShell>
  )
}

export function ShotBriefDialog({
  planId,
  sceneId,
  onClose,
}: {
  planId: PlanId
  sceneId: string
  onClose: () => void
}) {
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [brief, setBrief] = useState<SceneShotBrief | null>(null)

  useEffect(() => {
    let cancelled = false
    void fetchShotBrief(planId, sceneId)
      .then((res) => {
        if (!cancelled) setBrief(res)
      })
      .catch((err: unknown) => {
        if (!cancelled) setError(err instanceof Error ? err.message : '补拍清单失败')
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [planId, sceneId])

  return (
    <DialShell title="补拍清单" hint="给人看的拍摄规格，不写进成片。" onClose={onClose}>
      {loading && <p className="text-xs text-muted-foreground">正在写这一镜该怎么拍…</p>}
      {error && <p className="text-xs text-destructive">{error}</p>}
      {brief && (
        <div className="space-y-1 text-xs leading-relaxed">
          <p className="font-medium">{brief.what_to_shoot}</p>
          <p>时长 {brief.duration_seconds.toFixed(1)}s · {brief.emotion}</p>
          <p className="text-muted-foreground">参考：{brief.reference}</p>
          <ul className="list-disc pl-4 text-muted-foreground">
            {brief.tips.map((tip) => (
              <li key={tip}>{tip}</li>
            ))}
          </ul>
        </div>
      )}
      <DialActions onClose={onClose} closeLabel="知道了" />
    </DialShell>
  )
}

export function SectionLocalEditDialog({
  planId,
  sectionLabel,
  onClose,
  onApplied,
}: {
  planId: PlanId
  sectionLabel: string
  onClose: () => void
  onApplied: (plan: Plan) => void
}) {
  const [instruction, setInstruction] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [diffs, setDiffs] = useState<ComposeEditDiff[]>([])
  const [note, setNote] = useState('')

  const preview = async () => {
    const text = instruction.trim()
    if (!text) return
    setBusy(true)
    setError(null)
    try {
      const scoped = `只改「${sectionLabel}」这一段：${text}`
      const res = await api.post<ComposeEditResponse>('/edit/compose', {
        plan_id: planId,
        step: 'step2',
        instruction: scoped,
        apply: false,
      } satisfies ComposeEditRequest)
      setDiffs(res.diffs)
      setNote(res.note ?? '')
    } catch (err) {
      setError(err instanceof Error ? err.message : '局部改片失败')
    } finally {
      setBusy(false)
    }
  }

  const apply = async () => {
    const text = instruction.trim()
    if (!text || diffs.length === 0) return
    setBusy(true)
    setError(null)
    try {
      const confirmed = diffs
        .map((d) => d.args)
        .filter((a): a is Record<string, unknown> => !!a && Object.keys(a).length > 0)
      const res = await api.post<ComposeEditResponse>('/edit/compose', {
        plan_id: planId,
        step: 'step2',
        instruction: `只改「${sectionLabel}」这一段：${text}`,
        apply: true,
        confirmed_ops: confirmed.length > 0 ? confirmed : undefined,
      } satisfies ComposeEditRequest)
      if (!res.plan) {
        setNote(res.note || '没有写回修改')
        return
      }
      onApplied(res.plan)
    } catch (err) {
      setError(err instanceof Error ? err.message : '应用改片失败')
    } finally {
      setBusy(false)
    }
  }

  return (
    <DialShell title={`局部改片 · ${sectionLabel}`} hint="只作用于这一段。确认前不改 plan。" onClose={onClose}>
      <textarea
        value={instruction}
        onChange={(e) => setInstruction(e.target.value)}
        rows={3}
        placeholder="例如：这一段口播改得更短，画面改成产品特写"
        className="w-full resize-y rounded-md border border-border bg-background px-2 py-1.5 text-xs outline-none focus:border-primary"
      />
      {error && <p className="text-xs text-destructive">{error}</p>}
      {note && <p className="text-[11px] text-muted-foreground">{note}</p>}
      {diffs.length > 0 && (
        <ul className="max-h-40 space-y-1 overflow-y-auto text-xs">
          {diffs.map((d, i) => (
            <li key={`${d.op}-${i}`} className="rounded border border-border px-2 py-1">
              {d.summary}
            </li>
          ))}
        </ul>
      )}
      <div className="flex justify-end gap-2">
        <button type="button" onClick={onClose} className="rounded-md border border-border px-3 py-1.5 text-xs">
          放弃
        </button>
        <button
          type="button"
          disabled={busy || !instruction.trim()}
          onClick={() => void preview()}
          className="rounded-md border border-border px-3 py-1.5 text-xs disabled:opacity-50"
        >
          {busy ? '处理中…' : '预览 diff'}
        </button>
        <button
          type="button"
          disabled={busy || diffs.length === 0}
          onClick={() => void apply()}
          className="rounded-md bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground disabled:opacity-50"
        >
          应用到本段
        </button>
      </div>
    </DialShell>
  )
}

function DialShell({
  title,
  hint,
  onClose,
  children,
}: {
  title: string
  hint: string
  onClose: () => void
  children: ReactNode
}) {
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4" onClick={onClose}>
      <div
        className="flex max-h-[80vh] w-full max-w-md flex-col gap-3 overflow-y-auto rounded-lg border border-border bg-card p-4 shadow-xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div>
          <h3 className="text-sm font-semibold">{title}</h3>
          <p className="mt-1 text-[11px] text-muted-foreground">{hint}</p>
        </div>
        {children}
      </div>
    </div>
  )
}

function DialActions({
  onClose,
  onConfirm,
  confirmLabel,
  confirmDisabled,
  closeLabel = '放弃',
}: {
  onClose: () => void
  onConfirm?: () => void
  confirmLabel?: string
  confirmDisabled?: boolean
  closeLabel?: string
}) {
  return (
    <div className="flex justify-end gap-2">
      <button type="button" onClick={onClose} className="rounded-md border border-border px-3 py-1.5 text-xs">
        {closeLabel}
      </button>
      {onConfirm && (
        <button
          type="button"
          disabled={confirmDisabled}
          onClick={onConfirm}
          className="rounded-md bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground disabled:opacity-50"
        >
          {confirmLabel}
        </button>
      )}
    </div>
  )
}
