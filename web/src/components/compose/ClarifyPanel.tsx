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
   *  平行，UI 分两组 chip 显示。即使用户没上传素材也能看到「主题里识别到了 X、Y」。
   *  用户可编辑：chip 上有 × 删除；末尾输入框 + Enter 添加；编辑后这些主体会被
   *  写回 outline.content（「（涉及 ...）」机械追加），并作为 detected_subjects
   *  二次跑澄清时的硬约束。 */
  const [briefSubjects, setBriefSubjects] = useState<string[]>([])
  /** 是否被用户手动改过——改过之后下次 outline_ready 不再覆盖（避免 LLM 重抽冲掉用户编辑）。 */
  const [briefSubjectsDirty, setBriefSubjectsDirty] = useState(false)
  /** 末尾添加输入框 draft */
  const [subjectDraft, setSubjectDraft] = useState('')
  /** 由意图澄清清洗后留下的素材识别子集（≤detectedSubjects 全集）；handleAdopt 时只把这部分
   *  union 进 outline.content，避免「耳钉/美甲」这类陪衬被当主体硬塞进段落分镜。 */
  const [relevantDetected, setRelevantDetected] = useState<string[]>([])
  /** 被意图清洗丢弃的素材识别项；UI 用删除线灰显告诉用户「这些 AI 已替你忽略」。 */
  const [droppedDetected, setDroppedDetected] = useState<string[]>([])
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
    setBriefSubjectsDirty(false)
    setSubjectDraft('')
    setRelevantDetected([])
    setDroppedDetected([])
  }

  /** chip 删除：reuse 后 mark dirty 防止下轮 LLM 覆盖。 */
  const removeBriefSubject = (s: string) => {
    setBriefSubjects((prev) => prev.filter((x) => x !== s))
    setBriefSubjectsDirty(true)
  }

  /** chip 添加：去重 + 长度 1–12 + 不超 6 个；mark dirty。 */
  const addBriefSubject = (raw: string) => {
    const v = raw.trim()
    if (!v) return
    if (v.length > 12) {
      setError('单个物体最多 12 个字。')
      return
    }
    setBriefSubjects((prev) => {
      if (prev.includes(v)) return prev
      if (prev.length >= 6) {
        setError('最多 6 个物体（先删一个再加）。')
        return prev
      }
      return [...prev, v]
    })
    setBriefSubjectsDirty(true)
    setSubjectDraft('')
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
            if (Array.isArray(ev.payload.brief_subjects) && !briefSubjectsDirty) {
              // 用户没动过 chip 才允许 LLM 覆盖；编辑过的尊重用户判断。
              setBriefSubjects(ev.payload.brief_subjects.slice(0, 6))
            }
            // 意图清洗后的素材识别——拆成 relevant / dropped 两组。后端会强制把
            // 黑名单（耳钉/美甲/构图词等）剔到 dropped 里；前端直接信任。
            if (Array.isArray(ev.payload.relevant_detected_subjects)) {
              setRelevantDetected(ev.payload.relevant_detected_subjects)
            }
            if (Array.isArray(ev.payload.dropped_detected_subjects)) {
              setDroppedDetected(ev.payload.dropped_detected_subjects)
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
    // 把用户编辑过的 briefSubjects + 意图清洗后的相关素材识别合并，机械写进 outline.content
    // 末尾「（涉及 X、Y、Z）」——保证下游 adapt / decompose / aigc prompt 看得到具体物体
    // （后端 brief 文本是唯一传输通道，所以必须落地到 content）。
    // **关键**：只 union relevantDetected，不要把全集 detectedSubjects 塞进去——
    // 否则被清洗丢弃的耳钉/美甲会通过 content 反向污染下游 plan.subject_anchors。
    // relevantDetected 兜底：若 LLM/SSE 还没回填（异常路径），回退到全集，确保素材不丢。
    const detectedToUnion =
      relevantDetected.length > 0 || droppedDetected.length > 0
        ? relevantDetected
        : detectedSubjects ?? []
    const unionSubjects = Array.from(
      new Set([
        ...briefSubjects.map((s) => s.trim()).filter(Boolean),
        ...detectedToUnion.map((s) => s.trim()).filter(Boolean),
      ]),
    )
    let mergedOutline = outline
    if (unionSubjects.length > 0) {
      const content = (outline.content ?? '').trim()
      const missing = unionSubjects.filter((s) => !content.includes(s))
      if (missing.length > 0) {
        const suffix = `（涉及${missing.join('、')}）`
        const newContent = (content ? content + suffix : `核心可拍物体：${unionSubjects.join('、')}`).slice(0, 400)
        mergedOutline = { ...outline, content: newContent }
      }
    }
    const mergedStitched = stitchBrief(mergedOutline)
    // 用接口拼一次（让后端有「采纳了什么」的最终 ground truth；返回值与本地一致）
    void api
      .post<ClarifyFinalizeResponse>('/clarify/finalize', {
        outline: mergedOutline,
        initial_brief: (snapshotBrief || initialBrief).trim(),
        transcript: transcript as ClarifyTurn[],
      })
      .catch(() => {})

    onAdopt(mergedStitched.slice(0, 1000))
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
          - 推断可拍物体 (briefSubjects)：LLM 反推用户最可能拍到的具体实物。**用户可编辑**——
            chip 上 ✕ 删除、末尾输入框回车添加，最多 6 个。编辑过的清单会在采纳时机械写进
            outline.content（「（涉及 ...）」追加），并下传到 plan.subject_anchors，
            参与后续 adapted_sections / 分镜 / AIGC prompt 的硬约束。
          - 素材识别 (detectedSubjects)：VLM 从用户已上传的图片/视频里识别的对象（只读）
          两路在 handleAdopt 时合并写进 outline.content，形成闭环。 */}
      <div className="space-y-1.5">
        <div className="rounded-md border-2 border-amber-500/60 bg-amber-50/70 px-2 py-2 text-[11px] shadow-sm dark:bg-amber-950/30">
          <div className="mb-1.5 flex items-center justify-between">
            <span className="font-semibold text-amber-700 dark:text-amber-300">
              ⚠ 推断可拍物体（请检查；识别错了直接 ✕ 删，缺的回车补）
            </span>
            <span className="text-[10px] text-muted-foreground">
              {briefSubjects.length}/6
            </span>
          </div>
          <div className="flex flex-wrap items-center gap-1.5">
            {briefSubjects.length > 0 ? (
              briefSubjects.map((s) => (
                <span
                  key={`b-${s}`}
                  className="group inline-flex items-center gap-1 rounded bg-emerald-500/20 px-1.5 py-0.5 text-emerald-800 dark:text-emerald-200"
                >
                  {s}
                  <button
                    type="button"
                    aria-label={`删除 ${s}`}
                    onClick={() => removeBriefSubject(s)}
                    className="text-emerald-700/70 hover:text-rose-600 dark:text-emerald-300/70 dark:hover:text-rose-400"
                  >
                    ×
                  </button>
                </span>
              ))
            ) : (
              <span className="text-muted-foreground">
                暂无——AI 没识别到具体物体，建议手动加（如「青铜鼎」「玉器」）
              </span>
            )}
            {briefSubjects.length < 6 && (
              <input
                value={subjectDraft}
                onChange={(e) => setSubjectDraft(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') {
                    e.preventDefault()
                    addBriefSubject(subjectDraft)
                  } else if (e.key === 'Escape') {
                    setSubjectDraft('')
                  }
                }}
                onBlur={() => {
                  if (subjectDraft.trim()) addBriefSubject(subjectDraft)
                }}
                placeholder="+ 加物体（回车确认）"
                maxLength={12}
                className="w-32 rounded border border-amber-500/40 bg-background/70 px-1.5 py-0.5 text-[11px] placeholder:text-muted-foreground/60 focus:border-amber-500 focus:outline-none"
              />
            )}
          </div>
          {briefSubjectsDirty && (
            <div className="mt-1 text-[10px] text-amber-700 dark:text-amber-300">
              ✎ 已被你编辑过；采纳后将写进段落分镜的主体锚点
            </div>
          )}
        </div>
        <div className="rounded-md border border-dashed border-border bg-card/50 px-2 py-1.5 text-[10px]">
          {detectedSubjects.length > 0 ? (
            <div className="space-y-1">
              {/* 保留组：与本次脚本主题相关的——会进 content */}
              <div className="flex flex-wrap items-center gap-1">
                <span className="text-muted-foreground">
                  素材识别 · 保留 {(relevantDetected.length || detectedSubjects.length)}：
                </span>
                {(relevantDetected.length > 0 || droppedDetected.length > 0
                  ? relevantDetected
                  : detectedSubjects
                ).map((s) => (
                  <span
                    key={`m-keep-${s}`}
                    className="rounded bg-sky-500/10 px-1.5 py-0.5 text-sky-700 dark:text-sky-300"
                  >
                    {s}
                  </span>
                ))}
                <span className="text-muted-foreground">· 一定会出现在 content</span>
              </div>
              {/* 丢弃组：陪衬物（耳钉/美甲/构图词等）——已被意图清洗剔除，不进 content */}
              {droppedDetected.length > 0 && (
                <div className="flex flex-wrap items-center gap-1">
                  <span className="text-muted-foreground">
                    AI 已忽略 {droppedDetected.length}：
                  </span>
                  {droppedDetected.map((s) => (
                    <span
                      key={`m-drop-${s}`}
                      className="rounded bg-muted/60 px-1.5 py-0.5 text-muted-foreground line-through opacity-70"
                      title="与脚本主题无关，已被意图清洗剔除"
                    >
                      {s}
                    </span>
                  ))}
                  <span className="text-muted-foreground">· 不会进段落分镜</span>
                </div>
              )}
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
