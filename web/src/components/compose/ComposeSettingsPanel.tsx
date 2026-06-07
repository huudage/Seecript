import { useState, type ChangeEvent } from 'react'

import { cn } from '@/lib/utils'
import type { AspectRatio, ComposeSettings, FrameDesignSystem, MigrationPreference, TargetPlatform } from '@/types/schemas'
import { FrameDesignPicker } from './FrameDesignPicker'

/**
 * Compose 页"高级设置"折叠面板：目标时长 / 平台 / 画面比例 / 视频风格 / 迁移倾向 / 结尾引导。
 *
 * v3 起：
 * - 删除「整体调性」（tone）入口——调性表达统一让位给「视频风格」（FrameDesignPicker，
 *   原 frame.md 设计系统），由 preset + palette + motion_density 这一组 token 表达；
 *   schema 字段 ComposeSettings.tone 仍保留（默认 tight_hype），后端兼容老 plan。
 * - 删除「必须出现的关键词」入口——发现实际工程里这字段更多是字卡策划锚点而非用户表达欲望，
 *   写文案有结尾引导即可；schema 字段 ComposeSettings.keywords 仍保留默认空数组。
 *
 * 注：字幕 / 口播 / TTS 设置在四轨板上，这里只放全局结构参数。
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

const MIGRATION_OPTIONS: { value: MigrationPreference; label: string; hint: string }[] = [
  { value: 'amp_emotion', label: '情绪增强', hint: '钩子更猛 / 收尾燃 / CTA 强' },
  { value: 'amp_pace', label: '节奏紧凑', hint: '段段缩短 / 信息更密' },
  { value: 'mirror', label: '平淡复刻', hint: '保持原片调性' },
]

export function ComposeSettingsPanel({
  value,
  onChange,
}: {
  value: ComposeSettings
  onChange: (patch: Partial<ComposeSettings>) => void
}) {
  const [open, setOpen] = useState(true)

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
            {value.cta ? ` · 结尾「${value.cta}」` : ''}
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

          {/* 视频风格（原 frame.md 设计系统）—— 全片视觉统一的真正抓手 */}
          <FrameDesignPicker
            value={value.frame_design}
            onChange={(patch: Partial<FrameDesignSystem>) =>
              onChange({ frame_design: { ...value.frame_design, ...patch } })
            }
          />

          {/* 结构迁移倾向 —— 决定 plan/copy/aigc agent 的"调性版本" */}
          <div>
            <label className="text-[11px] font-semibold text-muted-foreground">
              结构迁移倾向
              <span className="ml-2 font-normal text-muted-foreground/70">
                决定新结构相对原片的偏向
              </span>
            </label>
            <div className="mt-1 grid grid-cols-3 gap-1.5">
              {MIGRATION_OPTIONS.map((opt) => (
                <button
                  key={opt.value}
                  type="button"
                  onClick={() => onChange({ migration_preference: opt.value })}
                  className={cn(
                    'rounded-md border px-2 py-1.5 text-left text-xs transition',
                    value.migration_preference === opt.value
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

          {/* 结尾引导语 */}
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
        </div>
      )}
    </div>
  )
}
