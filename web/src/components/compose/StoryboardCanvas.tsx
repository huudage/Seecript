import { useCallback, useMemo, useRef, useState } from 'react'
import {
  Background,
  Handle,
  MarkerType,
  Position,
  ReactFlow,
  ReactFlowProvider,
  type Edge,
  type Node,
  type NodeChange,
  type NodeProps,
} from '@xyflow/react'
import '@xyflow/react/dist/style.css'

import { CopilotDial, type DialAction } from '@/components/compose/CopilotDial'
import { CANVAS_MATERIAL_MIME, hasCanvasMaterialPayload } from '@/lib/dnd'
import { isUnfilledScene } from '@/lib/renderChecklist'
import { videoBlockTitle as blockTitle } from '@/lib/sections'
import { TRANSITION_LABEL } from '@/lib/transitions'
import { cn } from '@/lib/utils'
import type {
  AdaptedSection,
  FillResult,
  Gap,
  Material,
  Plan,
  Scene,
  TextCardSpec,
  TransitionStyle,
} from '@/types/schemas'

/**
 * 结构画布（PRD-v2 §5.2-F3 · Epic-3）。
 *
 * 心智模型是结构故事板：节点 = 叙事段落（AdaptedSection）容器，
 * 连线 = 叙事顺序 + 转场载体；底部常驻成片总览条补偿「无时间线」的时序感。
 *
 * D2 能力：
 * - 段落块拖拽重排（F4/US-3.2）——横向单轴，位置由叙事链序推导，松手提交新链序
 * - 选中实拍块 → 块底边切分游标（F4/US-3.5，边界规则与后端 canvas_ops 同口径）
 * - 连线点击 → 转场选择器（复用 step3 的 TransitionStylePicker）
 * - 块内层三轴状态点 v0（字幕/标题条/口播；口播/字幕点击进单镜编辑）
 * 所有结构操作即时生效，前端 editStore 撤销栈兜底（F13 契约）。
 *
 * 功能盘：右键唤出锚定动作。盘内只列已接线的回调，没接的不出现。
 * 视频块不标注样例结构名——结构迁移只在工具条里作参考。
 */

/** 块上显示用户主题；角色名和空主题都叫「视频块 N」。 */

/* ===================== 拖拽重排 / 切分常量 ===================== */

// 块布局：宽 240px（w-60），步进 300px——拖拽插入位按「拖拽块中心 vs 其余块原位中心」比较
const BLOCK_SPACING = 300
const HALF_BLOCK_W = 120
// 与后端 canvas_ops._MIN_SHOT_SECONDS / _MIN_SECTION_SECONDS 同口径
const REAL_SOURCES = new Set(['user_material', 'sample'])
const MIN_SHOT_SPLIT = 0.5
const MIN_SECTION_SPLIT = 2.0

/** 拖拽块中心落在其余哪个原位槽：返回插入下标（其余块保持原相对顺序）。 */
function insertionIndex(centerX: number, from: number, count: number): number {
  let insert = 0
  for (let i = 0; i < count; i++) {
    if (i === from) continue
    if (i * BLOCK_SPACING + HALF_BLOCK_W < centerX) insert++
  }
  return insert
}

/** 块内时间点 → 命中的镜 + 镜内偏移 + 是否为有效切点（实拍源且距镜两端 ≥ 0.5s）。 */
function sceneAtTime(
  t: number,
  scenes: Scene[],
): { scene: Scene; offset: number; valid: boolean } | null {
  let acc = 0
  for (const s of scenes) {
    if (t <= acc + s.duration + 1e-6) {
      const offset = Math.max(0, Math.min(s.duration, t - acc))
      const valid =
        REAL_SOURCES.has(s.source) && offset >= MIN_SHOT_SPLIT && offset <= s.duration - MIN_SHOT_SPLIT
      return { scene: s, offset, valid }
    }
    acc += s.duration
  }
  return null
}

/* ===================== 节点数据 ===================== */

interface SlotChipData {
  scene: Scene
  thumbUrl: string | null
  textCardSpec: TextCardSpec | null
  /** good/weak/missing 匹配档位之外，优先级：user_edited 已审 > needs_fill 待补 > matched ✓ > — */
  badge: 'reviewed' | 'fill' | 'matched' | 'none'
  /** 块内层三轴 v0：本镜时间窗上是否叠了字幕 / 标题条（口播看 scene.voiceover_url）。 */
  axis: { subtitle: boolean; titleBar: boolean }
  selected: boolean
}

export interface SectionBlockNodeData {
  section: AdaptedSection
  scenes: Scene[]
  start: number
  end: number
  slots: SlotChipData[]
  gapStatus: 'ok' | 'warn' | 'miss' | null
  filled: boolean
  selected: boolean
  /** v2 D4：整片仍是 AI 初稿时块半透明，并挂「AI 初稿」贴纸。 */
  draft: boolean
  insights?: Record<string, { summary: string; tags: string[] }>
  onDismissInsight?: (sceneId: string) => void
  /** 切分入口（US-3.5）：选中块时渲染；不可切时给 reason 提示。 */
  split: { splittable: boolean; reason?: string }
  onSplit?: (sceneId: string, splitAt: number) => void
  onAxisEdit?: (scene: Scene, section: AdaptedSection) => void
  /** 素材库卡片 HTML5 拖到槽位换源（US-3.2）。 */
  onSwapMaterial?: (sceneId: string, materialId: string) => void
  [key: string]: unknown
}

/* ===================== 段落块节点 ===================== */

/* ---- 块底边切分游标（US-3.5） ---- */

function SplitScrubber({
  scenes,
  onSplit,
}: {
  scenes: Scene[]
  onSplit: (sceneId: string, splitAt: number) => void
}) {
  const sorted = useMemo(() => [...scenes].sort((a, b) => a.start - b.start), [scenes])
  const total = sorted.reduce((acc, s) => acc + s.duration, 0)
  const [pos, setPos] = useState<number | null>(null)
  const draggingRef = useRef(false)
  const trackRef = useRef<HTMLDivElement>(null)

  const timeAt = (clientX: number): number => {
    const rect = trackRef.current?.getBoundingClientRect()
    if (!rect || rect.width <= 0 || total <= 0) return 0
    const ratio = Math.min(1, Math.max(0, (clientX - rect.left) / rect.width))
    return ratio * total
  }

  const hit = pos !== null && total > 0 ? sceneAtTime(pos, sorted) : null
  const pct = pos !== null && total > 0 ? (pos / total) * 100 : 0

  const handlePointerDown = (e: React.PointerEvent<HTMLDivElement>) => {
    e.stopPropagation()
    // pointer 极快点击时可能已释放，setPointerCapture 会抛 NotFoundError——吞掉不影响拖拽
    try {
      e.currentTarget.setPointerCapture(e.pointerId)
    } catch {
      /* noop */
    }
    draggingRef.current = true
    setPos(timeAt(e.clientX))
  }
  const handlePointerMove = (e: React.PointerEvent<HTMLDivElement>) => {
    if (!draggingRef.current) return
    setPos(timeAt(e.clientX))
  }
  const handlePointerUp = () => {
    if (!draggingRef.current) return
    draggingRef.current = false
    if (pos !== null && hit && hit.valid) {
      onSplit(hit.scene.scene_id, Math.round(hit.offset * 100) / 100)
    }
    setPos(null)
  }

  if (total <= 0) return null
  const ticks = Array.from({ length: Math.floor(total) }, (_, i) => i + 1)

  return (
    <div className="nodrag border-t border-border/70 bg-background/60 px-2 pb-1 pt-1" data-split-scrubber>
      <div
        ref={trackRef}
        className="relative h-5 cursor-ew-resize"
        onPointerDown={handlePointerDown}
        onPointerMove={handlePointerMove}
        onPointerUp={handlePointerUp}
        onPointerCancel={handlePointerUp}
      >
        {/* 轨道底 + 秒刻度（整 5s 加高） */}
        <div className="absolute inset-x-0 bottom-1 h-1 rounded bg-border" />
        {ticks.map((s) => (
          <span
            key={s}
            className="absolute bottom-1 w-px bg-zinc-400"
            style={{ left: `${(s / total) * 100}%`, height: s % 5 === 0 ? 7 : 4 }}
          />
        ))}
        {pos === null && (
          <span className="absolute inset-x-0 bottom-2.5 text-center text-[8px] text-muted-foreground">
            ✂ 拖动切分游标（实拍块一分为二）
          </span>
        )}
        {pos !== null && hit && (
          <>
            {/* 切点虚线预览：从游标向上贯穿块内容区 */}
            <div
              className="pointer-events-none absolute bottom-4 w-0 border-l border-dashed border-rose-500/80"
              style={{ left: `${pct}%`, height: 56 }}
            />
            <div
              className={cn(
                'pointer-events-none absolute bottom-0.5 z-10 h-3.5 w-3.5 -translate-x-1/2 rounded-full border-2 border-white shadow',
                hit.valid ? 'bg-rose-500' : 'bg-zinc-400',
              )}
              style={{ left: `${pct}%` }}
            />
            <span
              className="pointer-events-none absolute -top-4 z-10 -translate-x-1/2 whitespace-nowrap rounded bg-zinc-900/85 px-1 py-px text-[9px] font-medium text-white"
              style={{ left: `${Math.min(88, Math.max(12, pct))}%` }}
            >
              {hit.valid
                ? `#${hit.scene.shot_order + 1} 镜 @${hit.offset.toFixed(1)}s`
                : '此处不可切'}
            </span>
          </>
        )}
      </div>
    </div>
  )
}

function SectionBlockNode({ data }: NodeProps<Node<SectionBlockNodeData>>) {
  const { section, scenes, start, end, slots, selected, draft } = data
  const title = blockTitle(section)
  const duration = end - start
  const [dropHover, setDropHover] = useState<string | null>(null)

  return (
    <div
      className={cn(
        'w-60 overflow-hidden rounded-lg border bg-card shadow-sm transition-shadow',
        draft && 'opacity-60',
        selected ? 'border-primary ring-2 ring-primary/40' : 'border-border hover:shadow-md',
      )}
    >
      <Handle type="target" position={Position.Left} className="!h-2 !w-2 !border-0 !bg-zinc-400" />
      <Handle type="source" position={Position.Right} className="!h-2 !w-2 !border-0 !bg-zinc-400" />

      <div className="h-1.5 w-full bg-zinc-400" />
      <div className="flex items-center gap-1.5 px-2 pt-1.5">
        <span className="truncate text-xs font-semibold" title={section.content_description || title}>
          {title}
        </span>
        {draft && (
          <span className="shrink-0 rounded bg-amber-500/20 px-1 text-[8px] font-semibold text-amber-700">
            AI 初稿
          </span>
        )}
        <span className="ml-auto shrink-0 font-mono text-[9px] text-muted-foreground">
          {duration.toFixed(1)}s
        </span>
      </div>

      {/* 素材槽：每镜一槽（缩略图 + 匹配徽章 + 主体） */}
      <div className="flex flex-col gap-1 px-2 py-1.5">
        {slots.map((slot) => {
          const hover = dropHover === slot.scene.scene_id
          return (
            <div
              key={slot.scene.scene_id}
              data-scene-id={slot.scene.scene_id}
              className={cn(
                'flex items-center gap-1.5 overflow-hidden rounded border bg-background/60',
                hover
                  ? 'border-primary ring-2 ring-primary/60 bg-primary/5'
                  : slot.selected
                    ? 'border-primary ring-1 ring-primary/50'
                    : 'border-border/70 hover:border-primary/40',
              )}
              title={`第 ${slot.scene.shot_order + 1} 镜 · ${slot.scene.duration.toFixed(1)}s\n${slot.scene.shot_subject || ''}${slot.scene.narration ? `\n${slot.scene.narration}` : ''}${data.onSwapMaterial ? '\n（可从素材库拖卡片到此处换源）' : ''}`}
              onDragOver={(e) => {
                if (!data.onSwapMaterial || !hasCanvasMaterialPayload(e)) return
                e.preventDefault()
                e.dataTransfer.dropEffect = 'copy'
                setDropHover(slot.scene.scene_id)
              }}
              onDragLeave={() => setDropHover((cur) => (cur === slot.scene.scene_id ? null : cur))}
              onDrop={(e) => {
                if (!data.onSwapMaterial) return
                e.preventDefault()
                setDropHover(null)
                const materialId = e.dataTransfer.getData(CANVAS_MATERIAL_MIME)
                if (materialId) data.onSwapMaterial(slot.scene.scene_id, materialId)
              }}
            >
              <div className="h-8 w-12 shrink-0 overflow-hidden rounded-sm bg-black/25">
                {slot.textCardSpec ? (
                  <div
                    className="flex h-full w-full items-center justify-center px-0.5 text-center text-[8px] font-bold leading-tight"
                    style={{ background: slot.textCardSpec.bg_color, color: slot.textCardSpec.text_color }}
                  >
                    {(slot.textCardSpec.main_text || '字卡').slice(0, 8)}
                  </div>
                ) : slot.thumbUrl ? (
                  <img src={slot.thumbUrl} alt="" loading="lazy" className="h-full w-full object-cover" />
                ) : (
                  <div className="flex h-full w-full items-center justify-center text-[9px] font-bold text-white/70">
                    {slot.scene.source === 'aigc_t2v'
                      ? 'AI'
                      : slot.scene.source === 'aigc_image'
                        ? '图'
                        : slot.scene.source === 'user_material'
                          ? '素'
                          : slot.scene.source === 'sample'
                            ? '样'
                            : '字'}
                  </div>
                )}
              </div>
              <div className="min-w-0 flex-1 py-0.5 pr-1">
                <div className="flex items-center gap-1">
                  <span className="truncate text-[10px] font-medium">
                    #{slot.scene.shot_order + 1} {slot.scene.shot_subject || slot.scene.scene_id}
                  </span>
                </div>
                {data.insights?.[slot.scene.scene_id] && (
                  <div className="mt-0.5 flex items-start gap-1 rounded bg-amber-500/10 px-1 py-0.5 text-[8px] leading-tight text-amber-800">
                    <span className="min-w-0 flex-1">
                      {data.insights[slot.scene.scene_id].summary}
                      {data.insights[slot.scene.scene_id].tags.length > 0
                        ? ` · ${data.insights[slot.scene.scene_id].tags.slice(0, 3).join(' / ')}`
                        : ''}
                    </span>
                    <button
                      type="button"
                      className="nodrag shrink-0 font-semibold"
                      title="摘除 AI 理解贴纸"
                      onClick={(e) => {
                        e.stopPropagation()
                        data.onDismissInsight?.(slot.scene.scene_id)
                      }}
                    >
                      ×
                    </button>
                  </div>
                )}
                <div className="flex items-center gap-1 text-[9px] text-muted-foreground">
                  <span className="shrink-0">{slot.scene.duration.toFixed(1)}s</span>
                  {/* 块内层三轴 v0：字=字幕 题=标题条 播=口播；字/播点击进单镜编辑 */}
                  {slot.axis.subtitle && (
                    <button
                      type="button"
                      className="rounded-sm bg-amber-500/15 px-0.5 text-[8px] font-semibold text-amber-600 hover:bg-amber-500/25"
                      title="字幕轴——点击编辑口播文字（保存后字幕重生成）"
                      onClick={(e) => {
                        e.stopPropagation()
                        data.onAxisEdit?.(slot.scene, section)
                      }}
                    >
                      字
                    </button>
                  )}
                  {slot.axis.titleBar && (
                    <span
                      className="rounded-sm bg-violet-500/15 px-0.5 text-[8px] font-semibold text-violet-600"
                      title="标题条轴——在包装轨（step3）编辑"
                    >
                      题
                    </span>
                  )}
                  {slot.scene.voiceover_url && (
                    <button
                      type="button"
                      className="rounded-sm bg-sky-500/15 px-0.5 text-[8px] font-semibold text-sky-600 hover:bg-sky-500/25"
                      title="口播轴（已合成音频）——点击编辑口播文字"
                      onClick={(e) => {
                        e.stopPropagation()
                        data.onAxisEdit?.(slot.scene, section)
                      }}
                    >
                      播
                    </button>
                  )}
                </div>
              </div>
            </div>
          )
        })}
      </div>

      <div className="flex items-center justify-between border-t border-border/70 bg-background/40 px-2 py-1 text-[9px] text-muted-foreground">
        <span>
          {scenes.length} 镜 · {start.toFixed(1)}s → {end.toFixed(1)}s
        </span>
      </div>

      {/* 选中块 → 底边切分游标（US-3.5）；不可切时给出原因 */}
      {selected &&
        (data.split.splittable && data.onSplit ? (
          <SplitScrubber scenes={scenes} onSplit={data.onSplit} />
        ) : (
          <div
            className="nodrag border-t border-border/70 bg-background/60 px-2 py-1 text-[8px] text-muted-foreground"
            title={data.split.reason}
          >
            ✂ 不可切分：{data.split.reason ?? '未知原因'}
          </div>
        ))}
    </div>
  )
}

const nodeTypes = { sectionBlock: SectionBlockNode } as const

/* ===================== 主组件 ===================== */

interface Props {
  plan: Plan
  gaps: Gap[]
  filledGapIds: Set<string>
  materials?: Material[]
  fills?: FillResult[]
  selectedSectionId?: string | null
  /** 当前选中分镜（如经 ShotEditDialog 等入口选中）→ 对应槽 chip 高亮。 */
  selectedSceneId?: string | null
  /** 点段落块 / 总览条分段 → 通知父级选段（与 FourTrackBoard onSelectScene 同一联动口径）。 */
  onSelectSection?: (section: AdaptedSection, firstScene: Scene, gap: Gap | null) => void
  /** 功能盘动作（PRD-v2 F6/Epic-5）：全部可选——没接线的回调不进盘（白名单可视化）。 */
  onEditSection?: (section: AdaptedSection, firstScene: Scene) => void
  onEditShot?: (scene: Scene, section: AdaptedSection) => void
  onEditTransition?: (sceneId: string, currentStyle: TransitionStyle | null) => void
  onRecommendPackaging?: (sceneId: string) => void
  /** 盘内「配口播」（F10 ②）：人触发，父级打开 diff，确认前不写 plan。 */
  onAssignVoiceover?: (sectionId: string, sectionLabel: string) => void
  /** 盘内「局部改片」：作用域限该段，父级打开 diff。 */
  onLocalEdit?: (sectionId: string, sectionLabel: string) => void
  /** 实拍块「AI 理解」：贴纸，不写 plan。 */
  onAiInsight?: (scene: Scene) => void
  /** 实拍块「AI 裁剪」：diff 后由父级确认才换入出点。 */
  onAiTrim?: (scene: Scene) => void
  /** 生成：字卡 / 图片 / 视频。悬停展开表单，不叫缺口。 */
  onFillSlot?: (section: AdaptedSection, scene: Scene, action: 'copy' | 'aigc_image' | 'aigc') => void
  /** 悬停到某个「生成」方向时通知父级把表单放到光标旁。 */
  onFillHover?: (
    section: AdaptedSection,
    scene: Scene,
    action: 'copy' | 'aigc_image' | 'aigc',
    anchor: { x: number; y: number },
  ) => void
  onFillHoverEnd?: () => void
  /** 自然语言改片：打开多轮对话。自进化蒸馏仍走这条对话，不另起一套。 */
  onNaturalEdit?: () => void
  /** 空槽「补拍清单」：给人看，不写 plan。 */
  onShotBrief?: (scene: Scene, section: AdaptedSection) => void
  /** 画布空白「添加视频块」。 */
  onAppendBlock?: () => void
  /** AI 理解贴纸，按 scene_id。可摘除。 */
  insights?: Record<string, { summary: string; tags: string[] }>
  onDismissInsight?: (sceneId: string) => void
  /** 段落块拖拽重排（F4/US-3.2）：父级调端点拿新 Plan 并 setPlanAndPush。 */
  onReorderSections?: (sectionIds: string[]) => void
  /** 实拍块切分（F4/US-3.5）：父级调端点拿新 Plan 并 setPlanAndPush。 */
  onSplitScene?: (sceneId: string, splitAt: number) => void
  /** 块内层三轴点击（口播/字幕）→ 单镜编辑入口。 */
  onAxisEdit?: (scene: Scene, section: AdaptedSection) => void
  /** 素材库卡片拖到槽位换源（F4/US-3.2）：父级复用 swap-source 端点。 */
  onSwapMaterial?: (sceneId: string, materialId: string) => void
  className?: string
}

type Block = {
  section: AdaptedSection
  firstScene: Scene
  scenes: Scene[]
  slots: SlotChipData[]
  start: number
  end: number
  gap: Gap | null
  filled: boolean
  splittable: boolean
  splitReason: string | undefined
}

type DialAnchorState =
  | { kind: 'section'; section: AdaptedSection; firstScene: Scene }
  | { kind: 'scene'; scene: Scene; section: AdaptedSection }
  | { kind: 'edge'; sceneId: string; currentStyle: TransitionStyle | null }
  | { kind: 'pane' }

export function StoryboardCanvas({
  plan,
  gaps,
  filledGapIds,
  materials,
  fills,
  selectedSectionId = null,
  selectedSceneId = null,
  onSelectSection,
  onEditSection,
  onEditShot,
  onEditTransition,
  onRecommendPackaging,
  onAssignVoiceover,
  onLocalEdit,
  onAiInsight,
  onAiTrim,
  onFillSlot,
  onFillHover,
  onFillHoverEnd,
  onNaturalEdit,
  onShotBrief,
  onAppendBlock,
  insights,
  onDismissInsight,
  onReorderSections,
  onSplitScene,
  onAxisEdit,
  onSwapMaterial,
  className,
}: Props) {
  const materialById = useMemo(() => {
    const m = new Map<string, Material>()
    ;(materials ?? []).forEach((it) => m.set(it.material_id, it))
    return m
  }, [materials])

  const fillBySectionId = useMemo(() => {
    const m = new Map<string, FillResult>()
    ;(fills ?? []).forEach((f) => {
      if (f.section_id) m.set(f.section_id, f)
    })
    return m
  }, [fills])

  // 段落分组：优先 parent_section_id（stage-24 起跨 silent rebuild 稳定），
  // 老 plan 无此字段时按 scene_id `sc-N` 正则回落对 sec.order——与 FourTrackBoard 同口径。
  const blocks = useMemo<Block[]>(() => {
    const scenes = plan.main_track
    const out: Block[] = []
    for (const sec of plan.adapted_sections) {
      const inSec: Scene[] = []
      for (const sc of scenes) {
        if (sc.parent_section_id) {
          if (sc.parent_section_id === sec.section_id) inSec.push(sc)
        } else {
          const m = /sc-(\d+)/.exec(sc.scene_id)
          if (m && Number(m[1]) === sec.order) inSec.push(sc)
        }
      }
      if (inSec.length === 0) continue
      const start = Math.min(...inSec.map((s) => s.start))
      const end = Math.max(...inSec.map((s) => s.start + s.duration))
      const fill = fillBySectionId.get(sec.section_id) ?? null
      const slots: SlotChipData[] = inSec.map((sc) => {
        let thumbUrl: string | null = null
        if (sc.source === 'aigc_t2v') thumbUrl = fill?.cover_url ?? null
        else if (sc.source === 'aigc_image') thumbUrl = sc.aigc_image_url ?? fill?.aigc_image_url ?? null
        else if (sc.source === 'user_material')
          thumbUrl = materialById.get(sc.source_ref)?.thumbnail_url ?? null
        const shotPlan = sec.shots?.find((s) => s.order === sc.shot_order) ?? null
        const badge: SlotChipData['badge'] =
          sc.user_edited === true
            ? 'reviewed'
            : sc.needs_fill === true
              ? 'fill'
              : shotPlan?.matched_material_id
                ? 'matched'
                : 'none'
        const scStart = sc.start
        const scEnd = sc.start + sc.duration
        const axis = {
          subtitle: plan.packaging_track.some(
            (it) => it.kind === 'subtitle' && it.start < scEnd - 0.01 && it.end > scStart + 0.01,
          ),
          titleBar: plan.packaging_track.some(
            (it) => it.kind === 'title_bar' && it.start < scEnd - 0.01 && it.end > scStart + 0.01,
          ),
        }
        return {
          scene: sc,
          thumbUrl,
          textCardSpec: sc.text_card_spec ?? null,
          badge,
          axis,
          selected: sc.scene_id === selectedSceneId,
        }
      })
      // 段 gap 口径与 FourTrackBoard 一致：优先未补且非 ok 的，其次任意一个
      let gap: Gap | null = null
      for (const g of gaps) {
        if (g.section_id !== sec.section_id) continue
        if (!filledGapIds.has(g.gap_id) && g.status !== 'ok') {
          gap = g
          break
        }
        if (!gap) gap = g
      }
      // 切分边界（US-3.5，与后端 canvas_ops 同口径）：全实拍 + 无字幕/标题条/口播轴 + 两段各可 ≥2s
      const allReal = inSec.every((s) => REAL_SOURCES.has(s.source))
      const hasAxisPkg = plan.packaging_track.some(
        (it) =>
          (it.kind === 'subtitle' || it.kind === 'title_bar') &&
          it.start < end - 0.01 &&
          it.end > start + 0.01,
      )
      const hasVoiceover = inSec.some((s) => s.voiceover_url)
      const splittable = allReal && !hasAxisPkg && !hasVoiceover && end - start >= 2 * MIN_SECTION_SPLIT
      const splitReason = !allReal
        ? '块内含非实拍镜（AIGC / 字卡）'
        : hasAxisPkg
          ? '块含字幕/标题条轴，先摘除内层'
          : hasVoiceover
            ? '块含口播音轨，先摘除口播'
            : end - start < 2 * MIN_SECTION_SPLIT
              ? '块过短（切分后两段各需 ≥2s）'
              : undefined
      out.push({
        section: sec,
        firstScene: inSec[0],
        scenes: inSec,
        slots,
        start,
        end,
        gap,
        filled: gap ? filledGapIds.has(gap.gap_id) : false,
        splittable,
        splitReason,
      })
    }
    return out
  }, [plan, gaps, filledGapIds, materialById, fillBySectionId, selectedSceneId])

  const nodes = useMemo<Node<SectionBlockNodeData>[]>(
    () =>
      blocks.map((b, i) => ({
        id: b.section.section_id,
        type: 'sectionBlock',
        position: { x: i * BLOCK_SPACING, y: 0 },
        data: {
          section: b.section,
          scenes: b.scenes,
          start: b.start,
          end: b.end,
          slots: b.slots,
          gapStatus: b.gap?.status ?? null,
          filled: b.filled,
          selected: b.section.section_id === selectedSectionId,
          draft: plan.structure_confirmed === false,
          insights,
          onDismissInsight,
          split: { splittable: b.splittable, reason: b.splitReason },
          onSplit: onSplitScene,
          onAxisEdit,
          onSwapMaterial,
        },
      })),
    [blocks, insights, onDismissInsight, plan.structure_confirmed, selectedSectionId, onSplitScene, onAxisEdit, onSwapMaterial],
  )

  // 连线：叙事链序 + 转场标签（取下一块首镜的 transition_in）
  const edges = useMemo<Edge[]>(
    () =>
      blocks.slice(0, -1).map((b, i) => {
        const next = blocks[i + 1]
        const style = next.firstScene.transition_in?.style
        return {
          id: `edge-${b.section.section_id}-${next.section.section_id}`,
          source: b.section.section_id,
          target: next.section.section_id,
          label: style ? TRANSITION_LABEL[style] : undefined,
          labelStyle: { fontSize: 10, fill: '#64748b' },
          labelBgStyle: { fill: '#f1f5f9', fillOpacity: 0.9 },
          labelBgPadding: [4, 2],
          labelBgBorderRadius: 4,
          style: { stroke: '#94a3b8', strokeWidth: 1.5 },
          markerEnd: { type: MarkerType.ArrowClosed, width: 14, height: 14, color: '#94a3b8' },
        }
      }),
    [blocks],
  )

  const total = plan.duration_seconds || blocks.reduce((acc, b) => acc + (b.end - b.start), 0)

  const handleSelect = (b: Block) => {
    onSelectSection?.(b.section, b.firstScene, b.gap)
  }

  /* ===================== 拖拽重排（F4/US-3.2） ===================== */

  // 横向单轴重排：onNodesChange 只吸收 position 变化进覆盖位（其余变化交给 ReactFlow 内部态），
  // 被跨越的块按「拖拽块中心 vs 其余块原位中心」实时让位，松手提交新链序。
  const [drag, setDrag] = useState<{ id: string; x: number } | null>(null)

  const handleNodesChange = useCallback((changes: NodeChange<Node<SectionBlockNodeData>>[]) => {
    for (const ch of changes) {
      if (ch.type === 'position' && ch.dragging && ch.position && ch.id) {
        setDrag({ id: ch.id, x: ch.position.x })
      }
    }
  }, [])

  const handleNodeDragStop = useCallback(() => {
    if (drag && onReorderSections && blocks.length > 1) {
      const ids = blocks.map((b) => b.section.section_id)
      const from = ids.indexOf(drag.id)
      if (from >= 0) {
        const insert = insertionIndex(drag.x + HALF_BLOCK_W, from, ids.length)
        const next = [...ids]
        next.splice(from, 1)
        next.splice(insert, 0, drag.id)
        if (next.join('\n') !== ids.join('\n')) onReorderSections(next)
      }
    }
    setDrag(null)
  }, [drag, blocks, onReorderSections])

  // 拖拽中的实时让位布局：拖拽块跟手（y 锁 0），其余块滑向让出的槽位
  const nodesWithDrag = useMemo<Node<SectionBlockNodeData>[]>(() => {
    if (!drag) return nodes
    const from = nodes.findIndex((n) => n.id === drag.id)
    if (from < 0) return nodes
    const insert = insertionIndex(drag.x + HALF_BLOCK_W, from, nodes.length)
    return nodes.map((n, i) => {
      let target: number
      if (i === from) target = insert
      else {
        const k = i > from ? i - 1 : i
        target = k < insert ? k : k + 1
      }
      return i === from
        ? { ...n, position: { x: drag.x, y: 0 }, dragging: true }
        : { ...n, position: { x: target * BLOCK_SPACING, y: 0 } }
    })
  }, [nodes, drag])

  /* ===================== 功能盘：右键唤出 ===================== */

  // 盘锚点持有旧 plan 的 section/scene 引用，plan 一变（弹窗应用 / silent rebuild / 撤销）
  // 就作废——用「开盘时的 planId」做渲染守卫收掉，不用 effect setState（避免级联渲染）
  const [dial, setDial] = useState<{
    x: number
    y: number
    anchor: DialAnchorState
    planId: string
  } | null>(null)

  // 命中测试（冒泡顺序）：分镜槽 chip > 段落块节点 > 连线 > 空白画布
  const hitTestAnchor = (target: Element): DialAnchorState => {
    const sceneEl = target.closest('[data-scene-id]')
    if (sceneEl) {
      const sceneId = sceneEl.getAttribute('data-scene-id')
      const block = blocks.find((b) => b.scenes.some((s) => s.scene_id === sceneId))
      const scene = block?.scenes.find((s) => s.scene_id === sceneId)
      if (block && scene) return { kind: 'scene', scene, section: block.section }
    }
    const nodeEl = target.closest('.react-flow__node')
    if (nodeEl) {
      const block = blocks.find((b) => b.section.section_id === nodeEl.getAttribute('data-id'))
      if (block) return { kind: 'section', section: block.section, firstScene: block.firstScene }
    }
    const edgeEl = target.closest('[data-testid^="rf__edge"]')
    if (edgeEl) {
      const edgeId = (edgeEl.getAttribute('data-testid') ?? '').replace('rf__edge-', '')
      const edge = edges.find((ed) => ed.id === edgeId)
      const next = edge ? blocks.find((b) => b.section.section_id === edge.target) : undefined
      if (next) {
        return {
          kind: 'edge',
          sceneId: next.firstScene.scene_id,
          currentStyle: next.firstScene.transition_in?.style ?? null,
        }
      }
    }
    return { kind: 'pane' }
  }

  const dialActions = useMemo<DialAction[]>(() => {
    if (!dial) return []
    const a = dial.anchor
    const actions: DialAction[] = []
    const pushGenerate = (section: AdaptedSection, scene: Scene) => {
      if (!onFillSlot) return
      const items = [
        { id: 'fill-copy', action: 'copy' as const, title: '字卡', angle: Math.PI },
        { id: 'fill-image', action: 'aigc_image' as const, title: '图片', angle: 0 },
        { id: 'fill-t2v', action: 'aigc' as const, title: '视频', angle: Math.PI / 2 },
      ]
      for (const item of items) {
        actions.push({
          id: item.id,
          label: '生成',
          group: 'ai',
          hover: true,
          hoverTitle: item.title,
          angle: item.angle,
          run: () => onFillSlot(section, scene, item.action),
        })
      }
    }
    if (a.kind === 'section') {
      if (onNaturalEdit)
        actions.push({
          id: 'nl-edit',
          label: '自然语言改片',
          group: 'ai',
          run: () => onNaturalEdit(),
        })
      pushGenerate(a.section, a.firstScene)
      if (onEditSection)
        actions.push({
          id: 'edit-section',
          label: '编辑段',
          group: 'structure',
          run: () => onEditSection(a.section, a.firstScene),
        })
      if (onLocalEdit)
        actions.push({
          id: 'local-edit',
          label: '局部改片',
          group: 'ai',
          run: () =>
            onLocalEdit(a.section.section_id, blockTitle(a.section)),
        })
      if (onRecommendPackaging)
        actions.push({
          id: 'ai-packaging',
          label: '包装',
          group: 'ai',
          run: () => onRecommendPackaging(a.firstScene.scene_id),
        })
      if (onAssignVoiceover)
        actions.push({
          id: 'voiceover',
          label: '配口播',
          group: 'ai',
          run: () =>
            onAssignVoiceover(
              a.section.section_id,
              blockTitle(a.section),
            ),
        })
    } else if (a.kind === 'scene') {
      const empty = isUnfilledScene(a.scene)
      const footage = a.scene.source === 'user_material' && !empty
      if (footage && onAiInsight)
        actions.push({
          id: 'ai-insight',
          label: 'AI 理解',
          group: 'ai',
          run: () => onAiInsight(a.scene),
        })
      if (footage && onAiTrim)
        actions.push({
          id: 'ai-trim',
          label: 'AI 裁剪',
          group: 'ai',
          run: () => onAiTrim(a.scene),
        })
      if (onNaturalEdit)
        actions.push({
          id: 'nl-edit',
          label: '自然语言改片',
          group: 'ai',
          run: () => onNaturalEdit(),
        })
      if (onFillSlot) pushGenerate(a.section, a.scene)
      if (empty && onShotBrief)
        actions.push({
          id: 'shot-brief',
          label: '补拍清单',
          group: 'ai',
          run: () => onShotBrief(a.scene, a.section),
        })
      if (onEditShot)
        actions.push({
          id: 'edit-shot',
          label: '编辑本镜',
          group: 'structure',
          run: () => onEditShot(a.scene, a.section),
        })
    } else if (a.kind === 'pane') {
      if (onNaturalEdit)
        actions.push({
          id: 'nl-edit',
          label: '自然语言改片',
          group: 'ai',
          run: () => onNaturalEdit(),
        })
      if (onAppendBlock)
        actions.push({
          id: 'append-block',
          label: '添加视频块',
          group: 'structure',
          run: () => onAppendBlock(),
        })
    } else if (a.kind === 'edge') {
      if (onEditTransition)
        actions.push({
          id: 'transition',
          label: '转场',
          group: 'structure',
          run: () => onEditTransition(a.sceneId, a.currentStyle),
        })
    }
    return actions
  }, [
    dial,
    onAiInsight,
    onAiTrim,
    onAppendBlock,
    onAssignVoiceover,
    onEditSection,
    onEditShot,
    onEditTransition,
    onFillSlot,
    onNaturalEdit,
    onLocalEdit,
    onRecommendPackaging,
    onShotBrief,
  ])

  const dialAnchorLabel = (a: DialAnchorState): string => {
    switch (a.kind) {
      case 'section':
        return blockTitle(a.section)
      case 'scene':
        return `镜 #${a.scene.shot_order + 1}`
      case 'edge':
        return '连线'
      case 'pane':
        return '画布'
    }
  }

  const handleCanvasMouseDown = (e: React.MouseEvent<HTMLDivElement>) => {
    if (e.button === 0 && dial && !(e.target as Element).closest('[data-copilot-dial]')) {
      setDial(null)
    }
  }

  const handleContextMenu = (e: React.MouseEvent<HTMLDivElement>) => {
    e.preventDefault()
    if ((e.target as Element).closest('[data-copilot-dial]')) {
      setDial(null)
      return
    }
    const anchor = hitTestAnchor(e.target as Element)
    setDial({ x: e.clientX, y: e.clientY, anchor, planId: plan.plan_id })
  }

  if (blocks.length === 0) {
    return (
      <div
        className={cn(
          'flex h-40 items-center justify-center rounded-lg border border-dashed border-border bg-background/30 px-4 text-center text-xs text-muted-foreground',
          className,
        )}
      >
        本 plan 没有段落块。右键空白处可以添加视频块。
      </div>
    )
  }

  return (
    <div
      className={cn('flex flex-col gap-2', className)}
      onMouseDownCapture={handleCanvasMouseDown}
      onContextMenu={handleContextMenu}
    >
      {/* 画布区：节点 = 段落块，连线 = 叙事顺序 + 转场；拖块重排 / 选中切分 / 点连线调转场 */}
      <div className="h-[calc(100vh-11rem)] min-h-[640px] w-full overflow-hidden rounded-lg border border-border bg-slate-50">
        <ReactFlowProvider>
          <ReactFlow
            key={plan.plan_id}
            nodes={nodesWithDrag}
            edges={edges}
            nodeTypes={nodeTypes}
            fitView
            fitViewOptions={{ padding: 0.12, maxZoom: 1 }}
            nodesDraggable={!!onReorderSections && blocks.length > 1}
            nodesConnectable={false}
            edgesFocusable={false}
            onNodesChange={onReorderSections ? handleNodesChange : undefined}
            onNodeDragStop={onReorderSections ? handleNodeDragStop : undefined}
            onNodeClick={(_, node) => {
              const b = blocks.find((x) => x.section.section_id === node.id)
              if (b) handleSelect(b)
            }}
            onEdgeClick={(_, edge) => {
              if (!onEditTransition) return
              const next = blocks.find((b) => b.section.section_id === edge.target)
              if (next) onEditTransition(next.firstScene.scene_id, next.firstScene.transition_in?.style ?? null)
            }}
            minZoom={0.4}
            maxZoom={1.6}
          >
            <Background gap={16} size={1} />
          </ReactFlow>
        </ReactFlowProvider>
      </div>

      {/* 成片总览条（F3）：按连线链序显示各块时长占比 + 总时长；点分段选段 */}
      <div className="flex items-stretch gap-1.5">
        <div className="flex min-w-0 flex-1 overflow-hidden rounded-md border border-border">
          {blocks.map((b) => {
            const w = total > 0 ? Math.max(4, ((b.end - b.start) / total) * 100) : 100 / blocks.length
            return (
              <button
                key={b.section.section_id}
                type="button"
                onClick={() => handleSelect(b)}
                className={cn(
                  'flex h-7 min-w-0 items-center gap-1 overflow-hidden bg-zinc-600 px-1.5 text-[9px] font-medium transition-opacity hover:opacity-80',
                  b.section.section_id === selectedSectionId ? 'opacity-100 ring-2 ring-inset ring-white/70' : 'opacity-70',
                )}
                style={{ width: `${w}%` }}
                title={`${blockTitle(b.section)} · ${(b.end - b.start).toFixed(1)}s`}
              >
                <span className="truncate text-white">{blockTitle(b.section)}</span>
                <span className="ml-auto shrink-0 font-mono text-white/85">
                  {(b.end - b.start).toFixed(0)}s
                </span>
              </button>
            )
          })}
        </div>
        <div className="flex shrink-0 items-center rounded-md border border-border bg-card px-2 font-mono text-[10px] text-muted-foreground">
          全片 {total.toFixed(1)}s · {blocks.length} 段
        </div>
        <div className="hidden shrink-0 items-center rounded-md border border-border bg-card px-2 text-[10px] text-muted-foreground sm:flex">
          拖块 重排 · 右键 功能盘
        </div>
      </div>

      {dial && dial.planId === plan.plan_id && (
        <CopilotDial
          x={dial.x}
          y={dial.y}
          anchorLabel={dialAnchorLabel(dial.anchor)}
          actions={dialActions}
          hint={
            dial.anchor.kind === 'pane'
              ? onAppendBlock
                ? '空白处可添加视频块 · 右键点视频块 / 分镜槽 / 连线可唤出对应动作'
                : '空白处暂无盘内能力 · 右键点视频块 / 分镜槽 / 连线可唤出对应动作'
              : undefined
          }
          onHoverAction={(actionId) => {
            if (!actionId) {
              onFillHoverEnd?.()
              return
            }
            const fillById = {
              'fill-copy': 'copy',
              'fill-image': 'aigc_image',
              'fill-t2v': 'aigc',
            } as const
            const action = fillById[actionId as keyof typeof fillById]
            if (!action) return
            const anchor = dial.anchor
            const section = anchor.kind === 'section' || anchor.kind === 'scene' ? anchor.section : null
            const scene =
              anchor.kind === 'section' ? anchor.firstScene : anchor.kind === 'scene' ? anchor.scene : null
            if (!section || !scene) return
            const angle = action === 'copy' ? Math.PI : action === 'aigc_image' ? 0 : Math.PI / 2
            onFillHover?.(section, scene, action, {
              x: dial.x + Math.cos(angle) * 210,
              y: dial.y + Math.sin(angle) * 150,
            })
          }}
          onClose={() => setDial(null)}
        />
      )}

    </div>
  )
}
