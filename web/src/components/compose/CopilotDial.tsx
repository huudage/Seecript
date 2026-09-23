import { useEffect, useRef, useState, type ReactNode } from 'react'
import { cn } from '@/lib/utils'

/**
 * 功能盘（PRD-v2 §5.2-F6）——鼠标跟随的锚定动作盘。
 *
 * 右键唤出 / 重锚定；左键空白、Esc、盘内再右键关闭。
 * 「生成」项散落在左 / 右 / 下三个方向，悬停展开表单，指针移进表单不收起。
 * 其余动作点击后关闭。自进化对话（自然语言改片）与结构动作走点击。
 */

export type DialActionGroup = 'ai' | 'structure'

export interface DialAction {
  id: string
  label: string
  group: DialActionGroup
  run: () => void
  /** 悬停展开，不因点击立刻关掉盘。生成类用这个。 */
  hover?: boolean
  /** 悬停层标题，按钮本身仍写「生成」。 */
  hoverTitle?: string
  /** 固定方位（弧度）。0 为右，PI/2 为下，PI 为左。 */
  angle?: number
}

interface Props {
  x: number
  y: number
  /** 盘心锚点标识（段落 / 第 N 镜 / 连线 / 画布）。 */
  anchorLabel: string
  actions: DialAction[]
  /** 盘下提示（如空白锚点说明为何无动作）。 */
  hint?: string
  /** 当前悬停的生成项对应的表单。 */
  hoverPanel?: ReactNode
  onHoverAction?: (actionId: string | null) => void
  onClose: () => void
}

const RADIUS = 92
const HALF = RADIUS + 56

function angularDistance(a: number, b: number): number {
  return Math.abs(Math.atan2(Math.sin(a - b), Math.cos(a - b)))
}

function placeActions(actions: DialAction[]): { action: DialAction; angle: number }[] {
  const fixed = actions.filter((action) => action.angle != null)
  const rest = actions.filter((action) => action.angle == null)
  const used = fixed.map((action) => action.angle as number)
  const restAngles: number[] = []
  let cursor = -Math.PI * 0.82
  for (let i = 0; i < rest.length; i += 1) {
    let guard = 0
    while (used.some((angle) => angularDistance(cursor, angle) < 0.55) && guard < 24) {
      cursor += 0.42
      guard += 1
    }
    restAngles.push(cursor)
    used.push(cursor)
    cursor += rest.length > 1 ? (Math.PI * 0.7) / rest.length : 0.5
  }
  let restIndex = 0
  return actions.map((action) => {
    if (action.angle != null) return { action, angle: action.angle }
    const angle = restAngles[restIndex] ?? -Math.PI / 2
    restIndex += 1
    return { action, angle }
  })
}

export function CopilotDial({
  x,
  y,
  anchorLabel,
  actions,
  hint,
  hoverPanel,
  onHoverAction,
  onClose,
}: Props) {
  const [openHoverId, setOpenHoverId] = useState<string | null>(null)
  const leaveTimer = useRef<number | null>(null)

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  const clearLeave = () => {
    if (leaveTimer.current != null) {
      window.clearTimeout(leaveTimer.current)
      leaveTimer.current = null
    }
  }

  const showHover = (actionId: string) => {
    clearLeave()
    setOpenHoverId(actionId)
    onHoverAction?.(actionId)
  }

  const scheduleHide = () => {
    clearLeave()
    leaveTimer.current = window.setTimeout(() => {
      setOpenHoverId(null)
      onHoverAction?.(null)
    }, 220)
  }

  const cx = Math.min(Math.max(x, HALF), Math.max(window.innerWidth - HALF, HALF))
  const cy = Math.min(Math.max(y, HALF), Math.max(window.innerHeight - HALF, HALF))
  const placed = placeActions(actions)
  const open = placed.find((item) => item.action.id === openHoverId && item.action.hover)

  return (
    <div data-copilot-dial className="fixed z-50" style={{ left: cx, top: cy }}>
      <button
        type="button"
        onClick={onClose}
        title="点击关闭功能盘（Esc）"
        className="absolute flex h-16 w-16 -translate-x-1/2 -translate-y-1/2 cursor-pointer flex-col items-center justify-center rounded-full border border-border bg-card px-1 text-center shadow-lg"
      >
        <span className="w-full truncate text-[10px] font-semibold leading-tight">{anchorLabel}</span>
        <span className="mt-0.5 text-[8px] text-muted-foreground">关闭</span>
      </button>

      {placed.map(({ action, angle }) => {
        const left = RADIUS * Math.cos(angle)
        const top = RADIUS * Math.sin(angle)
        return (
          <button
            key={action.id}
            type="button"
            onMouseEnter={() => {
              if (action.hover) showHover(action.id)
            }}
            onMouseLeave={() => {
              if (action.hover) scheduleHide()
            }}
            onClick={() => {
              if (action.hover) {
                action.run()
                showHover(action.id)
                return
              }
              action.run()
              onClose()
            }}
            className={cn(
              'absolute flex h-14 w-14 -translate-x-1/2 -translate-y-1/2 flex-col items-center justify-center rounded-full border px-1 text-center shadow-md transition-transform hover:scale-105 active:scale-95',
              action.group === 'ai'
                ? 'border-primary/50 bg-primary/10 text-primary hover:bg-primary/20'
                : 'border-border bg-card text-foreground hover:bg-secondary',
              openHoverId === action.id && 'scale-105 ring-2 ring-primary/40',
            )}
            style={{ left, top }}
          >
            <span className="w-full truncate text-[10px] font-semibold leading-tight">{action.label}</span>
            <span
              className={cn(
                'text-[8px] leading-tight',
                action.group === 'ai' ? 'text-primary/70' : 'text-muted-foreground',
              )}
            >
              {action.hover ? '悬停' : action.group === 'ai' ? 'AI · diff' : '结构'}
            </span>
          </button>
        )
      })}

      {open && hoverPanel && (
        <div
          className="absolute z-10 w-[360px] -translate-x-1/2 -translate-y-1/2"
          style={{
            left: (RADIUS + 210) * Math.cos(open.angle),
            top: (RADIUS + 168) * Math.sin(open.angle),
          }}
          onMouseEnter={clearLeave}
          onMouseLeave={scheduleHide}
        >
          <div className="max-h-[70vh] overflow-auto rounded-lg border border-border bg-card p-3 text-left shadow-xl">
            <div className="mb-2 text-[11px] font-semibold text-foreground">
              生成 · {open.action.hoverTitle}
            </div>
            {hoverPanel}
          </div>
        </div>
      )}

      {hint && !open && (
        <div className="absolute top-[120px] w-56 -translate-x-1/2 rounded-md border border-border bg-card/95 px-2 py-1.5 text-center text-[9px] leading-relaxed text-muted-foreground shadow-md">
          {hint}
        </div>
      )}
    </div>
  )
}
