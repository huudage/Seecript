/**
 * FrameDesignPicker —— frame.md 设计系统选择器。
 *
 * 灵感来自 HyperFrames 的 frame.md：把品牌设计系统翻译为视频可消费的 token，
 * packaging/copy/aigc agent 都从这里读色板/字体/动效密度，避免分段视觉割裂。
 *
 * 设计：
 * - 顶部一组 preset chips：custom + 9 个 HyperFrames 模板风格名（仅做风格 hint，
 *   不预下载素材）。选 preset 即把名字喂给后端 LLM 当风格基准。
 * - 折叠的「细调」区暴露 palette / motion_density / 颗粒 / 暗角 / 备注，
 *   让用户能在 preset 上覆写若干字段。
 * - 这是个纯展示组件——没有访问 catalog API，preset 名字硬编码（与后端
 *   FrameDesignPreset Literal 一一对应）。
 */
import { useState, type ChangeEvent, type KeyboardEvent } from 'react'

import { cn } from '@/lib/utils'
import type { FrameDesignPreset, FrameDesignSystem, MotionDensity } from '@/types/schemas'

const PRESET_OPTIONS: { value: FrameDesignPreset; label: string; hint: string }[] = [
  { value: 'custom', label: 'Custom', hint: '逐项手填' },
  { value: 'biennale-yellow', label: 'Biennale Yellow', hint: '高对比柠檬黄+纯黑' },
  { value: 'blockframe', label: 'BlockFrame', hint: '建筑感网格' },
  { value: 'blue-professional', label: 'Blue Pro', hint: '冷蓝商务克制' },
  { value: 'bold-poster', label: 'Bold Poster', hint: '海报字+撞色' },
  { value: 'broadside', label: 'Broadside', hint: '阔幅排版' },
  { value: 'capsule', label: 'Capsule', hint: '柔和胶囊圆角' },
  { value: 'cartesian', label: 'Cartesian', hint: '坐标系网格' },
  { value: 'cobalt-grid', label: 'Cobalt', hint: '钴蓝网格' },
  { value: 'coral', label: 'Coral', hint: '珊瑚暖色' },
  { value: 'creative-mode', label: 'Creative', hint: '玩味实验' },
]

const MOTION_OPTIONS: { value: MotionDensity; label: string; hint: string }[] = [
  { value: 'minimal', label: '克制', hint: '品牌片调性' },
  { value: 'balanced', label: '适中', hint: '默认' },
  { value: 'kinetic', label: '高动效', hint: '抖音 / Reels' },
]

export function FrameDesignPicker({
  value,
  onChange,
}: {
  value: FrameDesignSystem
  onChange: (patch: Partial<FrameDesignSystem>) => void
}) {
  const [open, setOpen] = useState(false)
  const [draftColor, setDraftColor] = useState('')

  const summary = (() => {
    const parts: string[] = []
    parts.push(PRESET_OPTIONS.find((p) => p.value === value.preset)?.label ?? value.preset)
    if (value.motion_density !== 'balanced') {
      parts.push(MOTION_OPTIONS.find((m) => m.value === value.motion_density)?.label ?? value.motion_density)
    }
    if (value.palette.length) parts.push(`${value.palette.length} 色`)
    if (value.grain_overlay) parts.push('颗粒')
    if (value.vignette) parts.push('暗角')
    return parts.join(' · ')
  })()

  const handleAddColor = () => {
    const next = draftColor.trim().toUpperCase()
    if (!/^#[0-9A-F]{6}$/.test(next) || value.palette.includes(next) || value.palette.length >= 6) {
      setDraftColor('')
      return
    }
    onChange({ palette: [...value.palette, next] })
    setDraftColor('')
  }

  const handleColorKey = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'Enter') {
      e.preventDefault()
      handleAddColor()
    }
  }

  const removeColor = (c: string) => onChange({ palette: value.palette.filter((x) => x !== c) })

  const handleNotes = (e: ChangeEvent<HTMLTextAreaElement>) =>
    onChange({ notes: e.target.value.slice(0, 200) })

  return (
    <div>
      <label className="text-[11px] font-semibold text-muted-foreground">
        frame.md 设计系统
        <span className="ml-2 font-normal text-muted-foreground/70">{summary}</span>
      </label>

      {/* preset chips */}
      <div className="mt-1 flex flex-wrap gap-1.5">
        {PRESET_OPTIONS.map((opt) => (
          <button
            key={opt.value}
            type="button"
            onClick={() => onChange({ preset: opt.value })}
            title={opt.hint}
            className={cn(
              'rounded-full border px-2 py-0.5 text-[11px] transition',
              value.preset === opt.value
                ? 'border-primary bg-primary/10 text-foreground'
                : 'border-border bg-background/60 text-muted-foreground hover:border-primary/60',
            )}
          >
            {opt.label}
          </button>
        ))}
      </div>

      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="mt-2 text-[11px] text-muted-foreground hover:text-foreground"
      >
        {open ? '▾ 收起细调' : '▸ 展开细调（色板 / 动效密度 / 质感）'}
      </button>

      {open && (
        <div className="mt-2 space-y-3 rounded-md border border-border bg-background/30 p-2">
          {/* palette */}
          <div>
            <label className="text-[10px] font-semibold text-muted-foreground">主色板（最多 6 色，HEX）</label>
            <div className="mt-1 flex flex-wrap items-center gap-1.5">
              {value.palette.map((c) => (
                <span
                  key={c}
                  className="inline-flex items-center gap-1 rounded border border-border bg-background px-1.5 py-0.5 font-mono text-[10px]"
                >
                  <span className="inline-block h-3 w-3 rounded-sm border border-border/80" style={{ backgroundColor: c }} />
                  {c}
                  <button
                    type="button"
                    onClick={() => removeColor(c)}
                    className="ml-0.5 text-muted-foreground hover:text-destructive"
                    aria-label={`移除 ${c}`}
                  >
                    ×
                  </button>
                </span>
              ))}
              {value.palette.length < 6 && (
                <input
                  type="text"
                  value={draftColor}
                  onChange={(e) => setDraftColor(e.target.value)}
                  onKeyDown={handleColorKey}
                  onBlur={handleAddColor}
                  placeholder="#FFE600"
                  className="w-20 rounded-md border border-border bg-background/60 px-1.5 py-0.5 font-mono text-[10px] outline-none focus:border-primary"
                />
              )}
            </div>
          </div>

          {/* motion */}
          <div>
            <label className="text-[10px] font-semibold text-muted-foreground">动效密度</label>
            <div className="mt-1 grid grid-cols-3 gap-1">
              {MOTION_OPTIONS.map((opt) => (
                <button
                  key={opt.value}
                  type="button"
                  onClick={() => onChange({ motion_density: opt.value })}
                  className={cn(
                    'rounded-md border px-1.5 py-1 text-left text-[10px] transition',
                    value.motion_density === opt.value
                      ? 'border-primary bg-primary/10 text-foreground'
                      : 'border-border bg-background/60 text-muted-foreground hover:border-primary/60',
                  )}
                >
                  <div className="font-semibold">{opt.label}</div>
                  <div className="text-[9px] text-muted-foreground">{opt.hint}</div>
                </button>
              ))}
            </div>
          </div>

          {/* texture toggles */}
          <div className="flex items-center gap-3">
            <label className="flex items-center gap-1 text-[10px] text-muted-foreground">
              <input
                type="checkbox"
                checked={value.grain_overlay}
                onChange={(e) => onChange({ grain_overlay: e.target.checked })}
              />
              颗粒/胶片质感
            </label>
            <label className="flex items-center gap-1 text-[10px] text-muted-foreground">
              <input
                type="checkbox"
                checked={value.vignette}
                onChange={(e) => onChange({ vignette: e.target.checked })}
              />
              暗角
            </label>
          </div>

          {/* notes */}
          <div>
            <label className="text-[10px] font-semibold text-muted-foreground">额外风格备注（≤ 200 字）</label>
            <textarea
              value={value.notes}
              onChange={handleNotes}
              rows={2}
              placeholder="例：阳光调，避免冷蓝；标题大字号，正文 22px"
              className="mt-1 w-full rounded-md border border-border bg-background/60 px-2 py-1 text-xs outline-none focus:border-primary"
            />
          </div>
        </div>
      )}
    </div>
  )
}
