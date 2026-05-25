import { PageShell, PlaceholderCard } from '@/components/layout/PageShell'

export default function DecomposePage() {
  return (
    <PageShell
      title="样例拆解"
      subtitle="PySceneDetect 镜头切分 · librosa BGM 能量曲线 · ASR 口播 · VLM 帧打标 · LLM 段落结构。"
    >
      <PlaceholderCard
        step="模块 2 · #17"
        description="进入选中的样例 → 触发 /api/decompose；SSE 推每一步进度（镜头切分 → 音频分析 → ASR → VLM → 段落结构）。完成后渲染镜头时间轴 + 节奏曲线 + Hook/Body/CTA 分段。"
      />
    </PageShell>
  )
}
