import { useEffect, useMemo, useRef, useState } from 'react'
import {
  Background,
  Handle,
  Position,
  ReactFlow,
  ReactFlowProvider,
  type Edge,
  type Node,
  type NodeProps,
} from '@xyflow/react'
import '@xyflow/react/dist/style.css'

import { CopilotDial, type DialAction } from '@/components/compose/CopilotDial'
import { getSectionMeta } from '@/lib/sections'
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
 * 结构画布（PRD-v2 §5.2-F3 · Epic-3）——D1 骨架版。
 *
 * 心智模型是结构故事板：节点 = 叙事段落（AdaptedSection）容器，
 * 连线 = 叙事顺序 + 转场载体；底部常驻成片总览条补偿「无时间线」的时序感。
 *
 * D1 范围：节点/连线/总览条渲染 + 选段联动（点击块同步 selectedSectionId，
 * 驱动下方补全工作台）。拖拽重排 / 换槽素材 / 块内层三轴（D2 U1）后续落——
 * 届时节点位置改由叙事链序推导，拖拽走 plan_store.replace 新 plan_id 入撤销栈（F13 契约）。
 *
 * D3 前置的盘交互（F6 修订版）：中键唤出锚定功能盘，右键撤销上一步（配瞬时
 * 反馈贴纸）。盘内动作是白名单可视化——只列已接线的回调，没接的不出现。
 */

/* ===================== role 四族色带 ===================== */
// PRD-v2 F3：块头 role 色带按「开场/发展/高潮/收尾」四族语义着色。
// v1 曾把段色全废成中性（lib/sections.ts），但 v2 画布以 PRD 为权威恢复语义色；
// 9 种 SectionKind 收敛到四族，动态主体角色（step_N/item_N/daily_N）归发展。
type RoleFamily = 'opening' | 'development' | 'climax' | 'closing'

const ROLE_FAMILY: Record<string, RoleFamily> = {
  opening: 'opening',
  intro: 'opening',
  hook: 'opening',
  establish: 'opening',
  title_card: 'opening',
  intro_scene: 'opening',
  development: 'development',
  flow: 'development',
  info_block: 'development',
  climax: 'climax',
  peak: 'climax',
  payoff: 'climax',
  closing: 'closing',
  recap: 'closing',
  closer: 'closing',
  resolve: 'closing',
  wrap_up: 'closing',
}

const FAMILY_META: Record<RoleFamily, { label: string; band: string; text: string; soft: string }> = {
  opening: { label: '开场', band: 'bg-sky-500', text: 'text-sky-600', soft: 'bg-sky-500/15' },
  development: { label: '发展', band: 'bg-zinc-500', text: 'text-zinc-600', soft: 'bg-zinc-500/15' },
  climax: { label: '高潮', band: 'bg-rose-500', text: 'text-rose-600', soft: 'bg-rose-500/15' },
  closing: { label: '收尾', band: 'bg-emerald-500', text: 'text-emerald-600', soft: 'bg-emerald-500/15' },
}

function roleFamilyOf(role: string): RoleFamily {
  return ROLE_FAMILY[role] ?? 'development'
}

/* ===================== 节点数据 ===================== */

interface SlotChipData {
  scene: Scene
  thumbUrl: string | null
  textCardSpec: TextCardSpec | null
  /** good/weak/missing 匹配档位之外，优先级：user_edited 已审 > needs_fill 待补 > matched ✓ > — */
  badge: 'reviewed' | 'fill' | 'matched' | 'none'
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
  [key: string]: unknown
}

/* ===================== 段落块节点 ===================== */

const SLOT_BADGE: Record<SlotChipData['badge'], { text: string; cls: string }> = {
  reviewed: { text: '已审', cls: 'bg-sky-500/95' },
  fill: { text: '待补', cls: 'bg-rose-500/95' },
  matched: { text: '✓', cls: 'bg-emerald-500/95' },
  none: { text: '—', cls: 'bg-zinc-500/85' },
}

function SectionBlockNode({ data }: NodeProps<Node<SectionBlockNodeData>>) {
  const { section, scenes, start, end, slots, gapStatus, filled, selected } = data
  const meta = getSectionMeta(section.role)
  const family = FAMILY_META[roleFamilyOf(section.role)]
  const duration = end - start

  return (
    <div
      className={cn(
        'w-60 overflow-hidden rounded-lg border bg-card shadow-sm transition-shadow',
        selected ? 'border-primary ring-2 ring-primary/40' : 'border-border hover:shadow-md',
      )}
    >
      <Handle type="target" position={Position.Left} className="!h-2 !w-2 !border-0 !bg-zinc-400" />
      <Handle type="source" position={Position.Right} className="!h-2 !w-2 !border-0 !bg-zinc-400" />

      {/* 块头：role 色带 + 段主题 + 时长 */}
      <div className={cn('h-1.5 w-full', family.band)} />
      <div className="flex items-center gap-1.5 px-2 pt-1.5">
        <span className={cn('rounded px-1 py-px text-[9px] font-semibold', family.soft, family.text)}>
          {meta.label}
        </span>
        <span className="truncate text-xs font-semibold" title={section.content_description}>
          {section.theme || meta.label}
        </span>
        <span className="ml-auto shrink-0 font-mono text-[9px] text-muted-foreground">
          {duration.toFixed(1)}s
        </span>
      </div>

      {/* 素材槽：每镜一槽（缩略图 + 匹配徽章 + 主体） */}
      <div className="flex flex-col gap-1 px-2 py-1.5">
        {slots.map((slot) => {
          const badge = SLOT_BADGE[slot.badge]
          return (
            <div
              key={slot.scene.scene_id}
              data-scene-id={slot.scene.scene_id}
              className={cn(
                'flex items-center gap-1.5 overflow-hidden rounded border bg-background/60',
                slot.selected
                  ? 'border-primary ring-1 ring-primary/50'
                  : 'border-border/70 hover:border-primary/40',
              )}
              title={`第 ${slot.scene.shot_order + 1} 镜 · ${slot.scene.duration.toFixed(1)}s\n${slot.scene.shot_subject || ''}${slot.scene.narration ? `\n${slot.scene.narration}` : ''}`}
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
                  <span className={cn('rounded px-1 text-[8px] font-bold leading-tight text-white', badge.cls)}>
                    {badge.text}
                  </span>
                  <span className="truncate text-[10px] font-medium">
                    #{slot.scene.shot_order + 1} {slot.scene.shot_subject || slot.scene.scene_id}
                  </span>
                </div>
                <div className="truncate text-[9px] text-muted-foreground">
                  {slot.scene.duration.toFixed(1)}s
                  {slot.scene.voiceover_url ? ' · 🎙' : ''}
                </div>
              </div>
            </div>
          )
        })}
      </div>

      {/* 块底：镜数 + 缺口态 */}
      <div className="flex items-center justify-between border-t border-border/70 bg-background/40 px-2 py-1 text-[9px] text-muted-foreground">
        <span>
          {scenes.length} 镜 · {start.toFixed(1)}s → {end.toFixed(1)}s
        </span>
        {filled ? (
          <span className="rounded bg-emerald-500/15 px-1 py-px font-medium text-emerald-600">已补</span>
        ) : gapStatus === 'miss' ? (
          <span className="rounded bg-rose-500/15 px-1 py-px font-medium text-rose-600">缺画面</span>
        ) : gapStatus === 'warn' ? (
          <span className="rounded bg-amber-500/15 px-1 py-px font-medium text-amber-600">待修补</span>
        ) : (
          <span className="rounded bg-emerald-500/10 px-1 py-px font-medium text-emerald-600">OK</span>
        )}
      </div>
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
  /** 右键撤销（F6 修订）：返回是否真的撤销了，驱动画布内瞬时反馈贴纸。 */
  onUndo?: () => boolean
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
  onUndo,
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
        return {
          scene: sc,
          thumbUrl,
          textCardSpec: sc.text_card_spec ?? null,
          badge,
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
      out.push({
        section: sec,
        firstScene: inSec[0],
        scenes: inSec,
        slots,
        start,
        end,
        gap,
        filled: gap ? filledGapIds.has(gap.gap_id) : false,
      })
    }
    return out
  }, [plan, gaps, filledGapIds, materialById, fillBySectionId, selectedSceneId])

  const nodes = useMemo<Node<SectionBlockNodeData>[]>(
    () =>
      blocks.map((b, i) => ({
        id: b.section.section_id,
        type: 'sectionBlock',
        position: { x: i * 300, y: 0 },
        data: {
          section: b.section,
          scenes: b.scenes,
          start: b.start,
          end: b.end,
          slots: b.slots,
          gapStatus: b.gap?.status ?? null,
          filled: b.filled,
          selected: b.section.section_id === selectedSectionId,
        },
      })),
    [blocks, selectedSectionId],
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
        }
      }),
    [blocks],
  )

  const total = plan.duration_seconds || blocks.reduce((acc, b) => acc + (b.end - b.start), 0)

  const handleSelect = (b: Block) => {
    onSelectSection?.(b.section, b.firstScene, b.gap)
  }

  /* ===================== 功能盘 + 右键撤销（F6 修订） ===================== */

  // 盘锚点持有旧 plan 的 section/scene 引用，plan 一变（弹窗应用 / silent rebuild / 撤销）
  // 就作废——用「开盘时的 planId」做渲染守卫收掉，不用 effect setState（避免级联渲染）
  const [dial, setDial] = useState<{
    x: number
    y: number
    anchor: DialAnchorState
    planId: string
  } | null>(null)
  const [undoToast, setUndoToast] = useState<{ text: string; x: number; y: number } | null>(null)
  const toastTimer = useRef<number | null>(null)

  useEffect(
    () => () => {
      if (toastTimer.current !== null) window.clearTimeout(toastTimer.current)
    },
    [],
  )

  const showUndoToast = (text: string, x: number, y: number) => {
    if (toastTimer.current !== null) window.clearTimeout(toastTimer.current)
    setUndoToast({ text, x, y })
    toastTimer.current = window.setTimeout(() => setUndoToast(null), 1600)
  }

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
    if (a.kind === 'section') {
      if (onEditSection)
        actions.push({
          id: 'edit-section',
          label: '编辑段',
          group: 'structure',
          run: () => onEditSection(a.section, a.firstScene),
        })
      if (onRecommendPackaging)
        actions.push({
          id: 'ai-packaging',
          label: 'AI 包装',
          group: 'ai',
          run: () => onRecommendPackaging(a.firstScene.scene_id),
        })
    } else if (a.kind === 'scene') {
      if (onEditShot)
        actions.push({
          id: 'edit-shot',
          label: '编辑本镜',
          group: 'structure',
          run: () => onEditShot(a.scene, a.section),
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
  }, [dial, onEditSection, onEditShot, onEditTransition, onRecommendPackaging])

  const dialAnchorLabel = (a: DialAnchorState): string => {
    switch (a.kind) {
      case 'section':
        return `段 · ${a.section.theme || getSectionMeta(a.section.role).label}`
      case 'scene':
        return `镜 #${a.scene.shot_order + 1}`
      case 'edge':
        return '连线'
      case 'pane':
        return '画布'
    }
  }

  // capture 阶段拦中键：preventDefault 掐掉浏览器自动滚动，再决定开盘/收盘
  const handleCanvasMouseDown = (e: React.MouseEvent<HTMLDivElement>) => {
    if (e.button === 1) {
      e.preventDefault()
      if ((e.target as Element).closest('[data-copilot-dial]')) {
        setDial(null)
        return
      }
      const anchor = hitTestAnchor(e.target as Element)
      setDial({ x: e.clientX, y: e.clientY, anchor, planId: plan.plan_id })
      return
    }
    if (e.button === 0 && dial && !(e.target as Element).closest('[data-copilot-dial]')) {
      setDial(null)
    }
  }

  const handleContextMenu = (e: React.MouseEvent<HTMLDivElement>) => {
    e.preventDefault()
    setDial(null)
    const undone = onUndo?.() ?? false
    showUndoToast(undone ? '已撤销 ↶' : '没有可撤销的操作', e.clientX, e.clientY)
  }

  if (blocks.length === 0) {
    return (
      <div
        className={cn(
          'flex h-40 items-center justify-center rounded-lg border border-dashed border-border bg-background/30 px-4 text-center text-xs text-muted-foreground',
          className,
        )}
      >
        本 plan 无段落结构（老数据未携带 adapted_sections）——切回四轨视图查看。
      </div>
    )
  }

  return (
    <div
      className={cn('flex flex-col gap-2', className)}
      onMouseDownCapture={handleCanvasMouseDown}
      onContextMenu={handleContextMenu}
    >
      {/* 画布区：节点 = 段落块，连线 = 叙事顺序 + 转场；D1 只读骨架，拖拽交互 D2 落 */}
      <div className="h-[420px] w-full overflow-hidden rounded-lg border border-border bg-slate-50">
        <ReactFlowProvider>
          <ReactFlow
            key={plan.plan_id}
            nodes={nodes}
            edges={edges}
            nodeTypes={nodeTypes}
            fitView
            fitViewOptions={{ padding: 0.12, maxZoom: 1 }}
            nodesDraggable={false}
            nodesConnectable={false}
            edgesFocusable={false}
            onNodeClick={(_, node) => {
              const b = blocks.find((x) => x.section.section_id === node.id)
              if (b) handleSelect(b)
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
            const family = FAMILY_META[roleFamilyOf(b.section.role)]
            const w = total > 0 ? Math.max(4, ((b.end - b.start) / total) * 100) : 100 / blocks.length
            return (
              <button
                key={b.section.section_id}
                type="button"
                onClick={() => handleSelect(b)}
                className={cn(
                  'flex h-7 min-w-0 items-center gap-1 overflow-hidden px-1.5 text-[9px] font-medium transition-opacity hover:opacity-80',
                  family.band,
                  b.section.section_id === selectedSectionId ? 'opacity-100 ring-2 ring-inset ring-white/70' : 'opacity-70',
                )}
                style={{ width: `${w}%` }}
                title={`${getSectionMeta(b.section.role).label} · ${b.section.theme || ''} · ${(b.end - b.start).toFixed(1)}s`}
              >
                <span className="truncate text-white">
                  {b.section.theme || getSectionMeta(b.section.role).label}
                </span>
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
          中键 功能盘 · 右键 撤销
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
              ? '空白处暂无盘内能力——添加视频块 / 字卡 / AIGC 图 / 补拍清单 随 D2/D5 接入'
              : undefined
          }
          onClose={() => setDial(null)}
        />
      )}

      {undoToast && (
        <div
          className="pointer-events-none fixed z-50 -translate-x-1/2 -translate-y-full rounded-md bg-zinc-900/90 px-2 py-1 text-[11px] font-medium text-white shadow-md"
          style={{ left: undoToast.x, top: undoToast.y - 8 }}
        >
          {undoToast.text}
        </div>
      )}
    </div>
  )
}
