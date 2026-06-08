import { useState } from 'react'
import { LineChart, Line, XAxis, YAxis, Tooltip, ResponsiveContainer, ReferenceLine } from 'recharts'
import { SECTION_LABEL, SECTION_BG } from '@/lib/sections'
import { cn } from '@/lib/utils'
import type { SampleManifest } from '@/types/schemas'

/**
 * 节奏故事卡 —— 替代 6 张技术卡片堆叠。
 * 用户一眼看懂：视频分几段、每段干什么、情绪怎么走、整体风格是什么。
 * 想看详情的点「查看详细分镜」展开。
 */
export function StoryCard({ manifest }: { manifest: SampleManifest }) {
  const [showShots, setShowShots] = useState(false)

  const moodCurve = manifest.rhythm.mood_curve ?? []
  const rhythmData = manifest.rhythm.times.map((t, i) => ({
    t,
    mood: moodCurve[i] ?? 0,
  }))
  const hasMood = moodCurve.length > 0

  return (
    <div className="space-y-6">
      {/* ====== 视频 + 段落故事 ====== */}
      <div className="overflow-hidden rounded-2xl border border-border bg-card">
        {/* 视频 */}
        {manifest.video_url && (
          <div className="border-b border-border">
            <video
              src={manifest.video_url}
              controls
              preload="metadata"
              className="aspect-video w-full bg-black"
            />
          </div>
        )}

        {/* 段落故事 —— 横条 */}
        <div className="p-6">
          <h2 className="mb-4 text-lg font-bold">这个视频怎么讲故事的</h2>

          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
            {manifest.sections.map((sec, idx) => {
              const duration = sec.end - sec.start
              return (
                <div
                  key={idx}
                  className={cn(
                    'flex flex-col gap-2 rounded-xl px-4 py-3 text-sm',
                    SECTION_BG[sec.role],
                    'bg-opacity-80 text-white',
                  )}
                >
                  <div className="flex items-center justify-between">
                    <span className="font-bold">{SECTION_LABEL[sec.role]}</span>
                    <span className="text-xs opacity-70">{duration.toFixed(0)}s</span>
                  </div>
                  <div className="text-sm font-semibold">{sec.theme}</div>
                  <div className="text-xs leading-relaxed opacity-80">{sec.summary}</div>
                  <div className="text-xs opacity-60">
                    {sec.start.toFixed(0)}s – {sec.end.toFixed(0)}s
                  </div>
                </div>
              )
            })}
          </div>

          {/* 情绪节奏 + 风格标签 */}
          <div className="mt-6 grid grid-cols-1 gap-4 md:grid-cols-2">
            {/* 情绪走势 */}
            {hasMood && (
              <div>
                <h3 className="mb-2 text-sm font-semibold text-muted-foreground">📊 情绪节奏</h3>
                <div className="h-32">
                  <ResponsiveContainer width="100%" height="100%">
                    <LineChart data={rhythmData} margin={{ top: 4, right: 4, bottom: 4, left: 0 }}>
                      <XAxis dataKey="t" hide />
                      <YAxis domain={[0, 1]} hide />
                      <Tooltip
                        formatter={(v: number) => `${Math.round(v * 100)}%`}
                        labelFormatter={(t: number) => `${t.toFixed(1)}s`}
                        contentStyle={{ fontSize: 12, background: 'hsl(240 8% 10%)', border: '1px solid hsl(240 5% 18%)', borderRadius: 8 }}
                      />
                      <Line type="monotone" dataKey="mood" stroke="hsl(190 90% 50%)" dot={false} strokeWidth={2.5} isAnimationActive={false} />
                      {manifest.climax_position != null && (
                        <ReferenceLine x={manifest.climax_position} stroke="hsl(190 90% 50%)" strokeDasharray="4 2" strokeOpacity={0.6} />
                      )}
                    </LineChart>
                  </ResponsiveContainer>
                </div>
              </div>
            )}

            {/* 风格标签 */}
            <div>
              <h3 className="mb-2 text-sm font-semibold text-muted-foreground">🎨 风格与包装</h3>
              <div className="flex flex-wrap gap-2">
                {manifest.understanding && (
                  <>
                    <Tag>{manifest.understanding.archetype}</Tag>
                    <Tag>{manifest.understanding.tone}</Tag>
                  </>
                )}
                <Tag>{manifest.has_voice ? '有人声' : '纯背景音乐'}</Tag>
                <Tag>{manifest.packaging.subtitle_style || '标准字幕'}</Tag>
                <Tag>{manifest.packaging.cover_style || '标准封面'}</Tag>
                {manifest.packaging.transition_types.slice(0, 2).map((t) => (
                  <Tag key={t}>{t}</Tag>
                ))}
              </div>

              {/* BGM 匹配度 */}
              {manifest.rhythm.bgm_fit_score != null && (
                <div className="mt-3 flex items-center gap-2 text-sm">
                  <span className="text-muted-foreground">🎵 BGM 匹配度</span>
                  <span className={cn(
                    'rounded-full px-2 py-0.5 text-xs font-medium',
                    manifest.rhythm.bgm_fit_score >= 0.65 ? 'bg-emerald-500/20 text-emerald-400'
                      : manifest.rhythm.bgm_fit_score >= 0.45 ? 'bg-amber-500/20 text-amber-400'
                        : 'bg-rose-500/20 text-rose-400',
                  )}>
                    {Math.round(manifest.rhythm.bgm_fit_score * 100)}%
                  </span>
                  {manifest.rhythm.bgm_fit_note && (
                    <span className="text-xs text-muted-foreground">{manifest.rhythm.bgm_fit_note}</span>
                  )}
                </div>
              )}

              {/* AI 总结 */}
              {manifest.understanding?.narrative_summary && (
                <p className="mt-3 text-sm leading-relaxed text-muted-foreground">
                  {manifest.understanding.narrative_summary}
                </p>
              )}
            </div>
          </div>

          {/* 展开详细分镜 */}
          <button
            onClick={() => setShowShots(!showShots)}
            className="mt-4 flex w-full items-center justify-center gap-2 rounded-lg py-2 text-sm text-muted-foreground transition-colors hover:bg-secondary hover:text-foreground"
          >
            {showShots ? '收起' : '查看详细分镜'}（{manifest.shots.length} 个镜头）
            <span className={cn('transition-transform', showShots && 'rotate-180')}>▾</span>
          </button>

          {showShots && (
            <div className="mt-3 grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-4">
              {manifest.shots.map((shot) => (
                <div key={shot.index} className="overflow-hidden rounded-lg border border-border bg-background">
                  <div
                    className="aspect-video w-full bg-gradient-to-br from-secondary to-muted"
                    style={{
                      backgroundImage: shot.thumbnail_url ? `url(${shot.thumbnail_url})` : undefined,
                      backgroundSize: 'cover',
                    }}
                  />
                  <div className="space-y-1 p-2">
                    <div className="flex items-center justify-between text-xs text-muted-foreground">
                      <span>#{shot.index + 1}</span>
                      <span className="font-mono">{shot.duration.toFixed(1)}s</span>
                    </div>
                    {shot.tags.length > 0 && (
                      <div className="flex flex-wrap gap-1">
                        {shot.tags.slice(0, 3).map((t) => (
                          <span key={t} className="rounded-full bg-secondary px-1.5 py-0.5 text-[10px] text-muted-foreground">{t}</span>
                        ))}
                      </div>
                    )}
                    {shot.visual_summary && (
                      <p className="text-xs leading-relaxed text-muted-foreground line-clamp-2">{shot.visual_summary}</p>
                    )}
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  )
}

function Tag({ children }: { children: string }) {
  return (
    <span className="rounded-full border border-border bg-secondary px-2.5 py-1 text-xs text-foreground/80">
      {children}
    </span>
  )
}
