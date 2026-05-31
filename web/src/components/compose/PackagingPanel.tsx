import { useState } from 'react'

import { api } from '@/api/client'
import { SECTION_BG, SECTION_SHORT } from '@/lib/sections'
import { TRANSITION_LABEL, TRANSITION_TONE } from '@/lib/transitions'
import { cn } from '@/lib/utils'
import {
  DEFAULT_PACKAGING_PREFERENCES,
  type PackagingPreferences,
  type PackagingPreset,
  type PackagingRecommendation,
  type PackagingRecommendRequest,
  type Plan,
  type SubtitleBackground,
  type SubtitleFontSize,
  type SubtitlePosition,
  type TransitionStyle,
} from '@/types/schemas'

/**
 * 包装推荐面板：调 POST /api/packaging/recommend，apply=true 把转场+封面写到 plan.packaging_track。
 * 推荐成功后回调 onPlanUpdated 让父级重拉 plan，storyboard 同步。
 *
 * 用户可配置（v3.5）：
 * - 4 个风格预设卡（极简/活力/信息流/对话）：点击即按预设字段配置发起推荐
 * - 自定义折叠面板：6 种转场风格 checkbox + 字幕字号/位置/底色/双语 + 封面策略/时长 + LLM 温度
 * - 用户在 UI 上动了任何具体字段会自动切回 custom 预设
 *
 * prefs 入口优先级：本地 state > plan.settings.packaging_prefs > DEFAULT_PACKAGING_PREFERENCES
 */
export function PackagingPanel({
  plan,
  onPlanUpdated,
}: {
  plan: Plan
  onPlanUpdated?: (plan: Plan) => void
}) {
  const [running, setRunning] = useState(false)
  const [rec, setRec] = useState<PackagingRecommendation | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [prefs, setPrefs] = useState<PackagingPreferences>(
    () => plan.settings?.packaging_prefs ?? DEFAULT_PACKAGING_PREFERENCES,
  )
  const [expanded, setExpanded] = useState(false)

  const updatePref = <K extends keyof PackagingPreferences>(
    key: K,
    value: PackagingPreferences[K],
  ) => {
    // 动了任何具体字段就把 preset 切回 custom（除非用户正在点预设卡）
    setPrefs((prev) => ({ ...prev, [key]: value, preset: 'custom' as PackagingPreset }))
  }

  const pickPreset = (preset: PackagingPreset) => {
    // 预设只改 preset 字段；具体字段由后端 expand_preset 在 server 端展开。
    // 前端保留 preset，提交时把这份 prefs 传过去，后端按预设展开后落盘到 plan.settings。
    setPrefs((prev) => ({ ...prev, preset }))
  }

  const run = async (overridePreset?: PackagingPreset) => {
    setRunning(true)
    setError(null)
    const submitPrefs: PackagingPreferences = overridePreset
      ? { ...prefs, preset: overridePreset }
      : prefs
    try {
      const body: PackagingRecommendRequest = {
        plan_id: plan.plan_id,
        apply: true,
        preferences: submitPrefs,
      }
      const resp = await api.post<PackagingRecommendation>('/packaging/recommend', body)
      setRec(resp)
      try {
        const fresh = await api.get<Plan>(`/plan/${plan.plan_id}`)
        // 服务器侧已展开预设并写回 plan.settings.packaging_prefs；前端 state 跟着同步
        if (fresh.settings?.packaging_prefs) {
          setPrefs(fresh.settings.packaging_prefs)
        }
        onPlanUpdated?.(fresh)
      } catch {
        /* 拉新版失败不阻塞展示 */
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : '包装推荐失败')
    } finally {
      setRunning(false)
    }
  }

  return (
    <section className="space-y-3 rounded-lg border border-border bg-card p-4">
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-sm font-semibold">包装推荐 · 转场 + 封面</h2>
          <p className="text-[11px] text-muted-foreground">
            LLM 看完主轨之后，给每段切换挑一种转场，再写一份开场封面，自动落到 packaging_track。
          </p>
        </div>
        <button
          onClick={() => void run()}
          disabled={running}
          className={cn(
            'rounded-md bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground transition-colors',
            running && 'cursor-not-allowed opacity-60',
          )}
        >
          {running ? '推荐中…' : rec ? '重新推荐' : '一键包装'}
        </button>
      </div>

      <PresetCards
        active={prefs.preset}
        running={running}
        onPick={(p) => {
          pickPreset(p)
          void run(p)
        }}
      />

      <button
        type="button"
        onClick={() => setExpanded((x) => !x)}
        className="flex w-full items-center justify-between rounded-md border border-dashed border-border bg-background/30 px-3 py-1.5 text-[11px] text-muted-foreground hover:bg-background/50"
      >
        <span>自定义高级设置（{expanded ? '收起' : '展开'}）</span>
        <span>{expanded ? '▴' : '▾'}</span>
      </button>

      {expanded && (
        <CustomPanel prefs={prefs} updatePref={updatePref} />
      )}

      {error && (
        <div className="rounded-md border border-destructive/40 bg-destructive/5 px-3 py-2 text-xs text-destructive">
          {error}
        </div>
      )}

      {rec && (
        <div className="space-y-3">
          {rec.cover && <CoverPreview cover={rec.cover} />}
          {rec.transitions.length > 0 && (
            <div className="space-y-1.5">
              <h3 className="text-xs font-semibold text-muted-foreground">段落转场（{rec.transitions.length}）</h3>
              <ul className="space-y-1.5">
                {rec.transitions.map((t) => (
                  <li
                    key={t.item_id}
                    className="flex flex-wrap items-center gap-2 rounded-md border border-border bg-background/40 px-2.5 py-1.5 text-xs"
                  >
                    <span className="font-mono text-[11px] text-muted-foreground">
                      {t.at_seconds.toFixed(1)}s
                    </span>
                    <span
                      className={cn(
                        'rounded px-1.5 py-0.5 text-[10px] font-medium text-white',
                        SECTION_BG[t.from_section],
                      )}
                    >
                      {SECTION_SHORT[t.from_section]}
                    </span>
                    <span className="text-muted-foreground">→</span>
                    <span
                      className={cn(
                        'rounded px-1.5 py-0.5 text-[10px] font-medium text-white',
                        SECTION_BG[t.to_section],
                      )}
                    >
                      {SECTION_SHORT[t.to_section]}
                    </span>
                    <span
                      className={cn(
                        'rounded px-1.5 py-0.5 text-[10px] font-medium',
                        TRANSITION_TONE[t.style],
                      )}
                      title={t.style}
                    >
                      {TRANSITION_LABEL[t.style]} · {t.duration.toFixed(1)}s
                    </span>
                    <span className="min-w-0 flex-1 truncate text-muted-foreground" title={t.reason}>
                      {t.reason}
                    </span>
                  </li>
                ))}
              </ul>
            </div>
          )}
          {rec.notes.length > 0 && (
            <ul className="space-y-0.5 text-[10px] text-muted-foreground">
              {rec.notes.map((n, i) => (
                <li key={i}>· {n}</li>
              ))}
            </ul>
          )}
        </div>
      )}

      {!rec && !running && !error && (
        <p className="rounded-md border border-dashed border-border bg-background/30 px-3 py-2 text-[11px] text-muted-foreground">
          点击右上方按钮：LLM 会基于当前 plan 的段落顺序与你的主题，写一份转场表 + 一份封面方案。
        </p>
      )}
    </section>
  )
}

// ---------------------------------------------------------------------------
// 风格预设卡片
// ---------------------------------------------------------------------------

const PRESET_META: Record<
  Exclude<PackagingPreset, 'custom'>,
  { label: string; tagline: string; tone: string }
> = {
  minimalist: {
    label: '极简',
    tagline: '硬切+溶解，底部小字幕，无底色',
    tone: 'border-slate-300 hover:bg-slate-100',
  },
  energetic: {
    label: '活力',
    tagline: '6 种转场全开，大字幕带阴影，封面 1.5s',
    tone: 'border-rose-300 hover:bg-rose-50',
  },
  info_feed: {
    label: '信息流',
    tagline: '溶解+滑动+扫切，顶部渐变底字幕',
    tone: 'border-emerald-300 hover:bg-emerald-50',
  },
  dialogue: {
    label: '对话',
    tagline: '硬切+溶解为主，大字幕，双语开启',
    tone: 'border-sky-300 hover:bg-sky-50',
  },
}

function PresetCards({
  active,
  running,
  onPick,
}: {
  active: PackagingPreset
  running: boolean
  onPick: (p: PackagingPreset) => void
}) {
  return (
    <div className="grid grid-cols-2 gap-2 md:grid-cols-4">
      {(Object.keys(PRESET_META) as Array<Exclude<PackagingPreset, 'custom'>>).map((key) => {
        const meta = PRESET_META[key]
        const isActive = active === key
        return (
          <button
            key={key}
            type="button"
            disabled={running}
            onClick={() => onPick(key)}
            className={cn(
              'flex flex-col items-start gap-1 rounded-md border bg-background/40 p-2 text-left transition-colors',
              meta.tone,
              isActive && 'ring-2 ring-primary',
              running && 'cursor-not-allowed opacity-60',
            )}
          >
            <span className="text-xs font-semibold">{meta.label}</span>
            <span className="text-[10px] leading-tight text-muted-foreground">{meta.tagline}</span>
          </button>
        )
      })}
    </div>
  )
}

// ---------------------------------------------------------------------------
// 自定义折叠面板
// ---------------------------------------------------------------------------

const ALL_TRANSITIONS: TransitionStyle[] = ['hard_cut', 'dissolve', 'slide', 'zoom', 'whip', 'wipe']
const FONT_SIZES: SubtitleFontSize[] = ['small', 'medium', 'large']
const POSITIONS: SubtitlePosition[] = ['top', 'middle', 'bottom']
const BACKGROUNDS: SubtitleBackground[] = ['none', 'shadow', 'gradient']

const FONT_LABEL: Record<SubtitleFontSize, string> = { small: '小', medium: '中', large: '大' }
const POSITION_LABEL: Record<SubtitlePosition, string> = { top: '顶部', middle: '居中', bottom: '底部' }
const BG_LABEL: Record<SubtitleBackground, string> = { none: '无底', shadow: '阴影', gradient: '渐变' }

function CustomPanel({
  prefs,
  updatePref,
}: {
  prefs: PackagingPreferences
  updatePref: <K extends keyof PackagingPreferences>(key: K, value: PackagingPreferences[K]) => void
}) {
  const toggleTransition = (style: TransitionStyle) => {
    const set = new Set(prefs.allowed_transition_styles)
    if (set.has(style)) {
      // 至少保留 1 个
      if (set.size > 1) set.delete(style)
    } else {
      set.add(style)
    }
    updatePref('allowed_transition_styles', ALL_TRANSITIONS.filter((s) => set.has(s)))
  }

  return (
    <div className="space-y-3 rounded-md border border-border bg-background/20 p-3">
      <div>
        <h4 className="mb-1.5 text-[11px] font-semibold text-muted-foreground">转场风格白名单</h4>
        <div className="flex flex-wrap gap-1.5">
          {ALL_TRANSITIONS.map((style) => {
            const checked = prefs.allowed_transition_styles.includes(style)
            return (
              <button
                key={style}
                type="button"
                onClick={() => toggleTransition(style)}
                className={cn(
                  'rounded px-2 py-0.5 text-[10px] font-medium transition-colors',
                  checked ? TRANSITION_TONE[style] : 'bg-muted text-muted-foreground hover:bg-muted/70',
                )}
              >
                {TRANSITION_LABEL[style]}
              </button>
            )
          })}
        </div>
        <label className="mt-2 flex items-center gap-2 text-[11px] text-muted-foreground">
          <span>最长转场时长</span>
          <input
            type="range"
            min={0.2}
            max={1.5}
            step={0.1}
            value={prefs.max_transition_duration}
            onChange={(e) => updatePref('max_transition_duration', parseFloat(e.target.value))}
            className="flex-1"
          />
          <span className="font-mono">{prefs.max_transition_duration.toFixed(1)}s</span>
        </label>
      </div>

      <div className="grid grid-cols-1 gap-3 md:grid-cols-3">
        <RadioGroup
          label="字幕字号"
          value={prefs.subtitle_font_size}
          options={FONT_SIZES}
          renderLabel={(v) => FONT_LABEL[v]}
          onChange={(v) => updatePref('subtitle_font_size', v)}
        />
        <RadioGroup
          label="字幕位置"
          value={prefs.subtitle_position}
          options={POSITIONS}
          renderLabel={(v) => POSITION_LABEL[v]}
          onChange={(v) => updatePref('subtitle_position', v)}
        />
        <RadioGroup
          label="字幕底色"
          value={prefs.subtitle_background}
          options={BACKGROUNDS}
          renderLabel={(v) => BG_LABEL[v]}
          onChange={(v) => updatePref('subtitle_background', v)}
        />
      </div>

      <label className="flex items-center gap-2 text-[11px] text-muted-foreground">
        <input
          type="checkbox"
          checked={prefs.subtitle_bilingual}
          onChange={(e) => updatePref('subtitle_bilingual', e.target.checked)}
        />
        <span>双语字幕（中文上 + 英文下；LLM 翻译可能轻微影响推荐耗时）</span>
      </label>

      <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
        <div>
          <h4 className="mb-1.5 text-[11px] font-semibold text-muted-foreground">封面文字来源</h4>
          <div className="flex gap-1.5">
            {(['auto', 'video_goal', 'custom'] as const).map((src) => (
              <button
                key={src}
                type="button"
                onClick={() => updatePref('cover_text_source', src)}
                className={cn(
                  'rounded px-2 py-0.5 text-[10px] transition-colors',
                  prefs.cover_text_source === src
                    ? 'bg-primary text-primary-foreground'
                    : 'bg-muted text-muted-foreground hover:bg-muted/70',
                )}
              >
                {src === 'auto' ? 'LLM 自动' : src === 'video_goal' ? 'video_goal' : '自定义'}
              </button>
            ))}
          </div>
          {prefs.cover_text_source === 'custom' && (
            <input
              type="text"
              maxLength={20}
              value={prefs.cover_custom_text ?? ''}
              onChange={(e) => updatePref('cover_custom_text', e.target.value)}
              placeholder="≤20 字，渲染时截到 12 字"
              className="mt-1.5 w-full rounded border border-border bg-background px-2 py-1 text-[11px]"
            />
          )}
        </div>
        <label className="flex items-center gap-2 text-[11px] text-muted-foreground">
          <span>封面停留</span>
          <input
            type="range"
            min={0.6}
            max={2.0}
            step={0.1}
            value={prefs.cover_duration}
            onChange={(e) => updatePref('cover_duration', parseFloat(e.target.value))}
            className="flex-1"
          />
          <span className="font-mono">{prefs.cover_duration.toFixed(1)}s</span>
        </label>
      </div>

      <label className="flex items-center gap-2 text-[11px] text-muted-foreground">
        <input
          type="checkbox"
          checked={prefs.cover_with_subtitle}
          onChange={(e) => updatePref('cover_with_subtitle', e.target.checked)}
        />
        <span>封面同时显示副标题（关掉则只渲主标题）</span>
      </label>

      <label className="flex items-center gap-2 text-[11px] text-muted-foreground">
        <span>LLM 创造度</span>
        <input
          type="range"
          min={0.3}
          max={0.9}
          step={0.05}
          value={prefs.llm_temperature}
          onChange={(e) => updatePref('llm_temperature', parseFloat(e.target.value))}
          className="flex-1"
        />
        <span className="font-mono">{prefs.llm_temperature.toFixed(2)}</span>
      </label>
    </div>
  )
}

function RadioGroup<T extends string>({
  label,
  value,
  options,
  renderLabel,
  onChange,
}: {
  label: string
  value: T
  options: readonly T[]
  renderLabel: (v: T) => string
  onChange: (v: T) => void
}) {
  return (
    <div>
      <h4 className="mb-1.5 text-[11px] font-semibold text-muted-foreground">{label}</h4>
      <div className="flex gap-1.5">
        {options.map((opt) => (
          <button
            key={opt}
            type="button"
            onClick={() => onChange(opt)}
            className={cn(
              'flex-1 rounded px-2 py-0.5 text-[10px] transition-colors',
              value === opt
                ? 'bg-primary text-primary-foreground'
                : 'bg-muted text-muted-foreground hover:bg-muted/70',
            )}
          >
            {renderLabel(opt)}
          </button>
        ))}
      </div>
    </div>
  )
}

function CoverPreview({ cover }: { cover: NonNullable<PackagingRecommendation['cover']> }) {
  const bg = cover.palette[1] ?? '#1F2937'
  const accent = cover.palette[0] ?? '#FFE600'
  const sub = cover.palette[2] ?? '#FFFFFF'
  const isLeft = cover.layout === 'left' || cover.layout === 'stacked'

  return (
    <div className="space-y-1.5">
      <h3 className="text-xs font-semibold text-muted-foreground">封面方案</h3>
      <div className="flex items-stretch gap-3">
        <div
          className="relative h-32 w-56 shrink-0 overflow-hidden rounded-md border border-border"
          style={{
            backgroundColor: bg,
            display: 'flex',
            flexDirection: 'column',
            justifyContent: 'center',
            alignItems: isLeft ? 'flex-start' : 'center',
            padding: '0 14px',
          }}
        >
          {cover.layout === 'split' && (
            <div
              className="absolute right-0 top-0 bottom-0 w-2/5"
              style={{ backgroundColor: accent }}
            />
          )}
          <div
            style={{
              color: cover.layout === 'split' ? sub : accent,
              fontSize: 22,
              fontWeight: 900,
              lineHeight: 1.1,
              textAlign: isLeft ? 'left' : 'center',
              zIndex: 2,
            }}
          >
            {cover.title}
          </div>
          {cover.subtitle && (
            <div
              style={{
                color: sub,
                fontSize: 11,
                fontWeight: 500,
                marginTop: 6,
                opacity: 0.85,
                zIndex: 2,
              }}
            >
              {cover.subtitle}
            </div>
          )}
        </div>
        <div className="flex flex-1 flex-col gap-1 text-xs">
          <div className="flex items-center gap-1.5">
            <span className="text-muted-foreground">布局</span>
            <span className="rounded bg-secondary px-1.5 py-0.5 text-[10px] font-medium">
              {cover.layout}
            </span>
          </div>
          <div className="flex items-center gap-1.5">
            <span className="text-muted-foreground">主色</span>
            {cover.palette.map((c) => (
              <span
                key={c}
                className="inline-block h-4 w-8 rounded border border-border"
                style={{ backgroundColor: c }}
                title={c}
              />
            ))}
          </div>
          <p className="text-[11px] text-muted-foreground">{cover.style_note}</p>
        </div>
      </div>
    </div>
  )
}
