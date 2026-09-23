import { useEffect, useState } from 'react'

import {
  regenerateNarrations,
  type NarrationProposal,
} from '@/api/plan'
import type { Plan, PlanId } from '@/types/schemas'

/**
 * 配口播 diff（PRD-v2 F10 ② / US-5.2）。
 * 打开即 dry-run；点「应用到本段」才落 plan。放弃不写盘。
 */

interface Props {
  planId: PlanId
  sectionId: string
  sectionLabel: string
  onClose: () => void
  onApplied: (plan: Plan) => void
}

export function NarrationDiffDialog({
  planId,
  sectionId,
  sectionLabel,
  onClose,
  onApplied,
}: Props) {
  const [loading, setLoading] = useState(true)
  const [applying, setApplying] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [note, setNote] = useState('')
  const [proposals, setProposals] = useState<NarrationProposal[]>([])

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError(null)
    void regenerateNarrations(planId, { section_ids: [sectionId], apply: false })
      .then((res) => {
        if (cancelled) return
        setProposals(res.proposals)
        setNote(res.note)
      })
      .catch((err: unknown) => {
        if (cancelled) return
        setError(err instanceof Error ? err.message : '配口播建议失败')
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [planId, sectionId])

  const apply = async () => {
    setApplying(true)
    setError(null)
    try {
      const res = await regenerateNarrations(planId, {
        section_ids: [sectionId],
        apply: true,
        proposals,
      })
      if (!res.applied || res.updated_scene_ids.length === 0) {
        setNote(res.note || '没有写入口播')
        setProposals(res.proposals)
        return
      }
      onApplied(res.plan)
    } catch (err) {
      setError(err instanceof Error ? err.message : '应用口播失败')
    } finally {
      setApplying(false)
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4">
      <div className="flex max-h-[80vh] w-full max-w-lg flex-col gap-3 rounded-lg border border-border bg-card p-4 shadow-xl">
        <div>
          <h3 className="text-sm font-semibold">配口播 · {sectionLabel}</h3>
          <p className="mt-1 text-[11px] text-muted-foreground">
            建议先给人看。确认前不改画布上的口播稿。
          </p>
        </div>

        <div className="min-h-0 flex-1 space-y-2 overflow-y-auto">
          {loading && <p className="text-xs text-muted-foreground">正在生成本段口播建议…</p>}
          {error && (
            <p className="rounded-md border border-destructive/40 bg-destructive/5 px-2 py-1.5 text-xs text-destructive">
              {error}
            </p>
          )}
          {!loading && proposals.length === 0 && !error && (
            <p className="text-xs text-muted-foreground">{note || '这一段没有可改的口播。'}</p>
          )}
          {proposals.map((p) => (
            <div key={p.scene_id} className="rounded-md border border-border px-2 py-1.5 text-xs">
              <div className="font-mono text-[10px] text-muted-foreground">{p.scene_id}</div>
              <p className="mt-1 text-muted-foreground line-through">{p.old_narration || '（空）'}</p>
              <p className="mt-1 font-medium">{p.new_narration}</p>
            </div>
          ))}
        </div>

        {note && proposals.length > 0 && (
          <p className="text-[11px] text-muted-foreground">{note}</p>
        )}

        <div className="flex justify-end gap-2">
          <button
            type="button"
            onClick={onClose}
            className="rounded-md border border-border bg-card px-3 py-1.5 text-xs font-medium hover:bg-secondary"
          >
            放弃
          </button>
          <button
            type="button"
            disabled={loading || applying || proposals.length === 0}
            onClick={() => void apply()}
            className="rounded-md bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {applying ? '写入中…' : '应用到本段'}
          </button>
        </div>
      </div>
    </div>
  )
}
