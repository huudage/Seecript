import type { BGMAnalysis } from '@/types/schemas'
import { cn } from '@/lib/utils'

const ENERGY_SHAPE_SHORT: Record<BGMAnalysis['energy_shape'], string> = {
  flat: '全程平稳',
  single_peak: '单峰爆发',
  multi_peak: '多峰起伏',
  build_up: '渐强推进',
  wave: '波浪起伏',
}

const ENERGY_SHAPE_HINT: Record<BGMAnalysis['energy_shape'], string> = {
  flat: '没有明显高潮，做底色不抢戏 · 适合科普 / 治愈 / Vlog',
  single_peak: '一条主高潮线 · 适合带 CTA / 卖点对比 / 反转视频',
  multi_peak: '两次以上峰值 · 适合长剧情 / 多卖点串烧',
  build_up: '能量从低到高一直走 · 适合预告 / 蓄势 / 反差揭示',
  wave: '高低反复起伏 · 适合情绪 Vlog / 故事性叙事',
}

const ENERGY_SHAPE_HINT_SAMPLE: Record<BGMAnalysis['energy_shape'], string> = {
  flat: '整段听感平稳——复刻时按均匀节奏走，靠画面/口播变化撑住注意力',
  single_peak: '一次能量爆发——复刻时把最强卖点 / 反转放在这一刻',
  multi_peak: '多次峰值——复刻时按"卖点-钩子-卖点"分段排画面',
  build_up: '能量持续抬升——复刻时画面信息密度也要逐段加码，尾段收 CTA',
  wave: '能量来回起伏——复刻时画面跟着情绪波动切，避免一镜到底',
}

const HIGHLIGHT_KIND_LABEL: Record<BGMAnalysis['climaxes'][number]['kind'], string> = {
  climax: '高潮',
  drop: 'Drop',
  build_start: '蓄势',
  release: '释放',
  break: '留白',
}

export interface BgmAnalysisCardProps {
  analysis: BGMAnalysis
  /** 左侧标签文案（默认"背景音乐分析"，Decompose 页可传"音轨理解"） */
  leftTitle?: string
  /** 左侧标签副标题 */
  leftSubtitle?: string
  /** 契合度标签的 hover 提示文案 */
  fitHint?: string
  /**
   * 语义变体：
   * - "bgm"（默认）：Compose 页选曲视角，"曲子配什么视频"
   * - "sample"：Decompose 页迁移视角，"这条样例音轨教会我什么节奏"
   */
  variant?: 'bgm' | 'sample'
}

const COPY: Record<'bgm' | 'sample', {
  unknownTitle: string
  climaxHeader: string
  emptyClimax: string
  calmHeader: string
  fitPrefix: string
  advicePrefix: string
}> = {
  bgm: {
    unknownTitle: '未知曲风',
    climaxHeader: '⚡ 关键节点（建议对齐到视频）',
    emptyClimax: '无突出鼓点 / 全程平稳——曲子作底色用，不需要刻意对齐节奏。',
    calmHeader: '🔉 平稳段（适合压口播 / 慢镜头）',
    fitPrefix: '契合：',
    advicePrefix: '建议：',
  },
  sample: {
    unknownTitle: '未识别音轨结构',
    climaxHeader: '⚡ 关键节奏点（复刻时让画面同步压在这一刻）',
    emptyClimax: '整段音轨节奏平稳，没有突出的节奏点——按均匀节奏走即可，不必硬对齐。',
    calmHeader: '🔉 平稳区间（可承载长口播 / 慢镜头 / 信息密度）',
    fitPrefix: '题材契合：',
    advicePrefix: '迁移建议：',
  },
}

/**
 * 多模态音频理解结果可视化卡。在 Compose 与 Decompose 两个页面共用。
 *
 * 数据契约：复用后端 `BGMAnalysis` schema —— title_guess / mood_tags / energy_shape /
 * climaxes / calm_segments / overall_advice 等字段。decompose 时 theme_fit 解读为
 * "音轨与视频题材的契合度"，compose 时解读为"BGM 与 brief 的契合度"。
 */
export function BgmAnalysisCard({
  analysis,
  leftTitle = '背景音乐分析',
  leftSubtitle = 'AI 听完曲子的解读',
  fitHint = 'AI 判断曲子和你主题的契合度（0-100%）',
  variant = 'bgm',
}: BgmAnalysisCardProps) {
  const copy = COPY[variant]
  const score = Math.max(0, Math.min(1, analysis.theme_fit_score))
  const scoreLabel = score >= 0.7 ? '高契合' : score >= 0.4 ? '中等契合' : '低契合'
  const shapeLabel = ENERGY_SHAPE_SHORT[analysis.energy_shape]
  const shapeHint = (variant === 'sample' ? ENERGY_SHAPE_HINT_SAMPLE : ENERGY_SHAPE_HINT)[analysis.energy_shape]
  return (
    <div className="grid grid-cols-[88px_1fr] gap-1">
      <div className="pr-1 text-xs text-muted-foreground">
        <span className="font-semibold text-foreground">{leftTitle}</span>
        <p className="mt-0.5 text-xs leading-tight">{leftSubtitle}</p>
      </div>
      <div className="space-y-2 rounded-md border border-violet-400/40 bg-violet-400/5 p-2 text-xs">
        <div className="flex flex-wrap items-center gap-1.5">
          <span className="rounded-full bg-violet-500/15 px-2 py-0.5 font-medium text-violet-700 dark:text-violet-200">
            {analysis.title_guess || copy.unknownTitle}
          </span>
          {analysis.mood_tags.slice(0, 6).map((tag) => (
            <span
              key={tag}
              className="rounded-full border border-violet-400/40 px-1.5 py-0.5 text-xs text-violet-700 dark:text-violet-200"
            >
              {tag}
            </span>
          ))}
          <span
            className={cn(
              'ml-auto rounded px-1.5 py-0.5 font-mono text-xs',
              score >= 0.7 && 'bg-emerald-500/15 text-emerald-700 dark:text-emerald-300',
              score >= 0.4 && score < 0.7 && 'bg-amber-500/15 text-amber-700 dark:text-amber-300',
              score < 0.4 && 'bg-rose-500/15 text-rose-700 dark:text-rose-300',
            )}
            title={fitHint}
          >
            {scoreLabel} · {(score * 100).toFixed(0)}%
          </span>
        </div>

        <div className="rounded border border-fuchsia-400/40 bg-gradient-to-r from-violet-400/10 to-fuchsia-400/10 p-1.5">
          <div className="flex items-center gap-2 text-[10.5px]">
            <span className="rounded bg-fuchsia-500/20 px-1.5 py-0.5 font-bold text-fuchsia-700 dark:text-fuchsia-200">
              {shapeLabel}
            </span>
            <span className="text-xs text-muted-foreground">{shapeHint}</span>
          </div>
          {analysis.energy_shape_reason && (
            <p className="mt-1 text-[10.5px] leading-snug text-foreground/80">
              {analysis.energy_shape_reason}
            </p>
          )}
        </div>

        {analysis.theme_fit_reason && (
          <p className="text-[10.5px] leading-snug text-muted-foreground">
            <span className="font-semibold text-foreground/70">{copy.fitPrefix}</span>
            {analysis.theme_fit_reason}
          </p>
        )}

        {analysis.climaxes.length > 0 ? (
          <div className="space-y-0.5 text-[10.5px] leading-snug">
            <p className="text-xs font-semibold text-foreground/70">
              {copy.climaxHeader}
            </p>
            <ul className="space-y-0.5">
              {analysis.climaxes.map((hl, idx) => (
                <li key={idx} className="flex flex-wrap items-baseline gap-1.5">
                  <span className="font-mono text-xs text-fuchsia-700 dark:text-fuchsia-300">
                    {hl.at_seconds.toFixed(1)}s
                  </span>
                  <span className="rounded bg-fuchsia-500/15 px-1 text-xs font-semibold text-fuchsia-700 dark:text-fuchsia-200">
                    {HIGHLIGHT_KIND_LABEL[hl.kind]}
                  </span>
                  {hl.label && <span className="font-medium text-foreground">{hl.label}</span>}
                  <span className="text-muted-foreground">→ {hl.fit_with_video}</span>
                </li>
              ))}
            </ul>
          </div>
        ) : (
          <p className="rounded border border-dashed border-violet-400/40 bg-violet-400/5 px-2 py-1 text-[10.5px] text-muted-foreground">
            {copy.emptyClimax}
          </p>
        )}

        {analysis.calm_segments.length > 0 && (
          <div className="space-y-0.5 text-[10.5px] leading-snug">
            <p className="text-xs font-semibold text-foreground/70">
              {copy.calmHeader}
            </p>
            <ul className="space-y-0.5">
              {analysis.calm_segments.map((seg, idx) => (
                <li key={idx} className="flex flex-wrap items-baseline gap-1.5">
                  <span className="font-mono text-xs text-violet-700 dark:text-violet-300">
                    {seg.start.toFixed(1)}–{seg.end.toFixed(1)}s
                  </span>
                  <span className="text-muted-foreground">{seg.note}</span>
                </li>
              ))}
            </ul>
          </div>
        )}

        {analysis.overall_advice && (
          <p className="rounded bg-background/60 px-2 py-1 text-[10.5px] leading-snug text-foreground/80">
            <span className="text-muted-foreground">{copy.advicePrefix}</span>
            {analysis.overall_advice}
          </p>
        )}
      </div>
    </div>
  )
}
