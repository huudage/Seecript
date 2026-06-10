import { useCallback, useEffect, useMemo, useRef, useState } from 'react'

import { cn } from '@/lib/utils'
import { getSectionMeta } from '@/lib/sections'
import { TRANSITION_LABEL, TRANSITION_TONE } from '@/lib/transitions'
import { BgmAnalysisCard } from './BgmAnalysisCard'
import type {
  AdaptedSection,
  BGMConfig,
  FillResult,
  Gap,
  GapStatus,
  Material,
  PackagingItem,
  Plan,
  SampleManifest,
  Scene,
  TextCardSpec,
  TransitionStyle,
  TTSVoice,
} from '@/types/schemas'

/**
 * Compose 页四轨横向工作面板：
 *
 *   时间轴标尺
 *   ─────────────────────────────────────────
 *   内容轨   [scene-0]  [scene-1]  [scene-2]  ...
 *   字幕轨   [字幕-0]   [字卡画面] [字幕-2]    ...   ← subtitle_enabled 开关；step2 起就可见
 *   口播轨   [TTS-0]    [TTS-1]    [TTS-2]    ...   ← voiceover_enabled 开关；step3 才显
 *   包装轨   [转场] [封面] [标题] [贴纸] ...        ← step3 才显
 *   BGM 轨   [════════════ track ↑peak ⇄anchor ═════════]  ← step3 才显
 *
 * 设计取舍：
 * - 每轨宽度 = plan.duration_seconds 等距映射到容器宽度；scene 块按 start/duration 定位。
 * - 内容轨色按 section role；点击 = 选中段对应 gap（在父级控制 Fill 面板）。
 * - 字幕轨默认关；开了后 scene.narration 作为字幕；`text_card_spec` 非空段渲染"字卡画面"灰格跳过字幕。
 * - 口播轨默认关；开了后展示每段 TTS 合成状态徽章 + 单段/全段合成按钮。
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
  /** 点击口播轨某段 → 通知父级选中 scene 做 TTS 操作。 */
  onSelectVoice?: (scene: Scene) => void
  /** 点击字幕轨某段 → 唤出字幕浮窗（R3：step3 字幕手动编辑入口）。 */
  onEditSubtitle?: (scene: Scene) => void
  /** 点击包装轨某项 → 通知父级选中 PackagingItem 做包装 NL 编辑。 */
  onSelectPackaging?: (item: PackagingItem) => void
  /** 当前选中的 scene（用于内容轨/字幕轨/口播轨高亮）。 */
  selectedSceneId?: string | null
  /** 当前选中的包装项 item_id（用于包装轨高亮）。 */
  selectedPackagingItemId?: string | null
  /** 单段 TTS 合成。 */
  onSynthesizeScene?: (sceneId: string) => void | Promise<void>
  /** 一键全段 TTS 合成。 */
  onSynthesizeAll?: () => void | Promise<void>
  /** 删除某段 TTS 音频。 */
  onClearVoice?: (sceneId: string) => void | Promise<void>
  /** 触发"一键包装推荐"。 */
  onRecommendPackaging?: () => void | Promise<void>
  /** 删除包装项——走 DELETE /packaging/items/{plan_id}/{item_id}。 */
  onDeletePackagingItem?: (itemId: string) => void | Promise<void>
  /** 智能添加包装组件：父级负责 sceneId+kind 选择 + recommend-for-scene → place 链路。 */
  onRecommendPackagingForScene?: (sceneId: string, kind: 'title_bar' | 'sticker' | 'cover') => void | Promise<void>
  /** 触发上传 BGM 弹窗（父级控制 Asset library 选择 / 上传 UI）。 */
  onPickBgm?: () => void
  /** 拖动 BGM anchor 到新位置（秒，可正可负）。 */
  onBgmAnchorChange?: (newAnchorSeconds: number) => void | Promise<void>
  /** 清除 BGM 绑定。 */
  onClearBgm?: () => void | Promise<void>
  /** 调整 BGM 音量（0 ~ 1）。组件内部 debounce 300ms 后才会触发。 */
  onBgmVolumeChange?: (volume: number) => void | Promise<void>
  /** 翻转 plan.settings.subtitle_enabled——字幕轨左侧开关。 */
  onToggleSubtitle?: (enabled: boolean) => void | Promise<void>
  /** 翻转 plan.settings.voiceover_enabled——口播轨左侧开关（仅 step3 展现）。 */
  onToggleVoiceover?: (enabled: boolean) => void | Promise<void>
  /** 切换 plan.settings.tts_voice——口播轨上选音色后写回 plan. */
  onChangeTtsVoice?: (voice: TTSVoice) => void | Promise<void>
  /** 处于"批量合成中"等忙状态时禁用所有按钮。 */
  busy?: boolean
  /** 只读模式：渲染页用——隐藏所有写操作按钮，保留 onSelectScene 触发自然语言编辑。 */
  readOnly?: boolean
  /**
   * 展示阶段：
   * - 'content-only'：内容轨 + 字幕轨展开（让用户在 step2 就能调字幕），口播 / 包装 / BGM 三轨隐藏。
   * - 'full'（默认）：五轨全展开（内容 / 字幕 / 口播 / 包装 / BGM）。
   */
  phase?: 'content-only' | 'full'
  /**
   * 内容轨渲染粒度：
   * - 'shots'（默认）：每个分镜一个独立块（stage-24 行为，step3 实时预览用）。
   * - 'sections'：每个 AdaptedSection 聚合成一个块（step2 补全场景用，方便以片段为单位选段补缺口）。
   */
  contentTrackMode?: 'shots' | 'sections'
  /** Remotion Player 当前秒；驱动轨道上的垂直播放头。 */
  playheadSeconds?: number
  /** 标尺 / 空白区域 / scene block 点击时回调（秒）→ Player seek。 */
  onSeek?: (seconds: number) => void
  /**
   * 1-2 条参考样例 manifest。渲染在内容轨上方,提供"样例 vs 迁移"并排对照。
   * - 长度 1:展示一条 A 样例轨
   * - 长度 2:展示两条样例轨 (A 在上、B 紧随)
   * - undefined / 空:不渲染样例轨,保持旧布局
   */
  referenceManifests?: SampleManifest[]
  /**
   * 用户素材库——内容轨缩略图按 scene.source_ref 反查 thumbnail_url。
   * 缺失时回落到色块占位。
   */
  materials?: Material[]
  /**
   * 已采纳的 fills——内容轨 AIGC 段按 fill.cover_url 拿首帧；
   * 字卡段不依赖 fill（Scene.text_card_spec 已携带规格直接 CSS 复刻）。
   */
  fills?: FillResult[]
  /** @deprecated 拖动会触发 step 跳回 step2，且与 click-to-edit 弹窗模型语义重叠；保留 prop 仅供老调用方编译通过。 */
  onMovePackagingItem?: (itemId: string, newStartSeconds: number) => void | Promise<void>
  /** 拉伸包装项时长（剪映式拖手柄）：传 newStart 和 newEnd（秒），父级走 update_packaging_item_time op 落盘。 */
  onResizePackagingItem?: (itemId: string, newStart: number, newEnd: number) => void | Promise<void>
  /** 打开"包装方案"侧抽屉——配合 actions 区"打开方案 ⤢"按钮。 */
  onOpenPackagingDrawer?: () => void
  /** 点击包装组件 → 父级唤起编辑弹窗（PR-I.2：替代拖动平移与重生按钮）。 */
  onEditPackagingItem?: (item: PackagingItem) => void
  /** 点击分镜间的转场节点 → 唤起转场样式选择弹窗。 */
  onEditTransition?: (sceneId: string, currentStyle: TransitionStyle | null) => void
  /**
   * stage-37：sections 模式下 ✏ 段编辑按钮 → 唤起 SectionEditDialog（theme/content_description）。
   * 与 onSelectScene 互不冲突——选段为 Fill 工作台服务，编辑走弹窗。
   */
  onEditSection?: (section: AdaptedSection, firstScene: Scene) => void
  /**
   * stage-37：sections 模式下展开后点单个分镜小块 → 唤起 ShotEditDialog
   * （subject/visual/narration 三字段双写 Scene + ShotPlan）。
   */
  onEditShot?: (scene: Scene, section: AdaptedSection) => void
}

const STATUS_GLYPH: Record<GapStatus, string> = { ok: '✓', warn: '!', miss: '×' }

/**
 * R3 后段色统一中性，无法区分相邻片段；这里按 AdaptedSection.section_id 顺序循环
 * 分配一组高饱和外边框色，让"哪几镜属于同一段"在内容轨上一眼可见。仅做视觉分组，
 * 与缺口状态正交——状态信息已由左侧红条/右上角徽章承担。
 */
const SECTION_BORDER_PALETTE = [
  'border-rose-400',
  'border-amber-400',
  'border-emerald-400',
  'border-sky-400',
  'border-violet-400',
  'border-fuchsia-400',
  'border-orange-400',
  'border-teal-400',
]

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

/**
 * 包装轨 3 轨道分配——贪心算法，按 start 升序遍历，每一项放进第一条不冲突的 lane（0/1/2）。
 * 第 4 条及以后超出的项目会回退到 lane 0（视觉上仍可见但叠在底层）。
 *
 * 之所以做：用户反馈一条轨道不好调整；多个 packaging item 时间窗重叠时挤一行看不清，
 * 也无法精准点中下面那一项。3 轨够用，超出概率极低。
 */
function assignPackagingLanes(items: PackagingItem[]): Map<string, number> {
  const sorted = [...items].sort((a, b) => a.start - b.start)
  const lanes: number[] = [0, 0, 0] // 每条 lane 当前已占到的 end 时间
  const result = new Map<string, number>()
  for (const it of sorted) {
    let placed = false
    for (let i = 0; i < lanes.length; i++) {
      if (it.start >= lanes[i] - 1e-3) {
        lanes[i] = it.end
        result.set(it.item_id, i)
        placed = true
        break
      }
    }
    if (!placed) {
      // 4+ 重叠：放进当前最早结束的 lane（仍重叠但视觉上次之）
      let minEnd = lanes[0]
      let minIdx = 0
      for (let i = 1; i < lanes.length; i++) {
        if (lanes[i] < minEnd) { minEnd = lanes[i]; minIdx = i }
      }
      lanes[minIdx] = Math.max(lanes[minIdx], it.end)
      result.set(it.item_id, minIdx)
    }
  }
  return result
}

/**
 * 字卡 mini 预览——按 TextCardSpec 用 CSS 复刻一张缩略图，让内容轨片段一眼看到画面长啥样。
 * 与 FillCopyPanel 的 CardPreview 风格对齐，但更小更密——只显示主标题首 2 字 + 颜色块。
 */
function TextCardThumb({ spec }: { spec: TextCardSpec }) {
  const bgStyle: React.CSSProperties = (() => {
    switch (spec.bg_mode) {
      case 'gradient':
        return { background: `linear-gradient(135deg, ${spec.bg_color} 0%, ${spec.accent_color} 100%)` }
      case 'dark_overlay':
        return { background: spec.bg_color, boxShadow: 'inset 0 0 0 9999px rgba(0,0,0,0.45)' }
      case 'image_blur':
        return { background: `radial-gradient(circle at 30% 30%, ${spec.accent_color}55, ${spec.bg_color})` }
      default:
        return { background: spec.bg_color }
    }
  })()
  const head = (spec.main_text || '字').slice(0, 4)
  return (
    <div
      className="flex h-full w-full flex-col items-center justify-center overflow-hidden text-center font-bold leading-none"
      style={{ ...bgStyle, color: spec.text_color }}
    >
      <span className="text-[10px]" style={{ letterSpacing: spec.font_family === 'tech_mono' ? '0.05em' : 'normal' }}>
        {head}
      </span>
      {spec.emoji_decor.length > 0 && (
        <span className="mt-0.5 text-[8px] leading-none">{spec.emoji_decor.slice(0, 2).join('')}</span>
      )}
    </div>
  )
}

/**
 * 缩略图 + 文字层——用在内容轨每个片段上，参考 Premiere/CapCut 的"封面+标签"渲染。
 * - 优先级：text_card_spec > fill.cover_url（aigc）> material.thumbnail_url（用户上传）> 兜底色块
 * - 缩略图占左侧固定 36px 宽（窄片段会自动让位）；文字行盖在右侧底部。
 */
function SceneThumb({
  scene,
  thumbnailUrl,
  textCardSpec,
}: {
  scene: Scene
  thumbnailUrl: string | null
  textCardSpec: TextCardSpec | null
}) {
  if (textCardSpec) {
    return <TextCardThumb spec={textCardSpec} />
  }
  if (thumbnailUrl) {
    return (
      <img
        src={thumbnailUrl}
        alt=""
        loading="lazy"
        className="h-full w-full object-cover"
      />
    )
  }
  // 兜底：source 类型字符
  const glyph =
    scene.source === 'aigc_t2v' ? 'AI'
    : scene.source === 'aigc_image' ? '图'
    : scene.source === 'user_material' ? '素'
    : scene.source === 'sample' ? '样'
    : '字'
  return (
    <div className="flex h-full w-full items-center justify-center bg-black/30 text-[10px] font-bold text-white/80">
      {glyph}
    </div>
  )
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
  onSelectVoice,
  onEditSubtitle,
  onSelectPackaging,
  selectedSceneId = null,
  selectedPackagingItemId = null,
  onSynthesizeScene,
  onSynthesizeAll,
  onClearVoice,
  onDeletePackagingItem,
  onRecommendPackagingForScene,
  onPickBgm,
  onBgmAnchorChange,
  onClearBgm,
  onBgmVolumeChange,
  onToggleSubtitle,
  onToggleVoiceover,
  onChangeTtsVoice,
  busy = false,
  readOnly = false,
  phase = 'full',
  contentTrackMode = 'shots',
  playheadSeconds = 0,
  onSeek,
  referenceManifests,
  materials,
  fills,
  onEditPackagingItem,
  onEditTransition,
  onEditSection,
  onEditShot,
  onResizePackagingItem,
}: Props) {
  const total = plan.duration_seconds || 0
  const scenes = plan.main_track
  const packaging = plan.packaging_track
  const adapted = plan.adapted_sections
  const bgm = plan.bgm
  const subtitleEnabled = plan.settings.subtitle_enabled
  const voiceoverEnabled = plan.settings.voiceover_enabled
  const ticks = useMemo(() => makeTicks(total), [total])
  const showSecondaryTracks = phase === 'full'

  // stage-37：sections 模式下 ▾ 按钮展开看分镜，再点分镜 → ShotEditDialog
  const [expandedSectionIds, setExpandedSectionIds] = useState<Set<string>>(() => new Set())
  const toggleExpand = useCallback((sid: string) => {
    setExpandedSectionIds((prev) => {
      const next = new Set(prev)
      if (next.has(sid)) next.delete(sid)
      else next.add(sid)
      return next
    })
  }, [])

  // 播放头测量：取 timebar 1fr 列的实际像素位置 + board 容器位置，让红线从 board
  // 顶部到底部精确铺满，不再用 9999px 超长溢出。ResizeObserver 监听窗口缩放/字体变。
  const boardRef = useRef<HTMLDivElement>(null)
  const timeLaneRef = useRef<HTMLDivElement>(null)
  const [laneGeom, setLaneGeom] = useState<{ left: number; width: number; top: number; height: number } | null>(null)
  useEffect(() => {
    const lane = timeLaneRef.current
    const board = boardRef.current
    if (!lane || !board) return
    const update = () => {
      const lr = lane.getBoundingClientRect()
      const br = board.getBoundingClientRect()
      setLaneGeom({
        left: lr.left - br.left,
        width: lr.width,
        top: lr.top - br.top,
        height: br.height - (lr.top - br.top) - 4,
      })
    }
    update()
    const ro = new ResizeObserver(update)
    ro.observe(lane)
    ro.observe(board)
    window.addEventListener('resize', update)
    return () => {
      ro.disconnect()
      window.removeEventListener('resize', update)
    }
  }, [])

  // 内容轨缩略图查表（按 material_id 反查 user 上传素材的封面 / 按 section_id 反查 fill cover）
  const materialById = useMemo(() => {
    const m = new Map<string, Material>()
    ;(materials ?? []).forEach((it) => m.set(it.material_id, it))
    return m
  }, [materials])
  const fillBySectionId = useMemo(() => {
    const m = new Map<string, FillResult>()
    ;(fills ?? []).forEach((f) => {
      if (f.section_id) m.set(f.section_id, f)
    })
    return m
  }, [fills])

  // 包装轨守门：开口播时必须所有 scene 都已合成 wav；关口播则内容轨齐就行。
  // 取舍：scene.narration 为空那段不算"未合成"——视为天然静默，不会卡包装。
  // 注：subtitle_enabled 不影响包装就绪——字幕单独受 subtitle_enabled 控，不阻塞封面/转场等。
  const allVoicesReady = useMemo(() => {
    if (!voiceoverEnabled) return true
    if (scenes.length === 0) return false
    return scenes.every((sc) => !(sc.narration ?? '').trim() || !!sc.voiceover_url)
  }, [scenes, voiceoverEnabled])
  void allVoicesReady // 守门指标暂不显示在轨上，但保留计算便于之后接入

  // section_id → AdaptedSection 索引
  const sectionById = useMemo(() => {
    const m = new Map<string, AdaptedSection>()
    for (const sec of adapted) m.set(sec.section_id, sec)
    return m
  }, [adapted])

  // section_id → 外边框色（按段顺序循环）；让相邻片段在轨道上一眼区分
  const sectionBorderById = useMemo(() => {
    const m = new Map<string, string>()
    adapted.forEach((sec, i) => {
      m.set(sec.section_id, SECTION_BORDER_PALETTE[i % SECTION_BORDER_PALETTE.length])
    })
    return m
  }, [adapted])

  // 包装轨按 kind 分桶：subtitle 由 subtitle_enabled + 字幕轨直接画，包装轨只展示其它（标题/转场/封面/贴纸）
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
  /** 3 轨道分配——避免重叠包装项挤一行难选中。 */
  const packagingLanes = useMemo(() => assignPackagingLanes(nonSubtitleItems), [nonSubtitleItems])

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

  // 内容轨按片段聚合：step2 用 contentTrackMode='sections' 时,把每个 AdaptedSection 的
  // 多个分镜 Scene 合并成一个块,默认以片段为单位展示;补全/选段以 section 为单位更直观。
  // shots 模式仍按 Scene 逐一渲染（step3 实时预览需要看到每镜的真实时长与转场）。
  const sectionBlocks = useMemo(() => {
    type Block = { section: AdaptedSection; firstScene: Scene; scenesInSec: Scene[]; start: number; end: number }
    if (contentTrackMode !== 'sections') return [] as Block[]
    const blocks: Block[] = []
    for (const sec of adapted) {
      const inSec: Scene[] = []
      for (const sc of scenes) {
        const m = /sc-(\d+)/.exec(sc.scene_id)
        if (m && Number(m[1]) === sec.order) inSec.push(sc)
      }
      if (inSec.length === 0) continue
      const start = Math.min(...inSec.map((s) => s.start))
      const end = Math.max(...inSec.map((s) => s.start + s.duration))
      blocks.push({ section: sec, firstScene: inSec[0], scenesInSec: inSec, start, end })
    }
    return blocks
  }, [adapted, scenes, contentTrackMode])

  // section 块的选中态：当前选中的 scene 属于该 section 时整段高亮
  const selectedSectionId = useMemo(() => {
    if (contentTrackMode !== 'sections' || !selectedSceneId) return null
    const m = /sc-(\d+)/.exec(selectedSceneId)
    if (!m) return null
    const sec = adapted.find((s) => s.order === Number(m[1]))
    return sec?.section_id ?? null
  }, [adapted, contentTrackMode, selectedSceneId])

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

  /* ==================== 包装轨 row ref（被 packagingRowRef 仅作 DOM 测量用，已不再支持拖动平移）==================== */
  const packagingRowRef = useRef<HTMLDivElement | null>(null)

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
    <div ref={boardRef} className="relative space-y-2 rounded-lg border border-border bg-card p-4">
      {/* ===================== 时间标尺 ===================== */}
      <div className="grid grid-cols-[88px_1fr] items-center">
        <span className="text-[10px] font-semibold text-muted-foreground">时间轴</span>
        <div
          ref={timeLaneRef}
          className={cn(
            'relative h-5 border-b border-border',
            onSeek ? 'cursor-pointer' : undefined,
          )}
          onClick={
            onSeek
              ? (e) => {
                  const rect = e.currentTarget.getBoundingClientRect()
                  const x = e.clientX - rect.left
                  const ratio = rect.width > 0 ? x / rect.width : 0
                  onSeek(Math.max(0, Math.min(total, ratio * total)))
                }
              : undefined
          }
        >
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

      {/* 播放头 overlay：放在 board 顶层，按 timeLaneRef 实测位置铺一条精确长度的
          竖线。stage-37：step2 也展示——配合 onSeek 让用户拖动预览，
          step2 没真 Remotion Player 也能在画面上看到当前秒。 */}
      {total > 0 && laneGeom && (
        <div
          className="pointer-events-none absolute z-30 w-px bg-rose-500/95"
          style={{
            left: `${laneGeom.left + laneGeom.width * Math.max(0, Math.min(1, playheadSeconds / total))}px`,
            top: `${laneGeom.top}px`,
            height: `${Math.max(0, laneGeom.height)}px`,
            boxShadow: '0 0 6px rgba(244,63,94,0.6)',
          }}
        />
      )}

      {/* ===================== 样例参考轨（1-2 条,放在内容轨之上做并排对照） ===================== */}
      {referenceManifests && referenceManifests.length > 0 && referenceManifests.map((mf, idx) => {
        const slotLabel = referenceManifests.length === 1 ? '样例轨' : idx === 0 ? '样例 A' : '样例 B'
        const sampleTotal = mf.duration_seconds || mf.sections.reduce((m, s) => Math.max(m, s.end), 0) || 0
        return (
          <TrackRow
            key={`ref-${idx}-${mf.sample_id}`}
            label={slotLabel}
            hint={`${mf.sections.length} 段 · ${sampleTotal.toFixed(1)}s`}
          >
            {mf.sections.map((sec, sIdx) => {
              // 样例段时间轴尺度可能 != plan 时间轴。按 plan.duration_seconds 等比映射
              // (用户看的是结构对位,不是绝对时长对齐),这样一眼就能看出每段被压缩 / 拉伸到多长。
              const ratio = sampleTotal > 0 ? total / sampleTotal : 1
              const left = pctOf(sec.start * ratio, total)
              const width = pctOf((sec.end - sec.start) * ratio, total)
              const meta = getSectionMeta(sec.role)
              return (
                <div
                  key={`ref-${idx}-${sIdx}`}
                  className={cn(
                    'absolute top-1 bottom-1 overflow-hidden rounded-md border border-border/50 text-[10px] text-white shadow-sm',
                    meta.bg,
                    'opacity-80',
                  )}
                  style={{ left: `${left}%`, width: `calc(${width}% - 2px)` }}
                  title={`${meta.label} · ${sec.theme || ''}\n${sec.summary || ''}`}
                >
                  <div className="flex h-full flex-col justify-between p-1">
                    <span className="font-mono text-[9px] opacity-80">{meta.short}</span>
                    <span className="truncate text-[10px] font-semibold leading-tight">
                      {sec.theme || meta.label}
                    </span>
                  </div>
                </div>
              )
            })}
          </TrackRow>
        )
      })}

      {/* ===================== 内容轨（迁移后的方案轨,放在样例轨下方便对照） =====================
          contentTrackMode='sections'（step2 补全场景）：以 AdaptedSection 为最小块，
          每块汇总该段所有分镜的起止 + 缩略图（取首镜），方便用户以片段为单位查看缺口；
          点击块仍走 onSelectScene(firstScene, gap, section)，复用 step2 现有 Fill 面板逻辑。
          contentTrackMode='shots'（step3 默认）：保留 stage-24 的逐镜渲染，看转场看时长更精准。 */}
      <TrackRow
        label="内容轨"
        hint={
          contentTrackMode === 'sections'
            ? `${sectionBlocks.length} 段 · ${scenes.length} 镜`
            : `${scenes.length} 段`
        }
        thick
      >
        {contentTrackMode === 'sections' ? sectionBlocks.map((block) => {
          const { section, firstScene, scenesInSec, start, end } = block
          const left = pctOf(start, total)
          const width = pctOf(end - start, total)
          // 取该段任意一个 gap：优先未补 / 非 ok 的；与 shots 路径保持选择口径一致
          let gap: Gap | null = null
          for (const sc of scenesInSec) {
            const g = sceneToGap.get(sc.scene_id) ?? null
            if (g && !filledGapIds.has(g.gap_id) && g.status !== 'ok') {
              gap = g
              break
            }
            if (g && !gap) gap = g
          }
          const status: GapStatus | null = gap?.status ?? null
          const filled = gap && filledGapIds.has(gap.gap_id)
          const effectiveStatus: GapStatus = filled ? 'ok' : (status ?? 'ok')
          const selected = section.section_id === selectedSectionId
          const expanded = expandedSectionIds.has(section.section_id)
          const canExpand = scenesInSec.length > 1

          // 整段缩略图：跟 shots 模式相同的优先级，但只看首镜（代表画面）。
          const fillForSection = fillBySectionId.get(section.section_id) ?? null
          let thumbUrl: string | null = null
          if (fillForSection?.action === 'aigc' && fillForSection.cover_url) {
            thumbUrl = fillForSection.cover_url
          } else if (fillForSection?.action === 'aigc_image' && fillForSection.aigc_image_url) {
            thumbUrl = fillForSection.aigc_image_url
          } else if (firstScene.source === 'aigc_t2v') {
            thumbUrl = fillForSection?.cover_url ?? null
          } else if (firstScene.source === 'aigc_image') {
            thumbUrl = firstScene.aigc_image_url ?? fillForSection?.aigc_image_url ?? null
          } else if (firstScene.source === 'user_material') {
            const mat = materialById.get(firstScene.source_ref)
            thumbUrl = mat?.thumbnail_url ?? null
          }
          const textCardSpec =
            firstScene.text_card_spec ?? fillForSection?.text_card_spec ?? null

          // stage-37 展开态：把段块按真实 scene.start/duration 拆成 N 个独立小镜块
          if (expanded) {
            return (
              <div
                key={section.section_id}
                className="absolute top-1 bottom-1"
                style={{ left: `${left}%`, width: `calc(${width}% - 2px)` }}
              >
                {/* 段头条：✏ 编辑段 + ▾ 折叠回去；占 14px 高，下面才是分镜小块 */}
                <div className="absolute inset-x-0 top-0 z-[1] flex h-3.5 items-center gap-1 rounded-t bg-black/55 px-1">
                  <span className="rounded bg-white/15 px-1 font-mono text-[8px] text-white">
                    {getSectionMeta(section.role).short}
                  </span>
                  <span className="truncate text-[9px] font-semibold text-white/90">
                    {section.theme || getSectionMeta(section.role).label}
                  </span>
                  <button
                    type="button"
                    onClick={(e) => {
                      e.stopPropagation()
                      onEditSection?.(section, firstScene)
                    }}
                    title="编辑段（主题 / 描述）"
                    className="ml-auto inline-flex h-3 w-3 items-center justify-center rounded bg-white/20 text-[9px] text-white hover:bg-white/40"
                  >
                    ✏
                  </button>
                  <button
                    type="button"
                    onClick={(e) => {
                      e.stopPropagation()
                      toggleExpand(section.section_id)
                    }}
                    title="折叠回整段"
                    className="inline-flex h-3 w-3 items-center justify-center rounded bg-white/20 text-[9px] text-white hover:bg-white/40"
                  >
                    ▴
                  </button>
                </div>
                {/* 分镜小块 row：剩余高度，按 scene.start/duration 等比映射到段内 % */}
                <div className="absolute inset-x-0 top-3.5 bottom-0">
                  {scenesInSec.map((sc) => {
                    const innerLeft = ((sc.start - start) / Math.max(0.001, end - start)) * 100
                    const innerWidth = (sc.duration / Math.max(0.001, end - start)) * 100
                    const matFor = sc.source === 'user_material' ? materialById.get(sc.source_ref) : null
                    let scThumb: string | null = null
                    if (sc.source === 'aigc_t2v') scThumb = fillForSection?.cover_url ?? null
                    else if (sc.source === 'aigc_image') scThumb = sc.aigc_image_url ?? fillForSection?.aigc_image_url ?? null
                    else if (sc.source === 'user_material') scThumb = matFor?.thumbnail_url ?? null
                    const scTextCard = sc.text_card_spec ?? null
                    const isSelectedShot = selectedSceneId === sc.scene_id
                    // stage-58 G5：取本镜在 AdaptedSection.shots 里的 match_quality / match_score
                    const parentSec = sc.parent_section_id ? sectionById.get(sc.parent_section_id) : null
                    const shotPlan = parentSec?.shots?.find((s) => s.order === sc.shot_order) ?? null
                    const matchQuality = shotPlan?.match_quality ?? null
                    const matchScore = typeof shotPlan?.match_score === 'number' ? shotPlan.match_score : null
                    const matchedMat = shotPlan?.matched_material_id
                      ? materialById.get(shotPlan.matched_material_id)
                      : null
                    return (
                      <button
                        key={sc.scene_id}
                        type="button"
                        onClick={(e) => {
                          e.stopPropagation()
                          onEditShot?.(sc, section)
                        }}
                        className={cn(
                          'absolute inset-y-0 overflow-hidden rounded border text-left text-[9px] text-white shadow-sm transition-all',
                          getSectionMeta(section.role).bg,
                          isSelectedShot
                            ? 'z-10 border-white ring-2 ring-white/80 brightness-110'
                            : 'border-white/30 hover:brightness-110',
                        )}
                        style={{
                          left: `${innerLeft}%`,
                          width: `calc(${innerWidth}% - 1px)`,
                        }}
                        title={`第 ${sc.shot_order + 1} 镜 · ${sc.duration.toFixed(1)}s\n${sc.shot_subject || ''}\n${sc.narration || ''}\n点击：编辑本镜（subject / visual / narration）`}
                      >
                        <div className="absolute inset-0">
                          <SceneThumb scene={sc} thumbnailUrl={scThumb} textCardSpec={scTextCard} />
                          <div className="pointer-events-none absolute inset-x-0 bottom-0 h-4 bg-gradient-to-t from-black/70 to-transparent" />
                        </div>
                        {sc.source === 'user_material' && (
                          // stage-60: 分镜匹配状态徽章（替代旧 2px 圆点）——用户报障"是否匹配成功无清晰指示"
                          // 优先级：needs_fill=true → 红"待补"；matched_material_id 有 → 绿"✓"；否则 → 灰"—"
                          // 鼠标悬停 tooltip 给出 match_quality / match_score / 命中素材文件名
                          <span
                            className={cn(
                              'pointer-events-none absolute left-0.5 top-0.5 z-[2] rounded px-1 text-[8px] font-bold leading-tight text-white shadow-sm',
                              sc.needs_fill === true
                                ? 'bg-rose-500/95'
                                : (shotPlan?.matched_material_id || matchQuality === 'good')
                                  ? 'bg-emerald-500/95'
                                  : matchQuality === 'weak'
                                    ? 'bg-amber-500/95'
                                    : 'bg-zinc-500/85',
                            )}
                            title={
                              sc.needs_fill === true
                                ? '本镜待补：素材未匹配上 / 兜底字卡占位'
                                : shotPlan?.matched_material_id
                                  ? `已匹配${matchQuality ? `（${matchQuality === 'good' ? '准' : matchQuality === 'weak' ? '弱' : '缺'}）` : ''}` +
                                    (matchScore !== null ? `\n匹配分 ${(matchScore * 100).toFixed(0)}%` : '') +
                                    (matchedMat?.filename ? `\n命中素材：${matchedMat.filename}` : '')
                                  : '未匹配（顺位兜底）'
                            }
                          >
                            {sc.needs_fill === true
                              ? '待补'
                              : shotPlan?.matched_material_id
                                ? '✓'
                                : '—'}
                          </span>
                        )}
                        {sc.source === 'user_material' && typeof sc.fit_score === 'number' && (
                          // stage-59：素材-段落 适配度徽章。绿/黄/红三档，不抢戏但一眼看到「这条搭不搭」
                          <span
                            className={cn(
                              'pointer-events-none absolute right-0.5 top-0.5 z-[2] rounded px-1 text-[8px] font-bold leading-tight text-white shadow-sm',
                              sc.fit_score >= 0.7
                                ? 'bg-emerald-500/90'
                                : sc.fit_score >= 0.4
                                  ? 'bg-amber-500/90'
                                  : 'bg-rose-500/90',
                            )}
                            title={`素材适配度 ${(sc.fit_score * 100).toFixed(0)}%${sc.fit_reason ? `\n${sc.fit_reason}` : ''}`}
                          >
                            {(sc.fit_score * 100).toFixed(0)}
                          </span>
                        )}
                        <div className="relative z-[1] flex h-full flex-col justify-end p-0.5">
                          <span className="truncate rounded bg-black/45 px-1 text-[9px] font-semibold leading-tight">
                            #{sc.shot_order + 1} · {sc.shot_subject || sc.scene_id}
                          </span>
                        </div>
                      </button>
                    )
                  })}
                </div>
              </div>
            )
          }

          return (
            <button
              key={section.section_id}
              onClick={() => onSelectScene(firstScene, gap, section)}
              className={cn(
                'absolute top-1 bottom-1 overflow-hidden rounded-md border-2 text-left text-[10px] text-white shadow-sm transition-all',
                getSectionMeta(section.role).bg,
                sectionBorderById.get(section.section_id) ?? 'border-border',
                selected
                  ? 'z-10 scale-[1.02] ring-4 ring-white ring-offset-2 ring-offset-card shadow-lg brightness-110'
                  : 'hover:brightness-110',
              )}
              style={{ left: `${left}%`, width: `calc(${width}% - 2px)` }}
              title={`${getSectionMeta(section.role).label} · ${section.theme}\n${section.content_description}\n（含 ${scenesInSec.length} 镜，✏ 编辑段、▾ 展开分镜逐镜编辑）`}
            >
              <div className="absolute inset-0">
                <SceneThumb scene={firstScene} thumbnailUrl={thumbUrl} textCardSpec={textCardSpec} />
                <div className="pointer-events-none absolute inset-x-0 top-0 h-4 bg-gradient-to-b from-black/55 to-transparent" />
                <div className="pointer-events-none absolute inset-x-0 bottom-0 h-6 bg-gradient-to-t from-black/65 to-transparent" />
                {effectiveStatus === 'miss' && (
                  <div
                    className="pointer-events-none absolute inset-y-0 left-0 w-1 bg-rose-500"
                    title="该段尚未补齐素材"
                  />
                )}
                {/* 多镜段：右上角悬挂分镜竖线刻度，让用户直观感觉到「这段被切成几镜」 */}
                {scenesInSec.length > 1 && (
                  <div className="pointer-events-none absolute inset-0">
                    {scenesInSec.slice(1).map((sc) => {
                      const innerLeft = ((sc.start - start) / Math.max(0.001, end - start)) * 100
                      return (
                        <div
                          key={`tick-${sc.scene_id}`}
                          className="absolute inset-y-0 w-px bg-white/40"
                          style={{ left: `${innerLeft}%` }}
                        />
                      )
                    })}
                  </div>
                )}
              </div>
              <div className="relative z-[1] flex h-full flex-col justify-between p-1">
                <div className="flex items-center justify-between gap-1">
                  <span className="rounded bg-black/40 px-1 font-mono text-[9px] text-white">
                    {getSectionMeta(section.role).short}
                  </span>
                  <div className="flex items-center gap-1">
                    {scenesInSec.length > 1 && (
                      <span
                        className="inline-flex h-3 items-center justify-center rounded-full bg-violet-300/95 px-1 font-mono text-[9px] font-bold text-violet-900"
                        title={`本段拆为 ${scenesInSec.length} 个分镜（▾ 展开后可单独编辑）`}
                      >
                        {scenesInSec.length}镜
                      </span>
                    )}
                    {/* stage-59：单镜段时显示素材适配度（多镜段太挤就不挂了，分镜展开时各自有徽章） */}
                    {scenesInSec.length === 1 && firstScene.source === 'user_material' && typeof firstScene.fit_score === 'number' && (
                      <span
                        className={cn(
                          'inline-flex h-3 items-center justify-center rounded-full px-1 font-mono text-[9px] font-bold',
                          firstScene.fit_score >= 0.7
                            ? 'bg-emerald-300 text-emerald-900'
                            : firstScene.fit_score >= 0.4
                              ? 'bg-amber-300 text-amber-900'
                              : 'bg-rose-300 text-rose-900',
                        )}
                        title={`素材适配度 ${(firstScene.fit_score * 100).toFixed(0)}%${firstScene.fit_reason ? `\n${firstScene.fit_reason}` : ''}`}
                      >
                        {(firstScene.fit_score * 100).toFixed(0)}%
                      </span>
                    )}
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
                    {/* stage-37：✏ 编辑段、▾ 展开看分镜——挂在角标行右侧
                        stage-60：去掉 canExpand 门禁——上游 dedup / 视觉差异度门禁会把
                        近重复 sub-shot 折叠到 1 镜，单镜段也允许展开进入分镜级编辑入口，
                        避免「明明本来有展开按钮，重新生成后没了」的体感。 */}
                    {onEditSection && (
                      <span
                        role="button"
                        tabIndex={0}
                        onClick={(e) => {
                          e.stopPropagation()
                          onEditSection(section, firstScene)
                        }}
                        onKeyDown={(e) => {
                          if (e.key === 'Enter' || e.key === ' ') {
                            e.preventDefault()
                            e.stopPropagation()
                            onEditSection(section, firstScene)
                          }
                        }}
                        title="编辑段（主题 / 描述）"
                        className="inline-flex h-3 w-3 cursor-pointer items-center justify-center rounded bg-white/70 text-[9px] font-bold text-foreground hover:bg-white"
                      >
                        ✏
                      </span>
                    )}
                    {onEditShot && (
                      <span
                        role="button"
                        tabIndex={0}
                        onClick={(e) => {
                          e.stopPropagation()
                          toggleExpand(section.section_id)
                        }}
                        onKeyDown={(e) => {
                          if (e.key === 'Enter' || e.key === ' ') {
                            e.preventDefault()
                            e.stopPropagation()
                            toggleExpand(section.section_id)
                          }
                        }}
                        title={canExpand ? '展开看分镜（点击各分镜独立编辑）' : '展开本段进入分镜编辑（单镜段）'}
                        className="inline-flex h-3 w-3 cursor-pointer items-center justify-center rounded bg-white/70 text-[9px] font-bold text-foreground hover:bg-white"
                      >
                        ▾
                      </span>
                    )}
                  </div>
                </div>
                <div className="truncate rounded bg-black/40 px-1 text-[10px] font-semibold leading-tight text-white">
                  {section.theme || getSectionMeta(section.role).label}
                </div>
              </div>
            </button>
          )
        }) : scenes.map((scene) => {
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
          const selected = scene.scene_id === selectedSceneId || (gap != null && gap.gap_id === selectedGapId)
          // 转场标块已抽离到 scenes 循环之外的兄弟层——button 的 overflow-hidden 会把
          // 负偏移的徽章裁掉；按片段边界绘制更能体现"接缝"语义。

          // 缩略图来源——剪辑软件那种"片段封面"
          const fillForSection = section ? fillBySectionId.get(section.section_id) ?? null : null
          let thumbUrl: string | null = null
          // 优先用最新 fill 的 cover——批量补全后 fills 已经 set 但 plan 还没 rebuild 完，
          // 这一步能让 AIGC 封面/字卡画面立刻闪出来，避免"生成完毕但预览还是旧的"体感。
          if (fillForSection?.action === 'aigc' && fillForSection.cover_url) {
            thumbUrl = fillForSection.cover_url
          } else if (fillForSection?.action === 'aigc_image' && fillForSection.aigc_image_url) {
            // aigc_image：直接用本地化后的 /aigc-images/ 路径当封面缩略图
            thumbUrl = fillForSection.aigc_image_url
          } else if (scene.source === 'aigc_t2v') {
            thumbUrl = fillForSection?.cover_url ?? null
          } else if (scene.source === 'aigc_image') {
            thumbUrl = scene.aigc_image_url ?? fillForSection?.aigc_image_url ?? null
          } else if (scene.source === 'user_material') {
            const mat = materialById.get(scene.source_ref)
            thumbUrl = mat?.thumbnail_url ?? null
          }
          // text_card_spec 优先级：scene.text_card_spec（plan 已重建后的权威值）
          //   > fillForSection.text_card_spec（fill 已落地但 plan 还没重建完，让内容轨先显出来）
          // 解决批量补全后内容轨预览"延迟刷新"——/plan/build 慢时也能立刻看到字卡。
          const textCardSpec =
            scene.text_card_spec ?? fillForSection?.text_card_spec ?? null

          return (
            <button
              key={scene.scene_id}
              onClick={() => onSelectScene(scene, gap, section)}
              className={cn(
                'absolute top-1 bottom-1 overflow-hidden rounded-md border-2 text-left text-[10px] text-white shadow-sm transition-all',
                getSectionMeta(scene.section).bg,
                section ? sectionBorderById.get(section.section_id) ?? 'border-border' : 'border-border',
                selected
                  ? 'z-10 scale-[1.02] ring-4 ring-white ring-offset-2 ring-offset-card shadow-lg brightness-110'
                  : 'hover:brightness-110',
              )}
              style={{ left: `${left}%`, width: `calc(${width}% - 2px)` }}
              title={
                section
                  ? `${getSectionMeta(scene.section).label} · ${section.theme}\n${section.content_description}`
                  : `${getSectionMeta(scene.section).label} · ${scene.duration.toFixed(1)}s`
              }
            >
              {/* 缩略图层：填满整段，文字 / 状态徽章浮在上层 */}
              <div className="absolute inset-0">
                <SceneThumb scene={scene} thumbnailUrl={thumbUrl} textCardSpec={textCardSpec} />
                {/* 顶部渐变：保证 short 标签可读 */}
                <div className="pointer-events-none absolute inset-x-0 top-0 h-4 bg-gradient-to-b from-black/55 to-transparent" />
                {/* 底部渐变：保证 theme 文字可读 */}
                <div className="pointer-events-none absolute inset-x-0 bottom-0 h-6 bg-gradient-to-t from-black/65 to-transparent" />
                {/* R3：缺失状态用 4px 左侧红条替代红色边框（保留 ! 徽章在右上） */}
                {effectiveStatus === 'miss' && (
                  <div
                    className="pointer-events-none absolute inset-y-0 left-0 w-1 bg-rose-500"
                    title="该段尚未补齐素材"
                  />
                )}
              </div>

              {/* 转场徽章已抽到 scenes 循环外的兄弟层（见 TrackRow 末尾）——button overflow-hidden
                  会裁掉负偏移的徽章，按真实接缝坐标渲染才能体现"两段之间"的语义。 */}
              <div className="relative z-[1] flex h-full flex-col justify-between p-1">
                <div className="flex items-center justify-between gap-1">
                  <span className="rounded bg-black/40 px-1 font-mono text-[9px] text-white">
                    {getSectionMeta(scene.section).short}
                  </span>
                  <div className="flex items-center gap-1">
                    {/* stage-24 分镜数徽章：当本段被拆为多个分镜时显示 N 镜 */}
                    {section?.shots && section.shots.length > 1 && (
                      <span
                        className="inline-flex h-3 items-center justify-center rounded-full bg-violet-300/95 px-1 font-mono text-[9px] font-bold text-violet-900"
                        title={`本段拆为 ${section.shots.length} 个分镜：${section.shots.map((sh) => sh.subject || `#${sh.order + 1}`).join(' / ')}`}
                      >
                        {section.shots.length}镜
                      </span>
                    )}
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
                </div>
                <div className="truncate rounded bg-black/40 px-1 text-[10px] font-semibold leading-tight text-white">
                  {section?.theme || getSectionMeta(scene.section).label}
                </div>
              </div>
            </button>
          )
        })}

        {/* 转场交互层：钉在相邻分镜的接缝上（scene.start = 上一段的 end）。
            stage-26：从包装轨迁来——把转场操作放回内容轨分镜之间，更直观。
            step2 不显示——转场属于「最后一公里」决策，与字幕/口播/包装/BGM 同 step3 解锁；
            进入 step3 时由 handleEnterStep3 自动跑一次 packaging/recommend → apply（只取 transitions）。
            点击徽章 → 父级唤起 TransitionEditDialog；hard_cut/null 显示「硬切」灰色基线。
            z-30 保证在 scene 按钮（z-10 selected）和 display-only 徽章之上。 */}
        {showSecondaryTracks && scenes.length > 1 && scenes.slice(1).map((sc, idx) => {
          const at = sc.start
          const left = pctOf(at, total)
          const trans = sc.transition_in ?? null
          const styleLabel = trans ? TRANSITION_LABEL[trans.style] : '硬切'
          const tone = trans ? TRANSITION_TONE[trans.style] : 'bg-slate-300/80 text-slate-900'
          const interactive = !!onEditTransition && !readOnly
          return (
            <button
              type="button"
              key={`transition-${sc.scene_id}-${idx}`}
              disabled={!interactive}
              onClick={(e) => {
                e.stopPropagation()
                onEditTransition?.(sc.scene_id, trans?.style ?? null)
              }}
              title={
                interactive
                  ? `转场到「${sc.scene_id}」：${styleLabel}\n点击：选择转场样式`
                  : `转场到「${sc.scene_id}」：${styleLabel}`
              }
              className={cn(
                'absolute top-1/2 z-30 inline-flex h-5 -translate-x-1/2 -translate-y-1/2 select-none items-center gap-0.5 rounded-full border border-white/70 px-1.5 text-[9px] font-semibold shadow-md',
                tone,
                interactive ? 'cursor-pointer hover:scale-110 hover:brightness-110' : 'cursor-default opacity-80',
              )}
              style={{ left: `${left}%` }}
            >
              <span>⇆</span>
              <span className="max-w-[44px] truncate">{styleLabel}</span>
            </button>
          )
        })}
      </TrackRow>

      {/* ===================== 字幕轨（step3 才显，与 TTS/包装/BGM 同步） =====================
          R3 设计取舍：step1 仅做"内容对位 + 缺失诊断"——拉字幕轨进去只会让用户分心；
          step2 已经把字幕轨彻底去除（包括字幕的文本编辑也搬到 step3 浮窗里）；
          字幕作为口播副产物（生成 / 编辑 / 烧入）一律放 step3。
      */}
      {showSecondaryTracks && (
      <TrackRow
        label="字幕轨"
        hint={
          subtitleEnabled
            ? `${scenes.filter((s) => s.text_card_spec == null && (s.narration ?? '').trim()).length}/${scenes.length} 段有字幕`
            : '已关闭'
        }
        labelExtra={
          onToggleSubtitle ? (
            <button
              onClick={() => void onToggleSubtitle(!subtitleEnabled)}
              disabled={busy || readOnly}
              role="switch"
              aria-checked={subtitleEnabled}
              title={subtitleEnabled ? '关闭字幕（视频不烧字幕）' : '开启字幕（AI 自动按每段口播文案生成）'}
              className={cn(
                'relative inline-flex h-4 w-8 shrink-0 items-center rounded-full transition-colors',
                subtitleEnabled ? 'bg-sky-500/80' : 'bg-muted-foreground/30',
                (busy || readOnly) && 'cursor-not-allowed opacity-60',
              )}
            >
              <span
                className={cn(
                  'inline-block h-3 w-3 transform rounded-full bg-white shadow transition-transform',
                  subtitleEnabled ? 'translate-x-4' : 'translate-x-0.5',
                )}
              />
            </button>
          ) : null
        }
      >
        {!subtitleEnabled ? (
          <div className="absolute inset-1 flex items-center justify-center rounded-md border border-dashed border-border bg-background/30 text-[10px] text-muted-foreground">
            字幕已关闭（开关启用后 AI 自动按段落生成字幕，可点击片段编辑）
          </div>
        ) : (
          scenes.map((scene) => {
            const left = pctOf(scene.start, total)
            const width = pctOf(scene.duration, total)
            const isTextCard = scene.text_card_spec != null
            const subText = (scene.narration ?? '').trim()
            const subSelected = selectedSceneId === scene.scene_id
            if (isTextCard) {
              // 字卡画面段：跳过字幕（字卡本身已显示主副标，再叠字幕会重复）
              return (
                <div
                  key={scene.scene_id}
                  className="absolute top-1 bottom-1 flex items-center justify-center overflow-hidden rounded-md border border-dashed border-border/60 bg-muted/40 text-[9px] text-muted-foreground"
                  style={{ left: `${left}%`, width: `calc(${width}% - 2px)` }}
                  title="本段使用字卡画面，已自带可读文字，无需再叠字幕"
                >
                  字卡画面 · 无需字幕
                </div>
              )
            }
            return (
              <div
                key={scene.scene_id}
                role="button"
                tabIndex={0}
                onClick={() => onEditSubtitle?.(scene)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' || e.key === ' ') {
                    e.preventDefault()
                    onEditSubtitle?.(scene)
                  }
                }}
                className={cn(
                  'absolute top-1 bottom-1 overflow-hidden rounded-md border text-left text-[10px] shadow-sm transition',
                  subText
                    ? 'border-sky-400/60 bg-sky-500/15 text-sky-700 dark:text-sky-200'
                    : 'border-dashed border-border bg-background/40 text-muted-foreground',
                  onEditSubtitle && 'cursor-pointer',
                  subSelected
                    ? 'ring-2 ring-primary'
                    : onEditSubtitle
                      ? 'hover:brightness-110'
                      : '',
                )}
                style={{ left: `${left}%`, width: `calc(${width}% - 2px)` }}
                title={
                  subText
                    ? `${subText}\n点击：手动编辑字幕`
                    : '本段无字幕文案 · 点击：手动补字幕'
                }
              >
                <div className="flex h-full flex-col gap-0.5 px-1 py-0.5">
                  <span className="font-mono text-[9px] opacity-70">{scene.scene_id}</span>
                  <span className="truncate text-[10px] leading-tight">
                    {subText || '（待生成）'}
                  </span>
                </div>
              </div>
            )
          })
        )}
      </TrackRow>
      )}

      {/* ===================== 口播轨（step3 才显，TTS 合成开关与音色） ===================== */}
      {showSecondaryTracks && (
      <TrackRow
        label="口播轨"
        hint={voiceoverEnabled ? `${scenes.filter((s) => s.voiceover_url).length}/${scenes.length} 已合成` : '已关闭（视频走纯背景音乐）'}
        labelExtra={
          onToggleVoiceover ? (
            <button
              onClick={() => void onToggleVoiceover(!voiceoverEnabled)}
              disabled={busy || readOnly}
              role="switch"
              aria-checked={voiceoverEnabled}
              title={voiceoverEnabled ? '关闭口播（视频走纯背景音乐）' : '开启口播（AI 自动配音）'}
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
                  title="选个配音音色，下一次合成生效"
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
                  title="按选中音色一次性给所有镜头合成配音，自动对齐镜头时长"
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
            口播已关闭（开启后 AI 会按每段文案自动合成配音）
          </div>
        ) : (
          scenes.map((scene) => {
            const left = pctOf(scene.start, total)
            const width = pctOf(scene.duration, total)
            const hasNarration = (scene.narration ?? '').trim().length > 0
            const hasAudio = !!scene.voiceover_url
            const state = hasAudio ? 'ready' : hasNarration ? 'pending' : 'empty'
            const voiceSelected = selectedSceneId === scene.scene_id
            return (
              <div
                key={scene.scene_id}
                role="button"
                tabIndex={0}
                onClick={() => onSelectVoice?.(scene)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' || e.key === ' ') {
                    e.preventDefault()
                    onSelectVoice?.(scene)
                  }
                }}
                className={cn(
                  'absolute top-1 bottom-1 overflow-hidden rounded-md border text-left text-[10px] shadow-sm transition',
                  state === 'ready'
                    ? 'border-emerald-400/60 bg-emerald-500/15 text-emerald-700 dark:text-emerald-300'
                    : state === 'pending'
                      ? 'border-amber-400/60 bg-amber-500/10 text-amber-700 dark:text-amber-300'
                      : 'border-dashed border-border bg-background/40 text-muted-foreground',
                  onSelectVoice && 'cursor-pointer',
                  voiceSelected ? 'ring-2 ring-primary' : onSelectVoice ? 'hover:brightness-110' : '',
                )}
                style={{ left: `${left}%`, width: `calc(${width}% - 2px)` }}
                title={
                  state === 'ready'
                    ? `已合成 · ${scene.narration ?? ''}\n点击：用自然语言改文案`
                    : state === 'pending'
                      ? `待合成 · ${scene.narration ?? ''}\n点击：用自然语言改文案`
                      : '这段没文案 · 点击：用自然语言生成'
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
                  <span className="truncate text-[9px] leading-tight">
                    {hasNarration
                      ? `${state === 'ready' ? '🎵 ' : state === 'pending' ? '⏳ ' : ''}${(scene.narration ?? '').trim()}`
                      : '无文案'}
                  </span>
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
        hint={`${nonSubtitleItems.length} 项 · 3 轨${subtitleItems.length > 0 ? ` · 字幕 ${subtitleItems.length} 项见字幕轨` : ''}`}
        rowRef={packagingRowRef}
        actions={
          !readOnly && onRecommendPackagingForScene ? (
            <PackagingSmartAddButton
              scenes={scenes}
              busy={busy}
              onPick={(sceneId, kind) => onRecommendPackagingForScene(sceneId, kind)}
            />
          ) : null
        }
        tall
      >
        {nonSubtitleItems.length === 0 ? (
          <div className="absolute inset-1 flex items-center justify-center rounded-md border border-dashed border-border bg-background/30 text-center text-[10px] text-muted-foreground">
            还没生成包装项——用顶部「✨ AI 包装方案」批量生成、左侧「✨ 添加组件」按段添加，或点轨上空白处插入
          </div>
        ) : (
          <>
            {/* 3 lane 分隔虚线（视觉提示） */}
            <div className="pointer-events-none absolute inset-x-0 top-1/3 h-px bg-border/30" />
            <div className="pointer-events-none absolute inset-x-0 top-2/3 h-px bg-border/30" />
            {nonSubtitleItems.map((it, i) => {
              const left = pctOf(it.start, total)
              const span = Math.max(0.6, pctOf(it.end - it.start, total))
              const pkgSelected = selectedPackagingItemId === it.item_id
              const canEdit = !!onEditPackagingItem && !readOnly && !busy
              const canDelete = !!onDeletePackagingItem && !readOnly && !busy
              const canResize = !!onResizePackagingItem && !readOnly && !busy
              const lane = packagingLanes.get(it.item_id) ?? 0
              // h-24 = 6rem = 96px，3 lane 每条 32px，留 1px 间隙
              const laneTop = lane * 32 + 2
              return (
                <PackagingItemBlock
                  key={`${it.item_id}-${i}`}
                  item={it}
                  index={i}
                  left={left}
                  width={span}
                  laneTopPx={laneTop}
                  pkgSelected={pkgSelected}
                  canEdit={canEdit}
                  canDelete={canDelete}
                  canResize={canResize}
                  total={total}
                  rowRef={packagingRowRef}
                  onEditPackagingItem={onEditPackagingItem}
                  onSelectPackaging={onSelectPackaging}
                  onDeletePackagingItem={onDeletePackagingItem}
                  onResizePackagingItem={onResizePackagingItem}
                />
              )
            })}
          </>
        )}
      </TrackRow>
      )}

      {/* ===================== BGM 轨 ===================== */}
      {showSecondaryTracks && (
      <TrackRow
        label="背景音乐"
        hint={
          bgm?.track_url
            ? `${bgm.duration_seconds?.toFixed(1) ?? '?'}s · 音量 ${bgm.volume.toFixed(2)} · ${bgm.duck_with_voice ? '口播时自动让音' : '不让音'}`
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
                  title="拖动调节背景音乐音量（0 ~ 1.0）"
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
            还没选背景音乐（上传 MP3 / WAV，自动分析高潮点）
          </div>
        )}
      </TrackRow>
      )}

      {/* BGM 分析卡：LLM 切的曲风 / 情绪 / 4-6 段结构 / 视频匹配建议 */}
      {showSecondaryTracks && bgm?.analysis && (
        <div className="mt-2">
          <BgmAnalysisCard analysis={bgm.analysis} />
        </div>
      )}

      {/* ============== 图例 ============== */}
      <div className="flex flex-wrap items-center gap-2 pt-1 text-[10px] text-muted-foreground">
        <span>状态：</span>
        <span className="inline-flex items-center gap-1">
          <span className="inline-block h-2 w-2 rounded-full bg-emerald-500" /> 已补
        </span>
        <span className="inline-flex items-center gap-1">
          <span className="inline-block h-2 w-2 rounded-full bg-amber-500" /> 待调
        </span>
        <span className="inline-flex items-center gap-1">
          <span className="inline-block h-3 w-1 rounded bg-rose-500" /> 缺失
        </span>
        <span className="ml-2 text-[10px] text-muted-foreground/70">
          · 段名以文字角标显示在内容块左上角
        </span>
      </div>
    </div>
  )
}

/* ---------------- 私有子组件 ---------------- */

function PackagingSmartAddButton({
  scenes,
  busy,
  onPick,
}: {
  scenes: Scene[]
  busy: boolean
  onPick: (sceneId: string, kind: 'title_bar' | 'sticker' | 'cover') => void | Promise<void>
}) {
  const [open, setOpen] = useState(false)
  const [pending, setPending] = useState(false)
  const [sceneId, setSceneId] = useState<string>(scenes[0]?.scene_id ?? '')
  const [kind, setKind] = useState<'title_bar' | 'sticker' | 'cover'>('title_bar')

  useEffect(() => {
    if (!sceneId && scenes[0]) setSceneId(scenes[0].scene_id)
  }, [scenes, sceneId])

  const submit = useCallback(async () => {
    if (!sceneId) return
    setPending(true)
    try {
      await onPick(sceneId, kind)
      setOpen(false)
    } finally {
      setPending(false)
    }
  }, [sceneId, kind, onPick])

  return (
    <div className="relative">
      <button
        onClick={() => setOpen((v) => !v)}
        disabled={busy || scenes.length === 0}
        className="rounded border border-primary/40 bg-primary/10 px-2 py-0.5 text-[10px] text-primary hover:bg-primary/20 disabled:opacity-50"
        title="智能添加包装组件（按段+类型走 LLM 推荐）"
      >
        ✨ 添加组件
      </button>
      {open && (
        <div
          className="absolute left-0 top-full z-30 mt-1 w-56 rounded-md border border-border bg-popover p-2 shadow-lg"
          onClick={(e) => e.stopPropagation()}
        >
          <div className="mb-1 text-[10px] text-muted-foreground">按段落智能生成单个包装项</div>
          <label className="mb-1 block text-[10px] text-muted-foreground">目标段</label>
          <select
            value={sceneId}
            onChange={(e) => setSceneId(e.target.value)}
            className="mb-2 w-full rounded border border-border bg-background px-1.5 py-1 text-[11px]"
          >
            {scenes.map((sc, idx) => (
              <option key={sc.scene_id} value={sc.scene_id}>
                {`段${idx + 1} · ${(sc.shot_subject || sc.narration || sc.scene_id).slice(0, 16)}`}
              </option>
            ))}
          </select>
          <label className="mb-1 block text-[10px] text-muted-foreground">类型</label>
          <select
            value={kind}
            onChange={(e) => setKind(e.target.value as 'title_bar' | 'sticker' | 'cover')}
            className="mb-2 w-full rounded border border-border bg-background px-1.5 py-1 text-[11px]"
          >
            <option value="title_bar">标题条</option>
            <option value="sticker">贴纸</option>
            <option value="cover">封面</option>
          </select>
          <div className="flex justify-end gap-1">
            <button
              onClick={() => setOpen(false)}
              disabled={pending}
              className="rounded border border-border bg-background px-2 py-0.5 text-[10px] hover:bg-secondary"
            >
              取消
            </button>
            <button
              onClick={() => void submit()}
              disabled={pending || !sceneId}
              className="rounded bg-primary px-2 py-0.5 text-[10px] text-primary-foreground hover:opacity-90 disabled:opacity-50"
            >
              {pending ? '生成中…' : '生成并落轨'}
            </button>
          </div>
        </div>
      )}
    </div>
  )
}

function TrackRow({
  label,
  hint,
  labelExtra,
  actions,
  children,
  rowRef,
  thick,
  tall,
  onDragOver,
  onDrop,
}: {
  label: string
  hint?: string
  labelExtra?: React.ReactNode
  actions?: React.ReactNode
  children: React.ReactNode
  rowRef?: React.RefObject<HTMLDivElement | null>
  thick?: boolean
  /** 比 thick 还高，用于 3 lane 包装轨。 */
  tall?: boolean
  onDragOver?: React.DragEventHandler<HTMLDivElement>
  onDrop?: React.DragEventHandler<HTMLDivElement>
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
        onDragOver={onDragOver}
        onDrop={onDrop}
        className={cn(
          'relative rounded-md border border-border bg-background/40',
          tall ? 'h-24' : thick ? 'h-16' : 'h-12',
        )}
      >
        {children}
      </div>
    </div>
  )
}

/**
 * 包装项单条——支持点击编辑 / 左右手柄拖拽改时长（剪映式）。
 *
 * 拖手柄落地：onmousemove 在像素级算 dx，按 (rowWidth / total) 折算回秒，松手时调
 * onResizePackagingItem(itemId, newStart, newEnd)。clamp 到 [0, total]，最短 0.3s。
 *
 * 平移整条：抓内部主体（非手柄）拖动会平移 start/end（保持 duration），松手调
 * onResizePackagingItem 一并落盘。
 */
function PackagingItemBlock({
  item,
  index,
  left,
  width,
  laneTopPx,
  pkgSelected,
  canEdit,
  canDelete,
  canResize,
  total,
  rowRef,
  onEditPackagingItem,
  onSelectPackaging,
  onDeletePackagingItem,
  onResizePackagingItem,
}: {
  item: PackagingItem
  index: number
  left: number
  width: number
  laneTopPx: number
  pkgSelected: boolean
  canEdit: boolean
  canDelete: boolean
  canResize: boolean
  total: number
  rowRef: React.RefObject<HTMLDivElement | null>
  onEditPackagingItem?: (it: PackagingItem) => void
  onSelectPackaging?: (it: PackagingItem) => void
  onDeletePackagingItem?: (id: string) => void | Promise<void>
  onResizePackagingItem?: (id: string, newStart: number, newEnd: number) => void | Promise<void>
}) {
  const [drag, setDrag] = useState<null | {
    mode: 'left' | 'right' | 'move'
    startX: number
    origStart: number
    origEnd: number
    rowPx: number
    livePreview: { start: number; end: number }
  }>(null)
  // 痛报：『包装轨的组件拖动完成之后会自动触发弹窗编辑』
  // 修：mouseup 把 drag 置 null 后，浏览器仍会把 mousedown→mouseup 派发为 click，
  // handleClick 那一刻 drag 已是 null 检测不到拖动 → 误开弹窗。
  // 用 ref 显式标记『刚刚结束拖动且确实移动了』，让接下来的 click 直接吃掉。
  const suppressNextClickRef = useRef(false)

  const beginDrag = (mode: 'left' | 'right' | 'move') => (e: React.MouseEvent) => {
    if (!canResize) return
    e.preventDefault()
    e.stopPropagation()
    const row = rowRef.current
    if (!row || total <= 0) return
    const rowPx = row.getBoundingClientRect().width
    setDrag({
      mode,
      startX: e.clientX,
      origStart: item.start,
      origEnd: item.end,
      rowPx,
      livePreview: { start: item.start, end: item.end },
    })
  }

  useEffect(() => {
    if (!drag) return
    const handleMove = (e: MouseEvent) => {
      const dx = e.clientX - drag.startX
      const dt = (dx / drag.rowPx) * total
      let newStart = drag.origStart
      let newEnd = drag.origEnd
      if (drag.mode === 'left') {
        newStart = Math.max(0, Math.min(drag.origEnd - 0.3, drag.origStart + dt))
      } else if (drag.mode === 'right') {
        newEnd = Math.min(total, Math.max(drag.origStart + 0.3, drag.origEnd + dt))
      } else {
        const dur = drag.origEnd - drag.origStart
        newStart = Math.max(0, Math.min(total - dur, drag.origStart + dt))
        newEnd = newStart + dur
      }
      setDrag({ ...drag, livePreview: { start: newStart, end: newEnd } })
    }
    const handleUp = () => {
      const { livePreview, origStart, origEnd } = drag
      const moved =
        Math.abs(livePreview.start - origStart) > 0.05 ||
        Math.abs(livePreview.end - origEnd) > 0.05
      if (moved) {
        void onResizePackagingItem?.(item.item_id, livePreview.start, livePreview.end)
        suppressNextClickRef.current = true
      }
      setDrag(null)
    }
    window.addEventListener('mousemove', handleMove)
    window.addEventListener('mouseup', handleUp)
    return () => {
      window.removeEventListener('mousemove', handleMove)
      window.removeEventListener('mouseup', handleUp)
    }
  }, [drag, total, item.item_id, onResizePackagingItem])

  // 拖动时按 livePreview 渲染（实时反馈）；否则按 props
  const renderLeft = drag ? pctOf(drag.livePreview.start, total) : left
  const renderWidth = drag
    ? Math.max(0.6, pctOf(drag.livePreview.end - drag.livePreview.start, total))
    : width

  const handleClick = () => {
    if (drag) return
    if (suppressNextClickRef.current) {
      suppressNextClickRef.current = false
      return
    }
    if (canEdit) {
      onEditPackagingItem!(item)
    } else {
      onSelectPackaging?.(item)
    }
  }

  return (
    <div
      key={`${item.item_id}-${index}`}
      onClick={handleClick}
      className={cn(
        'group absolute flex h-7 select-none items-center justify-center overflow-hidden rounded text-[10px] font-medium shadow transition',
        PACKAGING_KIND_COLOR[item.kind],
        (canEdit || onSelectPackaging) && 'cursor-pointer',
        pkgSelected ? 'ring-2 ring-primary ring-offset-1 ring-offset-card' : 'hover:brightness-110',
      )}
      style={{
        left: `${renderLeft}%`,
        width: `calc(${renderWidth}% - 1px)`,
        top: `${laneTopPx}px`,
      }}
      title={
        (item.text
          ? `${PACKAGING_KIND_LABEL[item.kind]} · ${item.text}`
          : `${PACKAGING_KIND_LABEL[item.kind]} · ${(item.end - item.start).toFixed(1)}s`) +
        '\n点击：打开编辑弹窗 · 拖动两端：改时长 · 拖中间：整体平移'
      }
    >
      {/* 左手柄 */}
      {canResize && (
        <div
          onMouseDown={beginDrag('left')}
          onClick={(e) => e.stopPropagation()}
          className="absolute left-0 top-0 z-10 h-full w-1.5 cursor-ew-resize bg-black/30 opacity-0 transition-opacity hover:bg-black/50 group-hover:opacity-100"
          title="拖动改起始时间"
        />
      )}
      {/* 主体（点击 + 平移） */}
      <div
        onMouseDown={canResize ? beginDrag('move') : undefined}
        className="flex h-full w-full items-center justify-center px-2"
      >
        <span className="truncate">
          {drag
            ? `${drag.livePreview.start.toFixed(1)}–${drag.livePreview.end.toFixed(1)}s`
            : (item.text || PACKAGING_KIND_LABEL[item.kind])}
        </span>
      </div>
      {/* 右手柄 */}
      {canResize && (
        <div
          onMouseDown={beginDrag('right')}
          onClick={(e) => e.stopPropagation()}
          className="absolute right-0 top-0 z-10 h-full w-1.5 cursor-ew-resize bg-black/30 opacity-0 transition-opacity hover:bg-black/50 group-hover:opacity-100"
          title="拖动改结束时间"
        />
      )}
      {canDelete && (
        <span
          role="button"
          tabIndex={0}
          onClick={(e) => {
            e.stopPropagation()
            void onDeletePackagingItem!(item.item_id)
          }}
          onKeyDown={(e) => {
            if (e.key === 'Enter' || e.key === ' ') {
              e.preventDefault()
              e.stopPropagation()
              void onDeletePackagingItem!(item.item_id)
            }
          }}
          title="删除该包装项"
          className={cn(
            'absolute right-2 top-0.5 z-20 inline-flex h-3.5 w-3.5 cursor-pointer items-center justify-center rounded-full bg-black/40 text-[9px] font-bold text-white opacity-0 transition-opacity hover:bg-rose-500 group-hover:opacity-100',
            pkgSelected && 'opacity-100',
          )}
        >
          ×
        </span>
      )}
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
  const blockLeft = ((anchor - rangeMin) / rangeSpan) * 100
  const blockWidth = (duration / rangeSpan) * 100

  const analysis = bgm.analysis ?? null
  const climaxes = analysis?.climaxes ?? []
  const calmSegs = (analysis?.calm_segments ?? []).filter((s) => s.end > s.start && duration > 0)
  const shape = analysis?.energy_shape ?? null
  const shapeLabel = shape ? ENERGY_SHAPE_SHORT[shape] : null
  const hasAnalysis = Boolean(analysis)

  // peak 仅在没 LLM 分析时显示（兜底）
  const peakRel =
    !hasAnalysis && bgm.peak_seconds != null && bgm.peak_seconds >= 0 && duration > 0
      ? (bgm.peak_seconds / duration) * 100
      : null

  const visibleLeft = ((0 - rangeMin) / rangeSpan) * 100
  const visibleWidth = ((total - 0) / rangeSpan) * 100

  return (
    <div className="absolute inset-1 select-none">
      <div
        className="absolute inset-y-0 rounded-sm bg-foreground/[0.04]"
        style={{ left: `${visibleLeft}%`, width: `${visibleWidth}%` }}
      />
      {/* BGM 块——简化为单条底纹 + 关键节点闪电；不再切色块 */}
      <div
        className={cn(
          'absolute inset-y-1 overflow-visible rounded border border-violet-400/60 active:cursor-grabbing',
          'cursor-grab',
          // shape 不同走不同底纹（平稳=纯色 / 单峰=渐亮 / 渐强=由弱到强 / 多峰&波浪=波点）
          !hasAnalysis && 'bg-gradient-to-r from-violet-400/30 to-fuchsia-400/30',
          shape === 'flat' && 'bg-violet-400/15',
          shape === 'single_peak' && 'bg-gradient-to-r from-violet-400/20 via-violet-400/15 to-fuchsia-400/40',
          shape === 'build_up' && 'bg-gradient-to-r from-violet-300/15 to-fuchsia-500/45',
          (shape === 'multi_peak' || shape === 'wave') && 'bg-violet-400/15',
          draggingAnchor != null && 'ring-2 ring-violet-400/60',
        )}
        style={{ left: `${blockLeft}%`, width: `${blockWidth}%` }}
        onMouseDown={onMouseDown}
        title={
          hasAnalysis
            ? `背景音乐 · ${shapeLabel ?? ''} · 起点 ${anchor.toFixed(1)}s\n${analysis?.energy_shape_reason ?? ''}`
            : `背景音乐起点 = ${anchor.toFixed(1)}s（拖动改起点）\n正值=视频前几秒静音；负值=跳过音乐开头`
        }
      >
        {/* 平稳段：浅色波纹底，提示"这里可以压口播" */}
        {hasAnalysis && calmSegs.map((seg, idx) => {
          const segLeft = (seg.start / duration) * 100
          const segWidth = ((seg.end - seg.start) / duration) * 100
          return (
            <div
              key={`calm-${idx}`}
              className="absolute inset-y-0 bg-violet-200/25 dark:bg-violet-500/10"
              style={{ left: `${segLeft}%`, width: `${segWidth}%` }}
              title={`平稳段 ${seg.start.toFixed(1)}–${seg.end.toFixed(1)}s\n${seg.note}`}
            />
          )
        })}

        {/* 关键节点：闪电符号（climax/drop=黄红，build_start=橙，release/break=灰） */}
        {hasAnalysis && climaxes.map((hl, idx) => {
          const at = duration > 0 ? (hl.at_seconds / duration) * 100 : 0
          return (
            <span
              key={`hl-${idx}`}
              className={cn(
                'absolute -top-2 -translate-x-1/2 rounded px-1 text-[9px] font-bold shadow',
                (hl.kind === 'climax' || hl.kind === 'drop') && 'bg-fuchsia-500 text-white',
                hl.kind === 'build_start' && 'bg-amber-500 text-white',
                (hl.kind === 'release' || hl.kind === 'break') && 'bg-foreground/40 text-background',
              )}
              style={{ left: `${at}%` }}
              title={`${HIGHLIGHT_KIND_LABEL[hl.kind]} @ ${hl.at_seconds.toFixed(1)}s\n${hl.label}\n${hl.fit_with_video}`}
            >
              ⚡{hl.label || HIGHLIGHT_KIND_LABEL[hl.kind]}
            </span>
          )
        })}

        {/* 兜底 peak（无 LLM 分析时） */}
        {peakRel != null && (
          <span
            className="absolute -top-2 -translate-x-1/2 rounded bg-rose-500 px-1 text-[9px] font-bold text-white shadow"
            style={{ left: `${peakRel}%` }}
            title={`兜底高潮点 @ ${bgm.peak_seconds?.toFixed(1)}s（AI 没分析时的备用估计）`}
          >
            ↑peak
          </span>
        )}

        <span className="pointer-events-none absolute inset-0 flex items-center justify-center text-[10px] font-semibold text-violet-900 dark:text-violet-100">
          BGM · {anchor.toFixed(1)}s 起{shapeLabel ? ` · ${shapeLabel}` : ''}
        </span>
      </div>
      <div
        className="absolute inset-y-0 border-l border-dashed border-foreground/40"
        style={{ left: `${visibleLeft}%` }}
        title="视频 t=0"
      />
    </div>
  )
}

const ENERGY_SHAPE_SHORT: Record<NonNullable<BGMConfig['analysis']>['energy_shape'], string> = {
  flat: '全程平稳',
  single_peak: '单峰爆发',
  multi_peak: '多峰起伏',
  build_up: '渐强推进',
  wave: '波浪起伏',
}

const HIGHLIGHT_KIND_LABEL: Record<
  NonNullable<BGMConfig['analysis']>['climaxes'][number]['kind'],
  string
> = {
  climax: '高潮',
  drop: 'Drop',
  build_start: '蓄势',
  release: '释放',
  break: '留白',
}

/* ---------------- BGM 分析卡 ---------------- */
// 渲染逻辑搬到 ./BgmAnalysisCard.tsx 与 Decompose 页共用；这里只保留 ENERGY_SHAPE_*
// 与 HIGHLIGHT_KIND_LABEL 是因为 FourTrackBoard 上方时间轴的 chip 还用到了短标签。
