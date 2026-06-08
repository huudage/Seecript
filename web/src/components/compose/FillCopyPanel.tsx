import { useEffect, useMemo, useState } from 'react'

import { api } from '@/api/client'
import type {
  CopyOutline,
  CopyOutlineResponse,
  FillResult,
  Gap,
  GapFillRequest,
  Plan,
  TextCardAnimation,
  TextCardBgMode,
  TextCardFontFamily,
  TextCardLayout,
  TextCardSpec,
} from '@/types/schemas'
import { cn } from '@/lib/utils'
import { ThinkingSteps } from './ThinkingSteps'

/**
 * 字卡画面 · 客制化（重写）：copy fill 不再是『写口播』，而是给本段生成一张
 * 个性化字卡画面（字体/版式/颜色/动画/Emoji），由后端 ffmpeg drawtext 渲染成 mp4。
 *
 *   idle              → 用户点『AI 设计字卡 ✨』触发
 *   analyzing-outline → 调 /gap/copy-outline，展示思考过程
 *   outline           → 用户调主副文本 / 字体 / 版式 / 颜色 / 动画 / Emoji / 时长（实时预览）
 *   generating        → 调 /gap/fill action=copy 把 TextCardSpec 全字段送后端
 *   result            → 显示已采纳字卡的 CSS 复刻预览 + 微调入口
 */

type Phase =
  | 'idle'
  | 'analyzing-outline'
  | 'outline'
  | 'generating'
  | 'result'

const FONT_OPTIONS: { value: TextCardFontFamily; label: string; hint: string; cssFamily: string; cssWeight: number }[] = [
  { value: 'bold_sans', label: '粗黑', hint: '强冲击 / 标题', cssFamily: '"PingFang SC","Microsoft YaHei",sans-serif', cssWeight: 900 },
  { value: 'serif_classic', label: '宋体', hint: '稳重 / 经典', cssFamily: '"Source Han Serif SC","SimSun",serif', cssWeight: 700 },
  { value: 'handwriting', label: '手写', hint: '温度 / 治愈', cssFamily: '"KaiTi","STKaiti",cursive', cssWeight: 600 },
  { value: 'tech_mono', label: '科技等宽', hint: '数据 / 极客', cssFamily: '"JetBrains Mono","Consolas",monospace', cssWeight: 700 },
]

const LAYOUT_OPTIONS: { value: TextCardLayout; label: string; hint: string }[] = [
  { value: 'center', label: '居中', hint: '主+副紧贴中央' },
  { value: 'top', label: '顶部', hint: '画面上 1/3' },
  { value: 'bottom', label: '底部', hint: '画面下 1/3' },
  { value: 'split_top_bottom', label: '上下分栏', hint: '主上 / 副下' },
]

const BG_MODE_OPTIONS: { value: TextCardBgMode; label: string; hint: string }[] = [
  { value: 'solid', label: '纯色', hint: '干净专注' },
  { value: 'gradient', label: '渐变', hint: '柔和叙事' },
  { value: 'image_blur', label: '模糊图', hint: '氛围 / 兜底纯色' },
  { value: 'dark_overlay', label: '暗罩', hint: '强对比 / 影院' },
]

const ANIM_OPTIONS: { value: TextCardAnimation; label: string; hint: string }[] = [
  { value: 'fade_in', label: '淡入', hint: '稳妥通用' },
  { value: 'typewriter', label: '打字机', hint: '逐字揭示' },
  { value: 'bounce_word', label: '抖动', hint: '强调重点' },
  { value: 'zoom_pop', label: '放大弹出', hint: '冲击爆点' },
]

const EMOJI_POOL = ['✨', '🔥', '💡', '⚡', '🎯', '🚀', '💎', '⭐', '❗', '❓', '👀', '💯', '🌟', '🎉']

const DEFAULT_SPEC: TextCardSpec = {
  main_text: '',
  sub_text: '',
  font_family: 'bold_sans',
  layout: 'center',
  bg_mode: 'solid',
  bg_color: '#0F172A',
  text_color: '#FFFFFF',
  accent_color: '#22D3EE',
  animation: 'fade_in',
  emoji_decor: [],
  duration_seconds: 4.0,
}

/** 渲染 CSS 复刻预览——给用户即时反馈，最终成品是后端 ffmpeg drawtext。 */
function CardPreview({ spec }: { spec: TextCardSpec }) {
  const font = FONT_OPTIONS.find((f) => f.value === spec.font_family) ?? FONT_OPTIONS[0]
  const bgStyle: React.CSSProperties = (() => {
    switch (spec.bg_mode) {
      case 'gradient':
        return { background: `linear-gradient(135deg, ${spec.bg_color} 0%, ${spec.accent_color} 100%)` }
      case 'dark_overlay':
        return { background: `${spec.bg_color}`, boxShadow: `inset 0 0 0 9999px rgba(0,0,0,0.45)` }
      case 'image_blur':
        return { background: `radial-gradient(circle at 30% 30%, ${spec.accent_color}33, ${spec.bg_color})`, filter: 'none' }
      default:
        return { background: spec.bg_color }
    }
  })()

  const layoutCls = (() => {
    switch (spec.layout) {
      case 'top':
        return 'items-start pt-6'
      case 'bottom':
        return 'items-end pb-6'
      case 'split_top_bottom':
        return 'items-stretch justify-between py-6'
      default:
        return 'items-center'
    }
  })()

  const mainStyle: React.CSSProperties = {
    fontFamily: font.cssFamily,
    fontWeight: font.cssWeight,
    color: spec.text_color,
    textShadow: spec.animation === 'bounce_word' ? `0 2px 0 ${spec.accent_color}` : 'none',
    letterSpacing: spec.font_family === 'tech_mono' ? '0.05em' : 'normal',
  }
  const subStyle: React.CSSProperties = {
    fontFamily: font.cssFamily,
    color: spec.accent_color,
    fontWeight: Math.max(400, font.cssWeight - 300),
  }

  return (
    <div className="space-y-1">
      <div
        className={cn(
          'relative mx-auto flex aspect-[9/16] w-full max-w-[140px] flex-col justify-center overflow-hidden rounded-md border border-border px-3 text-center transition-all',
          layoutCls,
        )}
        style={bgStyle}
      >
        {spec.layout === 'split_top_bottom' ? (
          <>
            <div className="text-[13px] leading-tight" style={mainStyle}>
              {spec.main_text || <span className="opacity-30">主标题</span>}
            </div>
            <div className="text-xs leading-tight" style={subStyle}>
              {spec.sub_text}
            </div>
          </>
        ) : (
          <div className="flex flex-col items-center gap-1">
            <div className="text-[13px] leading-tight" style={mainStyle}>
              {spec.main_text || <span className="opacity-30">主标题</span>}
            </div>
            {spec.sub_text && (
              <div className="text-xs leading-tight" style={subStyle}>
                {spec.sub_text}
              </div>
            )}
          </div>
        )}
        {spec.emoji_decor.length > 0 && (
          <div className="absolute right-1.5 top-1.5 flex gap-0.5 text-[14px] leading-none">
            {spec.emoji_decor.slice(0, 3).map((e, i) => (
              <span key={`${i}-${e}`}>{e}</span>
            ))}
          </div>
        )}
      </div>
      <p className="text-center text-xs text-muted-foreground">
        预览（仅样式参考） · 成品由 AI 直接烧制到画面
      </p>
    </div>
  )
}

export function FillCopyPanel({
  gap,
  fill,
  plan,
  onResult,
}: {
  gap: Gap
  fill: FillResult | null
  plan: Plan | null
  onResult: (fill: FillResult) => void
}) {
  const initialPhase: Phase = fill && fill.action === 'copy' && fill.text_card_spec ? 'result' : 'idle'
  const [phase, setPhase] = useState<Phase>(initialPhase)
  const [analyzeError, setAnalyzeError] = useState<string | null>(null)
  const [genError, setGenError] = useState<string | null>(null)
  const [thinking, setThinking] = useState<string[]>([])
  const [outline, setOutline] = useState<CopyOutline | null>(null)
  const [promptHint, setPromptHint] = useState('')

  // 受控 spec（outline 阶段实时修改）
  const [spec, setSpec] = useState<TextCardSpec>(fill?.text_card_spec ?? DEFAULT_SPEC)

  useEffect(() => {
    if (fill && fill.action === 'copy' && fill.text_card_spec) {
      setPhase('result')
      setSpec(fill.text_card_spec)
    } else {
      setPhase('idle')
      setSpec(DEFAULT_SPEC)
    }
    setAnalyzeError(null)
    setGenError(null)
    setOutline(null)
    setThinking([])
  }, [gap.gap_id, fill])

  const globalKeywords = useMemo(
    () => plan?.settings?.keywords ?? [],
    [plan?.settings?.keywords],
  )

  const updateSpec = <K extends keyof TextCardSpec>(key: K, value: TextCardSpec[K]) =>
    setSpec((prev) => ({ ...prev, [key]: value }))

  const toggleEmoji = (e: string) => {
    setSpec((prev) => {
      const exists = prev.emoji_decor.includes(e)
      if (exists) return { ...prev, emoji_decor: prev.emoji_decor.filter((x) => x !== e) }
      if (prev.emoji_decor.length >= 3) return prev
      return { ...prev, emoji_decor: [...prev.emoji_decor, e] }
    })
  }

  const startAnalyze = async () => {
    setPhase('analyzing-outline')
    setAnalyzeError(null)
    setThinking([])
    const ac = new AbortController()
    const timer = window.setTimeout(() => ac.abort(), 60_000)
    try {
      const resp = await api.post<CopyOutlineResponse>(
        '/gap/copy-outline',
        { gap_id: gap.gap_id, hint: promptHint.trim() || undefined },
        { signal: ac.signal },
      )
      setOutline(resp.outline)
      setThinking(resp.thinking ?? [])
      setSpec({
        ...resp.outline.recommended_spec,
        main_text: resp.outline.main_text || resp.outline.recommended_spec.main_text,
        sub_text: resp.outline.sub_text || resp.outline.recommended_spec.sub_text,
      })
      const delayMs = Math.max(700, (resp.thinking?.length ?? 0) * 600)
      window.setTimeout(() => setPhase('outline'), delayMs)
    } catch (err) {
      const raw = err instanceof Error ? err.message : '分析失败'
      const friendly =
        ac.signal.aborted
          ? 'AI 分析超时（60s）—— 服务器在思考但网络可能中断了。请重试。'
          : /failed to fetch|networkerror|load failed/i.test(raw)
            ? '请求未送达后端：可能网络不稳或服务在重启，稍等后重试。'
            : raw
      setAnalyzeError(friendly)
      setPhase('idle')
    } finally {
      window.clearTimeout(timer)
    }
  }

  const skipToGenerate = () =>
    runFill({
      ...DEFAULT_SPEC,
      main_text: gap.requirement.slice(0, 24),
      sub_text: '',
      prompt_hint: gap.requirement,
    })

  const runFill = async (extra: Record<string, unknown>) => {
    setPhase('generating')
    setGenError(null)
    const ac = new AbortController()
    const timer = window.setTimeout(() => ac.abort(), 90_000)
    try {
      const body: GapFillRequest = {
        gap_id: gap.gap_id,
        action: 'copy',
        params: extra,
      }
      const result = await api.post<FillResult>('/gap/fill', body, { signal: ac.signal })
      onResult(result)
      if (result.text_card_spec) setSpec(result.text_card_spec)
      setPhase('result')
    } catch (err) {
      const raw = err instanceof Error ? err.message : '生成失败'
      const friendly =
        ac.signal.aborted
          ? '字卡生成超时（90s）—— ffmpeg 可能还在跑，请重试或检查后端日志。'
          : /failed to fetch|networkerror|load failed/i.test(raw)
            ? '请求未送达后端：稍等后重试。'
            : raw
      setGenError(friendly)
      setPhase('outline')
    } finally {
      window.clearTimeout(timer)
    }
  }

  const generateFromSpec = () =>
    runFill({
      main_text: spec.main_text.trim(),
      sub_text: spec.sub_text.trim(),
      font_family: spec.font_family,
      layout: spec.layout,
      bg_mode: spec.bg_mode,
      bg_color: spec.bg_color,
      text_color: spec.text_color,
      accent_color: spec.accent_color,
      animation: spec.animation,
      emoji_decor: spec.emoji_decor,
      duration_seconds: spec.duration_seconds,
      emotional_hook: outline?.emotional_hook ?? 'resonance',
      core_message: outline?.core_message ?? '',
      forced_keywords: outline?.must_include_keywords ?? [],
      tone_lean: outline?.tone_lean ?? '',
      prompt_hint: promptHint.trim() || undefined,
    })

  return (
    <div className="space-y-2 rounded-md border border-border bg-background/40 p-3">
      <div className="flex items-center justify-between">
        <h4 className="text-xs font-semibold">字卡画面 · 客制化</h4>
        <span className="text-xs text-muted-foreground">
          {phase === 'idle' && '待开始'}
          {phase === 'analyzing-outline' && '分析中…'}
          {phase === 'outline' && '调参'}
          {phase === 'generating' && '渲染中…'}
          {phase === 'result' && '已生成'}
        </span>
      </div>

      {phase === 'idle' && (
        <div className="space-y-2">
          <p className="text-xs text-muted-foreground">
            AI 看一遍段落 + 整体设置，先给一份字卡设计（主副文本 / 字体 / 版式 / 配色 / 动画 / Emoji）；
            你再随心调，AI 最终把字卡烧制成视频片段，落到画面轨。
          </p>
          <textarea
            value={promptHint}
            onChange={(e) => setPromptHint(e.target.value.slice(0, 200))}
            rows={2}
            placeholder="可选：给 AI 一个方向（如『暗黑科技风』『治愈手写』『大字报式爆点』）"
            className="w-full resize-y rounded-md border border-border bg-background px-2 py-1.5 text-xs outline-none focus:border-primary"
          />
          <div className="flex items-center justify-between gap-2">
            <button
              onClick={skipToGenerate}
              className="text-xs text-muted-foreground underline-offset-2 hover:underline"
            >
              跳过 · 用默认风格直接生成
            </button>
            <button
              onClick={startAnalyze}
              className="rounded-md bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground hover:bg-primary/90"
            >
              AI 设计字卡 ✨
            </button>
          </div>
          {analyzeError && <p className="text-xs text-destructive">{analyzeError}</p>}
        </div>
      )}

      {phase === 'analyzing-outline' && (
        <div className="space-y-2 rounded-md bg-secondary/30 p-2">
          <div className="flex items-center gap-2">
            <span className="relative flex h-2 w-2">
              <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-primary/60" />
              <span className="relative inline-flex h-2 w-2 rounded-full bg-primary" />
            </span>
            <span className="text-xs font-medium">AI 正在为本段设计字卡…</span>
          </div>
          <ThinkingSteps steps={thinking} animated />
        </div>
      )}

      {phase === 'outline' && (
        <div className="grid gap-3 lg:grid-cols-[1fr_140px]">
          <div className="space-y-3">
            {thinking.length > 0 && (
              <details className="rounded-md border border-border bg-background/30 px-2 py-1">
                <summary className="cursor-pointer text-xs text-muted-foreground">
                  AI 思考 ({thinking.length} 步)
                </summary>
                <div className="mt-1">
                  <ThinkingSteps steps={thinking} />
                </div>
              </details>
            )}

            {/* 主文本 */}
            <div className="space-y-1">
              <label className="flex items-center justify-between text-xs font-semibold text-muted-foreground">
                <span>主标题</span>
                <span className="font-mono text-xs">{spec.main_text.length}/24</span>
              </label>
              <input
                type="text"
                value={spec.main_text}
                onChange={(e) => updateSpec('main_text', e.target.value.slice(0, 24))}
                className="w-full rounded-md border border-border bg-background px-2 py-1.5 text-sm outline-none focus:border-primary"
              />
            </div>

            {/* 副文本 */}
            <div className="space-y-1">
              <label className="flex items-center justify-between text-xs font-semibold text-muted-foreground">
                <span>副标题（可空）</span>
                <span className="font-mono text-xs">{spec.sub_text.length}/40</span>
              </label>
              <input
                type="text"
                value={spec.sub_text}
                onChange={(e) => updateSpec('sub_text', e.target.value.slice(0, 40))}
                className="w-full rounded-md border border-border bg-background px-2 py-1.5 text-xs outline-none focus:border-primary"
              />
            </div>

            {/* font_family */}
            <div className="space-y-1">
              <label className="text-xs font-semibold text-muted-foreground">字体族</label>
              <div className="grid grid-cols-4 gap-1">
                {FONT_OPTIONS.map((opt) => (
                  <button
                    key={opt.value}
                    type="button"
                    onClick={() => updateSpec('font_family', opt.value)}
                    className={cn(
                      'rounded-md border px-1.5 py-1 text-left text-xs transition',
                      spec.font_family === opt.value
                        ? 'border-primary bg-primary/10 text-foreground'
                        : 'border-border bg-background/60 text-muted-foreground hover:border-primary/60',
                    )}
                  >
                    <div className="font-semibold">{opt.label}</div>
                    <div className="text-xs text-muted-foreground">{opt.hint}</div>
                  </button>
                ))}
              </div>
            </div>

            {/* layout */}
            <div className="space-y-1">
              <label className="text-xs font-semibold text-muted-foreground">版式</label>
              <div className="grid grid-cols-4 gap-1">
                {LAYOUT_OPTIONS.map((opt) => (
                  <button
                    key={opt.value}
                    type="button"
                    onClick={() => updateSpec('layout', opt.value)}
                    className={cn(
                      'rounded-md border px-1.5 py-1 text-left text-xs transition',
                      spec.layout === opt.value
                        ? 'border-primary bg-primary/10 text-foreground'
                        : 'border-border bg-background/60 text-muted-foreground hover:border-primary/60',
                    )}
                  >
                    <div className="font-semibold">{opt.label}</div>
                    <div className="text-xs text-muted-foreground">{opt.hint}</div>
                  </button>
                ))}
              </div>
            </div>

            {/* bg_mode */}
            <div className="space-y-1">
              <label className="text-xs font-semibold text-muted-foreground">背景模式</label>
              <div className="grid grid-cols-4 gap-1">
                {BG_MODE_OPTIONS.map((opt) => (
                  <button
                    key={opt.value}
                    type="button"
                    onClick={() => updateSpec('bg_mode', opt.value)}
                    className={cn(
                      'rounded-md border px-1.5 py-1 text-left text-xs transition',
                      spec.bg_mode === opt.value
                        ? 'border-primary bg-primary/10 text-foreground'
                        : 'border-border bg-background/60 text-muted-foreground hover:border-primary/60',
                    )}
                  >
                    <div className="font-semibold">{opt.label}</div>
                    <div className="text-xs text-muted-foreground">{opt.hint}</div>
                  </button>
                ))}
              </div>
            </div>

            {/* 颜色三件套 */}
            <div className="grid grid-cols-3 gap-2">
              <div className="space-y-1">
                <label className="text-xs font-semibold text-muted-foreground">背景</label>
                <div className="flex items-center gap-1">
                  <input
                    type="color"
                    value={spec.bg_color}
                    onChange={(e) => updateSpec('bg_color', e.target.value.toUpperCase())}
                    className="h-7 w-7 cursor-pointer rounded border border-border bg-transparent p-0"
                  />
                  <input
                    type="text"
                    value={spec.bg_color}
                    onChange={(e) => {
                      const v = e.target.value
                      if (/^#[0-9A-Fa-f]{0,6}$/.test(v)) updateSpec('bg_color', v.toUpperCase())
                    }}
                    className="w-full rounded-md border border-border bg-background px-1.5 py-1 text-xs font-mono outline-none focus:border-primary"
                  />
                </div>
              </div>
              <div className="space-y-1">
                <label className="text-xs font-semibold text-muted-foreground">文本</label>
                <div className="flex items-center gap-1">
                  <input
                    type="color"
                    value={spec.text_color}
                    onChange={(e) => updateSpec('text_color', e.target.value.toUpperCase())}
                    className="h-7 w-7 cursor-pointer rounded border border-border bg-transparent p-0"
                  />
                  <input
                    type="text"
                    value={spec.text_color}
                    onChange={(e) => {
                      const v = e.target.value
                      if (/^#[0-9A-Fa-f]{0,6}$/.test(v)) updateSpec('text_color', v.toUpperCase())
                    }}
                    className="w-full rounded-md border border-border bg-background px-1.5 py-1 text-xs font-mono outline-none focus:border-primary"
                  />
                </div>
              </div>
              <div className="space-y-1">
                <label className="text-xs font-semibold text-muted-foreground">强调</label>
                <div className="flex items-center gap-1">
                  <input
                    type="color"
                    value={spec.accent_color}
                    onChange={(e) => updateSpec('accent_color', e.target.value.toUpperCase())}
                    className="h-7 w-7 cursor-pointer rounded border border-border bg-transparent p-0"
                  />
                  <input
                    type="text"
                    value={spec.accent_color}
                    onChange={(e) => {
                      const v = e.target.value
                      if (/^#[0-9A-Fa-f]{0,6}$/.test(v)) updateSpec('accent_color', v.toUpperCase())
                    }}
                    className="w-full rounded-md border border-border bg-background px-1.5 py-1 text-xs font-mono outline-none focus:border-primary"
                  />
                </div>
              </div>
            </div>

            {/* animation */}
            <div className="space-y-1">
              <label className="text-xs font-semibold text-muted-foreground">入场动画</label>
              <div className="grid grid-cols-4 gap-1">
                {ANIM_OPTIONS.map((opt) => (
                  <button
                    key={opt.value}
                    type="button"
                    onClick={() => updateSpec('animation', opt.value)}
                    className={cn(
                      'rounded-md border px-1.5 py-1 text-left text-xs transition',
                      spec.animation === opt.value
                        ? 'border-primary bg-primary/10 text-foreground'
                        : 'border-border bg-background/60 text-muted-foreground hover:border-primary/60',
                    )}
                  >
                    <div className="font-semibold">{opt.label}</div>
                    <div className="text-xs text-muted-foreground">{opt.hint}</div>
                  </button>
                ))}
              </div>
            </div>

            {/* emoji */}
            <div className="space-y-1">
              <label className="flex items-center justify-between text-xs font-semibold text-muted-foreground">
                <span>Emoji 点缀（≤3）</span>
                <span className="font-mono text-xs">{spec.emoji_decor.length}/3</span>
              </label>
              <div className="flex flex-wrap gap-1">
                {EMOJI_POOL.map((e) => (
                  <button
                    key={e}
                    type="button"
                    onClick={() => toggleEmoji(e)}
                    className={cn(
                      'rounded-md border px-2 py-0.5 text-base leading-none transition',
                      spec.emoji_decor.includes(e)
                        ? 'border-primary bg-primary/10'
                        : 'border-border bg-background/60 hover:border-primary/60',
                    )}
                  >
                    {e}
                  </button>
                ))}
              </div>
            </div>

            {/* duration */}
            <div className="space-y-1">
              <label className="flex items-center justify-between text-xs font-semibold text-muted-foreground">
                <span>播放时长</span>
                <span className="font-mono">{spec.duration_seconds.toFixed(1)} s</span>
              </label>
              <input
                type="range"
                min={1.5}
                max={15}
                step={0.5}
                value={spec.duration_seconds}
                onChange={(e) => updateSpec('duration_seconds', Number(e.target.value))}
                className="w-full"
              />
            </div>

            {/* keywords hint（只读提示） */}
            {globalKeywords.length > 0 && outline?.must_include_keywords && outline.must_include_keywords.length > 0 && (
              <p className="text-xs text-muted-foreground">
                💡 AI 推荐承载关键词：{outline.must_include_keywords.join(' / ')}（已带入下次生成）
              </p>
            )}

            {genError && <p className="text-xs text-destructive">{genError}</p>}

            <div className="flex items-center justify-end gap-2">
              <button
                onClick={() => setPhase('idle')}
                className="rounded-md border border-border bg-background px-3 py-1 text-xs hover:bg-secondary"
              >
                重新分析
              </button>
              <button
                onClick={generateFromSpec}
                disabled={spec.main_text.trim().length === 0}
                className={cn(
                  'rounded-md bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground hover:bg-primary/90',
                  spec.main_text.trim().length === 0 && 'cursor-not-allowed opacity-60',
                )}
              >
                烧成字卡 →
              </button>
            </div>
          </div>

          {/* 右侧实时预览 */}
          <div className="space-y-1">
            <p className="text-xs font-semibold text-muted-foreground">实时预览</p>
            <CardPreview spec={spec} />
          </div>
        </div>
      )}

      {phase === 'generating' && (
        <div className="space-y-1 rounded-md bg-secondary/30 p-2">
          <div className="flex items-center gap-2">
            <span className="relative flex h-2 w-2">
              <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-primary/60" />
              <span className="relative inline-flex h-2 w-2 rounded-full bg-primary" />
            </span>
            <span className="text-xs font-medium">AI 正在把字卡烧制成视频片段…</span>
          </div>
          <p className="text-xs text-muted-foreground">通常 2-5 秒</p>
        </div>
      )}

      {phase === 'result' && fill && fill.action === 'copy' && fill.text_card_spec && (
        <div className="grid gap-3 lg:grid-cols-[1fr_140px]">
          <div className="space-y-2 text-xs">
            <p className="text-muted-foreground">已生成字卡画面规格：</p>
            <ul className="space-y-1 rounded-md border border-border bg-background/30 px-3 py-2 font-mono text-xs">
              <li>主：{fill.text_card_spec.main_text || <em className="text-muted-foreground">空</em>}</li>
              <li>副：{fill.text_card_spec.sub_text || <em className="text-muted-foreground">空</em>}</li>
              <li>字体 {fill.text_card_spec.font_family} · 版式 {fill.text_card_spec.layout} · 动画 {fill.text_card_spec.animation}</li>
              <li>背景 {fill.text_card_spec.bg_mode} {fill.text_card_spec.bg_color} / 文本 {fill.text_card_spec.text_color} / 强调 {fill.text_card_spec.accent_color}</li>
              <li>时长 {fill.text_card_spec.duration_seconds.toFixed(1)}s · Emoji {fill.text_card_spec.emoji_decor.join(' ') || '—'}</li>
            </ul>
            {fill.note && <p className="text-muted-foreground">{fill.note}</p>}
            <div className="flex items-center justify-end gap-2">
              <button
                onClick={() => setPhase('outline')}
                className="rounded-md border border-border bg-background px-3 py-1 text-xs hover:bg-secondary"
              >
                微调重新生成
              </button>
              <button
                onClick={() => setPhase('idle')}
                className="text-xs text-muted-foreground underline-offset-2 hover:underline"
              >
                重新分析
              </button>
            </div>
          </div>
          <div className="space-y-1">
            <p className="text-xs font-semibold text-muted-foreground">画面预览</p>
            <CardPreview spec={fill.text_card_spec} />
          </div>
        </div>
      )}
    </div>
  )
}
