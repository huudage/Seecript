import { useEffect, useRef, useState } from 'react'

import { api } from '@/api/client'
import { createSSE, type SSEHandle } from '@/api/sse'
import { cn } from '@/lib/utils'

/**
 * 视频工坊 step 1 内嵌:意图澄清面板。
 *
 * - LLM 多轮追问(最多 3 轮),流式输出「思考流」+ 重写稿 + 一个具体追问。
 * - 用户随时可「跳过追问、1 键定稿」。
 * - 仅在用户点「采纳」时,通过 onAdopt 回写 BriefInput。Q&A 期间不动外部 brief。
 *
 * 与服务端的协议:
 *   GET  /api/clarify/round?p=<base64(JSON)>   流式 SSE
 *   POST /api/clarify/finalize                  一键定稿
 */

const MAX_ROUNDS = 3

type Phase = 'idle' | 'streaming' | 'awaitAnswer' | 'finalDraft' | 'error'

interface Turn {
  question: string
  answer: string
}

interface ClarifyDonePayload {
  round: number
  question: string | null
  is_final: boolean
  final_brief: string | null
}

interface ClarifyProgressPayload {
  delta?: string
  draft?: string
}

function encodePayload(obj: unknown): string {
  const json = JSON.stringify(obj)
  // unicode-safe base64 url encoding
  const bytes = new TextEncoder().encode(json)
  let bin = ''
  bytes.forEach((b) => {
    bin += String.fromCharCode(b)
  })
  return btoa(bin).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '')
}

export function ClarifyPanel({
  initialBrief,
  onAdopt,
  disabled = false,
}: {
  initialBrief: string
  onAdopt: (finalBrief: string) => void
  disabled?: boolean
}) {
  const [open, setOpen] = useState(false)
  const [transcript, setTranscript] = useState<Turn[]>([])
  const [streaming, setStreaming] = useState('')
  const [draft, setDraft] = useState('')
  const [question, setQuestion] = useState<string | null>(null)
  const [answer, setAnswer] = useState('')
  const [phase, setPhase] = useState<Phase>('idle')
  const [error, setError] = useState('')
  const [snapshotBrief, setSnapshotBrief] = useState('')
  const sseRef = useRef<SSEHandle | null>(null)

  const round = transcript.length + 1

  useEffect(() => {
    return () => sseRef.current?.close()
  }, [])

  const reset = () => {
    sseRef.current?.close()
    sseRef.current = null
    setTranscript([])
    setStreaming('')
    setDraft('')
    setQuestion(null)
    setAnswer('')
    setPhase('idle')
    setError('')
  }

  const startRound = (forceFinalize = false, baseBrief?: string, baseTranscript?: Turn[]) => {
    sseRef.current?.close()
    const briefToUse = baseBrief ?? snapshotBrief ?? initialBrief
    const txToUse = baseTranscript ?? transcript
    if (!briefToUse.trim()) {
      setError('请先在主题输入框写一句话再点「澄清意图」。')
      setPhase('error')
      return
    }
    setStreaming('')
    setDraft('')
    setQuestion(null)
    setAnswer('')
    setError('')
    setPhase('streaming')

    const payload = {
      initial_brief: briefToUse.trim(),
      transcript: txToUse,
      force_finalize: forceFinalize,
    }
    const p = encodePayload(payload)
    sseRef.current = createSSE<ClarifyDonePayload, ClarifyProgressPayload>(
      `/clarify/round?p=${p}`,
      {
        onProgress: (ev) => {
          if (ev.step === 'thinking' && ev.payload?.delta) {
            setStreaming((prev) => prev + (ev.payload?.delta ?? ''))
          } else if (ev.step === 'draft_done' && ev.payload?.draft) {
            setDraft(ev.payload.draft)
          }
        },
        onDone: (d) => {
          if (d.is_final && d.final_brief) {
            setDraft(d.final_brief)
            setQuestion(null)
            setPhase('finalDraft')
          } else if (d.question) {
            setQuestion(d.question)
            setPhase('awaitAnswer')
          } else {
            // 后端没问也没 final 的兜底:当作 finalDraft
            setPhase('finalDraft')
          }
        },
        onError: (e) => {
          setError(e.detail || 'AI 连接断开，请重试。')
          setPhase('error')
        },
      },
    )
  }

  const handleStart = () => {
    setSnapshotBrief(initialBrief)
    setOpen(true)
    setTranscript([])
    startRound(false, initialBrief, [])
  }

  const handleSubmitAnswer = () => {
    if (!answer.trim() || !question) return
    const next = [...transcript, { question, answer: answer.trim() }]
    setTranscript(next)
    setAnswer('')
    // 第 3 轮回答后,服务端会自动 force_finalize
    startRound(false, snapshotBrief, next)
  }

  const handleSkipFinalize = async () => {
    sseRef.current?.close()
    setPhase('streaming')
    setStreaming('')
    setError('')
    try {
      const resp = await api.post<{ final_brief: string; round: number }>(
        '/clarify/finalize',
        {
          initial_brief: (snapshotBrief || initialBrief).trim(),
          transcript,
        },
      )
      setDraft(resp.final_brief)
      setQuestion(null)
      setPhase('finalDraft')
    } catch (err) {
      setError(err instanceof Error ? err.message : '定稿失败,请重试')
      setPhase('error')
    }
  }

  const handleAdopt = () => {
    if (!draft.trim()) return
    onAdopt(draft.slice(0, 500))
    setOpen(false)
    reset()
  }

  const handleRetry = () => {
    startRound(false, snapshotBrief || initialBrief, transcript)
  }

  if (!open) {
    return (
      <div className="rounded-md border border-dashed border-border bg-muted/20 p-3">
        <div className="flex items-center justify-between gap-3">
          <div className="text-[11px] leading-relaxed text-muted-foreground">
            想让 AI 把主题打磨得更精准?点「澄清意图」启动 1-3 轮追问,最后给你一段可以直接用的 brief。
          </div>
          <button
            type="button"
            onClick={handleStart}
            disabled={disabled || initialBrief.trim().length === 0}
            className={cn(
              'shrink-0 rounded-md bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground',
              (disabled || initialBrief.trim().length === 0) && 'cursor-not-allowed opacity-60',
            )}
            title={initialBrief.trim().length === 0 ? '请先写一句主题' : undefined}
          >
            澄清意图 ✨
          </button>
        </div>
      </div>
    )
  }

  return (
    <div className="space-y-3 rounded-md border border-primary/40 bg-primary/5 p-3">
      <div className="flex items-center justify-between">
        <div className="text-xs font-semibold text-foreground">
          意图澄清 · 第 {Math.min(round, MAX_ROUNDS)} / {MAX_ROUNDS} 轮
        </div>
        <button
          type="button"
          onClick={() => {
            setOpen(false)
            reset()
          }}
          className="text-[11px] text-muted-foreground hover:text-foreground"
        >
          关闭 ✕
        </button>
      </div>

      {transcript.length > 0 && (
        <div className="max-h-40 space-y-2 overflow-y-auto rounded-md bg-card/60 p-2 text-xs">
          {transcript.map((t, i) => (
            <div key={i} className="space-y-1">
              <div className="font-medium text-primary">Q{i + 1}. {t.question}</div>
              <div className="pl-3 text-foreground">→ {t.answer}</div>
            </div>
          ))}
        </div>
      )}

      {(phase === 'streaming' || streaming) && (
        <div className="rounded-md bg-background/70 p-2 text-[11px] leading-relaxed text-muted-foreground">
          <div className="mb-1 flex items-center gap-2">
            <div className="h-2 w-2 animate-pulse rounded-full bg-primary" />
            <span className="font-medium text-foreground">思考流程</span>
          </div>
          <pre className="whitespace-pre-wrap break-words font-sans">{streaming || '正在连接 AI…'}</pre>
        </div>
      )}

      {draft && (
        <div className="rounded-md border border-emerald-500/30 bg-emerald-50/60 p-2 text-xs leading-relaxed text-foreground dark:bg-emerald-950/30">
          <div className="mb-1 text-[11px] font-semibold text-emerald-700 dark:text-emerald-400">
            当前重写稿
          </div>
          <pre className="whitespace-pre-wrap break-words font-sans">{draft}</pre>
        </div>
      )}

      {phase === 'awaitAnswer' && question && (
        <div className="space-y-2 rounded-md border border-amber-500/30 bg-amber-50/60 p-2 dark:bg-amber-950/30">
          <div className="text-xs font-semibold text-amber-800 dark:text-amber-300">{question}</div>
          <textarea
            value={answer}
            onChange={(e) => setAnswer(e.target.value.slice(0, 200))}
            rows={2}
            placeholder="一句话回答即可,留白会被忽略"
            className="w-full resize-y rounded-md border border-border bg-background/80 p-2 text-xs outline-none focus:border-primary"
          />
        </div>
      )}

      {phase === 'error' && error && (
        <div className="rounded-md border border-destructive/40 bg-destructive/5 p-2 text-xs text-destructive">
          {error}
        </div>
      )}

      <div className="flex flex-wrap gap-2">
        {phase === 'awaitAnswer' && (
          <button
            type="button"
            onClick={handleSubmitAnswer}
            disabled={!answer.trim()}
            className={cn(
              'rounded-md bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground',
              !answer.trim() && 'cursor-not-allowed opacity-60',
            )}
          >
            下一轮 →
          </button>
        )}
        {(phase === 'awaitAnswer' || phase === 'streaming' || phase === 'error') && (
          <button
            type="button"
            onClick={() => {
              void handleSkipFinalize()
            }}
            className="rounded-md border border-border bg-card px-3 py-1.5 text-xs font-medium hover:bg-secondary"
          >
            {phase === 'streaming' ? '跳过追问 · 直接定稿' : '跳过追问 · 1 键定稿'}
          </button>
        )}
        {phase === 'error' && (
          <button
            type="button"
            onClick={handleRetry}
            className="rounded-md border border-border bg-card px-3 py-1.5 text-xs font-medium hover:bg-secondary"
          >
            重试当前轮
          </button>
        )}
        {phase === 'finalDraft' && draft && (
          <>
            <button
              type="button"
              onClick={handleAdopt}
              className="rounded-md bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground"
            >
              采纳并写回主题 ✓
            </button>
            <button
              type="button"
              onClick={() => {
                setTranscript([])
                setSnapshotBrief(initialBrief)
                startRound(false, initialBrief, [])
              }}
              className="rounded-md border border-border bg-card px-3 py-1.5 text-xs font-medium hover:bg-secondary"
            >
              用最新主题重澄清
            </button>
          </>
        )}
      </div>
    </div>
  )
}
