import { useMemo } from 'react'
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

import type { AdaptedSection, Gap, SampleManifest, Scene, SectionRole } from '@/types/schemas'
import type { Plan } from '@/types/schemas'
import { getSectionMeta } from '@/lib/sections'
import { cn } from '@/lib/utils'

/**
 * 结构对位图（R2 改版 + 双样例 + 段色连线）：
 *
 * - 支持 1 个或 2 个样例 manifest。
 *   1 个：左=样例 / 右=新方案。
 *   2 个：左=样例1 / 中=新方案 / 右=样例2，两侧分别向中间连线。
 * - 边色按"adapted 段的 order"挑 HSL 调色板里的颜色——每段一种色，
 *   两侧样例命中该段的连线、以及新方案节点的 accent，都用同一种色，
 *   方便顺着颜色把"样例1 第 N 段 → 新方案 第 M 段 → 样例2 第 K 段"串起来。
 * - 没有 manifest 时退化为纯 adapted 列表（避免 panel 空白）。
 */

// adapted section -> color，按 order 取下面调色板循环（足够覆盖 8~10 段长视频）。
const SECTION_PALETTE = [
  '#3b82f6', // blue-500
  '#10b981', // emerald-500
  '#f59e0b', // amber-500
  '#ec4899', // pink-500
  '#8b5cf6', // violet-500
  '#06b6d4', // cyan-500
  '#f97316', // orange-500
  '#84cc16', // lime-500
] as const

const NEW_COLOR = '#10b981'      // 仅"新方案独有段落"用——但优先 palette 取色
const ORPHAN_COLOR = '#94a3b8'   // 样例独有段（未沿用）

type RelationKind = 'hit' | 'new' | 'orphan'

const RELATION_LABEL: Record<RelationKind, string> = {
  hit: '命中',
  new: '新增段落',
  orphan: '未沿用',
}

interface SectionNodeData {
  label: string
  side: 'sample-left' | 'adapted' | 'sample-right'
  section: SectionRole
  theme: string
  meta: string
  tooltip: string
  relation: RelationKind
  accentColor: string
  [key: string]: unknown
}

const lighten = (hex: string, alpha = 0.12) => {
  // 浅色 bg：颜色 + 12% opacity，不破坏对比度。
  const h = hex.replace('#', '')
  const r = parseInt(h.slice(0, 2), 16)
  const g = parseInt(h.slice(2, 4), 16)
  const b = parseInt(h.slice(4, 6), 16)
  return `rgba(${r}, ${g}, ${b}, ${alpha})`
}

function SectionNode({ data }: NodeProps<Node<SectionNodeData>>) {
  const meta = getSectionMeta(data.section)
  const color = data.accentColor
  const sideLabel =
    data.side === 'sample-left' ? '样例1' : data.side === 'sample-right' ? '样例2' : '新方案'
  const isCenter = data.side === 'adapted'
  return (
    <div
      className="rounded-md border-2 px-3 py-2 shadow-sm"
      style={{
        background: lighten(color, 0.08),
        borderColor: color,
        width: 220,
      }}
      title={data.tooltip}
    >
      <div className="flex items-center justify-between gap-2">
        <div className="text-[11px] font-bold uppercase tracking-wider" style={{ color }}>
          {sideLabel} · {meta.label}
        </div>
        <span
          className="rounded px-1.5 py-0.5 text-[10px] font-medium text-white"
          style={{ background: color }}
        >
          {RELATION_LABEL[data.relation]}
        </span>
      </div>
      <div className="mt-0.5 line-clamp-1 text-sm font-semibold text-slate-800">{data.theme}</div>
      <div className="text-[11px] text-slate-500">{data.meta}</div>
      {/* 左侧样例：右出；中间新方案：左入 + 右出；右侧样例：左入 */}
      {(data.side === 'sample-left' || isCenter) && (
        <Handle
          type="source"
          position={Position.Right}
          style={{ background: color, opacity: data.relation === 'hit' ? 1 : 0 }}
        />
      )}
      {(data.side === 'sample-right' || isCenter) && (
        <Handle
          type="target"
          position={Position.Left}
          style={{ background: color, opacity: data.relation === 'hit' ? 1 : 0 }}
        />
      )}
    </div>
  )
}

const nodeTypes = { sectionNode: SectionNode } as const

function deriveAdaptedFromMainTrack(plan: Plan): AdaptedSection[] {
  const groups = new Map<SectionRole, Scene[]>()
  plan.main_track.forEach((sc) => {
    const arr = groups.get(sc.section) ?? []
    arr.push(sc)
    groups.set(sc.section, arr)
  })
  const result: AdaptedSection[] = []
  let order = 0
  groups.forEach((scenes, role) => {
    const totalDuration = scenes.reduce((acc, s) => acc + s.duration, 0)
    result.push({
      section_id: `derived-${role}-${order}`,
      role,
      theme: scenes[0]?.narration?.slice(0, 24) || role,
      content_description: scenes.map((s) => s.narration ?? '').filter(Boolean).join(' / '),
      source_section_indices: [],
      source_shot_indices: [],
      order: order++,
      duration_seconds: totalDuration,
      shots: [],
    })
  })
  return result
}

interface GraphResult {
  nodes: Node[]
  edges: Edge[]
  fallback: boolean
  dual: boolean
}

function buildGraph(manifests: SampleManifest[], plan: Plan | null, _gaps: Gap[]): GraphResult {
  if (!plan || manifests.length === 0) {
    return { nodes: [] as Node[], edges: [] as Edge[], fallback: false, dual: false }
  }

  const dual = manifests.length >= 2
  const leftManifest = manifests[0] ?? null
  const rightManifest = manifests[1] ?? null

  // 三列栅格：单样例时只用左+中两列。
  const LEFT_X = 40
  const CENTER_X = 320
  const RIGHT_X = 620
  const ROW_H = 110

  const adapted =
    plan.adapted_sections && plan.adapted_sections.length > 0
      ? plan.adapted_sections
      : deriveAdaptedFromMainTrack(plan)
  const fallback = !plan.adapted_sections || plan.adapted_sections.length === 0
  const adaptedSorted = adapted.slice().sort((a, b) => a.order - b.order)

  // 为每个 adapted role 指派一个 palette 色——同 role 在多个样例里同色。
  const adaptedRoleColor = new Map<SectionRole, string>()
  adaptedSorted.forEach((a, i) => {
    if (!adaptedRoleColor.has(a.role)) {
      adaptedRoleColor.set(a.role, SECTION_PALETTE[i % SECTION_PALETTE.length])
    }
  })

  const adaptedRoles = new Set<SectionRole>(adaptedSorted.map((a) => a.role))
  const nodes: Node[] = []
  const edges: Edge[] = []

  // 左列样例
  if (leftManifest) {
    leftManifest.sections.forEach((sec, i) => {
      const hit = adaptedRoles.has(sec.role)
      const relation: RelationKind = hit ? 'hit' : 'orphan'
      const color = hit ? (adaptedRoleColor.get(sec.role) ?? ORPHAN_COLOR) : ORPHAN_COLOR
      nodes.push({
        id: `left-${i}`,
        type: 'sectionNode',
        position: { x: LEFT_X, y: i * ROW_H },
        data: {
          side: 'sample-left',
          label: `${sec.start.toFixed(1)}–${sec.end.toFixed(1)}s`,
          section: sec.role,
          theme: sec.theme,
          meta: `${(sec.end - sec.start).toFixed(1)}s · ${sec.shot_indices.length} 镜头`,
          tooltip: `${sec.theme} · ${sec.summary}`,
          relation,
          accentColor: color,
        } satisfies SectionNodeData,
        draggable: false,
        selectable: false,
      })
    })
  }

  // 中列：新方案
  const scenesByRole = new Map<SectionRole, Scene[]>()
  plan.main_track.forEach((sc) => {
    const roleArr = scenesByRole.get(sc.section) ?? []
    roleArr.push(sc)
    scenesByRole.set(sc.section, roleArr)
  })

  const leftFirstIdxByRole = new Map<SectionRole, number>()
  leftManifest?.sections.forEach((sec, i) => {
    if (!leftFirstIdxByRole.has(sec.role)) leftFirstIdxByRole.set(sec.role, i)
  })
  const rightFirstIdxByRole = new Map<SectionRole, number>()
  rightManifest?.sections.forEach((sec, i) => {
    if (!rightFirstIdxByRole.has(sec.role)) rightFirstIdxByRole.set(sec.role, i)
  })
  const leftRoles = new Set<SectionRole>(leftManifest?.sections.map((s) => s.role) ?? [])
  const rightRoles = new Set<SectionRole>(rightManifest?.sections.map((s) => s.role) ?? [])

  adaptedSorted.forEach((a, i) => {
    const scList = scenesByRole.get(a.role) ?? []
    const sceneCount = scList.length || a.source_shot_indices.length
    const hitAnywhere = leftRoles.has(a.role) || rightRoles.has(a.role)
    const relation: RelationKind = hitAnywhere ? 'hit' : 'new'
    const color = adaptedRoleColor.get(a.role) ?? NEW_COLOR
    nodes.push({
      id: `adapted-${a.section_id}`,
      type: 'sectionNode',
      position: { x: CENTER_X, y: i * ROW_H },
      data: {
        side: 'adapted',
        label: a.section_id,
        section: a.role,
        theme: a.theme,
        meta: `${a.duration_seconds.toFixed(1)}s · ${sceneCount} 镜头`,
        tooltip: a.content_description || a.theme,
        relation,
        accentColor: color,
      } satisfies SectionNodeData,
      draggable: false,
      selectable: false,
    })

    // 左侧样例 → 新方案
    const leftIdx = leftFirstIdxByRole.get(a.role)
    if (leftIdx !== undefined) {
      edges.push({
        id: `e-left-${leftIdx}-to-${a.section_id}`,
        source: `left-${leftIdx}`,
        target: `adapted-${a.section_id}`,
        type: 'smoothstep',
        style: { stroke: color, strokeWidth: 2.5 },
        interactionWidth: 0,
      })
    }
    // 新方案 → 右侧样例
    const rightIdx = rightFirstIdxByRole.get(a.role)
    if (rightIdx !== undefined) {
      edges.push({
        id: `e-${a.section_id}-to-right-${rightIdx}`,
        source: `adapted-${a.section_id}`,
        target: `right-${rightIdx}`,
        type: 'smoothstep',
        style: { stroke: color, strokeWidth: 2.5 },
        interactionWidth: 0,
      })
    }
  })

  // 右列样例
  if (rightManifest) {
    rightManifest.sections.forEach((sec, i) => {
      const hit = adaptedRoles.has(sec.role)
      const relation: RelationKind = hit ? 'hit' : 'orphan'
      const color = hit ? (adaptedRoleColor.get(sec.role) ?? ORPHAN_COLOR) : ORPHAN_COLOR
      nodes.push({
        id: `right-${i}`,
        type: 'sectionNode',
        position: { x: RIGHT_X, y: i * ROW_H },
        data: {
          side: 'sample-right',
          label: `${sec.start.toFixed(1)}–${sec.end.toFixed(1)}s`,
          section: sec.role,
          theme: sec.theme,
          meta: `${(sec.end - sec.start).toFixed(1)}s · ${sec.shot_indices.length} 镜头`,
          tooltip: `${sec.theme} · ${sec.summary}`,
          relation,
          accentColor: color,
        } satisfies SectionNodeData,
        draggable: false,
        selectable: false,
      })
    })
  }

  return { nodes, edges, fallback, dual }
}

/**
 * 结构对照——只读栅格视图。
 *
 * 兼容 props：
 *   - `manifest`（单样例，老调用方）
 *   - `manifests`（数组，新双样例调用方），优先级更高
 */
export function StructureMapPanel({
  manifest,
  manifests,
  plan,
  gaps,
  className,
}: {
  manifest?: SampleManifest | null
  manifests?: SampleManifest[]
  plan: Plan | null
  gaps: Gap[]
  className?: string
}) {
  const resolvedManifests = useMemo<SampleManifest[]>(() => {
    if (manifests && manifests.length > 0) return manifests
    return manifest ? [manifest] : []
  }, [manifest, manifests])

  const { nodes, edges, fallback, dual } = useMemo(
    () => buildGraph(resolvedManifests, plan, gaps),
    [resolvedManifests, plan, gaps],
  )

  if (resolvedManifests.length === 0 || !plan) {
    return (
      <div className={cn('rounded-lg border border-dashed border-border bg-card p-6 text-sm text-muted-foreground', className)}>
        还没法对照——先把样例拆解好，再生成方案，结构图就会出现在这里。
      </div>
    )
  }

  return (
    <div className={cn('flex h-full flex-col gap-2', className)}>
      <div className="flex flex-wrap items-center gap-3 text-xs text-muted-foreground">
        <span className="inline-flex items-center gap-1">
          <span className="text-[10px] font-medium text-foreground">段色：</span>
          {Array.from({ length: Math.min(plan.adapted_sections?.length ?? 0, SECTION_PALETTE.length) }).map((_, i) => (
            <span
              key={i}
              className="inline-block h-3 w-4 rounded"
              style={{ background: SECTION_PALETTE[i % SECTION_PALETTE.length] }}
              title={`第 ${i + 1} 段`}
            />
          ))}
          <span className="ml-1 text-[10px] opacity-70">每段一色 · 连线同色串起两侧</span>
        </span>
        <span className="inline-flex items-center gap-1">
          <span
            className="inline-block rounded border-2 px-1.5 text-[10px] font-medium text-white"
            style={{ background: ORPHAN_COLOR, borderColor: ORPHAN_COLOR }}
          >
            未沿用
          </span>
          <span className="text-[10px] opacity-70">样例独有 · 不被采用</span>
        </span>
        {fallback && (
          <span className="text-[10px] text-amber-700">
            · 该方案未携带 adapted_sections，按 main_track 兜底展示
          </span>
        )}
        <span className="ml-auto text-[10px] opacity-70">
          只读栅格 · {dual ? '左样例1 / 中新方案 / 右样例2' : '左样例 / 右新方案'}
        </span>
      </div>
      <div className="flex-1 min-h-0 w-full overflow-hidden rounded-lg border border-border bg-slate-50">
        <ReactFlowProvider>
          <ReactFlow
            nodes={nodes}
            edges={edges}
            nodeTypes={nodeTypes}
            fitView
            fitViewOptions={{ padding: 0.15 }}
            proOptions={{ hideAttribution: true }}
            nodesDraggable={false}
            nodesConnectable={false}
            elementsSelectable={false}
            panOnDrag={false}
            zoomOnScroll={false}
            zoomOnPinch={false}
            zoomOnDoubleClick={false}
            preventScrolling={false}
          >
            <Background gap={16} size={1} />
          </ReactFlow>
        </ReactFlowProvider>
      </div>
    </div>
  )
}
