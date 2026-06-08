import { useEffect, useRef, useState } from 'react'

import { cn } from '@/lib/utils'

const STORAGE_KEY = 'seecript.cmdk_fab_pos.v1'
const DRAG_THRESHOLD_PX = 4

interface StoredPos {
  // 用 right/bottom（相对右下角）持久化，浏览器宽度变化时贴边不会跑出可视区。
  right: number
  bottom: number
}

const DEFAULT_POS: StoredPos = { right: 24, bottom: 24 }

const loadPos = (): StoredPos => {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    if (!raw) return DEFAULT_POS
    const parsed = JSON.parse(raw) as Partial<StoredPos>
    if (typeof parsed.right === 'number' && typeof parsed.bottom === 'number') {
      return { right: parsed.right, bottom: parsed.bottom }
    }
  } catch {
    // ignore — fall through to default
  }
  return DEFAULT_POS
}

const clampToViewport = (p: StoredPos, fab: HTMLElement): StoredPos => {
  const rect = fab.getBoundingClientRect()
  const maxRight = window.innerWidth - rect.width - 4
  const maxBottom = window.innerHeight - rect.height - 4
  return {
    right: Math.max(4, Math.min(p.right, maxRight)),
    bottom: Math.max(4, Math.min(p.bottom, maxBottom)),
  }
}

/**
 * ⌘K 浮动入口——支持拖动到任何位置，位置写 localStorage。
 *
 * 用户的 PlanPlayer / 右下面板会把默认右下角的按钮挡住，所以让它可拖动；
 * 同时拖动 ≤ 4px 仍视为点击（兼容 trackpad 微抖），不会误开命令面板。
 */
export function DraggableCommandFab({
  onClick,
  label = '对话编辑小助手',
  badge = '⌘K',
  title = '对话编辑小助手（⌘K）',
}: {
  onClick: () => void
  label?: string
  badge?: string
  title?: string
}) {
  const btnRef = useRef<HTMLButtonElement | null>(null)
  const [pos, setPos] = useState<StoredPos>(loadPos)
  const dragRef = useRef<{
    startX: number
    startY: number
    startRight: number
    startBottom: number
    moved: boolean
  } | null>(null)

  // 窗口尺寸变化时把按钮拉回可视区，避免缩窗后被挤出去。
  useEffect(() => {
    const onResize = () => {
      if (!btnRef.current) return
      setPos((prev) => clampToViewport(prev, btnRef.current!))
    }
    window.addEventListener('resize', onResize)
    return () => window.removeEventListener('resize', onResize)
  }, [])

  const onPointerDown = (e: React.PointerEvent<HTMLButtonElement>) => {
    if (!btnRef.current) return
    btnRef.current.setPointerCapture(e.pointerId)
    dragRef.current = {
      startX: e.clientX,
      startY: e.clientY,
      startRight: pos.right,
      startBottom: pos.bottom,
      moved: false,
    }
  }

  const onPointerMove = (e: React.PointerEvent<HTMLButtonElement>) => {
    const drag = dragRef.current
    if (!drag || !btnRef.current) return
    const dx = e.clientX - drag.startX
    const dy = e.clientY - drag.startY
    if (!drag.moved && Math.hypot(dx, dy) < DRAG_THRESHOLD_PX) return
    drag.moved = true
    const next = clampToViewport(
      {
        right: drag.startRight - dx,
        bottom: drag.startBottom - dy,
      },
      btnRef.current,
    )
    setPos(next)
  }

  const onPointerUp = (e: React.PointerEvent<HTMLButtonElement>) => {
    const drag = dragRef.current
    if (!drag) return
    try {
      btnRef.current?.releasePointerCapture(e.pointerId)
    } catch {
      // ignore
    }
    if (drag.moved) {
      try {
        localStorage.setItem(STORAGE_KEY, JSON.stringify(pos))
      } catch {
        // localStorage 满或被禁——位置仍生效到刷新前，不需要中断流程
      }
    } else {
      onClick()
    }
    dragRef.current = null
  }

  return (
    <button
      ref={btnRef}
      type="button"
      title={title}
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={onPointerUp}
      onClick={(e) => {
        // 拖动结束在 onPointerUp 里走点击；这里只拦截 React 合成 click，避免双触发。
        e.preventDefault()
      }}
      style={{ right: `${pos.right}px`, bottom: `${pos.bottom}px` }}
      className={cn(
        'fixed z-[120] flex cursor-grab items-center gap-1.5 rounded-full border bg-card/95 px-3 py-2 text-xs shadow-lg backdrop-blur',
        'touch-none select-none hover:bg-secondary active:cursor-grabbing',
      )}
    >
      <span className="rounded bg-primary/10 px-1.5 py-0.5 font-mono text-xs text-primary">{badge}</span>
      <span>{label}</span>
    </button>
  )
}
