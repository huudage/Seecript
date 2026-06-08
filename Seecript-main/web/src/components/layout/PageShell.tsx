import { cn } from '@/lib/utils'

interface PageShellProps {
  title: string
  subtitle?: string
  children: React.ReactNode
  className?: string
}

/** 5 个业务页共用的内容容器：标题 + 副标题 + 主区。 */
export function PageShell({ title, subtitle, children, className }: PageShellProps) {
  return (
    <section className={cn('mx-auto w-full max-w-screen-2xl px-6 py-8', className)}>
      <header className="mb-6">
        <h1 className="text-2xl font-semibold tracking-tight">{title}</h1>
        {subtitle && <p className="mt-1 text-sm text-muted-foreground">{subtitle}</p>}
      </header>
      {children}
    </section>
  )
}

interface PlaceholderCardProps {
  step: string
  description: string
}

/** 阶段 1 占位卡：写明下阶段会接的能力，避免空白页让人误以为坏了。 */
export function PlaceholderCard({ step, description }: PlaceholderCardProps) {
  return (
    <div className="rounded-lg border border-dashed border-border bg-card p-8">
      <div className="text-xs font-medium uppercase tracking-wider text-muted-foreground">
        {step}
      </div>
      <p className="mt-2 text-sm leading-relaxed text-foreground">{description}</p>
    </div>
  )
}
