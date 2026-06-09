import { useEffect, useRef, useState } from 'react'

import { api } from '@/api/client'
import { createSSE, type SSEHandle } from '@/api/sse'
import { cn } from '@/lib/utils'
import type {
  ClarifyFinalizeResponse,
  ClarifyOutline,
  ClarifyRoundDone,
  ClarifyRoundProgress,
  ClarifyTurn,
} from '@/types/schemas'

/**
 * 视频工坊 step 1 内嵌：意图澄清面板（v2 · 五件套结构化）。
 *
 * - LLM 多轮追问（最多 3 轮），每轮返回结构化 outline（topic/content/audience/goal/tone）+ 一个具体追问。
 * - 用户可在面板里直接编辑五件套字段，点「采纳」时由前端拼出 brief 写回。
 * - 「跳过追问」直接拼当前五件套，后端不再调 LLM（v2 改动）。
 *
 * 与服务端的协议：
 *   GET  /api/clarify/round?p=<base64(JSON)>   流式 SSE（progress.thinking / progress.outline_ready / done）
 *   POST /api/clarify/finalize                  纯字段拼接（v2 不再调 LLM）
 */

const MAX_ROUNDS = 3

type Phase = 'idle' | 'streaming' | 'awaitAnswer' | 'finalDraft' | 'error'

interface Turn {
  question: string
  answer: string
}

const EMPTY_OUTLINE: ClarifyOutline = {
  topic: null,
  content: null,
  audience: null,
  goal: null,
  tone: null,
}

const FIELD_DEFS: Array<{
  key: keyof ClarifyOutline
  label: string
  placeholder: string
  rows?: number
}> = [
  { key: 'topic', label: '主题', placeholder: '一句话讲这条视频要做什么', rows: 2 },
  { key: 'content', label: '内容卖点', placeholder: '核心卖点 / 亮点；多条用顿号分隔', rows: 3 },
  { key: 'audience', label: '受众', placeholder: '谁会看？年龄段 / 职业 / 场景任选', rows: 2 },
  { key: 'goal', label: '目的', placeholder: '卖货 / 种草 / 教程 / 娱乐 / 品牌', rows: 1 },
  { key: 'tone', label: '语气', placeholder: '温柔 / 高能 / 沙雕 / 严肃 …', rows: 1 },
]

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

/** 把五件套拼成 brief —— 与服务端 stitch_outline_to_brief 严格一致。 */
function stitchBrief(outline: ClarifyOutline): string {
  const order: Array<[string, string | null]> = [
    ['主题', outline.topic],
    ['内容', outline.content],
    ['受众', outline.audience],
    ['目的', outline.goal],
    ['语气', outline.tone],
  ]
  return order
    .filter(([, v]) => v && v.trim())
    .map(([label, v]) => `【${label}】${(v as string).trim()}`)
    .join('\n')
}

function isOutlineEmpty(outline: ClarifyOutline): boolean {
  return FIELD_DEFS.every(({ key }) => !outline[key]?.trim())
}

export function ClarifyPanel({
  initialBrief,
  onAdopt,
  disabled = false,
  clarified = false,
}: {
  initialBrief: string
  onAdopt: (finalBrief: string) => void
  disabled?: boolean
  /** 父组件回传：用户已经至少完成过一轮澄清（onAdopt 已触发过）。
   *  控制 banner 文案：未澄清显眼红边强提示，已澄清绿边收敛提示。 */
  clarified?: boolean
}) {
  const [open, setOpen] = useState(false)
  const [transcript, setTranscript] = useState<Turn[]>([])
  const [streaming, setStreaming] = useState('')
  const [outline, setOutline] = useState<ClarifyOutline>(EMPTY_OUTLINE)
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
    setOutline(EMPTY_OUTLINE)
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
    sseRef.current = createSSE<ClarifyRoundDone, ClarifyRoundProgress>(
      `/clarify/round?p=${p}`,
      {
        onProgress: (ev) => {
          if (ev.step === 'thinking' && ev.payload?.delta) {
            setStreaming((prev) => prev + (ev.payload?.delta ?? ''))
          } else if (ev.step === 'outline_ready' && ev.payload?.outline) {
            // 让用户在 done 事件之前就看到字段填上
            setOutline({ ...EMPTY_OUTLINE, ...ev.payload.outline })
            if (ev.payload.thinking) {
              setStreaming((prev) => (prev ? prev + '\n' + ev.payload!.thinking : ev.payload!.thinking || ''))
            }
          }
        },
        onDone: (d) => {
          setOutline({ ...EMPTY_OUTLINE, ...d.outline })
          if (d.is_final) {
            setQuestion(null)
            setPhase('finalDraft')
          } else if (d.question) {
            setQuestion(d.question)
            setPhase('awaitAnswer')
          } else {
            // 后端没问也没标 final 的兜底：当作 finalDraft，用户改完点采纳
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
    setOutline(EMPTY_OUTLINE)
    startRound(false, initialBrief, [])
  }

  const handleSubmitAnswer = () => {
    if (!answer.trim() || !question) return
    const next = [...transcript, { question, answer: answer.trim() }]
    setTranscript(next)
    setAnswer('')
    // 第 3 轮回答后，服务端会自动 force_finalize
    startRound(false, snapshotBrief, next)
  }

  const handleSkipFinalize = async () => {
    sseRef.current?.close()
    setError('')
    // 已经有 outline 直接拼字段过去；空了就给后端 fallback 到 initial_brief
    if (isOutlineEmpty(outline) && transcript.length === 0) {
      // 用户连一轮都没跑就点跳过 → 让后端兜底（initial_brief 当 final）
    }
    setPhase('streaming')
    try {
      const tx: ClarifyTurn[] = transcript
      const resp = await api.post<ClarifyFinalizeResponse>('/clarify/finalize', {
        outline,
        initial_brief: (snapshotBrief || initialBrief).trim(),
        transcript: tx,
      })
      setOutline({ ...EMPTY_OUTLINE, ...resp.outline })
      setQuestion(null)
      setPhase('finalDraft')
    } catch (err) {
      setError(err instanceof Error ? err.message : '定稿失败，请重试')
      setPhase('error')
    }
  }

  const handleAdopt = () => {
    const stitched = stitchBrief(outline).slice(0, 1000)
    if (!stitched.trim()) {
      setError('五件套全空，没法生成 brief。请至少填写主题。')
      setPhase('error')
      return
    }
    onAdopt(stitched)
    setOpen(false)
    reset()
  }

  const handleRetry = () => {
    startRound(false, snapshotBrief || initialBrief, transcript)
  }

  const updateField = (key: keyof ClarifyOutline, v: string) => {
    setOutline((prev) => ({ ...prev, [key]: v.length > 0 ? v : null }))
  }

  if (!open) {
    return (
      <div
        className={cn(
          'rounded-md border p-3',
          clarified
            ? 'border-emerald-500/40 bg-emerald-50/50 dark:bg-emerald-950/20'
            : 'border-amber-500/60 bg-amber-50/60 dark:bg-amber-950/30',
        )}
      >
        <div className="flex items-center justify-between gap-3">
          <div className="text-xs leading-relaxed">
            {clarified ? (
              <span className="text-emerald-800 dark:text-emerald-300">
                ✓ 已完成意图澄清。如果想换个方向，可以再做一轮。
              </span>
            ) : (
              <span className="text-amber-900 dark:text-amber-200">
                <span className="font-semibold">必做：</span>
                生成内容轨前先做一轮意图澄清——AI 把你的想法拆成「主题 / 内容 / 受众 / 目的 / 语气」5 件套，每个字段都能改。
              </span>
            )}
          </div>
          <button
            type="button"
            onClick={handleStart}
            disabled={disabled || initialBrief.trim().length === 0}
            className={cn(
              'shrink-0 rounded-md px-3 py-1.5 text-xs font-medium',
              clarified
                ? 'border border-border bg-card text-foreground hover:bg-secondary'
                : 'bg-primary text-primary-foreground',
              (disabled || initialBrief.trim().length === 0) && 'cursor-not-allowed opacity-60',
            )}
            title={initialBrief.trim().length === 0 ? '请先写一句主题' : undefined}
          >
            {clarified ? '重新澄清 ↻' : '开始澄清 ✨'}
          </button>
        </div>
      </div>
    )
  }

  const stitchedPreview = stitchBrief(outline)

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
          className="text-xs text-muted-foreground hover:text-foreground"
        >
          关闭 ✕
        </button>
      </div>

      {transcript.length > 0 && (
        <div className="max-h-32 space-y-2 overflow-y-auto rounded-md bg-card/60 p-2 text-xs">
          {transcript.map((t, i) => (
            <div key={i} className="space-y-1">
              <div className="font-medium text-primary">Q{i + 1}. {t.question}</div>
              <div className="pl-3 text-foreground">→ {t.answer}</div>
            </div>
          ))}
        </div>
      )}

      {(phase === 'streaming' || streaming) && (
        <div className="rounded-md bg-background/70 p-2 text-xs leading-relaxed text-muted-foreground">
          <div className="mb-1 flex items-center gap-2">
            <div className="h-2 w-2 animate-pulse rounded-full bg-primary" />
            <span className="font-medium text-foreground">思考流程</span>
          </div>
          <pre className="whitespace-pre-wrap break-words font-sans">{streaming || '正在连接 AI…'}</pre>
        </div>
      )}

      {/* 五件套字段 —— 任何阶段都展示，让用户随时能改 */}
      <div className="space-y-2 rounded-md border border-emerald-500/30 bg-emerald-50/40 p-2 dark:bg-emerald-950/20">
        <div className="flex items-center justify-between text-xs font-semibold text-emerald-700 dark:text-emerald-400">
          <span>五件套 outline · 任何字段都可以直接改</span>
          {phase === 'streaming' && <span className="text-muted-foreground">填字段中…</span>}
        </div>
        <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
          {FIELD_DEFS.map(({ key, label, placeholder, rows }) => {
            const value = outline[key] ?? ''
            const isWide = key === 'content' || key === 'topic'
            return (
              <div
                key={key}
                className={cn(
                  'flex flex-col gap-1',
                  isWide ? 'sm:col-span-2' : 'sm:col-span-1',
                )}
              >
                <label className="text-[10px] font-medium uppercase tracking-wider text-muted-foreground">
                  {label}
                </label>
                <textarea
                  value={value}
                  onChange={(e) => updateField(key, e.target.value)}
                  rows={rows ?? 1}
                  placeholder={placeholder}
                  className="w-full resize-y rounded-md border border-border bg-background/80 p-1.5 text-xs outline-none focus:border-primary"
                />
              </div>
            )
          })}
        </div>
        {stitchedPreview && (
          <div className="rounded-md bg-card/70 p-1.5 text-[11px] leading-relaxed text-muted-foreground">
            <div className="mb-0.5 text-[10px] uppercase tracking-wider">采纳后写回主题的 brief</div>
            <pre className="whitespace-pre-wrap break-words font-sans">{stitchedPreview}</pre>
          </div>
        )}
      </div>

      {phase === 'awaitAnswer' && question && (
        <div className="space-y-2 rounded-md border border-amber-500/30 bg-amber-50/60 p-2 dark:bg-amber-950/30">
          <div className="text-xs font-semibold text-amber-800 dark:text-amber-300">{question}</div>
          <textarea
            value={answer}
            onChange={(e) => setAnswer(e.target.value.slice(0, 200))}
            rows={2}
            placeholder="一句话回答即可，留白会被忽略"
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
        {phase === 'finalDraft' && (
          <>
            <button
              type="button"
              onClick={handleAdopt}
              disabled={!stitchedPreview.trim()}
              className={cn(
                'rounded-md bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground',
                !stitchedPreview.trim() && 'cursor-not-allowed opacity-60',
              )}
            >
              采纳并写回主题 ✓
            </button>
            <button
              type="button"
              onClick={() => {
                setTranscript([])
                setSnapshotBrief(initialBrief)
                setOutline(EMPTY_OUTLINE)
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
