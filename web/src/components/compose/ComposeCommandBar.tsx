/**
 * Compose 对话编辑小助手 ⌘K — v2 对话版
 *
 * 区别于 v1 的单次 modal：
 * - 打开时 agent 主动发开场白（说明当前 step、可改清单、3-4 个例子）
 * - 多轮对话累积上下文：每轮用户消息 → 后端 dry-run → 内嵌 diff 卡片 + 应用按钮
 * - 应用成功后 plan_id 刷新，agent 接着发"已完成"消息，对话继续
 * - 越界 / 未识别 → agent 用 note 自然语言回退，不打断对话节奏
 *
 * 作用域（与后端 _STEP_TOOLS 严格对齐）：
 * - step2：只改内容轨（文案 / 段时长 / 删段 / 重排）
 * - step3：禁内容轨，其余全开（字卡 / 包装项 / BGM 偏移 + 音量 / compose 设置）
 */
import { useEffect, useMemo, useRef, useState } from 'react'

import { api, ApiError } from '@/api/client'
import { cn } from '@/lib/utils'
import type {
  ComposeEditDiff,
  ComposeEditDismissRequest,
  ComposeEditRequest,
  ComposeEditResponse,
  ConversationListResponse,
  ConversationMessage,
  Plan,
  PlanId,
} from '@/types/schemas'

interface Props {
  open: boolean
  onClose: () => void
  planId: PlanId
  step: 'step2' | 'step3'
  /** 项目级历史 scope；空字符串 → 不加载/写入历史（兼容老 plan 没绑 project） */
  projectId: string
  /** apply 成功后用最新 plan 替换 store */
  onApplied: (plan: Plan) => void
}

const STEP_TITLE: Record<'step2' | 'step3', string> = {
  step2: 'step2 · 内容创作',
  step3: 'step3 · 包装编辑',
}

const STEP_SCOPE: Record<'step2' | 'step3', string> = {
  step2: '**可以改的**：段落文案 / 段时长 / 删段 / 重排 / 分镜画面 / 分镜口播 / 分镜主体 / 分镜时长 / 字卡文案与字号 / 包装项文字与时间 / 转场样式 / 整体口播重写 / BGM 偏移与音量 / Compose 设置（平台 / 比例 / 总时长 / 迁移倾向 / 字幕开关 / 口播开关 / TTS 音色 / 画面预设 / 包装预设）/ 单段或批量重排素材 / 重出字卡。**唯独 AI 生图（Seedream）/ AI 视频（Seedance）请到 AIGC 面板手改提示词后再点重生**。',
  step3: '**可以改的**：字卡文案与字号 / 包装项文字与时间 / 转场样式（hard_cut / dissolve / slide / zoom / whip / wipe）/ 整体口播重写（hint）/ BGM 偏移与音量 / Compose 设置 / 单段或批量重排素材 / 重出字卡 / 重生 AI 生图。**禁止改内容轨**——要改段落文案 / 段时长 / 删段 / 重排 / 分镜文本请回 step2。',
}

const STEP_EDIT_EXAMPLES: Record<'step2' | 'step3', string[]> = {
  step2: [
    '把第 1 段改成 5 秒',
    '把第 1 段第 2 镜画面改成「特写咖啡杯」',
    '把第 2 段第 1 镜口播改成「凌晨三点的便利店」',
    '重新挑第 3 段的素材',
    '所有段都重新生成字卡',
    '删除第 2 段',
    '把段落顺序改成 第 1 段、第 3 段、第 2 段',
  ],
  step3: [
    'BGM 推迟 2 秒',
    'BGM 音量调到 0.6',
    '把第 2 段字卡字号放大到 1.2',
    '把第 3 段转场改成 dissolve 0.4 秒',
    '整体口播改成更紧凑的语气',
    '把封面字卡时间挪到 0-2 秒',
    '画面改方版',
    '把最后一段字卡文字改成「现在就来」',
  ],
}

const QA_EXAMPLES = [
  '当前项目主题是什么？结构怎样？',
  '哪些段还没填素材？空着会影响什么？',
  '我这段适合用什么样的素材？',
  '现在的调性 / BGM / 比例是什么？',
]

const STEP_EXAMPLES: Record<'step2' | 'step3', string[]> = {
  step2: [...STEP_EDIT_EXAMPLES.step2.slice(0, 4), ...QA_EXAMPLES.slice(0, 2)],
  step3: [...STEP_EDIT_EXAMPLES.step3.slice(0, 4), ...QA_EXAMPLES.slice(0, 2)],
}

type Role = 'agent' | 'user'

interface ChatMessage {
  id: string
  role: Role
  /** 主体文字（agent 的 hint / user 的指令 / agent 的回执） */
  text: string
  /** agent 给出的待应用 diff（dry-run 结果），空数组表示无可执行动作 */
  diffs?: ComposeEditDiff[]
  /** 这条消息对应的指令（用户那条）—— apply 时复用 */
  instruction?: string
  /** 应用状态：pending=展示按钮 / applying=进行中 / applied=已成功 / dismissed=用户撤回 */
  applyState?: 'pending' | 'applying' | 'applied' | 'dismissed'
  /** agent 兜底说明 */
  note?: string | null
}

let _msgSeq = 0
const nextId = () => `m-${Date.now()}-${++_msgSeq}`


export function ComposeCommandBar({ open, onClose, planId, step, projectId, onApplied }: Props) {
  const [messages, setMessages] = useState<ChatMessage[]>([])
  const [draft, setDraft] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [historyLoading, setHistoryLoading] = useState(false)
  const inputRef = useRef<HTMLInputElement>(null)
  const scrollRef = useRef<HTMLDivElement>(null)

  // 当前 plan_id 实时跟随父级 props（apply 后父级会更新）
  const currentPlanId = planId

  // 打开时拉取项目级历史 + 拼上开场白
  useEffect(() => {
    if (!open) return
    let aborted = false
    setError(null)
    setDraft('')
    const intro = makeIntro(step)

    if (!projectId) {
      // 老 plan 没 project 锚——直接显示开场白，不持久化
      setMessages([intro])
      const t = setTimeout(() => inputRef.current?.focus(), 80)
      return () => clearTimeout(t)
    }

    setHistoryLoading(true)
    setMessages([intro])
    void (async () => {
      try {
        const res = await api.get<ConversationListResponse>(
          `/conversation/${encodeURIComponent(projectId)}`,
        )
        if (aborted) return
        const restored = (res.messages || []).map(toChatMessage)
        // intro 总是最新一条（开场白固定在头部，便于本轮新对话视觉锚定），历史在后面
        setMessages([intro, ...restored])
      } catch {
        // 历史加载失败不影响新对话——继续走
      } finally {
        if (!aborted) setHistoryLoading(false)
      }
    })()
    const t = setTimeout(() => inputRef.current?.focus(), 80)
    return () => {
      aborted = true
      clearTimeout(t)
    }
  }, [open, step, projectId])

  // 滚到底
  useEffect(() => {
    if (!scrollRef.current) return
    scrollRef.current.scrollTop = scrollRef.current.scrollHeight
  }, [messages])

  // ESC 关闭
  useEffect(() => {
    if (!open) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.preventDefault()
        onClose()
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open, onClose])

  const exampleChips = useMemo(() => STEP_EXAMPLES[step], [step])

  const sendMessage = async (text: string) => {
    const trimmed = text.trim()
    if (!trimmed || busy) return
    const userMsg: ChatMessage = { id: nextId(), role: 'user', text: trimmed }
    setMessages((m) => [...m, userMsg])
    setDraft('')
    setBusy(true)
    setError(null)
    try {
      const body: ComposeEditRequest = {
        plan_id: currentPlanId,
        step,
        instruction: trimmed,
        apply: false,
      }
      const res = await api.post<ComposeEditResponse>('/edit/compose', body)
      const replyText =
        res.diffs.length > 0
          ? `识别到 ${res.diffs.length} 项可执行的修改，确认后我就改。`
          : res.note || '我没看明白这个指令，可以再说具体一点吗？'
      const agentMsg: ChatMessage = {
        id: nextId(),
        role: 'agent',
        text: replyText,
        diffs: res.diffs,
        note: res.note ?? null,
        instruction: trimmed,
        applyState: res.diffs.length > 0 ? 'pending' : undefined,
      }
      setMessages((m) => [...m, agentMsg])
    } catch (e) {
      const msg = e instanceof ApiError ? e.message : (e as Error).message
      setError(msg || '请求失败')
      setMessages((m) => [
        ...m,
        { id: nextId(), role: 'agent', text: `请求失败：${msg || '未知错误'}` },
      ])
    } finally {
      setBusy(false)
    }
  }

  const applyMessage = async (msgId: string) => {
    const msg = messages.find((m) => m.id === msgId)
    if (!msg || !msg.instruction || !msg.diffs || msg.diffs.length === 0) return
    if (msg.applyState !== 'pending') return
    setMessages((all) => all.map((m) => (m.id === msgId ? { ...m, applyState: 'applying' } : m)))
    setError(null)
    try {
      // 把 dry-run 拿到的 args 原样回传，后端跳过 LLM 直接回放——保证 N 个 diff 一定 N 个落地
      const confirmed_ops = msg.diffs
        .map((d) => d.args)
        .filter((a): a is Record<string, unknown> => !!a && Object.keys(a).length > 0)
      const body: ComposeEditRequest = {
        plan_id: currentPlanId,
        step,
        instruction: msg.instruction,
        apply: true,
        confirmed_ops: confirmed_ops.length > 0 ? confirmed_ops : undefined,
      }
      const res = await api.post<ComposeEditResponse>('/edit/compose', body)
      if (res.plan) onApplied(res.plan)
      const actualCount = res.diffs.length
      const expected = msg.diffs.length
      const tail =
        actualCount === expected
          ? `已应用 ${actualCount} 项修改。还要继续调吗？`
          : `已应用 ${actualCount} / ${expected} 项（部分回放失败，目标可能已被同 plan 内别的改动顶替；可以再发一遍）。`
      setMessages((all) => [
        ...all.map((m) => (m.id === msgId ? { ...m, applyState: 'applied' as const } : m)),
        { id: nextId(), role: 'agent', text: tail },
      ])
    } catch (e) {
      const errMsg = e instanceof ApiError ? e.message : (e as Error).message
      setError(errMsg || '应用失败')
      setMessages((all) =>
        all.map((m) => (m.id === msgId ? { ...m, applyState: 'pending' as const } : m)),
      )
    }
  }

  const dismissMessage = (msgId: string) => {
    const msg = messages.find((m) => m.id === msgId)
    setMessages((all) => all.map((m) => (m.id === msgId ? { ...m, applyState: 'dismissed' } : m)))
    if (!msg || !msg.instruction || !msg.diffs || msg.diffs.length === 0) return
    // 把撤回的 diff 落到 profile TraceB 负信号——失败不打扰用户，蒸馏少一条而已
    const dismissed_ops: Array<Record<string, unknown>> = []
    for (const d of msg.diffs) {
      if (!d.args || Object.keys(d.args).length === 0) continue
      dismissed_ops.push({ op: d.op, ...d.args })
    }
    if (dismissed_ops.length === 0) return
    const body: ComposeEditDismissRequest = {
      plan_id: currentPlanId,
      step,
      instruction: msg.instruction,
      dismissed_ops,
    }
    api.post('/edit/compose/dismiss', body).catch(() => {
      // 静默：负信号沉淀失败不影响 UX
    })
  }

  const clearHistory = async () => {
    if (!projectId) return
    if (!window.confirm('确定清空当前项目的 ⌘K 对话历史吗？此操作不可恢复。')) return
    try {
      await api.delete(`/conversation/${encodeURIComponent(projectId)}`)
      setMessages([makeIntro(step)])
    } catch (e) {
      const msg = e instanceof ApiError ? e.message : (e as Error).message
      setError(msg || '清空失败')
    }
  }

  if (!open) return null

  return (
    <div
      className="fixed inset-0 z-[140] flex items-start justify-center bg-background/80 pt-[8vh] backdrop-blur-sm"
      onClick={onClose}
    >
      <div
        className="flex max-h-[80vh] w-full max-w-2xl flex-col rounded-xl border bg-card shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        {/* header */}
        <div className="flex items-center justify-between border-b px-5 py-3">
          <div className="flex items-center gap-2">
            <span className="rounded bg-primary/10 px-1.5 py-0.5 text-xs font-medium text-primary">
              ⌘K
            </span>
            <span className="text-sm font-medium">对话编辑小助手</span>
            <span className="text-xs text-muted-foreground">· {STEP_TITLE[step]}</span>
            {historyLoading && (
              <span className="text-xs text-muted-foreground">· 载入历史…</span>
            )}
          </div>
          <div className="flex items-center gap-3">
            {projectId && (
              <button
                onClick={clearHistory}
                className="text-xs text-muted-foreground hover:text-rose-500"
                title="清空当前项目的 ⌘K 对话历史"
              >
                清空历史
              </button>
            )}
            <button
              onClick={onClose}
              className="text-xs text-muted-foreground hover:text-foreground"
            >
              关闭 (Esc)
            </button>
          </div>
        </div>

        {/* messages */}
        <div ref={scrollRef} className="flex-1 space-y-3 overflow-y-auto px-5 py-4">
          {messages.map((m) => (
            <ChatBubble
              key={m.id}
              msg={m}
              onApply={() => applyMessage(m.id)}
              onDismiss={() => dismissMessage(m.id)}
              onPickExample={(ex) => setDraft(ex)}
              examples={exampleChips}
            />
          ))}
          {busy && (
            <div className="text-xs text-muted-foreground">小助手在想…</div>
          )}
        </div>

        {/* error */}
        {error && (
          <div className="mx-5 mb-2 rounded-lg border border-rose-500/40 bg-rose-500/10 px-3 py-2 text-xs text-rose-700">
            {error}
          </div>
        )}

        {/* input */}
        <div className="border-t px-5 py-3">
          <div className="flex items-center gap-2">
            <input
              ref={inputRef}
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter' && !e.shiftKey) {
                  e.preventDefault()
                  sendMessage(draft)
                }
              }}
              placeholder="用一句话说要改什么…"
              disabled={busy}
              className="flex-1 rounded-lg border bg-background px-3 py-2 text-sm outline-none ring-primary/30 focus:ring-2"
            />
            <button
              onClick={() => sendMessage(draft)}
              disabled={busy || !draft.trim()}
              className={cn(
                'rounded bg-primary px-3 py-2 text-xs font-medium text-primary-foreground',
                'hover:bg-primary/90 disabled:cursor-not-allowed disabled:opacity-60',
              )}
            >
              {busy ? '思考中' : '发送'}
            </button>
          </div>
          <div className="mt-1 text-xs text-muted-foreground">
            Enter 发送 · Esc 关闭 · plan_id：{currentPlanId.slice(0, 18)}…
          </div>
        </div>
      </div>
    </div>
  )
}


function makeIntro(step: 'step2' | 'step3'): ChatMessage {
  const examples = STEP_EXAMPLES[step]
  const text =
    `你好，我是 Compose 的对话编辑小助手 · 当前 ${STEP_TITLE[step]}。\n\n` +
    `我能做两件事：\n` +
    `1. **讲解项目**：聊聊本片当前的主题、结构、段落填充情况、调性/BGM/比例现状，帮你判断哪段空缺、要找什么素材。**不会编造**——上下文里没有的事我直说不知道。\n` +
    `2. **执行编辑**：${STEP_SCOPE[step]}\n\n` +
    `直接说话就行，比如：\n` +
    examples.map((e) => `· ${e}`).join('\n') +
    `\n\n下指令我会先给你看 diff 再确认；问问题我直接答。`
  return { id: nextId(), role: 'agent', text }
}


/** 把后端持久化的 ConversationMessage 还原成前端 ChatMessage（含 diff 卡片）。 */
function toChatMessage(m: ConversationMessage): ChatMessage {
  const meta = (m.meta || {}) as Record<string, unknown>
  const diffs = Array.isArray(meta.diffs) ? (meta.diffs as ComposeEditDiff[]) : undefined
  // 历史中已 applied/dismissed 的不再可重放——给一个终态标记
  let applyState: ChatMessage['applyState']
  if (m.kind === 'agent_apply') applyState = 'applied'
  else if (m.kind === 'agent_dismiss') applyState = 'dismissed'
  else if (diffs && diffs.length > 0 && meta.applied === false) applyState = 'pending'

  return {
    id: m.message_id || nextId(),
    role: m.role === 'user' ? 'user' : 'agent',
    text: m.text || '',
    diffs,
    applyState,
    note: typeof meta.note === 'string' ? (meta.note as string) : null,
  }
}


function ChatBubble({
  msg,
  onApply,
  onDismiss,
  onPickExample,
  examples,
}: {
  msg: ChatMessage
  onApply: () => void
  onDismiss: () => void
  onPickExample: (ex: string) => void
  examples: string[]
}) {
  const isAgent = msg.role === 'agent'
  const isIntro =
    isAgent && msg.text.startsWith('你好，我是 Compose 的对话编辑小助手')

  return (
    <div className={cn('flex', isAgent ? 'justify-start' : 'justify-end')}>
      <div
        className={cn(
          'max-w-[85%] rounded-2xl px-4 py-2.5 text-sm leading-relaxed',
          isAgent
            ? 'border bg-secondary/40'
            : 'bg-primary text-primary-foreground',
        )}
      >
        <div className="whitespace-pre-wrap">{renderRichText(msg.text)}</div>

        {/* diff 列表（agent 消息且有 diff） */}
        {isAgent && msg.diffs && msg.diffs.length > 0 && (
          <div className="mt-3 space-y-1.5 rounded-lg border bg-background/70 p-2">
            <div className="text-xs uppercase tracking-wider text-muted-foreground">
              将要修改（{msg.diffs.length}）
            </div>
            <ul className="space-y-1">
              {msg.diffs.map((d, i) => (
                <li key={i} className="flex items-start gap-2 text-xs">
                  <span className="mt-1 inline-block h-1.5 w-1.5 shrink-0 rounded-full bg-primary" />
                  <div className="flex-1">
                    <div className="font-medium">{d.summary}</div>
                    <div className="text-xs text-muted-foreground">
                      op={d.op}
                      {d.target_id ? ` · ${d.target_id}` : ''}
                    </div>
                  </div>
                </li>
              ))}
            </ul>
            <div className="mt-2 flex items-center justify-end gap-2">
              {msg.applyState === 'pending' && (
                <>
                  <button
                    onClick={onDismiss}
                    className="rounded border px-2.5 py-1 text-xs hover:bg-secondary"
                  >
                    撤回
                  </button>
                  <button
                    onClick={onApply}
                    className="rounded bg-primary px-3 py-1 text-xs font-medium text-primary-foreground hover:bg-primary/90"
                  >
                    应用 {msg.diffs.length} 项
                  </button>
                </>
              )}
              {msg.applyState === 'applying' && (
                <span className="text-xs text-muted-foreground">应用中…</span>
              )}
              {msg.applyState === 'applied' && (
                <span className="text-xs text-emerald-600">已应用 ✓</span>
              )}
              {msg.applyState === 'dismissed' && (
                <span className="text-xs text-muted-foreground">已撤回</span>
              )}
            </div>
          </div>
        )}

        {/* 介绍消息：贴示例 chip */}
        {isIntro && (
          <div className="mt-3 flex flex-wrap gap-1.5">
            {examples.map((ex) => (
              <button
                key={ex}
                onClick={() => onPickExample(ex)}
                className="rounded-full border bg-background/80 px-2.5 py-0.5 text-xs text-muted-foreground hover:bg-background"
              >
                {ex}
              </button>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}


/** 简陋的 markdown 渲染：仅支持 **bold** */
function renderRichText(text: string) {
  const parts = text.split(/(\*\*[^*]+\*\*)/g)
  return parts.map((p, i) => {
    if (p.startsWith('**') && p.endsWith('**')) {
      return (
        <strong key={i} className="font-semibold">
          {p.slice(2, -2)}
        </strong>
      )
    }
    return <span key={i}>{p}</span>
  })
}
