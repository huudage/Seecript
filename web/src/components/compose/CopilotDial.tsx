import { useEffect } from 'react'
import { cn } from '@/lib/utils'

/**
 * 功能盘（PRD-v2 §5.2-F6 · Epic-5 US-5.1）——鼠标跟随的锚定动作盘。
 *
 * 盘 = tool 白名单的可视化：只列当前锚点已接入的能力，未接入的不出现
 * （「盘上没有的能力就做不了」）。右键唤出 / 重锚定；左键空白、Esc、
 * 盘内再右键、点击动作后关闭。撤销在画布工具条。
 *
 * 动作分两组着色：AI 动作（先 diff 预览确认，US-5.2）与结构动作
 * （即时生效 + 撤销兜底，F13）。
 */

export type DialActionGroup = 'ai' | 'structure'

export interface DialAction {
  id: string
  label: string
  group: DialActionGroup
  run: () => void
}

interface Props {
  x: number
  y: number
  /** 盘心锚点标识（段落 / 第 N 镜 / 连线 / 画布）。 */
  anchorLabel: string
  actions: DialAction[]
  /** 盘下提示（如空白锚点说明为何无动作）。 */
  hint?: string
  onClose: () => void
}

const RADIUS = 76
const HALF = RADIUS + 40

export function CopilotDial({ x, y, anchorLabel, actions, hint, onClose }: Props) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  // 盘心对齐光标，但整体不溢出视口
  const cx = Math.min(Math.max(x, HALF), Math.max(window.innerWidth - HALF, HALF))
  const cy = Math.min(Math.max(y, HALF), Math.max(window.innerHeight - HALF, HALF))

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

      {actions.map((act, i) => {
        const angle = -Math.PI / 2 + (i * 2 * Math.PI) / Math.max(actions.length, 1)
        const left = RADIUS * Math.cos(angle)
        const top = RADIUS * Math.sin(angle)
        return (
          <button
            key={act.id}
            type="button"
            onClick={() => {
              act.run()
              onClose()
            }}
            className={cn(
              'absolute flex h-14 w-14 -translate-x-1/2 -translate-y-1/2 flex-col items-center justify-center rounded-full border px-1 text-center shadow-md transition-transform hover:scale-105 active:scale-95',
              act.group === 'ai'
                ? 'border-primary/50 bg-primary/10 text-primary hover:bg-primary/20'
                : 'border-border bg-card text-foreground hover:bg-secondary',
            )}
            style={{ left, top }}
          >
            <span className="w-full truncate text-[10px] font-semibold leading-tight">{act.label}</span>
            <span
              className={cn(
                'text-[8px] leading-tight',
                act.group === 'ai' ? 'text-primary/70' : 'text-muted-foreground',
              )}
            >
              {act.group === 'ai' ? 'AI · diff' : '结构'}
            </span>
          </button>
        )
      })}

      {hint && (
        <div className="absolute top-[104px] w-56 -translate-x-1/2 rounded-md border border-border bg-card/95 px-2 py-1.5 text-center text-[9px] leading-relaxed text-muted-foreground shadow-md">
          {hint}
        </div>
      )}
    </div>
  )
}
