import { PageShell, PlaceholderCard } from '@/components/layout/PageShell'

export default function MigratePage() {
  return (
    <PageShell
      title="迁移映射"
      subtitle="React Flow 三栏：样例槽位 ←→ 新方案分镜，缺口红虚线，补全绿标。"
    >
      <PlaceholderCard
        step="模块 4 · #19"
        description="左侧样例槽位列表 / 右侧新方案分镜列表 / 中间 @xyflow/react 渲染连线；点击边线可弹出 inspector 调整匹配。"
      />
    </PageShell>
  )
}
