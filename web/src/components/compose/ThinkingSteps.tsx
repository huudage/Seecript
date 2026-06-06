import { useEffect, useState } from 'react'

/**
 * 思考链：staggered fade-in 列表，每条 ~600ms 入场。
 *
 * 共享组件——FillAigcPanel / FillCopyPanel 的 analyzing 阶段都用这个，
 * 保证 agent 化 UI 的视觉语言一致。
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
      <p className="text-[11px] italic text-muted-foreground">
        正在调用大模型，请稍候…
      </p>
    )
  }
  return (
    <ol className="space-y-1 text-[11px]">
      {steps.slice(0, revealed).map((s, i) => (
        <li
          key={`${i}-${s.slice(0, 8)}`}
          className="flex items-start gap-2 rounded bg-secondary/50 px-2 py-1 text-foreground transition-opacity"
        >
          <span className="mt-px flex h-4 w-4 flex-shrink-0 items-center justify-center rounded-full bg-primary/20 text-[9px] font-mono text-primary">
            {i + 1}
          </span>
          <span className="flex-1">{s}</span>
        </li>
      ))}
    </ol>
  )
}
