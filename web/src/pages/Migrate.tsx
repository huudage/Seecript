import { useMemo } from 'react'
import {
  Background,
  Controls,
  Handle,
  Position,
  ReactFlow,
  ReactFlowProvider,
  type Edge,
  type Node,
  type NodeProps,
} from '@xyflow/react'
import '@xyflow/react/dist/style.css'
import { Link } from 'react-router-dom'

import { PageShell } from '@/components/layout/PageShell'
import { usePlanStore } from '@/stores/plan'
import { useSessionStore } from '@/stores/session'
import type { Gap, GapStatus, Scene, SectionKind } from '@/types/schemas'
import { cn } from '@/lib/utils'

const SECTION_LABEL: Record<SectionKind, string> = { hook: 'Hook', body: 'Body', cta: 'CTA' }
const SECTION_COLOR: Record<SectionKind, string> = {
  hook: '#ec4899',
  body: '#0ea5e9',
  cta: '#f59e0b',
}
const STATUS_STROKE: Record<GapStatus, string> = {
  ok: '#10b981',
  warn: '#f59e0b',
  miss: '#ef4444',
}

interface SectionNodeData {
  label: string
  section: SectionKind
  summary: string
  slotCount: number
  [key: string]: unknown
}

interface SceneNodeData {
  label: string
  section: SectionKind
  source: Scene['source']
  source_ref: string
  duration: number
  narration?: string | null
  [key: string]: unknown
}

function SectionNode({ data }: NodeProps<Node<SectionNodeData>>) {
  return (
    <div
      className="rounded-md border-2 px-3 py-2 shadow-sm"
      style={{
        background: '#fff',
        borderColor: SECTION_COLOR[data.section],
        minWidth: 180,
      }}
    >
      <div className="text-xs font-bold uppercase" style={{ color: SECTION_COLOR[data.section] }}>
        样例 · {SECTION_LABEL[data.section]}
      </div>
      <div className="mt-1 text-sm font-medium text-slate-800">{data.label}</div>
      <div className="text-xs text-slate-500">{data.slotCount} 槽位 · {data.summary}</div>
      <Handle type="source" position={Position.Right} style={{ background: SECTION_COLOR[data.section] }} />
    </div>
  )
}

function SceneNode({ data }: NodeProps<Node<SceneNodeData>>) {
  return (
    <div
      className="rounded-md border-2 px-3 py-2 shadow-sm"
      style={{
        background: '#fff',
        borderColor: SECTION_COLOR[data.section],
        minWidth: 200,
      }}
    >
      <div className="text-xs font-medium" style={{ color: SECTION_COLOR[data.section] }}>
        新方案 · {SECTION_LABEL[data.section]} · {data.duration.toFixed(1)}s
      </div>
      <div className="mt-1 text-sm font-medium text-slate-800">{data.label}</div>
      <div className="text-xs text-slate-500">来源：{data.source}</div>
      {data.narration && (
        <div className="mt-1 line-clamp-2 text-[11px] text-slate-600">{data.narration}</div>
      )}
      <Handle type="target" position={Position.Left} style={{ background: SECTION_COLOR[data.section] }} />
    </div>
  )
}

const nodeTypes = { sectionNode: SectionNode, sceneNode: SceneNode } as const

function buildGraph(manifest: ReturnType<typeof useSessionStore.getState>['manifest'], plan: ReturnType<typeof usePlanStore.getState>['plan'], gaps: Gap[]) {
  if (!manifest || !plan) return { nodes: [] as Node[], edges: [] as Edge[] }
  const nodes: Node[] = []
  const edges: Edge[] = []
  const LEFT_X = 50
  const RIGHT_X = 460
  const Y_STEP = 110

  manifest.sections.forEach((sec, i) => {
    nodes.push({
      id: `sec-${sec.kind}`,
      type: 'sectionNode',
      position: { x: LEFT_X, y: i * Y_STEP },
      data: {
        label: `${sec.start.toFixed(1)}–${sec.end.toFixed(1)}s`,
        section: sec.kind,
        summary: sec.summary,
        slotCount: sec.shot_indices.length,
      } satisfies SectionNodeData,
      draggable: true,
    })
  })

  plan.main_track.forEach((scene, i) => {
    nodes.push({
      id: `scene-${scene.scene_id}`,
      type: 'sceneNode',
      position: { x: RIGHT_X, y: i * (Y_STEP * 0.7) },
      data: {
        label: scene.scene_id,
        section: scene.section,
        source: scene.source,
        source_ref: scene.source_ref,
        duration: scene.duration,
        narration: scene.narration,
      } satisfies SceneNodeData,
      draggable: true,
    })
  })

  // 同 section 的 section node → scene node 连线，颜色依据该 section 命中状态
  const gapsBySection = gaps.reduce<Record<string, Gap[]>>((acc, g) => {
    acc[g.section] = acc[g.section] ?? []
    acc[g.section].push(g)
    return acc
  }, {})

  plan.main_track.forEach((scene) => {
    const secGaps = gapsBySection[scene.section] ?? []
    // 该 section 整体最差状态决定连线颜色
    let status: GapStatus = 'ok'
    for (const g of secGaps) {
      if (g.status === 'miss') {
        status = 'miss'
        break
      }
      if (g.status === 'warn') status = 'warn'
    }
    edges.push({
      id: `e-${scene.scene_id}`,
      source: `sec-${scene.section}`,
      target: `scene-${scene.scene_id}`,
      style: {
        stroke: STATUS_STROKE[status],
        strokeWidth: 2,
        strokeDasharray: status === 'miss' ? '6 4' : status === 'warn' ? '3 2' : undefined,
      },
      label: status === 'ok' ? '✅' : status === 'warn' ? '⚠️' : '❌',
      animated: status === 'miss',
    })
  })

  return { nodes, edges }
}

export default function MigratePage() {
  const manifest = useSessionStore((s) => s.manifest)
  const plan = usePlanStore((s) => s.plan)
  const gaps = usePlanStore((s) => s.gaps)

  const { nodes, edges } = useMemo(() => buildGraph(manifest, plan, gaps), [manifest, plan, gaps])

  if (!manifest || !plan) {
    return (
      <PageShell title="迁移映射" subtitle="先完成『拆解 + 缺口识别』。">
        <div className="rounded-lg border border-dashed border-border bg-card p-8 text-sm text-muted-foreground">
          缺少 manifest 或 plan，无法绘制迁移图。
          <Link to={manifest ? '/compose' : '/decompose'} className="ml-2 text-primary underline-offset-4 hover:underline">
            返回上一步 →
          </Link>
        </div>
      </PageShell>
    )
  }

  return (
    <PageShell
      title="迁移映射"
      subtitle={`样例 ${manifest.sample_id} → Plan ${plan.plan_id}，颜色：${legend()}`}
    >
      <div className="mb-3 flex flex-wrap gap-3 text-xs">
        <span className={cn('inline-flex items-center gap-1')}>
          <i className="inline-block h-0.5 w-6 bg-emerald-500" /> ok · 命中
        </span>
        <span className="inline-flex items-center gap-1">
          <i className="inline-block h-0.5 w-6 bg-amber-500" /> warn · 勉强
        </span>
        <span className="inline-flex items-center gap-1">
          <i className="inline-block h-0.5 w-6 bg-rose-500" /> miss · 缺口
        </span>
      </div>
      <div className="h-[640px] w-full rounded-lg border border-border bg-card">
        <ReactFlowProvider>
          <ReactFlow
            nodes={nodes}
            edges={edges}
            nodeTypes={nodeTypes}
            fitView
            proOptions={{ hideAttribution: true }}
          >
            <Background gap={16} size={1} />
            <Controls />
          </ReactFlow>
        </ReactFlowProvider>
      </div>
    </PageShell>
  )
}

function legend() {
  return '绿 = 命中，黄 = 勉强，红虚线 = 缺口'
}
