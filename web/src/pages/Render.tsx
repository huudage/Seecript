import { PageShell, PlaceholderCard } from '@/components/layout/PageShell'

export default function RenderPage() {
  return (
    <PageShell
      title="生成 / 自然语言编辑"
      subtitle="FFmpeg 主轨 + Seedance 首尾帧扩展 + Remotion 包装轨叠加；LLM tool calling 改 Plan + 增量重渲染 + 撤销栈。"
    >
      <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
        <PlaceholderCard
          step="模块 5+6 · #20 #21"
          description="左侧 AB 双版本对比播放器；下方 SSE 进度条；右侧渲染参数（变体 / BGM / 分辨率）。"
        />
        <PlaceholderCard
          step="模块 7 · #22"
          description="底部双轨标注式编辑器：选中一段时间 + 输入「把这段配音换成更口语化的版本」→ LLM tool calling 改 Plan → 增量重渲染。"
        />
      </div>
    </PageShell>
  )
}
