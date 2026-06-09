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
 * 视频工坊 step 1 内嵌：意图澄清面板（v3.1 · 思考时锁定 / 思考完即可编辑）。
 *
 * 流程：
 * - 第 1 轮 LLM 必须填满五件套 5 字段（topic/content/audience/goal/tone）+ 给一个建议性 question。
 * - 五件套字段的锁定规则：
 *     phase=streaming（AI 思考中） → 字段只读，禁止编辑（避免覆盖正在到达的 token）。
 *     phase=review/finalReview     → 字段是普通 textarea，用户随时手动调字。
 * - 用户决策出口：
 *   (a) 「确认采纳 ✓」 → 用本地（可能被改过）的 outline 拼成 brief 写回 BriefInput；
 *       BriefInput 写回后仍可编辑，可直接进入下一步。
 *   (b) 「需要调整 / 继续聊 ↓」 → 展开补充输入框，用户用自然语言补一段，触发下一轮 LLM 重算。
 * - 服务端硬上限 3 轮；第 3 轮强制 finalize 后只剩「确认采纳」一条出路。
 *
 * 与服务端的协议（与 v2 一致）：
 *   GET  /api/clarify/round?p=<base64(JSON)>   流式 SSE（progress.thinking / progress.outline_ready / done）
 *   POST /api/clarify/finalize                  纯字段拼接（不再调 LLM）
 */

const MAX_ROUNDS = 3

type Phase =
  | 'idle'           // 面板未打开
  | 'streaming'      // LLM 正在出 outline
  | 'review'         // 出完了 5 字段，等用户决定确认 or 补充
  | 'continueChat'   // 用户选了「需要调整」，正在输入补充
  | 'finalReview'    // 第 3 轮 / force-finalize 后的终态：只能采纳
  | 'error'

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
  rows?: number
}> = [
  { key: 'topic', label: '主题', rows: 2 },
  { key: 'content', label: '内容卖点', rows: 3 },
  { key: 'audience', label: '受众', rows: 2 },
  { key: 'goal', label: '目的', rows: 1 },
  { key: 'tone', label: '语气', rows: 1 },
]

function encodePayload(obj: unknown): string {
  const json = JSON.stringify(obj)
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

export function ClarifyPanel({
  initialBrief,
  onAdopt,
  disabled = false,
  clarified = false,
  detectedSubjects = [],
}: {
  initialBrief: string
  onAdopt: (finalBrief: string) => void
  disabled?: boolean
  /** 父组件回传：用户已经至少完成过一轮澄清（onAdopt 已触发过）。 */
  clarified?: boolean
  /** 用户已上传素材里 VLM 识别出的对象（去重 ≤20 个），父组件聚合后传入。
   *  作用：澄清提示 LLM 必须把这些对象点名写进 outline.content，避免素材带的物体被
   *  outline 漏掉（例：用户上传了「纸巾」素材但只在 brief 写了「家清好物」，LLM
   *  必须把「纸巾」补回 content）。 */
  detectedSubjects?: string[]
}) {
  const [open, setOpen] = useState(false)
  const [transcript, setTranscript] = useState<Turn[]>([])
  const [streaming, setStreaming] = useState('')
  const [outline, setOutline] = useState<ClarifyOutline>(EMPTY_OUTLINE)
  const [question, setQuestion] = useState<string | null>(null)
  const [supplement, setSupplement] = useState('')
  const [phase, setPhase] = useState<Phase>('idle')
  const [error, setError] = useState('')
  const [snapshotBrief, setSnapshotBrief] = useState('')
  /** LLM 从 INITIAL_BRIEF + outline.content 自抽的具象名词（≤6）；与 detectedSubjects
   *  平行，UI 分两组 chip 显示。即使用户没上传素材也能看到「主题里识别到了 X、Y」。 */
  const [briefSubjects, setBriefSubjects] = useState<string[]>([])
  const sseRef = useRef<SSEHandle | null>(null)

  const round = transcript.length + 1
  const isFinalRound = round >= MAX_ROUNDS
  const stitchedPreview = stitchBrief(outline)

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
    setSupplement('')
    setPhase('idle')
    setError('')
    setBriefSubjects([])
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
    setSupplement('')
    setError('')
    setPhase('streaming')

    const payload = {
      initial_brief: briefToUse.trim(),
      transcript: txToUse,
      force_finalize: forceFinalize,
      detected_subjects: detectedSubjects.slice(0, 20),
    }
    const p = encodePayload(payload)
    sseRef.current = createSSE<ClarifyRoundDone, ClarifyRoundProgress>(
      `/clarify/round?p=${p}`,
      {
        onProgress: (ev) => {
          if (ev.step === 'thinking' && ev.payload?.delta) {
            setStreaming((prev) => prev + (ev.payload?.delta ?? ''))
          } else if (ev.step === 'outline_ready' && ev.payload?.outline) {
            setOutline({ ...EMPTY_OUTLINE, ...ev.payload.outline })
            if (Array.isArray(ev.payload.brief_subjects)) {
              setBriefSubjects(ev.payload.brief_subjects.slice(0, 6))
            }
            if (ev.payload.thinking) {
              setStreaming((prev) =>
                prev ? prev + '\n' + (ev.payload!.thinking || '') : ev.payload!.thinking || '',
              )
            }
          }
        },
        onDone: (d) => {
          setOutline({ ...EMPTY_OUTLINE, ...d.outline })
          setQuestion(d.question)
          if (d.is_final) {
            setPhase('finalReview')
          } else {
            setPhase('review')
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
    setQuestion(null)
    startRound(false, initialBrief, [])
  }

  /** 进入对话补充模式——展开 textarea 让用户自然语言补充。 */
  const handleNeedAdjust = () => {
    setSupplement('')
    setPhase('continueChat')
  }

  /** 用户提交了补充 → 用作 transcript 的 answer，触发下一轮。 */
  const handleSubmitSupplement = () => {
    if (!supplement.trim()) return
    const q = question || '请用一句话补充你想强调或纠正的地方'
    const next = [...transcript, { question: q, answer: supplement.trim() }]
    setTranscript(next)
    setSupplement('')
    startRound(false, snapshotBrief, next)
  }

  const handleAdopt = () => {
    if (!stitchedPreview.trim()) {
      setError('五件套内容为空，没法生成 brief。请重试或换个 initial_brief。')
      setPhase('error')
      return
    }
    // 用接口拼一次（让后端有「采纳了什么」的最终 ground truth；返回值与本地一致）
    void api
      .post<ClarifyFinalizeResponse>('/clarify/finalize', {
        outline,
        initial_brief: (snapshotBrief || initialBrief).trim(),
        transcript: transcript as ClarifyTurn[],
      })
      .catch(() => null)
    onAdopt(stitchedPreview.slice(0, 1000))
    setOpen(false)
    reset()
  }

  const handleRetry = () => {
    startRound(false, snapshotBrief || initialBrief, transcript)
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
                ✓ 已完成意图澄清。主题框里现在可以直接编辑五件套；想换方向再点「重新澄清」。
              </span>
            ) : (
              <span className="text-amber-900 dark:text-amber-200">
                <span className="font-semibold">必做：</span>
                AI 会一次性把你的想法拆成 5 件套（主题 / 内容 / 受众 / 目的 / 语气），
                你只需要确认 OK 或者用一句话补充。
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

  return (
    <div className="space-y-3 rounded-md border border-primary/40 bg-primary/5 p-3">
      <div className="flex items-center justify-between">
        <div className="text-xs font-semibold text-foreground">
          意图澄清 · 第 {Math.min(round, MAX_ROUNDS)} / {MAX_ROUNDS} 轮
          {phase === 'finalReview' && (
            <span className="ml-2 text-amber-600">（最终轮，请确认或重新开始）</span>
          )}
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

      {/* 双路 subject 识别 chips：
          - 主题识别 (briefSubjects)：LLM 从 brief/outline.content 自抽的具象名词，没上传素材也有
          - 素材识别 (detectedSubjects)：VLM 从用户已上传的图片/视频里识别的对象
          两路 union 在脚本生成时一起作为锚点，配合服务端的 enforce_subjects_in_content 闭环。 */}
      <div className="space-y-1.5">
        <div className="rounded-md border border-dashed border-emerald-500/30 bg-emerald-50/40 px-2 py-1.5 text-[10px] dark:bg-emerald-950/20">
          {briefSubjects.length > 0 ? (
            <div className="flex flex-wrap items-center gap-1">
              <span className="text-muted-foreground">推断可拍物体 {briefSubjects.length}：</span>
              {briefSubjects.map((s) => (
                <span
                  key={`b-${s}`}
                  className="rounded bg-emerald-500/15 px-1.5 py-0.5 text-emerald-700 dark:text-emerald-300"
                >
                  {s}
                </span>
              ))}
              <span className="text-muted-foreground">· AI 反推你最可能拍到的具体实物（非主题词）</span>
            </div>
          ) : (
            <span className="text-muted-foreground">
              推断可拍物体：等待 AI 跑澄清——会反推「纸巾/瑜伽垫」这种镜头能拍到的具体实物。
            </span>
          )}
        </div>
        <div className="rounded-md border border-dashed border-border bg-card/50 px-2 py-1.5 text-[10px]">
          {detectedSubjects.length > 0 ? (
            <div className="flex flex-wrap items-center gap-1">
              <span className="text-muted-foreground">素材识别 {detectedSubjects.length}：</span>
              {detectedSubjects.map((s) => (
                <span
                  key={`m-${s}`}
                  className="rounded bg-sky-500/10 px-1.5 py-0.5 text-sky-700 dark:text-sky-300"
                >
                  {s}
                </span>
              ))}
              <span className="text-muted-foreground">· 一定会出现在 content</span>
            </div>
          ) : (
            <span className="text-muted-foreground">
              素材识别：尚未识别到——上传图片/视频后，VLM 标会自动加入并写进 content。
            </span>
          )}
        </div>
      </div>

      {transcript.length > 0 && (
        <div className="max-h-32 space-y-2 overflow-y-auto rounded-md bg-card/60 p-2 text-xs">
          <div className="text-[10px] uppercase tracking-wider text-muted-foreground">历史补充</div>
          {transcript.map((t, i) => (
            <div key={i} className="space-y-1">
              <div className="text-muted-foreground">AI 上轮假设：{t.question}</div>
              <div className="pl-3 text-foreground">→ 你的补充：{t.answer}</div>
            </div>
          ))}
        </div>
      )}

      {(phase === 'streaming' || (streaming && phase !== 'continueChat')) && (
        <div className="rounded-md bg-background/70 p-2 text-xs leading-relaxed text-muted-foreground">
          <div className="mb-1 flex items-center gap-2">
            <div className="h-2 w-2 animate-pulse rounded-full bg-primary" />
            <span className="font-medium text-foreground">思考流程</span>
          </div>
          <pre className="whitespace-pre-wrap break-words font-sans">
            {streaming || '正在连接 AI…'}
          </pre>
        </div>
      )}

      {/* 五件套字段 —— AI 思考时锁定；思考完成后允许用户手动编辑，再决定采纳 / 继续聊 */}
      <div className="space-y-2 rounded-md border border-emerald-500/30 bg-emerald-50/40 p-2 dark:bg-emerald-950/20">
        <div className="flex items-center justify-between text-xs font-semibold text-emerald-700 dark:text-emerald-400">
          <span>
            AI 整理出的五件套
            {phase === 'streaming'
              ? '（思考中，暂时锁定）'
              : '（可以直接改字，确认后写入主题框）'}
          </span>
          {phase === 'streaming' && <span className="text-muted-foreground">填字段中…</span>}
        </div>
        <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
          {FIELD_DEFS.map(({ key, label, rows }) => {
            const value = outline[key] ?? ''
            const isWide = key === 'content' || key === 'topic'
            const locked = phase === 'streaming' || phase === 'continueChat'
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
                  rows={rows ?? 1}
                  readOnly={locked}
                  onChange={(e) =>
                    setOutline((prev) => ({
                      ...prev,
                      [key]: e.target.value || null,
                    }))
                  }
                  placeholder={phase === 'streaming' ? '生成中…' : '（AI 未填写，点击补一句）'}
                  className={cn(
                    'min-h-[28px] w-full resize-y whitespace-pre-wrap break-words rounded-md border border-border/70 bg-background/60 p-1.5 text-xs leading-relaxed outline-none focus:border-primary',
                    locked && 'cursor-not-allowed opacity-80',
                    !value && 'italic text-muted-foreground/70',
                  )}
                />
              </div>
            )
          })}
        </div>
        {stitchedPreview && (phase === 'review' || phase === 'finalReview') && (
          <div className="rounded-md bg-card/70 p-1.5 text-[11px] leading-relaxed text-muted-foreground">
            <div className="mb-0.5 text-[10px] uppercase tracking-wider">采纳后写入主题框的内容</div>
            <pre className="whitespace-pre-wrap break-words font-sans">{stitchedPreview}</pre>
          </div>
        )}
      </div>

      {/* AI 的求证 / 提示 */}
      {phase === 'review' && question && (
        <div className="rounded-md border border-amber-500/30 bg-amber-50/60 p-2 text-xs text-amber-900 dark:bg-amber-950/30 dark:text-amber-200">
          <span className="font-semibold">AI 想跟你确认：</span>
          {question}
        </div>
      )}

      {/* 对话补充输入框 */}
      {phase === 'continueChat' && (
        <div className="space-y-2 rounded-md border border-primary/30 bg-background/70 p-2">
          <div className="text-xs font-semibold text-foreground">
            想调整哪里？用一句话告诉我（不用拘泥格式，AI 会重新整理）
          </div>
          <textarea
            value={supplement}
            onChange={(e) => setSupplement(e.target.value.slice(0, 300))}
            rows={3}
            autoFocus
            placeholder={
              question
                ? `比如回答上面那个问题：${question}\n或者直接说「受众改成宝妈」「语气要更高能」`
                : '比如：受众改成宝妈、语气要更高能、加一条卖点强调环保'
            }
            className="w-full resize-y rounded-md border border-border bg-background/80 p-2 text-xs outline-none focus:border-primary"
          />
          <div className="flex items-center justify-between">
            <span className="font-mono text-[10px] text-muted-foreground">{supplement.length}/300</span>
            <div className="flex gap-2">
              <button
                type="button"
                onClick={() => setPhase('review')}
                className="rounded-md border border-border bg-card px-2.5 py-1 text-xs hover:bg-secondary"
              >
                取消
              </button>
              <button
                type="button"
                onClick={handleSubmitSupplement}
                disabled={!supplement.trim()}
                className={cn(
                  'rounded-md bg-primary px-3 py-1 text-xs font-medium text-primary-foreground hover:bg-primary/90',
                  !supplement.trim() && 'cursor-not-allowed opacity-60',
                )}
              >
                重新整理 →
              </button>
            </div>
          </div>
        </div>
      )}

      {phase === 'error' && error && (
        <div className="rounded-md border border-destructive/40 bg-destructive/5 p-2 text-xs text-destructive">
          {error}
        </div>
      )}

      <div className="flex flex-wrap gap-2">
        {phase === 'review' && (
          <>
            <button
              type="button"
              onClick={handleAdopt}
              disabled={!stitchedPreview.trim()}
              className={cn(
                'rounded-md bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground hover:bg-primary/90',
                !stitchedPreview.trim() && 'cursor-not-allowed opacity-60',
              )}
            >
              确认采纳 ✓
            </button>
            {!isFinalRound && (
              <button
                type="button"
                onClick={handleNeedAdjust}
                className="rounded-md border border-border bg-card px-3 py-1.5 text-xs font-medium hover:bg-secondary"
              >
                需要调整 ↓
              </button>
            )}
          </>
        )}

        {phase === 'finalReview' && (
          <>
            <button
              type="button"
              onClick={handleAdopt}
              disabled={!stitchedPreview.trim()}
              className={cn(
                'rounded-md bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground hover:bg-primary/90',
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
              重新澄清
            </button>
          </>
        )}

        {phase === 'error' && (
          <button
            type="button"
            onClick={handleRetry}
            className="rounded-md border border-border bg-card px-3 py-1.5 text-xs font-medium hover:bg-secondary"
          >
            重试
          </button>
        )}
      </div>
    </div>
  )
}
