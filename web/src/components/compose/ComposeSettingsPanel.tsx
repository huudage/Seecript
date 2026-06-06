import { useState, type ChangeEvent, type KeyboardEvent } from 'react'

import { cn } from '@/lib/utils'
import type { AspectRatio, ComposeSettings, TargetPlatform, ToneStyle } from '@/types/schemas'

/**
 * Compose 页"高级设置"折叠面板：目标时长 / 平台 / 画面比例 / 调性 / CTA / 关键词。
 *
 * v2 起：『目标平台』决定节奏 + 字幕风格；『画面比例』独立控件，允许 B 站发竖屏等组合。
 *
 * 注：字幕轨 / 口播轨 已迁移到四轨板——字幕轨默认关，开关在 step2 字幕轨左侧；
 * 口播 TTS 默认关，开关与音色选择在 step3 口播轨左侧（一键合成同位置触发）。
 * 这里只保留全局结构参数。
 */

const PLATFORM_OPTIONS: { value: TargetPlatform; label: string; hint: string }[] = [
  { value: 'douyin', label: '抖音', hint: '强字幕 节奏紧凑' },
  { value: 'wechat', label: '视频号', hint: '节奏温和' },
  { value: 'xiaohongshu', label: '小红书', hint: '文艺克制' },
  { value: 'bilibili', label: 'B 站', hint: '叙事感' },
]

const ASPECT_OPTIONS: { value: AspectRatio; label: string; hint: string }[] = [
  { value: '9:16', label: '9:16', hint: '竖屏短视频' },
  { value: '16:9', label: '16:9', hint: '横屏长内容' },
  { value: '1:1', label: '1:1', hint: '方版橱窗' },
]

const TONE_OPTIONS: { value: ToneStyle; label: string; hint: string }[] = [
  { value: 'tight_hype', label: '紧凑高燃', hint: '快剪 + 强情绪' },
  { value: 'calm_narrative', label: '沉稳叙事', hint: '长镜头 + 余韵' },
  { value: 'casual_daily', label: '轻松日常', hint: '口语化' },
  { value: 'professional_cool', label: '专业冷静', hint: '高信息密度' },
]

export function ComposeSettingsPanel({
  value,
  onChange,
}: {
  value: ComposeSettings
  onChange: (patch: Partial<ComposeSettings>) => void
}) {
  const [open, setOpen] = useState(true)
  const [draftKw, setDraftKw] = useState('')

  const handleAddKeyword = () => {
    const next = draftKw.trim()
    if (!next || value.keywords.includes(next)) {
      setDraftKw('')
      return
    }
    if (value.keywords.length >= 5) {
      setDraftKw('')
      return
    }
    onChange({ keywords: [...value.keywords, next.slice(0, 12)] })
    setDraftKw('')
  }

  const handleKwKey = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'Enter' || e.key === ',') {
      e.preventDefault()
      handleAddKeyword()
    }
  }

  const removeKeyword = (kw: string) =>
    onChange({ keywords: value.keywords.filter((k) => k !== kw) })

  const handleDur = (e: ChangeEvent<HTMLInputElement>) => {
    const n = Number(e.target.value)
    if (Number.isFinite(n)) onChange({ target_duration_seconds: Math.max(10, Math.min(120, n)) })
  }

  const handleCta = (e: ChangeEvent<HTMLInputElement>) =>
    onChange({ cta: e.target.value.slice(0, 20) })

  return (
    <div className="rounded-md border border-border bg-background/40">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center justify-between px-3 py-2 text-left text-xs font-semibold text-foreground hover:bg-accent/40"
      >
        <span>
          高级设置
          <span className="ml-2 font-normal text-muted-foreground">
            {value.target_duration_seconds}s · {PLATFORM_OPTIONS.find((p) => p.value === value.target_platform)?.label}
            {' · '}
            {value.aspect_ratio}
            {' · '}
            {TONE_OPTIONS.find((t) => t.value === value.tone)?.label}
            {value.cta ? ` · 结尾「${value.cta}」` : ''}
            {value.keywords.length > 0 ? ` · ${value.keywords.length} 个关键词` : ''}
          </span>
        </span>
        <span className="text-muted-foreground">{open ? '▾' : '▸'}</span>
      </button>

      {open && (
        <div className="space-y-3 border-t border-border p-3">
          {/* 目标时长 */}
          <div>
            <label className="text-[11px] font-semibold text-muted-foreground">目标总时长（秒）</label>
            <div className="mt-1 flex items-center gap-2">
              <input
                type="range"
                min={10}
                max={120}
                step={5}
                value={value.target_duration_seconds}
                onChange={handleDur}
                className="flex-1"
              />
              <input
                type="number"
                min={10}
                max={120}
                value={value.target_duration_seconds}
                onChange={handleDur}
                className="w-16 rounded-md border border-border bg-background/60 px-2 py-1 text-right font-mono text-xs"
              />
            </div>
          </div>

          {/* 平台 */}
          <div>
            <label className="text-[11px] font-semibold text-muted-foreground">目标平台</label>
            <div className="mt-1 grid grid-cols-2 gap-1.5">
              {PLATFORM_OPTIONS.map((opt) => (
                <button
                  key={opt.value}
                  type="button"
                  onClick={() => onChange({ target_platform: opt.value })}
                  className={cn(
                    'rounded-md border px-2 py-1.5 text-left text-xs transition',
                    value.target_platform === opt.value
                      ? 'border-primary bg-primary/10 text-foreground'
                      : 'border-border bg-background/60 text-muted-foreground hover:border-primary/60',
                  )}
                >
                  <div className="font-semibold">{opt.label}</div>
                  <div className="text-[10px] text-muted-foreground">{opt.hint}</div>
                </button>
              ))}
            </div>
          </div>

          {/* 画面比例（v2 与平台解耦） */}
          <div>
            <label className="text-[11px] font-semibold text-muted-foreground">画面比例</label>
            <div className="mt-1 grid grid-cols-3 gap-1.5">
              {ASPECT_OPTIONS.map((opt) => (
                <button
                  key={opt.value}
                  type="button"
                  onClick={() => onChange({ aspect_ratio: opt.value })}
                  className={cn(
                    'rounded-md border px-2 py-1.5 text-left text-xs transition',
                    value.aspect_ratio === opt.value
                      ? 'border-primary bg-primary/10 text-foreground'
                      : 'border-border bg-background/60 text-muted-foreground hover:border-primary/60',
                  )}
                >
                  <div className="font-mono font-semibold">{opt.label}</div>
                  <div className="text-[10px] text-muted-foreground">{opt.hint}</div>
                </button>
              ))}
            </div>
          </div>

          {/* 调性 */}
          <div>
            <label className="text-[11px] font-semibold text-muted-foreground">整体调性</label>
            <div className="mt-1 grid grid-cols-2 gap-1.5">
              {TONE_OPTIONS.map((opt) => (
                <button
                  key={opt.value}
                  type="button"
                  onClick={() => onChange({ tone: opt.value })}
                  className={cn(
                    'rounded-md border px-2 py-1.5 text-left text-xs transition',
                    value.tone === opt.value
                      ? 'border-primary bg-primary/10 text-foreground'
                      : 'border-border bg-background/60 text-muted-foreground hover:border-primary/60',
                  )}
                >
                  <div className="font-semibold">{opt.label}</div>
                  <div className="text-[10px] text-muted-foreground">{opt.hint}</div>
                </button>
              ))}
            </div>
          </div>

          {/* 口播 TTS 已迁移到四轨板的口播 / 字幕轨——开篇不再选音色，由字幕轨一键 TTS 触发。 */}

          {/* CTA */}
          <div>
            <div className="flex items-center justify-between">
              <label className="text-[11px] font-semibold text-muted-foreground">结尾引导语（最多 20 字）</label>
              <span className="font-mono text-[10px] text-muted-foreground">{value.cta.length}/20</span>
            </div>
            <input
              type="text"
              value={value.cta}
              onChange={handleCta}
              placeholder="例：点击主页立即预约"
              className="mt-1 w-full rounded-md border border-border bg-background/60 px-2 py-1.5 text-sm outline-none focus:border-primary"
            />
          </div>

          {/* 关键词 */}
          <div>
            <div className="flex items-center justify-between">
              <label className="text-[11px] font-semibold text-muted-foreground">必须出现的关键词（最多 5 个）</label>
              <span className="font-mono text-[10px] text-muted-foreground">{value.keywords.length}/5</span>
            </div>
            <div className="mt-1 flex flex-wrap items-center gap-1.5">
              {value.keywords.map((kw) => (
                <span
                  key={kw}
                  className="inline-flex items-center gap-1 rounded-full border border-primary/30 bg-primary/10 px-2 py-0.5 text-xs text-foreground"
                >
                  {kw}
                  <button
                    type="button"
                    onClick={() => removeKeyword(kw)}
                    className="text-muted-foreground hover:text-destructive"
                    aria-label={`移除 ${kw}`}
                  >
                    ×
                  </button>
                </span>
              ))}
              {value.keywords.length < 5 && (
                <input
                  type="text"
                  value={draftKw}
                  onChange={(e) => setDraftKw(e.target.value.slice(0, 12))}
                  onKeyDown={handleKwKey}
                  onBlur={handleAddKeyword}
                  placeholder="回车确认"
                  className="w-24 rounded-md border border-border bg-background/60 px-2 py-1 text-xs outline-none focus:border-primary"
                />
              )}
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
