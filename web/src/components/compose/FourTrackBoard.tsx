import { useCallback, useEffect, useMemo, useRef, useState } from 'react'

import { cn } from '@/lib/utils'
import { SECTION_BG, SECTION_LABEL, SECTION_SHORT } from '@/lib/sections'
import { TRANSITION_LABEL, TRANSITION_TONE } from '@/lib/transitions'
import type {
  AdaptedSection,
  BGMConfig,
  Gap,
  GapStatus,
  PackagingItem,
  Plan,
  Scene,
  SectionRole,
  TTSVoice,
} from '@/types/schemas'

/**
 * Compose 页四轨横向工作面板：
 *
 *   时间轴标尺
 *   ─────────────────────────────────────────
 *   内容轨   [scene-0]  [scene-1]  [scene-2]  ...
 *   口播轨   [TTS-0]    [TTS-1]    [TTS-2]    ...
 *   包装轨   [字幕] [转场] [封面] [字幕] [贴纸] ...
 *   BGM 轨   [════════════ track ↑peak ⇄anchor ═════════]
 *
 * 设计取舍：
 * - 每轨宽度 = plan.duration_seconds 等距映射到容器宽度；scene 块按 start/duration 定位。
 * - 内容轨色按 section role；点击 = 选中段对应 gap（在父级控制 Fill 面板）。
 * - 口播轨每个 scene 显示一个状态徽章：'no narration' / 'has text' / 'has audio'。
 * - 包装轨 item 按 start/end 时间段定位；没 packaging_track 时显示空提示。
 * - BGM 轨当 plan.bgm.track_url 存在时显示连续长块 + ↑peak 标记 + 可拖动 anchor 滑块；
 *   未绑定 BGM 时显示"上传 / 选择 BGM"按钮（实际上传由父级弹窗处理）。
 *
 * 该组件是纯展示 + 事件回调，所有真实状态/写操作在 Compose.tsx 里集中。
 */

interface Props {
  plan: Plan
  gaps: Gap[]
  /** 已采纳的 fill 对应的 gap_id 集合，用于在轨上高亮"已补"。 */
  filledGapIds: Set<string>
  /** 当前选中的 gap_id，驱动内容轨 / Fill 面板互动。 */
  selectedGapId: string | null
  /** 点击内容轨某个 scene → 通知父级选中对应 gap（如有）/段。 */
  onSelectScene: (scene: Scene, gap: Gap | null, section: AdaptedSection | null) => void
  /** 单段 TTS 合成。 */
  onSynthesizeScene?: (sceneId: string) => void | Promise<void>
  /** 一键全段 TTS 合成。 */
  onSynthesizeAll?: () => void | Promise<void>
  /** 删除某段口播。 */
  onClearVoice?: (sceneId: string) => void | Promise<void>
  /** 触发"一键包装推荐"。 */
  onRecommendPackaging?: () => void | Promise<void>
  /** 触发上传 BGM 弹窗（父级控制 Asset library 选择 / 上传 UI）。 */
  onPickBgm?: () => void
  /** 拖动 BGM anchor 到新位置（秒，可正可负）。 */
  onBgmAnchorChange?: (newAnchorSeconds: number) => void | Promise<void>
  /** 清除 BGM 绑定。 */
  onClearBgm?: () => void | Promise<void>
  /** 调整 BGM 音量（0 ~ 1）。组件内部 debounce 300ms 后才会触发。 */
  onBgmVolumeChange?: (volume: number) => void | Promise<void>
  /** 翻转 plan.settings.voiceover_enabled——口播轨左侧的开关。 */
  onToggleVoiceover?: (enabled: boolean) => void | Promise<void>
  /** 切换 plan.settings.tts_voice——口播轨上选音色后写回 plan. */
  onChangeTtsVoice?: (voice: TTSVoice) => void | Promise<void>
  /** 处于"批量合成中"等忙状态时禁用所有按钮。 */
  busy?: boolean
  /** 只读模式：渲染页用——隐藏所有写操作按钮，保留 onSelectScene 触发自然语言编辑。 */
  readOnly?: boolean
  /**
   * 展示阶段：
   * - 'content-only'：素材刚分析完、缺口未补齐时只露内容轨，其余三轨隐藏（引导用户先补内容）。
   * - 'full'（默认）：四轨全展开。
   */
  phase?: 'content-only' | 'full'
}

const STATUS_COLOR: Record<GapStatus, string> = {
  ok: 'border-emerald-500/60 ring-emerald-500/40',
  warn: 'border-amber-500/60 ring-amber-500/40',
  miss: 'border-rose-500/60 ring-rose-500/40',
}

const STATUS_GLYPH: Record<GapStatus, string> = { ok: '✓', warn: '!', miss: '×' }

const PACKAGING_KIND_LABEL: Record<PackagingItem['kind'], string> = {
  subtitle: '字幕',
  title_bar: '标题',
  sticker: '贴纸',
  transition: '转场',
  cover: '封面',
}

const PACKAGING_KIND_COLOR: Record<PackagingItem['kind'], string> = {
  subtitle: 'bg-sky-400/80 text-sky-950',
  title_bar: 'bg-indigo-400/80 text-indigo-950',
  sticker: 'bg-fuchsia-400/80 text-fuchsia-950',
  transition: 'bg-amber-400/80 text-amber-950',
  cover: 'bg-emerald-400/80 text-emerald-950',
}

const VOICE_OPTIONS: { value: TTSVoice; label: string }[] = [
  { value: 'zh_female_qingxin', label: '清新女声' },
  { value: 'zh_female_wenrou', label: '温柔女声' },
  { value: 'zh_female_xiaoyu', label: '小渔女声' },
  { value: 'zh_male_jieshuo', label: '解说男声' },
  { value: 'zh_male_xueyi', label: '学奕男声' },
]

/** 把秒映射到 [0%, 100%]；防 NaN。 */
function pctOf(seconds: number, total: number): number {
  if (!Number.isFinite(seconds) || total <= 0) return 0
  return Math.max(0, Math.min(100, (seconds / total) * 100))
}

/** 生成时间刻度（每 5 秒一格，最多 12 格）。 */
function makeTicks(total: number): number[] {
  if (total <= 0) return []
  const step = total <= 30 ? 5 : total <= 60 ? 10 : 15
  const ticks: number[] = [0]
  for (let t = step; t <= total - 0.5; t += step) ticks.push(t)
  ticks.push(total)
  return ticks
}

export function FourTrackBoard({
  plan,
  gaps,
  filledGapIds,
  selectedGapId,
  onSelectScene,
  onSynthesizeScene,
  onSynthesizeAll,
  onClearVoice,
  onRecommendPackaging,
  onPickBgm,
  onBgmAnchorChange,
  onClearBgm,
  onBgmVolumeChange,
  onToggleVoiceover,
  onChangeTtsVoice,
  busy = false,
  readOnly = false,
  phase = 'full',
}: Props) {
  const total = plan.duration_seconds || 0
  const scenes = plan.main_track
  const packaging = plan.packaging_track
  const adapted = plan.adapted_sections
  const bgm = plan.bgm
  const voiceoverEnabled = plan.settings.voiceover_enabled
  const ticks = useMemo(() => makeTicks(total), [total])
  const showSecondaryTracks = phase === 'full'

  // 包装轨守门：开口播时必须所有 scene 都已合成 wav；关口播则内容轨齐就行。
  // 取舍：scene.narration 为空那段不算"未合成"——视为天然静默，不会卡包装。
  const allVoicesReady = useMemo(() => {
    if (!voiceoverEnabled) return true
    if (scenes.length === 0) return false
    return scenes.every((sc) => !(sc.narration ?? '').trim() || !!sc.voiceover_url)
  }, [scenes, voiceoverEnabled])
  const packagingReady = scenes.length > 0 && allVoicesReady

  // section_id → AdaptedSection 索引
  const sectionById = useMemo(() => {
    const m = new Map<string, AdaptedSection>()
    for (const sec of adapted) m.set(sec.section_id, sec)
    return m
  }, [adapted])

  // 包装轨按 kind 分桶：字幕跟随口播显示在口播轨内，其余（标题/转场/封面/贴纸）留在包装轨
  const subtitleItems = useMemo(
    () => packaging.filter((it) => it.kind === 'subtitle'),
    [packaging],
  )
  const nonSubtitleItems = useMemo(
    // transition 已经从 packaging 内化到 scene.transition_in（PlanStore 启动时迁移）；
    // 这里仍兜底滤一遍，以免老 plan 残留的 kind='transition' 项闪到轨上。
    () => packaging.filter((it) => it.kind !== 'subtitle' && it.kind !== 'transition'),
    [packaging],
  )

  // 把字幕按时间区间与 scene 对齐——scene 起讫与 subtitle.start/end 有重叠就归到该 scene
  const subtitleBySceneId = useMemo(() => {
    const m = new Map<string, PackagingItem>()
    for (const sc of scenes) {
      const scStart = sc.start
      const scEnd = sc.start + sc.duration
      const hit = subtitleItems.find(
        (it) => it.start < scEnd && it.end > scStart,
      )
      if (hit) m.set(sc.scene_id, hit)
    }
    return m
  }, [scenes, subtitleItems])

  // scene_id → 对应 gap（按 section_id 同段最早未补的）；用作内容轨点击关联
  const sceneToGap = useMemo(() => {
    const result = new Map<string, Gap | null>()
    // 按 section role 分桶时同时 fall back 老 plan 无 section_id 情况
    const byScene: Record<string, Gap[]> = {}
    for (const g of gaps) {
      // 用 g.section_id 找 section，得 order → scene_id 推断
      const sec = g.section_id ? sectionById.get(g.section_id) : null
      const sceneId = sec ? `sc-${sec.order}` : null
      if (sceneId) {
        ;(byScene[sceneId] ??= []).push(g)
      }
    }
    for (const sc of scenes) {
      const candidates = byScene[sc.scene_id] ?? []
      const unmet = candidates.find(
        (g) => !filledGapIds.has(g.gap_id) && g.status !== 'ok',
      )
      result.set(sc.scene_id, unmet ?? candidates[0] ?? null)
    }
    return result
  }, [filledGapIds, gaps, scenes, sectionById])

  /* ==================== BGM anchor 拖动 ==================== */

  const bgmRowRef = useRef<HTMLDivElement | null>(null)
  const [draggingAnchor, setDraggingAnchor] = useState<number | null>(null)

  const computeAnchorFromClientX = useCallback(
    (clientX: number): number => {
      const el = bgmRowRef.current
      if (!el || total <= 0 || !bgm?.duration_seconds) return 0
      const rect = el.getBoundingClientRect()
      const ratio = Math.max(0, Math.min(1, (clientX - rect.left) / rect.width))
      // 用户拖的是「视频时间轴上 BGM 起点」；范围允许 -bgm_duration ~ total
      // 这里把整条 row 等距映射为 [-bgm_duration, total]，负值 = 跳过 BGM 头部
      const minA = -bgm.duration_seconds
      const maxA = total
      return minA + ratio * (maxA - minA)
    },
    [bgm?.duration_seconds, total],
  )

  const handleAnchorMouseDown = useCallback(
    (e: React.MouseEvent) => {
      if (!bgm?.track_url || busy) return
      e.preventDefault()
      const onMove = (mv: MouseEvent) => {
        setDraggingAnchor(computeAnchorFromClientX(mv.clientX))
      }
      const onUp = (mv: MouseEvent) => {
        window.removeEventListener('mousemove', onMove)
        window.removeEventListener('mouseup', onUp)
        const final = computeAnchorFromClientX(mv.clientX)
        setDraggingAnchor(null)
        onBgmAnchorChange?.(Math.round(final * 10) / 10)
      }
      window.addEventListener('mousemove', onMove)
      window.addEventListener('mouseup', onUp)
    },
    [bgm?.track_url, busy, computeAnchorFromClientX, onBgmAnchorChange],
  )

  /* ==================== BGM 音量本地态 + 300ms debounce ==================== */

  // 本地输入态：拖滑块时立即更新视觉，debounce 300ms 才打 PATCH
  const [volumeDraft, setVolumeDraft] = useState<number | null>(null)
  const volumeDebounceRef = useRef<ReturnType<typeof setTimeout> | null>(null)

  // 外部 bgm.volume 变了（patch 回包）→ 清掉本地草稿
  useEffect(() => {
    setVolumeDraft(null)
  }, [bgm?.volume])

  useEffect(() => {
    return () => {
      if (volumeDebounceRef.current) clearTimeout(volumeDebounceRef.current)
    }
  }, [])

  const handleVolumeChange = useCallback(
    (next: number) => {
      const clamped = Math.max(0, Math.min(1, next))
      setVolumeDraft(clamped)
      if (volumeDebounceRef.current) clearTimeout(volumeDebounceRef.current)
      if (!onBgmVolumeChange) return
      volumeDebounceRef.current = setTimeout(() => {
        void onBgmVolumeChange(Math.round(clamped * 100) / 100)
      }, 300)
    },
    [onBgmVolumeChange],
  )

  /* ==================== 渲染 ==================== */

  if (total <= 0 || scenes.length === 0) {
    return (
      <div className="rounded-md border border-dashed border-border bg-background/30 p-6 text-center text-xs text-muted-foreground">
        plan 还没准备好——先点上方「智能分析」生成一份。
      </div>
    )
  }

  return (
    <div className="space-y-2 rounded-lg border border-border bg-card p-4">
      {/* ===================== 时间标尺 ===================== */}
      <div className="grid grid-cols-[88px_1fr] items-center">
        <span className="text-[10px] font-semibold text-muted-foreground">时间轴</span>
        <div className="relative h-5 border-b border-border">
          {ticks.map((t) => (
            <span
              key={t}
              className="absolute top-0 -translate-x-1/2 text-[10px] font-mono text-muted-foreground"
              style={{ left: `${pctOf(t, total)}%` }}
            >
              {t.toFixed(0)}s
            </span>
          ))}
        </div>
      </div>

      {/* ===================== 内容轨 ===================== */}
      <TrackRow label="内容轨" hint={`${scenes.length} 段`}>
        {scenes.map((scene) => {
          const left = pctOf(scene.start, total)
          const width = pctOf(scene.duration, total)
          // 用 source_ref ID 找 section 推断：scene.scene_id 形如 sc-<order>，匹配 AdaptedSection.order
          const orderMatch = /sc-(\d+)/.exec(scene.scene_id)
          const order = orderMatch ? Number(orderMatch[1]) : null
          const section =
            order != null
              ? adapted.find((s) => s.order === order) ?? null
              : null
          const gap = sceneToGap.get(scene.scene_id) ?? null
          const status: GapStatus | null = gap?.status ?? null
          const filled = gap && filledGapIds.has(gap.gap_id)
          const effectiveStatus: GapStatus = filled ? 'ok' : (status ?? 'ok')
          const selected = gap?.gap_id === selectedGapId
          // 转场标块：仅在「非首段 + transition_in 非 hard_cut」时绘制；点击 = 选中该 scene（让用户自然语言改）
          const trans =
            scene.start > 0 && scene.transition_in && scene.transition_in.style !== 'hard_cut'
              ? scene.transition_in
              : null

          return (
            <button
              key={scene.scene_id}
              onClick={() => onSelectScene(scene, gap, section)}
              className={cn(
                'absolute top-1 bottom-1 overflow-hidden rounded-md border-2 text-left text-[10px] text-white shadow-sm transition-all',
                SECTION_BG[scene.section],
                STATUS_COLOR[effectiveStatus],
                selected ? 'ring-2 ring-offset-1 ring-offset-card' : 'hover:brightness-110',
              )}
              style={{ left: `${left}%`, width: `calc(${width}% - 2px)` }}
              title={
                section
                  ? `${SECTION_LABEL[scene.section]} · ${section.theme}\n${section.content_description}`
                  : `${SECTION_LABEL[scene.section]} · ${scene.duration.toFixed(1)}s`
              }
            >
              {trans && (
                <span
                  className={cn(
                    'pointer-events-none absolute -left-1 top-1/2 -translate-y-1/2 rounded px-1 py-px text-[8px] font-semibold shadow-sm',
                    TRANSITION_TONE[trans.style],
                  )}
                  title={`转场：${TRANSITION_LABEL[trans.style]} · ${trans.duration.toFixed(1)}s（与上一段衔接）`}
                >
                  ◂{TRANSITION_LABEL[trans.style]}
                </span>
              )}
              <div className="flex h-full flex-col justify-between p-1">
                <div className="flex items-center justify-between gap-1">
                  <span className="font-mono text-[9px] opacity-80">
                    {SECTION_SHORT[scene.section]}
                  </span>
                  {gap && (
                    <span
                      className={cn(
                        'inline-flex h-3 min-w-3 items-center justify-center rounded-full px-1 text-[9px] font-bold',
                        effectiveStatus === 'ok'
                          ? 'bg-emerald-300 text-emerald-900'
                          : effectiveStatus === 'warn'
                            ? 'bg-amber-300 text-amber-900'
                            : 'bg-rose-300 text-rose-900',
                      )}
                    >
                      {STATUS_GLYPH[effectiveStatus]}
                    </span>
                  )}
                </div>
                <div className="truncate text-[10px] font-semibold leading-tight">
                  {section?.theme || SECTION_LABEL[scene.section]}
                </div>
              </div>
            </button>
          )
        })}
      </TrackRow>

      {/* ===================== 口播轨 ===================== */}
      {showSecondaryTracks && (
      <TrackRow
        label="口播轨"
        hint={voiceoverEnabled ? `${scenes.filter((s) => s.voiceover_url).length}/${scenes.length} 已合成` : '已关闭'}
        labelExtra={
          onToggleVoiceover ? (
            <button
              onClick={() => void onToggleVoiceover(!voiceoverEnabled)}
              disabled={busy || readOnly}
              role="switch"
              aria-checked={voiceoverEnabled}
              title={voiceoverEnabled ? '点击关闭口播（视频走纯 BGM）' : '点击开启口播（plan.settings.voiceover_enabled=true）'}
              className={cn(
                'relative inline-flex h-4 w-8 shrink-0 items-center rounded-full transition-colors',
                voiceoverEnabled ? 'bg-emerald-500/80' : 'bg-muted-foreground/30',
                (busy || readOnly) && 'cursor-not-allowed opacity-60',
              )}
            >
              <span
                className={cn(
                  'inline-block h-3 w-3 transform rounded-full bg-white shadow transition-transform',
                  voiceoverEnabled ? 'translate-x-4' : 'translate-x-0.5',
                )}
              />
            </button>
          ) : null
        }
        actions={
          voiceoverEnabled && !readOnly ? (
            <div className="flex w-full flex-col gap-1">
              {onChangeTtsVoice && (
                <select
                  value={plan.settings.tts_voice}
                  onChange={(e) => void onChangeTtsVoice(e.target.value as TTSVoice)}
                  disabled={busy}
                  title="选择 TTS 音色（写入 plan.settings.tts_voice，下次合成生效）"
                  className="w-full rounded border border-border bg-background/60 px-1 py-0.5 text-[10px] text-foreground outline-none focus:border-primary disabled:opacity-50"
                >
                  {VOICE_OPTIONS.map((v) => (
                    <option key={v.value} value={v.value}>
                      {v.label}
                    </option>
                  ))}
                </select>
              )}
              {onSynthesizeAll && (
                <button
                  onClick={() => void onSynthesizeAll()}
                  disabled={busy}
                  className="rounded border border-primary/40 bg-primary/10 px-2 py-0.5 text-[10px] text-primary hover:bg-primary/20 disabled:opacity-50"
                  title="按选中音色一次性合成全部 scene 的 TTS，自动按 scene.duration 做 ≤1.15× 加速对齐"
                >
                  一键全段合成
                </button>
              )}
            </div>
          ) : null
        }
      >
        {!voiceoverEnabled ? (
          <div className="absolute inset-1 flex items-center justify-center rounded-md border border-dashed border-border bg-background/30 text-[10px] text-muted-foreground">
            voiceover_enabled = false（仅 BGM，不烧字幕）
          </div>
        ) : (
          scenes.map((scene) => {
            const left = pctOf(scene.start, total)
            const width = pctOf(scene.duration, total)
            const hasNarration = (scene.narration ?? '').trim().length > 0
            const hasAudio = !!scene.voiceover_url
            const state = hasAudio ? 'ready' : hasNarration ? 'pending' : 'empty'
            const subtitle = subtitleBySceneId.get(scene.scene_id)
            const subtitleText = (subtitle?.text ?? scene.narration ?? '').trim()
            return (
              <div
                key={scene.scene_id}
                className={cn(
                  'absolute top-1 bottom-1 overflow-hidden rounded-md border text-[10px] shadow-sm',
                  state === 'ready'
                    ? 'border-emerald-400/60 bg-emerald-500/15 text-emerald-700 dark:text-emerald-300'
                    : state === 'pending'
                      ? 'border-amber-400/60 bg-amber-500/10 text-amber-700 dark:text-amber-300'
                      : 'border-dashed border-border bg-background/40 text-muted-foreground',
                )}
                style={{ left: `${left}%`, width: `calc(${width}% - 2px)` }}
                title={
                  state === 'ready'
                    ? `已合成 · ${scene.narration ?? ''}${subtitleText ? `\n字幕：${subtitleText}` : ''}`
                    : state === 'pending'
                      ? `待合成 · ${scene.narration ?? ''}${subtitleText ? `\n字幕：${subtitleText}` : ''}`
                      : '该段无 narration'
                }
              >
                <div className="flex h-full flex-col gap-0.5 px-1 py-0.5">
                  <div className="flex items-center gap-1">
                    <span className="font-mono text-[9px] opacity-70">{scene.scene_id}</span>
                    <span className="ml-auto inline-flex shrink-0 gap-0.5">
                      {!readOnly && state !== 'empty' && onSynthesizeScene && (
                        <button
                          onClick={(e) => {
                            e.stopPropagation()
                            void onSynthesizeScene(scene.scene_id)
                          }}
                          disabled={busy}
                          className="rounded bg-background/60 px-1 text-[9px] hover:bg-background disabled:opacity-50"
                          title={state === 'ready' ? '重新合成' : '合成口播'}
                        >
                          {state === 'ready' ? '↻' : '🔊'}
                        </button>
                      )}
                      {!readOnly && state === 'ready' && onClearVoice && (
                        <button
                          onClick={(e) => {
                            e.stopPropagation()
                            void onClearVoice(scene.scene_id)
                          }}
                          disabled={busy}
                          className="rounded bg-background/60 px-1 text-[9px] hover:bg-background disabled:opacity-50"
                          title="清除该段口播"
                        >
                          ×
                        </button>
                      )}
                    </span>
                  </div>
                  {subtitleText && state !== 'empty' && (
                    <div
                      className="flex items-center gap-1 truncate rounded bg-sky-500/15 px-1 text-[9px] font-medium text-sky-700 dark:text-sky-200"
                      title={`字幕：${subtitleText}`}
                    >
                      <span className="opacity-70">字</span>
                      <span className="truncate">{subtitleText}</span>
                    </div>
                  )}
                </div>
              </div>
            )
          })
        )}
      </TrackRow>
      )}

      {/* ===================== 包装轨 ===================== */}
      {showSecondaryTracks && (
      <TrackRow
        label="包装轨"
        hint={`${nonSubtitleItems.length} 项${subtitleItems.length > 0 ? `（字幕 ${subtitleItems.length} 项已并入口播轨）` : ''}`}
        actions={
          onRecommendPackaging && !readOnly ? (
            <button
              onClick={() => void onRecommendPackaging()}
              disabled={busy || !packagingReady}
              title={
                !packagingReady
                  ? voiceoverEnabled
                    ? '请先在口播轨"一键全段合成"，把所有 narration 的 TTS 跑完，再生成包装'
                    : '内容轨还没准备好'
                  : packaging.length > 0
                    ? '基于最新内容 + 字幕重写包装'
                    : '生成转场 / 封面 / 字幕等包装项'
              }
              className={cn(
                'rounded border border-primary/40 bg-primary/10 px-2 py-0.5 text-[10px] text-primary hover:bg-primary/20 disabled:opacity-50',
                !packagingReady && 'cursor-not-allowed',
              )}
            >
              {packaging.length > 0 ? '重新生成' : '一键生成'}
            </button>
          ) : null
        }
      >
        {nonSubtitleItems.length === 0 ? (
          <div className="absolute inset-1 flex items-center justify-center rounded-md border border-dashed border-border bg-background/30 text-center text-[10px] text-muted-foreground">
            {!packagingReady && voiceoverEnabled
              ? '内容轨与字幕轨准备就绪后，点右上「一键生成」自动写转场 / 封面 / 字幕'
              : '还没生成包装轨（标题 / 转场 / 封面 / 贴纸）'}
          </div>
        ) : (
          nonSubtitleItems.map((it, i) => {
            const left = pctOf(it.start, total)
            const span = Math.max(0.6, pctOf(it.end - it.start, total))
            return (
              <div
                key={`${it.item_id}-${i}`}
                className={cn(
                  'absolute top-1 bottom-1 flex items-center justify-center overflow-hidden rounded text-[10px] font-medium shadow',
                  PACKAGING_KIND_COLOR[it.kind],
                )}
                style={{ left: `${left}%`, width: `calc(${span}% - 1px)` }}
                title={
                  it.text
                    ? `${PACKAGING_KIND_LABEL[it.kind]} · ${it.text}`
                    : `${PACKAGING_KIND_LABEL[it.kind]} · ${(it.end - it.start).toFixed(1)}s`
                }
              >
                <span className="truncate px-1">{it.text || PACKAGING_KIND_LABEL[it.kind]}</span>
              </div>
            )
          })
        )}
      </TrackRow>
      )}

      {/* ===================== BGM 轨 ===================== */}
      {showSecondaryTracks && (
      <TrackRow
        label="BGM 轨"
        hint={
          bgm?.track_url
            ? `${bgm.duration_seconds?.toFixed(1) ?? '?'}s · vol ${bgm.volume.toFixed(2)} · ${bgm.duck_with_voice ? 'ducking' : 'no-duck'}`
            : '未绑定'
        }
        actions={
          !readOnly ? (
            <div className="flex flex-col items-stretch gap-1">
              <div className="flex items-center gap-1">
                {onPickBgm && (
                  <button
                    onClick={onPickBgm}
                    disabled={busy}
                    className="rounded border border-primary/40 bg-primary/10 px-2 py-0.5 text-[10px] text-primary hover:bg-primary/20 disabled:opacity-50"
                  >
                    {bgm?.track_url ? '换曲' : '上传 / 选择'}
                  </button>
                )}
                {bgm?.track_url && onClearBgm && (
                  <button
                    onClick={() => void onClearBgm()}
                    disabled={busy}
                    className="rounded border border-border bg-background/60 px-2 py-0.5 text-[10px] text-muted-foreground hover:bg-background disabled:opacity-50"
                  >
                    清除
                  </button>
                )}
              </div>
              {bgm?.track_url && onBgmVolumeChange && (
                <label
                  className="flex items-center gap-1 text-[10px] text-muted-foreground"
                  title="拖动 BGM 音量（0 ~ 1.0）；300ms debounce 后落到 plan.bgm.volume"
                >
                  <span className="shrink-0">vol</span>
                  <input
                    type="range"
                    min={0}
                    max={1}
                    step={0.05}
                    value={volumeDraft ?? bgm.volume}
                    onChange={(e) => handleVolumeChange(parseFloat(e.target.value))}
                    disabled={busy}
                    className="h-1 w-full cursor-pointer accent-primary disabled:cursor-not-allowed"
                  />
                  <span className="w-7 shrink-0 tabular-nums text-right">
                    {(volumeDraft ?? bgm.volume).toFixed(2)}
                  </span>
                </label>
              )}
            </div>
          ) : null
        }
        rowRef={bgmRowRef}
      >
        {bgm?.track_url && bgm.duration_seconds ? (
          <BgmStripe
            bgm={bgm}
            total={total}
            draggingAnchor={draggingAnchor}
            onMouseDown={readOnly ? () => {} : handleAnchorMouseDown}
          />
        ) : (
          <div className="absolute inset-1 flex items-center justify-center rounded-md border border-dashed border-border bg-background/30 text-[10px] text-muted-foreground">
            还没绑定 BGM（上传 MP3 / WAV，自动分析峰值）
          </div>
        )}
      </TrackRow>
      )}

      {/* ============== 图例 ============== */}
      <div className="flex flex-wrap items-center gap-2 pt-1 text-[10px] text-muted-foreground">
        <span>段色：</span>
        {(Object.entries(SECTION_LABEL) as [SectionRole, string][]).map(([role, label]) => (
          <span key={role} className="inline-flex items-center gap-1">
            <span className={cn('inline-block h-2 w-3 rounded', SECTION_BG[role])} />
            {label}
          </span>
        ))}
        <span className="ml-2">状态：</span>
        <span className="inline-flex items-center gap-1">
          <span className="inline-block h-2 w-2 rounded-full bg-emerald-500" /> 已补
        </span>
        <span className="inline-flex items-center gap-1">
          <span className="inline-block h-2 w-2 rounded-full bg-amber-500" /> 待调
        </span>
        <span className="inline-flex items-center gap-1">
          <span className="inline-block h-2 w-2 rounded-full bg-rose-500" /> 缺失
        </span>
      </div>
    </div>
  )
}

/* ---------------- 私有子组件 ---------------- */

function TrackRow({
  label,
  hint,
  labelExtra,
  actions,
  children,
  rowRef,
}: {
  label: string
  hint?: string
  labelExtra?: React.ReactNode
  actions?: React.ReactNode
  children: React.ReactNode
  rowRef?: React.RefObject<HTMLDivElement | null>
}) {
  return (
    <div className="grid grid-cols-[88px_1fr] items-stretch gap-1">
      <div className="flex flex-col items-start justify-center gap-0.5 pr-1 text-[11px]">
        <div className="flex w-full items-center gap-1.5">
          <span className="font-semibold text-foreground">{label}</span>
          {labelExtra}
        </div>
        {hint && <span className="text-[10px] text-muted-foreground">{hint}</span>}
        {actions}
      </div>
      <div
        ref={rowRef ?? undefined}
        className="relative h-12 rounded-md border border-border bg-background/40"
      >
        {children}
      </div>
    </div>
  )
}

function BgmStripe({
  bgm,
  total,
  draggingAnchor,
  onMouseDown,
}: {
  bgm: BGMConfig
  total: number
  draggingAnchor: number | null
  onMouseDown: (e: React.MouseEvent) => void
}) {
  const duration = bgm.duration_seconds ?? 0
  // 整条 row = [-duration, total]；BGM 块自身占其原始 duration
  const rangeMin = -duration
  const rangeMax = total
  const rangeSpan = rangeMax - rangeMin || 1

  const anchor = draggingAnchor ?? bgm.video_anchor_seconds
  // BGM 块的「视觉起点」= anchor，视觉宽度 = duration（同一映射比例）
  const blockLeft = ((anchor - rangeMin) / rangeSpan) * 100
  const blockWidth = (duration / rangeSpan) * 100

  // peak 在 BGM 内部的相对偏移
  const peakRel =
    bgm.peak_seconds != null && bgm.peak_seconds >= 0 && duration > 0
      ? (bgm.peak_seconds / duration) * blockWidth
      : null

  // 视频可见区域 [0, total]，用浅色背景标出来；超出范围部分会被裁掉视觉感
  const visibleLeft = ((0 - rangeMin) / rangeSpan) * 100
  const visibleWidth = ((total - 0) / rangeSpan) * 100

  return (
    <div className="absolute inset-1 select-none">
      {/* 视频时间轴可见窗（浅底色） */}
      <div
        className="absolute inset-y-0 rounded-sm bg-foreground/[0.04]"
        style={{ left: `${visibleLeft}%`, width: `${visibleWidth}%` }}
      />
      {/* BGM 块 */}
      <div
        className={cn(
          'absolute inset-y-1 cursor-grab rounded border border-violet-400/60 bg-gradient-to-r from-violet-400/30 to-fuchsia-400/30 active:cursor-grabbing',
          draggingAnchor != null && 'ring-2 ring-violet-400/60',
        )}
        style={{ left: `${blockLeft}%`, width: `${blockWidth}%` }}
        onMouseDown={onMouseDown}
        title={`BGM anchor = ${anchor.toFixed(1)}s（拖动改起点）\n正值=视频先静音；负值=跳过 BGM 头`}
      >
        {/* peak 标记 */}
        {peakRel != null && (
          <span
            className="absolute -top-2 -translate-x-1/2 rounded bg-rose-500 px-1 text-[9px] font-bold text-white shadow"
            style={{ left: `${peakRel}%` }}
            title={`AI 探测峰值 @ ${bgm.peak_seconds?.toFixed(1)}s`}
          >
            ↑peak
          </span>
        )}
        <span className="absolute inset-0 flex items-center justify-center text-[10px] font-semibold text-violet-900 dark:text-violet-200">
          BGM · {anchor.toFixed(1)}s
        </span>
      </div>
      {/* 起点参考线（视频 t=0） */}
      <div
        className="absolute inset-y-0 border-l border-dashed border-foreground/40"
        style={{ left: `${visibleLeft}%` }}
        title="视频 t=0"
      />
    </div>
  )
}
