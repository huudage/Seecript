import { PageShell, PlaceholderCard } from '@/components/layout/PageShell'

export default function ComposePage() {
  return (
    <PageShell
      title="新素材 / 缺口补全"
      subtitle="上传自有素材 → VLM 打标 → 槽位匹配 → 缺口识别 → 三种补全（结构重排 / 文案补全 / Seedream AIGC）。"
    >
      <PlaceholderCard
        step="模块 3 · #18"
        description="左栏拖拽上传区调 /api/material/upload；中栏槽位匹配状态（✅⚠️❌）；右栏缺口补全动作面板。"
      />
    </PageShell>
  )
}
