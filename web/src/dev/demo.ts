/**
 * 演示模式（dev-only）：URL 带 ?demo 激活，由 main.tsx 在 import.meta.env.DEV 下动态引入，
 * 生产构建整条链路被 tree-shake 掉。
 *
 * 本机没有 Python 后端（vite /api 代理只会 502），演示/截图时用仓库自带样例素材
 * （server/samples，vite serveSamples 中间件直出 /samples/*）撑起画布全链路：
 * 素材卡与段落块真实缩略图、预览弹窗真播放、拖重排/切分/换源/转场 + 右键撤销。
 * 写操作在 fetch 拦截里跑内存版，语义对齐 server/app/services/plans/canvas_ops.py。
 * 未 mock 的端点（渲染/AI 生成等）透传真 fetch，维持 502 的真实失败表现。
 */
import { usePlanStore } from '@/stores/plan'
import { useProjectsStore } from '@/stores/projects'
import { useSessionStore } from '@/stores/session'
import {
  DEFAULT_COMPOSE_SETTINGS,
  type AdaptedSection,
  type AnimationSpec,
  type Gap,
  type Material,
  type MaterialShot,
  type PackagingItem,
  type Plan,
  type Project,
  type Scene,
  type ShotPlan,
  type TextCardSpec,
  type TransitionStyle,
} from '@/types/schemas'

const DEMO_PROJECT_ID = 'proj-demo-canvas'
const DEMO_PLAN_ID = 'plan-demo-canvas'

const clone = <T,>(obj: T): T => JSON.parse(JSON.stringify(obj)) as T
const round3 = (n: number) => Math.round(n * 1000) / 1000

// ---------------------------------------------------------------- 演示数据

const DEMO_SETTINGS = {
  ...DEFAULT_COMPOSE_SETTINGS,
  target_duration_seconds: 23.5,
  target_platform: 'douyin' as const,
  aspect_ratio: '16:9' as const,
  tone: 'tight_hype' as const,
  migration_preference: 'amp_pace' as const,
  cta: '关注看下期节奏拆解',
  keywords: ['卡点', '转场', '节奏'],
  subtitle_enabled: true,
  voiceover_enabled: true,
}

const DEMO_PROJECT: Project = {
  project_id: DEMO_PROJECT_ID,
  name: '演示 · 剪辑教学爆款复刻',
  video_type: 'editing',
  reference_versions: [{ sample_id: 'sample-vlog-01', slot_id: 'a' }],
  brief: '把「剪辑技巧教学」的节奏结构复刻到我的影视混剪素材上',
  video_goal: '教 3 个卡点转场技巧，节奏快、适合抖音',
  settings: clone(DEMO_SETTINGS),
  last_plan_id: DEMO_PLAN_ID,
  last_render_job_id: null,
  status: 'planned',
  step_states: { library: 'saved', decompose: 'saved', compose: 'saved', render: 'pending' },
  current_step: 'compose',
  created_at: 1790000000,
  updated_at: 1790100000,
}

const MAT_VLOG = 'mat-vlog'
const MAT_MOTION = 'mat-motion'
const MAT_MKT = 'mat-mkt'

const VLOG = 'sample-vlog-01'
const MOTION = 'sample-motion-01'
const MKT = 'sample-marketing-01'

// [start, end, 镜头序号, caption] —— 取自各 manifest.v_*.json 的真实切镜表
const VLOG_SHOT_TABLE: Array<[number, number, number, string]> = [
  [0.0, 1.3, 0, '赛博朋克封面'],
  [1.3, 10.1, 1, '纯黑底教学页'],
  [11.7, 16.0, 4, '人物面部近景'],
  [16.0, 19.9, 5, '黑底知识点页'],
  [30.0, 30.8, 13, '影视人物镜头'],
  [38.0, 41.3, 17, '室内打斗场景'],
  [41.3, 42.5, 18, '倒地武打动作'],
  [44.2, 44.9, 20, '带伤人物特写'],
  [52.4, 56.8, 24, '黑底极简大字封面'],
  [81.4, 86.1, 34, '开枪火光特效'],
  [106.7, 115.2, 44, '励志情绪引导'],
  [115.2, 118.2, 45, '片尾引流页'],
]

const MOTION_SHOT_TABLE: Array<[number, number, number, string]> = [
  [0.0, 2.5, 0, '堆叠卡片动态'],
  [2.5, 4.6, 1, '潮酷拼贴风'],
  [4.6, 8.0, 2, '酒红复古标题'],
  [8.0, 13.9, 3, '功能清单排版'],
  [13.9, 16.0, 4, '霓虹线条几何'],
  [16.0, 18.2, 5, '云端人物跳跃'],
  [18.2, 19.7, 6, '云海山巅剪影'],
  [19.7, 22.9, 7, '粉云云端奔跑'],
  [22.9, 27.0, 8, '霓虹片头光效'],
  [27.0, 31.2, 9, '极简品牌落版'],
]

const MKT_SHOT_TABLE: Array<[number, number, number, string]> = [
  [0.0, 15.7, 0, '艺术展览宣传封面'],
  [15.7, 18.4, 1, '小红书账号展示帧'],
]

function shotsFromTable(
  sampleId: string,
  table: Array<[number, number, number, string]>,
): MaterialShot[] {
  return table.map(([start, end, index, caption], i) => ({
    index,
    start,
    end,
    duration: round3(end - start),
    thumbnail_url: `/samples/${sampleId}/shot-${String(index).padStart(2, '0')}.jpg`,
    caption,
    action_density: round3(0.25 + ((i * 37) % 60) / 100),
    recommended_role: null,
  }))
}

function buildDemoMaterials(): Material[] {
  return [
    {
      material_id: MAT_VLOG,
      filename: '影视混剪素材.mp4',
      media_type: 'video',
      duration_seconds: 118.2,
      thumbnail_url: `/samples/${VLOG}/cover.jpg`,
      file_url: `/samples/${VLOG}/video.mp4`,
      tags: ['影视混剪', '动作', '卡点', '黑底大字'],
      subjects: ['赛博朋克城市', '武打动作', '黑底标题卡'],
      recommended_section: 'opening',
      highlight_score: 0.82,
      highlight_reason: '动作密度高，卡点镜头多，适合开场与高潮',
      sort_order: 0,
      preprocess_status: 'ready',
      preprocess_error: null,
      shots: shotsFromTable(VLOG, VLOG_SHOT_TABLE),
      origin: 'upload',
    },
    {
      material_id: MAT_MOTION,
      filename: '动态图形参考.mp4',
      media_type: 'video',
      duration_seconds: 31.2,
      thumbnail_url: `/samples/${MOTION}/cover.jpg`,
      file_url: `/samples/${MOTION}/video.mp4`,
      tags: ['动态图形', '霓虹', '排版'],
      subjects: ['霓虹几何线条', '云端人物剪影'],
      recommended_section: null,
      highlight_score: 0.64,
      highlight_reason: '光效与排版镜头可当情绪高点底图',
      sort_order: 1,
      preprocess_status: 'ready',
      preprocess_error: null,
      shots: shotsFromTable(MOTION, MOTION_SHOT_TABLE),
      origin: 'upload',
    },
    {
      material_id: MAT_MKT,
      filename: '展览宣传成片.mp4',
      media_type: 'video',
      duration_seconds: 18.4,
      thumbnail_url: `/samples/${MKT}/cover.jpg`,
      file_url: `/samples/${MKT}/video.mp4`,
      tags: ['展览宣传', '封面排版'],
      subjects: ['艺术展览'],
      recommended_section: null,
      highlight_score: 0.45,
      highlight_reason: '排版封面适合 B-roll',
      sort_order: 2,
      preprocess_status: 'ready',
      preprocess_error: null,
      shots: shotsFromTable(MKT, MKT_SHOT_TABLE),
      origin: 'upload',
    },
  ]
}

function buildDemoGaps(): Gap[] {
  return [
    {
      gap_id: 'gap-demo-1',
      section: 'climax',
      section_id: 'sec-3',
      project_id: DEMO_PROJECT_ID,
      slot_index: 2,
      requirement: '情绪高点画面：霓虹光效下的主体特写，约 2s，卡在鼓点',
      status: 'miss',
      impact: 'high',
      matched_material_id: null,
      note: null,
      sample_thumbnail_url: `/samples/${MOTION}/shot-07.jpg`,
    },
  ]
}

function mkShot(
  order: number,
  subject: string,
  visual: string,
  narration: string,
  duration: number,
  extra: Partial<ShotPlan> = {},
): ShotPlan {
  return {
    order,
    subject,
    visual,
    narration,
    duration_seconds: duration,
    source_hint: 'user_material',
    matched_material_id: MAT_VLOG,
    matched_material_shot_index: null,
    match_quality: 'good',
    ...extra,
  }
}

function mkScene(
  id: string,
  secId: string,
  role: string,
  shotOrder: number,
  subject: string,
  start: number,
  duration: number,
  extra: Partial<Scene> = {},
): Scene {
  return {
    scene_id: id,
    section: role,
    parent_section_id: secId,
    shot_order: shotOrder,
    shot_subject: subject,
    source: 'user_material',
    source_ref: MAT_VLOG,
    start,
    duration,
    in_point: 0,
    out_point: null,
    narration: `${subject}的口播`,
    voiceover_url: null,
    aigc_video_urls: [],
    aigc_image_url: null,
    animation_spec: null,
    text_card_spec: null,
    needs_fill: false,
    user_edited: false,
    transition_in: null,
    ...extra,
  }
}

const DEMO_TEXT_CARD: TextCardSpec = {
  main_text: '节奏是情绪的呼吸',
  sub_text: '快三秒 · 慢一秒 · 再快',
  font_family: 'bold_sans',
  layout: 'center',
  bg_mode: 'gradient',
  bg_color: '#0f0a1e',
  text_color: '#ffffff',
  accent_color: '#22d3ee',
  animation: 'zoom_pop',
  emoji_decor: ['⚡'],
  duration_seconds: 2.0,
  font_size_pct: 1.1,
}

const DEMO_ANIMATION: AnimationSpec = {
  engine: 'remotion',
  animation_type: 'ken-burns',
  motion_direction: 'in',
  intensity: 0.6,
  transition: 'cross-fade',
  transition_duration: 0.3,
  image_urls: [`/samples/${MOTION}/shot-04.jpg`],
}

function buildDemoPlan(): Plan {
  const sections: AdaptedSection[] = [
    {
      section_id: 'sec-0',
      role: 'opening',
      theme: '黑底大字钩子',
      content_description: '3 秒抓住注意力：一句大字标题 + 强对比底色，逼观众停下滑动',
      adaptation_note: '复刻样例开场的静态标题卡节奏',
      tempo: 'fast',
      source_section_indices: [0],
      source_shot_indices: [24],
      order: 0,
      duration_seconds: 4.3,
      shots: [
        mkShot(0, '黑底黄色大字封面', '赛博朋克风格标题卡', '三秒钩子：一句话留住人', 4.3, {
          matched_material_shot_index: 24,
        }),
      ],
    },
    {
      section_id: 'sec-1',
      role: 'development',
      theme: '快剪动作冲击',
      content_description: '动作接动作的三连快剪，硬切 + whip 卡点，把节奏顶起来',
      adaptation_note: '把样例 38-45s 的打斗段压成 5s 三镜',
      tempo: 'fast',
      source_section_indices: [1],
      source_shot_indices: [17, 18, 20],
      order: 1,
      duration_seconds: 5.2,
      shots: [
        mkShot(0, '室内打斗', '武打动作中景', '动作接动作，硬切别拖泥带水', 3.3, {
          matched_material_shot_index: 17,
        }),
        mkShot(1, '倒地武打', '倒地动作特写', '重音落在倒地瞬间', 1.2, {
          matched_material_shot_index: 18,
        }),
        mkShot(2, '带伤人物特写', '面部特写收力', '喘口气，准备下一波', 0.7, {
          matched_material_shot_index: 20,
          match_quality: 'weak',
        }),
      ],
    },
    {
      section_id: 'sec-2',
      role: 'development',
      theme: '技巧讲解卡点',
      content_description: '黑底知识点页讲第一个技巧：转场前留 3 帧动作余量',
      adaptation_note: '沿用样例的知识点页版式',
      tempo: 'medium',
      source_section_indices: [1],
      source_shot_indices: [5],
      order: 2,
      duration_seconds: 4.0,
      shots: [
        mkShot(0, '黑底知识点页', '重点高亮排版', '转场前留 3 帧动作余量，观众才跟得上', 4.0, {
          matched_material_shot_index: 5,
        }),
      ],
    },
    {
      section_id: 'sec-3',
      role: 'climax',
      theme: '情绪高点演示',
      content_description: 'AI 生图光效镜头推到情绪顶点，字卡收一口，再留一个待补的特写槽',
      adaptation_note: '纯新增段：用 AIGC 光效顶情绪',
      tempo: 'peak',
      source_section_indices: [],
      source_shot_indices: [],
      order: 3,
      duration_seconds: 7.0,
      shots: [
        {
          order: 0,
          subject: '霓虹光效特写',
          visual: '霓虹线条几何光效，主体剪影，缓慢推近',
          narration: '光效把情绪推到顶',
          duration_seconds: 3.0,
          source_hint: 'aigc_image',
          matched_material_id: null,
          matched_material_shot_index: null,
          match_quality: 'missing',
        },
        {
          order: 1,
          subject: '字卡金句',
          visual: '渐变底大字卡',
          narration: '节奏是情绪的呼吸：快三秒慢一秒再快',
          duration_seconds: 2.0,
          source_hint: 'text_card',
          matched_material_id: null,
          matched_material_shot_index: null,
          match_quality: 'missing',
        },
        mkShot(2, '霓虹主体特写（待补）', '霓虹光效下的主体特写', '本镜缺素材，待补拍或 AIGC', 2.0, {
          matched_material_id: null,
          matched_material_shot_index: null,
          match_quality: 'missing',
        }),
      ],
    },
    {
      section_id: 'sec-4',
      role: 'closing',
      theme: '收尾引流',
      content_description: '励志情绪收尾 + 账号引流页，CTA 压在最后一帧',
      adaptation_note: '复用样例片尾引流页',
      tempo: 'deceleration',
      source_section_indices: [4],
      source_shot_indices: [45],
      order: 4,
      duration_seconds: 3.0,
      shots: [
        mkShot(0, '片尾引流页', '账号搜索引导', '关注我，下期拆情绪曲线', 3.0, {
          matched_material_shot_index: 45,
        }),
      ],
    },
  ]

  const mainTrack: Scene[] = [
    mkScene('sc-0', 'sec-0', 'opening', 0, '黑底黄色大字封面', 0, 4.3, {
      in_point: 52.4,
      out_point: 56.7,
    }),
    mkScene('sc-1', 'sec-1', 'development', 0, '室内打斗', 4.3, 3.3, {
      in_point: 38.0,
      out_point: 41.3,
      transition_in: { style: 'hard_cut', duration: 0.2 },
    }),
    mkScene('sc-2', 'sec-1', 'development', 1, '倒地武打', 7.6, 1.2, {
      in_point: 41.3,
      out_point: 42.5,
      transition_in: { style: 'whip', duration: 0.3 },
    }),
    mkScene('sc-3', 'sec-1', 'development', 2, '带伤人物特写', 8.8, 0.7, {
      in_point: 44.2,
      out_point: 44.9,
    }),
    mkScene('sc-4', 'sec-2', 'development', 0, '黑底知识点页', 9.5, 4.0, {
      in_point: 16.0,
      out_point: 19.9,
      transition_in: { style: 'dissolve', duration: 0.5 },
    }),
    mkScene('sc-5', 'sec-3', 'climax', 0, '霓虹光效特写', 13.5, 3.0, {
      source: 'aigc_image',
      source_ref: '',
      aigc_image_url: `/samples/${MOTION}/shot-04.jpg`,
      animation_spec: clone(DEMO_ANIMATION),
      transition_in: { style: 'zoom', duration: 0.4 },
    }),
    mkScene('sc-6', 'sec-3', 'climax', 1, '字卡金句', 16.5, 2.0, {
      source: 'text_card',
      source_ref: '',
      text_card_spec: clone(DEMO_TEXT_CARD),
      voiceover_url: `/samples/${VLOG}/video.mp4`,
      transition_in: { style: 'slide', duration: 0.4 },
    }),
    mkScene('sc-7', 'sec-3', 'climax', 2, '霓虹主体特写（待补）', 18.5, 2.0, {
      source_ref: '',
      needs_fill: true,
    }),
    mkScene('sc-8', 'sec-4', 'closing', 0, '片尾引流页', 20.5, 3.0, {
      in_point: 115.2,
      out_point: 118.2,
      transition_in: { style: 'dissolve', duration: 0.5 },
    }),
  ]

  const packagingTrack: PackagingItem[] = [
    {
      item_id: 'pkg-sub-1',
      kind: 'subtitle',
      start: 4.4,
      end: 7.4,
      text: '动作接动作，硬切别拖泥带水',
      style: {},
      lane_index: null,
    },
    {
      item_id: 'pkg-title-1',
      kind: 'title_bar',
      start: 9.5,
      end: 12.0,
      text: '卡点技巧 ① 动作衔接',
      style: {},
      lane_index: null,
    },
    {
      item_id: 'pkg-sub-2',
      kind: 'subtitle',
      start: 9.7,
      end: 13.2,
      text: '转场前留 3 帧动作余量，观众才跟得上',
      style: {},
      lane_index: null,
    },
  ]

  return {
    plan_id: DEMO_PLAN_ID,
    reference_versions: [{ sample_id: 'sample-vlog-01', slot_id: 'a' }],
    project_id: DEMO_PROJECT_ID,
    session_id: DEMO_PROJECT_ID,
    brief: DEMO_PROJECT.brief,
    video_goal: DEMO_PROJECT.video_goal,
    subject_anchors: ['霓虹光效', '武打动作', '黑底标题卡'],
    adapted_sections: sections,
    variant: 'A',
    duration_seconds: 23.5,
    main_track: mainTrack,
    packaging_track: packagingTrack,
    bgm: {
      bgm_asset_id: null,
      track_url: null,
      volume: 0.25,
      fade_in: 1.0,
      fade_out: 1.5,
      duration_seconds: null,
      peak_seconds: null,
      video_anchor_seconds: 0,
      duck_with_voice: true,
      duck_attenuation_db: -12,
      analysis: null,
    },
    settings: clone(DEMO_SETTINGS),
    kb_rules_applied: 0,
  }
}

// ------------------------------------------------ 内存版 canvas_ops（语义对齐后端）

const REAL_SOURCES = new Set(['user_material', 'sample'])

type OpResult = { ok: true } | { ok: false; error: string }

function relayTimeline(plan: Plan): void {
  const track: Scene[] = []
  const grouped = new Set(plan.adapted_sections.map((s) => s.section_id))
  let t = 0
  for (const sec of plan.adapted_sections) {
    for (const sc of plan.main_track
      .filter((s) => s.parent_section_id === sec.section_id)
      .sort((a, b) => a.start - b.start)) {
      sc.start = round3(t)
      t += sc.duration
      track.push(sc)
    }
  }
  for (const sc of plan.main_track) {
    if (!sc.parent_section_id || !grouped.has(sc.parent_section_id)) {
      sc.start = round3(t)
      t += sc.duration
      track.push(sc)
    }
  }
  plan.main_track = track
  const total = round3(t)
  plan.packaging_track = plan.packaging_track.filter((it) => {
    if (it.kind === 'subtitle') return false
    if (it.start >= total + 0.01) return false
    if (it.end > total + 0.01) it.end = total
    return true
  })
  plan.duration_seconds = total
  plan.settings.target_duration_seconds = Math.max(10, Math.min(300, total))
}

function doReorder(plan: Plan, sectionIds: string[]): OpResult {
  const current = plan.adapted_sections.map((s) => s.section_id)
  const sameSet =
    sectionIds.length === current.length &&
    [...sectionIds].sort().join('|') === [...current].sort().join('|')
  if (!sameSet) return { ok: false, error: 'section_ids 必须是当前全部段落 id 的重排' }
  if (sectionIds.join('|') === current.join('|')) return { ok: true }
  const byId = new Map(plan.adapted_sections.map((s) => [s.section_id, s]))
  plan.adapted_sections = sectionIds
    .map((id) => byId.get(id))
    .filter((s): s is AdaptedSection => s != null)
    .map((sec, i) => {
      sec.order = i
      return sec
    })
  relayTimeline(plan)
  return { ok: true }
}

function doSplit(plan: Plan, sceneId: string, splitAt: number): OpResult {
  const scene = plan.main_track.find((s) => s.scene_id === sceneId)
  if (!scene) return { ok: false, error: 'scene_id 不存在' }
  if (!REAL_SOURCES.has(scene.source))
    return { ok: false, error: '只有实拍块（用户素材 / 样例镜头）可切分' }
  const secId = scene.parent_section_id ?? ''
  const sec = plan.adapted_sections.find((s) => s.section_id === secId)
  if (!sec) return { ok: false, error: '所属段落不存在' }
  const blockScenes = plan.main_track
    .filter((s) => s.parent_section_id === secId)
    .sort((a, b) => a.start - b.start)
  const bs = blockScenes[0].start
  const lastScene = blockScenes[blockScenes.length - 1]
  const be = lastScene.start + lastScene.duration
  for (const it of plan.packaging_track) {
    if (
      (it.kind === 'subtitle' || it.kind === 'title_bar') &&
      it.start < be - 0.01 &&
      it.end > bs + 0.01
    ) {
      return { ok: false, error: '本块含字幕/标题条轴，先摘除内层再切分' }
    }
  }
  if (blockScenes.some((s) => s.voiceover_url))
    return { ok: false, error: '本块含口播音轨，先摘除口播再切分' }
  if (!(0.5 <= splitAt && splitAt <= scene.duration - 0.5))
    return { ok: false, error: '切点须距镜两端 >= 0.5s' }
  const pos = blockScenes.indexOf(scene)
  const k = scene.shot_order
  const firstDur = round3(
    blockScenes.slice(0, pos).reduce((a, s) => a + s.duration, 0) + splitAt,
  )
  const secondDur = round3(
    scene.duration - splitAt + blockScenes.slice(pos + 1).reduce((a, s) => a + s.duration, 0),
  )
  if (firstDur < 2 || secondDur < 2) return { ok: false, error: '切分后两段各需 >= 2s' }
  // 与后端 _unique_id 一致：-b 后缀被占用时追加随机段
  const uniq = (base: string, taken: Set<string>) =>
    taken.has(base) ? `${base}-${Math.random().toString(16).slice(2, 8)}` : base
  const sceneBId = uniq(`${sceneId}-b`, new Set(plan.main_track.map((s) => s.scene_id)))
  const secBId = uniq(`${secId}-b`, new Set(plan.adapted_sections.map((s) => s.section_id)))
  const sceneA: Scene = { ...scene, duration: round3(splitAt), out_point: round3(scene.in_point + splitAt) }
  const sceneB: Scene = {
    ...scene,
    scene_id: sceneBId,
    duration: round3(scene.duration - splitAt),
    in_point: round3(scene.in_point + splitAt),
    start: round3(scene.start + splitAt),
    shot_order: 0,
    narration: null,
    user_edited: false,
    parent_section_id: secBId,
  }
  const secA: AdaptedSection = {
    ...sec,
    shots: sec.shots.slice(0, k + 1),
    duration_seconds: firstDur,
  }
  const secB: AdaptedSection = {
    ...sec,
    section_id: secBId,
    shots: sec.shots.slice(k).map((sh, i) => ({ ...sh, order: i })),
    duration_seconds: secondDur,
    order: sec.order + 1,
    source_section_indices: [],
  }
  for (const s of blockScenes.slice(pos + 1)) {
    s.parent_section_id = secBId
    s.shot_order = Math.max(0, s.shot_order - k)
  }
  const idx = plan.main_track.indexOf(scene)
  plan.main_track.splice(idx, 1, sceneA, sceneB)
  // 与后端一致：原位替换为两段，后续段 order 原地 +1（不能 push tail 副本，会重复段落）
  const sIdx = plan.adapted_sections.indexOf(sec)
  plan.adapted_sections.splice(sIdx, 1, secA, secB)
  for (const s of plan.adapted_sections.slice(sIdx + 2)) s.order += 1
  return { ok: true }
}

function doSwap(plan: Plan, materials: Material[], sceneId: string, materialId: string): OpResult {
  const i = plan.main_track.findIndex((s) => s.scene_id === sceneId)
  if (i < 0) return { ok: false, error: 'scene_id 不存在' }
  const mat = materials.find((m) => m.material_id === materialId)
  if (!mat) return { ok: false, error: '素材不存在' }
  const sc = plan.main_track[i]
  plan.main_track[i] = {
    ...sc,
    source: 'user_material',
    source_ref: materialId,
    in_point: 0,
    out_point: round3(Math.min(sc.duration, mat.duration_seconds ?? sc.duration)),
    aigc_video_urls: [],
    aigc_image_url: null,
    animation_spec: null,
    text_card_spec: null,
    needs_fill: false,
    user_edited: true,
  }
  return { ok: true }
}

function doTransition(
  plan: Plan,
  sceneId: string,
  style: TransitionStyle,
  duration: number,
): OpResult {
  const scene = plan.main_track.find((s) => s.scene_id === sceneId)
  if (!scene) return { ok: false, error: 'scene_id 不存在' }
  const sorted = [...plan.main_track].sort((a, b) => a.start - b.start)
  if (sorted[0]?.scene_id === sceneId)
    return { ok: false, error: '首段没有入场转场' }
  scene.transition_in = style === 'hard_cut' ? null : { style, duration }
  return { ok: true }
}

// ---------------------------------------------------------------- fetch 拦截

function jsonRes(obj: unknown, status = 200): Response {
  return new Response(JSON.stringify(obj), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

interface DemoState {
  plan: Plan
  gaps: Gap[]
  materials: Material[]
  project: Project
}

function installFetchMock(state: DemoState): void {
  const nativeFetch = window.fetch.bind(window)
  window.fetch = (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    const url =
      typeof input === 'string'
        ? input
        : input instanceof URL
          ? `${input.pathname}${input.search}`
          : input.url
    const method = (
      init?.method ??
      (typeof input !== 'string' && !(input instanceof URL) ? input.method : 'GET')
    ).toUpperCase()
    let body: Record<string, unknown> | undefined
    if (init?.body != null) {
      try {
        body = JSON.parse(String(init.body)) as Record<string, unknown>
      } catch {
        body = undefined
      }
    }

    let m: RegExpMatchArray | null
    if (method === 'GET' && url === '/api/project') return Promise.resolve(jsonRes({ items: [state.project] }))
    if (method === 'GET' && (m = url.match(/^\/api\/project\/([^/?]+)$/))) {
      return Promise.resolve(
        m[1] === state.project.project_id ? jsonRes(state.project) : jsonRes({ detail: '项目不存在' }, 404),
      )
    }
    if (method === 'GET' && (m = url.match(/^\/api\/project\/([^/]+)\/step\/(\w+)$/))) {
      const snap =
        m[2] === 'compose'
          ? { step: 'compose', saved_at: 1790100000, payload: { plan_id: state.plan.plan_id } }
          : null
      return Promise.resolve(jsonRes(snap))
    }
    if (method === 'GET' && (m = url.match(/^\/api\/plan\/([^/?]+)$/)) && m[1] === state.plan.plan_id) {
      return Promise.resolve(jsonRes(clone(state.plan)))
    }
    if (method === 'GET' && url.startsWith('/api/gap')) {
      return Promise.resolve(jsonRes(clone(state.gaps)))
    }
    if (method === 'GET' && url.startsWith('/api/material')) {
      return Promise.resolve(jsonRes(clone(state.materials)))
    }
    if (
      method === 'GET' &&
      (m = url.match(/^\/api\/plan\/([^/]+)\/scene\/([^/]+)\/material\/([^/]+)\/shot-scores$/))
    ) {
      const mat = state.materials.find((x) => x.material_id === m![3])
      const scores = (mat?.shots ?? []).map((s, i) => {
        const score = round3(0.08 + ((i * 17) % 80) / 100)
        return {
          shot_index: s.index,
          score: round3(score),
          score_pct: Math.round(score * 100),
          quality: (score >= 0.3 ? 'good' : score >= 0.1 ? 'weak' : 'missing') as
            | 'good'
            | 'weak'
            | 'missing',
        }
      })
      return Promise.resolve(
        jsonRes({
          plan_id: m[1],
          scene_id: m[2],
          material_id: m[3],
          section_role: 'development',
          scene_shot_subject: '画面主体',
          scene_duration: 3.0,
          scores,
        }),
      )
    }
    if (method === 'POST' && (m = url.match(/^\/api\/plan\/([^/]+)\/sections\/reorder$/))) {
      const r = doReorder(state.plan, (body?.section_ids as string[]) ?? [])
      return Promise.resolve(r.ok ? jsonRes(clone(state.plan)) : jsonRes({ detail: r.error }, 422))
    }
    if (method === 'POST' && (m = url.match(/^\/api\/plan\/([^/]+)\/scene\/([^/]+)\/split$/))) {
      const r = doSplit(state.plan, m[2], Number(body?.split_at))
      return Promise.resolve(r.ok ? jsonRes(clone(state.plan)) : jsonRes({ detail: r.error }, 422))
    }
    if (method === 'POST' && (m = url.match(/^\/api\/plan\/([^/]+)\/scene\/([^/]+)\/swap-source$/))) {
      const r = doSwap(
        state.plan,
        state.materials,
        m[2],
        String(body?.material_id ?? ''),
      )
      return Promise.resolve(r.ok ? jsonRes(clone(state.plan)) : jsonRes({ detail: r.error }, 422))
    }
    if (method === 'PATCH' && (m = url.match(/^\/api\/plan\/([^/]+)\/scene\/([^/]+)\/transition$/))) {
      const r = doTransition(
        state.plan,
        m[2],
        (body?.style as TransitionStyle) ?? 'hard_cut',
        Number(body?.duration ?? 0.4),
      )
      return Promise.resolve(r.ok ? jsonRes(clone(state.plan)) : jsonRes({ detail: r.error }, 400))
    }
    return nativeFetch(input, init)
  }
}

// ---------------------------------------------------------------- 入口

export function installDemo(): void {
  if (!new URLSearchParams(location.search).has('demo')) return
  const w = window as typeof window & { __seecriptDemo?: boolean }
  if (w.__seecriptDemo) return
  w.__seecriptDemo = true

  const state: DemoState = {
    plan: buildDemoPlan(),
    gaps: buildDemoGaps(),
    materials: buildDemoMaterials(),
    project: DEMO_PROJECT,
  }

  installFetchMock(state)

  useProjectsStore.setState({
    projects: [clone(DEMO_PROJECT)],
    currentProjectId: DEMO_PROJECT.project_id,
    loading: false,
    error: null,
  })
  useSessionStore.setState({
    sessionId: DEMO_PROJECT.project_id,
    materials: clone(state.materials),
    brief: DEMO_PROJECT.brief ?? '',
    videoGoal: DEMO_PROJECT.video_goal ?? '',
    settings: clone(DEMO_SETTINGS),
    selectedReferences: [...DEMO_PROJECT.reference_versions],
    selectedSampleIds: DEMO_PROJECT.reference_versions.map((r) => r.sample_id),
    selectedSampleTitles: ['剪辑样例｜Vlog 节奏 · 氛围铺垫到高潮收尾'],
    sampleSource: 'system',
    videoType: 'editing',
    manifest: null,
    draftManifests: {},
  })
  const planStore = usePlanStore.getState()
  planStore.setPlan(clone(state.plan))
  planStore.setGaps(clone(state.gaps))
  planStore.setFills([])
}
