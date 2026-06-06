import { useEffect, useState } from 'react'

import { api } from '@/api/client'
import { SECTION_BG, SECTION_SHORT } from '@/lib/sections'
import { TRANSITION_LABEL, TRANSITION_TONE } from '@/lib/transitions'
import { cn } from '@/lib/utils'
import {
  DEFAULT_PACKAGING_PREFERENCES,
  type CoverCandidate,
  type PackagingPreferences,
  type PackagingPreset,
  type PackagingRecommendationV2,
  type PackagingRecommendRequest,
  type PackagingSelection,
  type Plan,
  type StickerCandidate,
  type SubtitleBackground,
  type SubtitleFontSize,
  type SubtitlePosition,
  type SubtitleStyleCandidate,
  type TitleBarCandidate,
  type TransitionCandidateBundle,
  type TransitionStyle,
} from '@/types/schemas'

/**
 * 包装推荐面板 V2：5 个独立维度多候选。
 * 1. 点「智能推荐」 → POST /packaging/recommend 拿 V2 candidates。
 * 2. 用户在 5 个维度独立挑选（字幕单选 / title_bars 多选 / stickers 多选 /
 *    每个 transition bundle 内单选 / 封面单选）。
 * 3. 点「应用所选」 → POST /packaging/apply 把 selection 写到 plan.packaging_track
 *    + Scene.transition_in，然后 onPlanUpdated 让父级刷新。
 */
export function PackagingPanel({
  plan,
  onPlanUpdated,
}: {
  plan: Plan
  onPlanUpdated?: (plan: Plan) => void
}) {
  const [running, setRunning] = useState(false)
  const [applying, setApplying] = useState(false)
  const [rec, setRec] = useState<PackagingRecommendationV2 | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [prefs, setPrefs] = useState<PackagingPreferences>(
    () => plan.settings?.packaging_prefs ?? DEFAULT_PACKAGING_PREFERENCES,
  )
  const [expanded, setExpanded] = useState(false)

  // 5 维度选择 state
  const [subId, setSubId] = useState<string | null>(null)
  const [tbIds, setTbIds] = useState<Set<string>>(new Set())
  const [stIds, setStIds] = useState<Set<string>>(new Set())
  const [trSel, setTrSel] = useState<Record<string, TransitionStyle>>({})
  const [coverId, setCoverId] = useState<string | null>(null)

  // rec 变更时初始化默认选择（首个候选）
  useEffect(() => {
    if (!rec) return
    setSubId(rec.subtitle_styles[0]?.candidate_id ?? null)
    setTbIds(new Set(rec.title_bars.slice(0, 1).map((c) => c.candidate_id)))
    setStIds(new Set(rec.stickers.slice(0, 1).map((c) => c.candidate_id)))
    const initialTr: Record<string, TransitionStyle> = {}
    for (const b of rec.transition_bundles) {
      if (b.options[0]) initialTr[b.candidate_id] = b.options[0].style
    }
    setTrSel(initialTr)
    setCoverId(rec.covers[0]?.candidate_id ?? null)
  }, [rec])

  const updatePref = <K extends keyof PackagingPreferences>(
    key: K,
    value: PackagingPreferences[K],
  ) => {
    setPrefs((prev) => ({ ...prev, [key]: value, preset: 'custom' as PackagingPreset }))
  }

  const pickPreset = (preset: PackagingPreset) => {
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
        preferences: submitPrefs,
      }
      const resp = await api.post<PackagingRecommendationV2>('/packaging/recommend', body)
      setRec(resp)
      try {
        const fresh = await api.get<Plan>(`/plan/${plan.plan_id}`)
        if (fresh.settings?.packaging_prefs) {
          setPrefs(fresh.settings.packaging_prefs)
        }
      } catch {
        /* ignore */
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : '包装推荐失败')
    } finally {
      setRunning(false)
    }
  }

  const apply = async () => {
    if (!rec) return
    setApplying(true)
    setError(null)
    try {
      const payload: PackagingSelection = {
        plan_id: plan.plan_id,
        subtitle_style_id: subId,
        title_bar_ids: Array.from(tbIds),
        sticker_ids: Array.from(stIds),
        transition_selections: trSel,
        cover_id: coverId,
        recommendation: rec,
      }
      const fresh = await api.post<Plan>('/packaging/apply', payload)
      onPlanUpdated?.(fresh)
    } catch (err) {
      setError(err instanceof Error ? err.message : '应用包装失败')
    } finally {
      setApplying(false)
    }
  }

  return (
    <section className="space-y-3 rounded-lg border border-border bg-card p-4">
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-sm font-semibold">智能包装 · 5 维度多候选</h2>
          <p className="text-[11px] text-muted-foreground">
            AI 一次给出字幕样式 / 标题条 / 贴纸 / 段落转场 / 封面 5 类候选，自己挑、自己组装。
          </p>
        </div>
        <div className="flex gap-2">
          <button
            onClick={() => void run()}
            disabled={running}
            className={cn(
              'rounded-md border border-primary px-3 py-1.5 text-xs font-medium text-primary transition-colors',
              running && 'cursor-not-allowed opacity-60',
              !running && 'hover:bg-primary/10',
            )}
          >
            {running ? '推荐中…' : rec ? '重新推荐' : '智能推荐'}
          </button>
          <button
            onClick={() => void apply()}
            disabled={!rec || applying}
            className={cn(
              'rounded-md bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground transition-colors',
              (!rec || applying) && 'cursor-not-allowed opacity-60',
            )}
          >
            {applying ? '应用中…' : '应用所选'}
          </button>
        </div>
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

      {expanded && <CustomPanel prefs={prefs} updatePref={updatePref} />}

      {error && (
        <div className="rounded-md border border-destructive/40 bg-destructive/5 px-3 py-2 text-xs text-destructive">
          {error}
        </div>
      )}

      {rec && (
        <div className="space-y-3">
          <DimSection title="字幕样式" subtitle="单选 · 决定所有口播字幕的统一样式">
            <div className="grid grid-cols-1 gap-2 md:grid-cols-3">
              {rec.subtitle_styles.map((c) => (
                <SubtitleCard
                  key={c.candidate_id}
                  c={c}
                  selected={subId === c.candidate_id}
                  onPick={() => setSubId(c.candidate_id)}
                />
              ))}
            </div>
          </DimSection>

          <DimSection title="标题条 / 卖点卡片" subtitle="可多选 · 烧到对应段落的指定时长">
            <div className="grid grid-cols-1 gap-2 md:grid-cols-2">
              {rec.title_bars.map((c) => (
                <TitleBarCard
                  key={c.candidate_id}
                  c={c}
                  plan={plan}
                  selected={tbIds.has(c.candidate_id)}
                  onToggle={() => {
                    const next = new Set(tbIds)
                    if (next.has(c.candidate_id)) next.delete(c.candidate_id)
                    else next.add(c.candidate_id)
                    setTbIds(next)
                  }}
                />
              ))}
              {rec.title_bars.length === 0 && (
                <p className="text-[11px] text-muted-foreground">无候选</p>
              )}
            </div>
          </DimSection>

          <DimSection title="贴纸 / CTA 强调" subtitle="可多选 · 通常 closing 给 1 条收尾">
            <div className="grid grid-cols-1 gap-2 md:grid-cols-2">
              {rec.stickers.map((c) => (
                <StickerCard
                  key={c.candidate_id}
                  c={c}
                  plan={plan}
                  selected={stIds.has(c.candidate_id)}
                  onToggle={() => {
                    const next = new Set(stIds)
                    if (next.has(c.candidate_id)) next.delete(c.candidate_id)
                    else next.add(c.candidate_id)
                    setStIds(next)
                  }}
                />
              ))}
              {rec.stickers.length === 0 && (
                <p className="text-[11px] text-muted-foreground">无候选</p>
              )}
            </div>
          </DimSection>

          <DimSection title="段落转场" subtitle="每个切换点单选风格（或留空不切换）">
            <div className="space-y-2">
              {rec.transition_bundles.map((b) => (
                <TransitionBundleRow
                  key={b.candidate_id}
                  b={b}
                  picked={trSel[b.candidate_id] ?? null}
                  onPick={(style) => {
                    setTrSel((prev) => {
                      const next = { ...prev }
                      if (style === null) delete next[b.candidate_id]
                      else next[b.candidate_id] = style
                      return next
                    })
                  }}
                />
              ))}
              {rec.transition_bundles.length === 0 && (
                <p className="text-[11px] text-muted-foreground">无候选</p>
              )}
            </div>
          </DimSection>

          <DimSection title="封面方案" subtitle="单选 · 决定开场 1-1.5s 的视觉冲击">
            <div className="grid grid-cols-1 gap-2 md:grid-cols-2 lg:grid-cols-3">
              {rec.covers.map((c) => (
                <CoverCard
                  key={c.candidate_id}
                  c={c}
                  selected={coverId === c.candidate_id}
                  onPick={() => setCoverId(c.candidate_id)}
                />
              ))}
            </div>
          </DimSection>

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
          点右上角「智能推荐」：AI 会按你当前的镜头顺序和主题，一次给 5 类候选。
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
    tagline: '硬切+溶解，小字幕，无底色',
    tone: 'border-slate-300 hover:bg-slate-100',
  },
  energetic: {
    label: '活力',
    tagline: '6 种切换全开，大字幕带阴影，封面停 1.5 秒',
    tone: 'border-rose-300 hover:bg-rose-50',
  },
  info_feed: {
    label: '信息流',
    tagline: '溶解+滑动+扫切，顶部渐变底色字幕',
    tone: 'border-emerald-300 hover:bg-emerald-50',
  },
  dialogue: {
    label: '对话',
    tagline: '硬切+溶解为主，大字幕，开双语',
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
// 5 维度卡片
// ---------------------------------------------------------------------------

function DimSection({
  title,
  subtitle,
  children,
}: {
  title: string
  subtitle: string
  children: React.ReactNode
}) {
  return (
    <div className="space-y-1.5 rounded-md border border-border bg-background/20 p-2.5">
      <div className="flex items-baseline gap-2">
        <h3 className="text-xs font-semibold">{title}</h3>
        <span className="text-[10px] text-muted-foreground">{subtitle}</span>
      </div>
      {children}
    </div>
  )
}

function SubtitleCard({
  c,
  selected,
  onPick,
}: {
  c: SubtitleStyleCandidate
  selected: boolean
  onPick: () => void
}) {
  return (
    <button
      type="button"
      onClick={onPick}
      className={cn(
        'flex flex-col gap-1 rounded-md border bg-background/40 p-2 text-left transition-colors',
        selected ? 'border-primary bg-primary/5' : 'border-border hover:border-primary/40',
      )}
    >
      <div className="flex items-center gap-1.5 text-[11px]">
        <span className="font-semibold">{c.label}</span>
        {selected && <span className="text-primary">✓</span>}
      </div>
      <div className="flex flex-wrap gap-1 text-[10px]">
        <Pill>{c.font_size === 'large' ? '大字' : c.font_size === 'small' ? '小字' : '中字'}</Pill>
        <Pill>{c.position === 'top' ? '顶' : c.position === 'middle' ? '中' : '底'}</Pill>
        <Pill>
          {c.background === 'none' ? '无底' : c.background === 'shadow' ? '阴影' : '渐变'}
        </Pill>
        {c.bilingual && <Pill>双语</Pill>}
      </div>
      <p className="text-[10px] text-muted-foreground">{c.rationale}</p>
    </button>
  )
}

function TitleBarCard({
  c,
  plan,
  selected,
  onToggle,
}: {
  c: TitleBarCandidate
  plan: Plan
  selected: boolean
  onToggle: () => void
}) {
  const scene = plan.main_track.find((s) => s.scene_id === c.target_scene_id)
  return (
    <button
      type="button"
      onClick={onToggle}
      className={cn(
        'flex flex-col gap-1 rounded-md border bg-background/40 p-2 text-left transition-colors',
        selected ? 'border-primary bg-primary/5' : 'border-border hover:border-primary/40',
      )}
    >
      <div className="flex items-center gap-1.5 text-[11px]">
        <input type="checkbox" checked={selected} readOnly className="cursor-pointer" />
        <span
          className="rounded px-1.5 py-0.5 text-[10px] font-medium"
          style={{ backgroundColor: c.background_color, color: c.color }}
        >
          {c.text}
        </span>
      </div>
      <div className="flex flex-wrap items-center gap-1 text-[10px] text-muted-foreground">
        {scene && (
          <span className={cn('rounded px-1 py-0.5 font-medium text-white', SECTION_BG[scene.section])}>
            {SECTION_SHORT[scene.section]}
          </span>
        )}
        <span className="font-mono">
          {c.start.toFixed(1)}-{c.end.toFixed(1)}s
        </span>
        <Pill>{c.position}</Pill>
      </div>
      <p className="text-[10px] text-muted-foreground">{c.rationale}</p>
    </button>
  )
}

function StickerCard({
  c,
  plan,
  selected,
  onToggle,
}: {
  c: StickerCandidate
  plan: Plan
  selected: boolean
  onToggle: () => void
}) {
  const scene = plan.main_track.find((s) => s.scene_id === c.target_scene_id)
  return (
    <button
      type="button"
      onClick={onToggle}
      className={cn(
        'flex flex-col gap-1 rounded-md border bg-background/40 p-2 text-left transition-colors',
        selected ? 'border-primary bg-primary/5' : 'border-border hover:border-primary/40',
      )}
    >
      <div className="flex items-center gap-1.5 text-[11px]">
        <input type="checkbox" checked={selected} readOnly className="cursor-pointer" />
        <span
          className="rounded px-1.5 py-0.5 text-[10px] font-bold"
          style={{ backgroundColor: c.background_color, color: c.color }}
        >
          {c.text}
        </span>
      </div>
      <div className="flex flex-wrap items-center gap-1 text-[10px] text-muted-foreground">
        {scene && (
          <span className={cn('rounded px-1 py-0.5 font-medium text-white', SECTION_BG[scene.section])}>
            {SECTION_SHORT[scene.section]}
          </span>
        )}
        <span className="font-mono">
          {c.start.toFixed(1)}-{c.end.toFixed(1)}s
        </span>
        <Pill>{c.position}</Pill>
      </div>
      <p className="text-[10px] text-muted-foreground">{c.rationale}</p>
    </button>
  )
}

function TransitionBundleRow({
  b,
  picked,
  onPick,
}: {
  b: TransitionCandidateBundle
  picked: TransitionStyle | null
  onPick: (style: TransitionStyle | null) => void
}) {
  return (
    <div className="flex flex-wrap items-center gap-2 rounded-md border border-border bg-background/30 px-2.5 py-1.5">
      <span className="font-mono text-[10px] text-muted-foreground">
        {b.at_seconds.toFixed(1)}s
      </span>
      <span
        className={cn(
          'rounded px-1.5 py-0.5 text-[10px] font-medium text-white',
          SECTION_BG[b.from_section] ?? 'bg-slate-500/80',
        )}
      >
        {SECTION_SHORT[b.from_section] ?? b.from_section.slice(0, 4)}
      </span>
      <span className="text-muted-foreground">→</span>
      <span
        className={cn(
          'rounded px-1.5 py-0.5 text-[10px] font-medium text-white',
          SECTION_BG[b.to_section] ?? 'bg-slate-500/80',
        )}
      >
        {SECTION_SHORT[b.to_section] ?? b.to_section.slice(0, 4)}
      </span>
      <div className="flex flex-wrap gap-1">
        {b.options.map((opt) => {
          const isPicked = picked === opt.style
          return (
            <button
              key={opt.style}
              type="button"
              onClick={() => onPick(isPicked ? null : opt.style)}
              className={cn(
                'rounded px-1.5 py-0.5 text-[10px] font-medium transition-colors',
                isPicked
                  ? cn('ring-1 ring-primary', TRANSITION_TONE[opt.style])
                  : cn(TRANSITION_TONE[opt.style], 'opacity-60 hover:opacity-100'),
              )}
              title={opt.reason}
            >
              {TRANSITION_LABEL[opt.style]} · {opt.duration.toFixed(1)}s
            </button>
          )
        })}
      </div>
      {b.rationale && (
        <span className="min-w-0 flex-1 truncate text-[10px] text-muted-foreground" title={b.rationale}>
          {b.rationale}
        </span>
      )}
    </div>
  )
}

function CoverCard({
  c,
  selected,
  onPick,
}: {
  c: CoverCandidate
  selected: boolean
  onPick: () => void
}) {
  const bg = c.palette[1] ?? '#1F2937'
  const accent = c.palette[0] ?? '#FFE600'
  const sub = c.palette[2] ?? '#FFFFFF'
  const isLeft = c.layout === 'left' || c.layout === 'stacked'

  return (
    <button
      type="button"
      onClick={onPick}
      className={cn(
        'flex flex-col gap-1.5 rounded-md border bg-background/40 p-2 text-left transition-colors',
        selected ? 'border-primary bg-primary/5' : 'border-border hover:border-primary/40',
      )}
    >
      <div
        className="relative h-24 w-full overflow-hidden rounded"
        style={{
          backgroundColor: bg,
          display: 'flex',
          flexDirection: 'column',
          justifyContent: 'center',
          alignItems: isLeft ? 'flex-start' : 'center',
          padding: '0 10px',
        }}
      >
        {c.layout === 'split' && (
          <div
            className="absolute right-0 top-0 bottom-0 w-2/5"
            style={{ backgroundColor: accent }}
          />
        )}
        <div
          style={{
            color: c.layout === 'split' ? sub : accent,
            fontSize: 18,
            fontWeight: 900,
            lineHeight: 1.05,
            textAlign: isLeft ? 'left' : 'center',
            zIndex: 2,
          }}
        >
          {c.title}
        </div>
        {c.subtitle && (
          <div
            style={{
              color: sub,
              fontSize: 10,
              marginTop: 3,
              opacity: 0.85,
              zIndex: 2,
            }}
          >
            {c.subtitle}
          </div>
        )}
      </div>
      <div className="flex items-center gap-1.5 text-[10px]">
        {selected && <span className="text-primary">✓</span>}
        <Pill>{c.layout}</Pill>
        {c.palette.slice(0, 3).map((p) => (
          <span
            key={p}
            className="inline-block h-3 w-5 rounded border border-border"
            style={{ backgroundColor: p }}
          />
        ))}
      </div>
      <p className="text-[10px] text-muted-foreground">{c.rationale}</p>
    </button>
  )
}

function Pill({ children }: { children: React.ReactNode }) {
  return (
    <span className="rounded bg-muted px-1.5 py-0.5 text-[10px] text-muted-foreground">
      {children}
    </span>
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
      if (set.size > 1) set.delete(style)
    } else {
      set.add(style)
    }
    updatePref('allowed_transition_styles', ALL_TRANSITIONS.filter((s) => set.has(s)))
  }

  return (
    <div className="space-y-3 rounded-md border border-border bg-background/20 p-3">
      <div>
        <h4 className="mb-1.5 text-[11px] font-semibold text-muted-foreground">允许的转场风格（影响 AI 生成候选）</h4>
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
          <span>切换最长时长</span>
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
          label="字幕字号（兜底）"
          value={prefs.subtitle_font_size}
          options={FONT_SIZES}
          renderLabel={(v) => FONT_LABEL[v]}
          onChange={(v) => updatePref('subtitle_font_size', v)}
        />
        <RadioGroup
          label="字幕位置（兜底）"
          value={prefs.subtitle_position}
          options={POSITIONS}
          renderLabel={(v) => POSITION_LABEL[v]}
          onChange={(v) => updatePref('subtitle_position', v)}
        />
        <RadioGroup
          label="字幕底色（兜底）"
          value={prefs.subtitle_background}
          options={BACKGROUNDS}
          renderLabel={(v) => BG_LABEL[v]}
          onChange={(v) => updatePref('subtitle_background', v)}
        />
      </div>

      <label className="flex items-center gap-2 text-[11px] text-muted-foreground">
        <span>AI 发挥度</span>
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
