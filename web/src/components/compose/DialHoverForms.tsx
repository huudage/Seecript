import { useEffect, useState } from 'react'

import type { AdaptedSection, Material } from '@/types/schemas'

/**
 * 空白画布功能盘的悬停表单。
 * 关联结构段只预填参数，不改原段。选中后用该段已有的拆解文案和时长当 AI 推荐值。
 */

export interface PaneCardInput {
  duration: number
  mainText: string
}

export interface PaneImageInput {
  duration: number
  prompt: string
  motion: 'hold' | 'push'
}

function sectionDefaults(section: AdaptedSection | undefined): { text: string; prompt: string; duration: number } {
  if (!section) return { text: '', prompt: '', duration: 3 }
  const text = (section.theme || section.content_description || '').trim().slice(0, 24)
  const prompt = (section.content_description || section.theme || '').trim().slice(0, 200)
  const duration = section.duration_seconds > 0 ? Math.round(section.duration_seconds * 10) / 10 : 3
  return { text, prompt, duration }
}

function SectionSelect({
  sections,
  value,
  onChange,
}: {
  sections: AdaptedSection[]
  value: string
  onChange: (sectionId: string) => void
}) {
  return (
    <label className="block text-[11px]">
      <span className="mb-1 block text-muted-foreground">关联结构段（可选）</span>
      <select
        value={value}
        onChange={(event) => onChange(event.target.value)}
        className="w-full rounded border border-border bg-background px-2 py-1"
      >
        <option value="">不关联，自己填</option>
        {sections.map((section, index) => (
          <option key={section.section_id} value={section.section_id}>
            {`段 ${index + 1} · ${(section.theme || section.content_description || '未命名').slice(0, 18)}`}
          </option>
        ))}
      </select>
      {value && (
        <span className="mt-1 block text-[10px] text-muted-foreground">已用该段拆解结果预填，可改。不会改掉原段。</span>
      )}
    </label>
  )
}

export function CardHoverForm({
  sections,
  pending,
  onSubmit,
}: {
  sections: AdaptedSection[]
  pending: boolean
  onSubmit: (input: PaneCardInput) => void
}) {
  const [sectionId, setSectionId] = useState('')
  const [duration, setDuration] = useState(3)
  const [mainText, setMainText] = useState('')

  useEffect(() => {
    const next = sectionDefaults(sections.find((section) => section.section_id === sectionId))
    if (!sectionId) return
    setDuration(next.duration)
    setMainText(next.text)
  }, [sectionId, sections])

  return (
    <form
      className="space-y-2 text-[11px]"
      onSubmit={(event) => {
        event.preventDefault()
        if (!mainText.trim()) return
        onSubmit({ duration, mainText: mainText.trim() })
      }}
    >
      <SectionSelect sections={sections} value={sectionId} onChange={setSectionId} />
      <label className="block">
        <span className="mb-1 block text-muted-foreground">时长（秒）</span>
        <input
          type="number"
          min={0.5}
          step={0.5}
          value={duration}
          onChange={(event) => setDuration(Number(event.target.value))}
          className="w-full rounded border border-border bg-background px-2 py-1"
        />
      </label>
      <label className="block">
        <span className="mb-1 block text-muted-foreground">主文案</span>
        <input
          value={mainText}
          maxLength={24}
          onChange={(event) => setMainText(event.target.value)}
          placeholder="最多 24 字"
          className="w-full rounded border border-border bg-background px-2 py-1"
        />
      </label>
      <button
        type="submit"
        disabled={pending || !mainText.trim() || duration <= 0}
        className="rounded bg-primary px-2 py-1 text-primary-foreground disabled:opacity-50"
      >
        {pending ? '生成中…' : '生成字卡'}
      </button>
    </form>
  )
}

export function ImageHoverForm({
  sections,
  pending,
  onSubmit,
}: {
  sections: AdaptedSection[]
  pending: boolean
  onSubmit: (input: PaneImageInput) => void
}) {
  const [sectionId, setSectionId] = useState('')
  const [duration, setDuration] = useState(4)
  const [prompt, setPrompt] = useState('')
  const [motion, setMotion] = useState<'hold' | 'push'>('push')

  useEffect(() => {
    if (!sectionId) return
    const next = sectionDefaults(sections.find((section) => section.section_id === sectionId))
    setDuration(next.duration)
    setPrompt(next.prompt)
  }, [sectionId, sections])

  return (
    <form
      className="space-y-2 text-[11px]"
      onSubmit={(event) => {
        event.preventDefault()
        if (!prompt.trim()) return
        onSubmit({ duration, prompt: prompt.trim(), motion })
      }}
    >
      <SectionSelect sections={sections} value={sectionId} onChange={setSectionId} />
      <label className="block">
        <span className="mb-1 block text-muted-foreground">生图提示词</span>
        <textarea
          value={prompt}
          maxLength={200}
          rows={3}
          onChange={(event) => setPrompt(event.target.value)}
          className="w-full rounded border border-border bg-background px-2 py-1"
        />
      </label>
      <label className="block">
        <span className="mb-1 block text-muted-foreground">时长（秒）</span>
        <input
          type="number"
          min={0.5}
          step={0.5}
          value={duration}
          onChange={(event) => setDuration(Number(event.target.value))}
          className="w-full rounded border border-border bg-background px-2 py-1"
        />
      </label>
      <label className="block">
        <span className="mb-1 block text-muted-foreground">渲染方式</span>
        <select
          value={motion}
          onChange={(event) => setMotion(event.target.value as 'hold' | 'push')}
          className="w-full rounded border border-border bg-background px-2 py-1"
        >
          <option value="hold">静图停留</option>
          <option value="push">缓慢推近</option>
        </select>
      </label>
      <button
        type="submit"
        disabled={pending || !prompt.trim() || duration <= 0}
        className="rounded bg-primary px-2 py-1 text-primary-foreground disabled:opacity-50"
      >
        {pending ? '生成中…' : '生图并渲染'}
      </button>
    </form>
  )
}

export function MaterialHoverForm({
  materials,
  pending,
  onPick,
}: {
  materials: Material[]
  pending: boolean
  onPick: (materialId: string) => void
}) {
  if (materials.length === 0) {
    return <p className="text-[11px] text-muted-foreground">素材库是空的。先在上一步上传视频。</p>
  }
  return (
    <div className="max-h-48 space-y-1 overflow-auto">
      {materials.map((material) => (
        <button
          key={material.material_id}
          type="button"
          disabled={pending}
          onClick={() => onPick(material.material_id)}
          className="flex w-full items-center justify-between rounded border border-border px-2 py-1 text-left text-[11px] hover:bg-secondary disabled:opacity-50"
        >
          <span className="truncate">{material.filename}</span>
          <span className="shrink-0 font-mono text-[10px] text-muted-foreground">
            {material.duration_seconds ? `${material.duration_seconds.toFixed(1)}s` : ''}
          </span>
        </button>
      ))}
    </div>
  )
}

export function PackagingHoverForm({
  kindLabel,
  defaultText,
  pending,
  onSubmit,
}: {
  kindLabel: string
  defaultText: string
  pending: boolean
  onSubmit: (text: string) => void
}) {
  const [text, setText] = useState(defaultText)
  useEffect(() => setText(defaultText), [defaultText])
  return (
    <form
      className="space-y-2 text-[11px]"
      onSubmit={(event) => {
        event.preventDefault()
        onSubmit(text.trim())
      }}
    >
      <p className="text-[10px] text-muted-foreground">只作用在这个视频块的时间范围，不含转场。</p>
      <label className="block">
        <span className="mb-1 block text-muted-foreground">{kindLabel}文案</span>
        <textarea
          value={text}
          rows={3}
          maxLength={80}
          onChange={(event) => setText(event.target.value)}
          className="w-full rounded border border-border bg-background px-2 py-1"
        />
      </label>
      <button
        type="submit"
        disabled={pending}
        className="rounded bg-primary px-2 py-1 text-primary-foreground disabled:opacity-50"
      >
        {pending ? '保存中…' : `放到本块`}
      </button>
    </form>
  )
}
