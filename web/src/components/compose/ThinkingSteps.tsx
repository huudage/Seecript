import { useEffect, useState } from 'react'

/**
 * 思考链：staggered fade-in 列表，每条 ~600ms 入场。
 *
 * 字节系增强：
 * - 无步骤时显示渐变旋转圆环 + 跳动点
 * - 有步骤时显示「AI 正在分析…」提示
 */
export function ThinkingSteps({
  steps,
  animated,
}: {
  steps: string[]
  animated?: boolean
}) {
  const [revealed, setRevealed] = useState(animated ? 0 : steps.length)
  useEffect(() => {
    if (!animated) {
      setRevealed(steps.length)
      return
    }
    setRevealed(0)
    if (steps.length === 0) return
    let i = 0
    const handle = window.setInterval(() => {
      i += 1
      setRevealed(i)
      if (i >= steps.length) window.clearInterval(handle)
    }, 600)
    return () => window.clearInterval(handle)
  }, [steps, animated])

  if (steps.length === 0) {
    return (
      <div className="flex flex-col items-center gap-3 py-4">
        {/* 渐变圆环 spinner */}
        <svg className="h-8 w-8 animate-spin" viewBox="0 0 32 32" fill="none">
          <defs>
            <linearGradient id="spinner-grad" x1="0" y1="0" x2="1" y2="1">
              <stop offset="0%" stopColor="hsl(262 83% 58%)" />
              <stop offset="100%" stopColor="hsl(262 83% 58% / 0.15)" />
            </linearGradient>
          </defs>
          <circle cx="16" cy="16" r="13" stroke="hsl(240 6% 90%)" strokeWidth="3" />
          <circle
            cx="16" cy="16" r="13"
            stroke="url(#spinner-grad)"
            strokeWidth="3"
            strokeLinecap="round"
            strokeDasharray="60 100"
          />
        </svg>
        <p className="text-xs text-muted-foreground">
          AI 正在思考
          <span className="dot-bounce-0 inline-block ml-0.5">.</span>
          <span className="dot-bounce-1 inline-block ml-0.5">.</span>
          <span className="dot-bounce-2 inline-block ml-0.5">.</span>
        </p>
      </div>
    )
  }
  return (
    <div>
      <p className="mb-2 text-xs text-muted-foreground">
        AI 正在分析
        <span className="dot-bounce-0 inline-block ml-0.5">.</span>
        <span className="dot-bounce-1 inline-block ml-0.5">.</span>
        <span className="dot-bounce-2 inline-block ml-0.5">.</span>
      </p>
      <ol className="space-y-1 text-xs">
        {steps.slice(0, revealed).map((s, i) => (
          <li
            key={`${i}-${s.slice(0, 8)}`}
            className="flex items-start gap-2 rounded bg-secondary/50 px-2 py-1 text-foreground transition-opacity"
          >
            <span className="mt-px flex h-4 w-4 flex-shrink-0 items-center justify-center rounded-full bg-primary/20 text-xs font-mono text-primary">
              {i + 1}
            </span>
            <span className="flex-1">{s}</span>
          </li>
        ))}
      </ol>
    </div>
  )
}