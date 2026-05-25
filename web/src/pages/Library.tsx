import { PageShell, PlaceholderCard } from '@/components/layout/PageShell'

export default function LibraryPage() {
  return (
    <PageShell
      title="素材库"
      subtitle="3 个内置样例（营销 / 剪辑 / Motion Graph），点击进入样例拆解。"
    >
      <PlaceholderCard
        step="模块 1 · #16"
        description="后端 GET /api/library 落地后，这里渲染 3 张样例卡片：封面、时长、镜头数、场景标签。"
      />
    </PageShell>
  )
}
