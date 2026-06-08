import { usePlanStore } from '@/stores/plan'
import { useSessionStore } from '@/stores/session'
import { useProjectsStore } from '@/stores/projects'
import type {
  AdaptedSection,
  BGMConfig,
  FillResult,
  Gap,
  Material,
  PackagingItem,
  Plan,
  Project,
  ProjectStatus,
  ReferenceVersion,
  Scene,
  ShotPlan,
  TTSVoice,
  TextCardSpec,
} from '@/types/schemas'

/** 一键种子所有 store，让工作台 UI 无需后端即可完整渲染。 */
export function seedDemoStores() {
  const demoId = 'demo-' + Date.now()

  // ── session ──
  const session = useSessionStore.getState()
  session.setSession(demoId)
  session.setBrief('30 秒介绍新款智能手表：从开箱惊喜到上手体验，节奏明快有冲击力')
  session.setVideoType('marketing')
  useSessionStore.setState({
    selectedReferences: [
      { sample_id: 'demo-sample-01', slot_id: 'v1' },
      { sample_id: 'demo-sample-02', slot_id: 'v1' },
    ] as ReferenceVersion[],
    selectedSampleIds: ['demo-sample-01', 'demo-sample-02'],
    selectedSampleTitles: ['热门数码开箱', '运动手表评测'],
    sampleSource: 'system',
    materials: mockMaterials,
    settings: {
      target_duration_seconds: 30,
      target_platform: 'douyin',
      aspect_ratio: '9:16',
      tone: 'tight_hype',
      migration_preference: 'amp_emotion',
      cta: '立即下单',
      keywords: ['智能手表', '开箱', '性价比'],
      subtitle_enabled: true,
      voiceover_enabled: true,
      tts_voice: 'zh_male_jieshuo' as TTSVoice,
      packaging_prefs: {
        preset: 'energetic',
        allowed_transition_styles: ['hard_cut', 'dissolve', 'slide', 'zoom', 'whip', 'wipe'],
        max_transition_duration: 0.8,
        subtitle_font_size: 'medium',
        subtitle_position: 'bottom',
        subtitle_background: 'shadow',
        subtitle_bilingual: false,
        cover_text_source: 'auto',
        cover_custom_text: null,
        cover_duration: 1.2,
        cover_with_subtitle: true,
        llm_temperature: 0.7,
      },
      frame_design: {
        preset: 'creative-mode',
        palette: ['#7c3aed', '#06b6d4', '#f59e0b'],
        background_color: '#0f0f23',
        typography_display: 'Inter',
        typography_body: 'Inter',
        typography_mono: 'JetBrains Mono',
        motion_density: 'kinetic',
        grain_overlay: true,
        vignette: true,
        notes: '科技感强、节奏快、色彩鲜明',
      },
    },
  })

  // ── projects ──
  useProjectsStore.setState({
    currentProjectId: demoId,
    projects: [
      {
        project_id: demoId,
        name: '智能手表开箱 Demo',
        status: 'planned' as ProjectStatus,
        video_type: 'marketing',
        video_goal: '30 秒快节奏开箱体验',
        brief: '30 秒介绍新款智能手表：从开箱惊喜到上手体验，节奏明快有冲击力',
        settings: null,
        reference_versions: [
          { sample_id: 'demo-sample-01', slot_id: 'v1' },
        ] as ReferenceVersion[],
        created_at: Date.now() / 1000,
        updated_at: Date.now() / 1000,
      } as Project,
    ],
  })

  // ── plan ──
  const planStore = usePlanStore.getState()
  planStore.setPlan(mockPlan)
  planStore.setGaps(mockGaps)
  // fills: need to set individually
  for (const fill of mockFills) {
    planStore.upsertFill(fill)
  }
  planStore.setSelectedGapId(mockGaps[0]?.gap_id ?? null)
}

/* ====================================================================== */
/*                           MOCK DATA                                    */
/* ====================================================================== */

const mockMaterials: Material[] = [
  {
    material_id: 'mat-01', filename: '手表开箱镜头.mp4', media_type: 'video',
    duration_seconds: 8, file_url: 'https://commondatastorage.googleapis.com/gtv-videos-bucket/sample/ForBiggerBlazes.mp4', tags: ['开箱', '数码', '特写'],
    recommended_section: 'hook', highlight_score: 0.88,
    highlight_reason: '画面冲击力强，适合开场', sort_order: 0,
  },
  {
    material_id: 'mat-02', filename: '表盘操作演示.mp4', media_type: 'video',
    duration_seconds: 12, file_url: 'https://commondatastorage.googleapis.com/gtv-videos-bucket/sample/ForBiggerEscapes.mp4', tags: ['操作', '触屏', '流畅'],
    recommended_section: 'feature', highlight_score: 0.75,
    highlight_reason: '操作流畅自然', sort_order: 1,
  },
  {
    material_id: 'mat-03', filename: '佩戴场景.mp4', media_type: 'video',
    duration_seconds: 6, file_url: 'https://commondatastorage.googleapis.com/gtv-videos-bucket/sample/ForBiggerFun.mp4', tags: ['佩戴', '运动', '户外'],
    recommended_section: 'demo', highlight_score: 0.62,
    highlight_reason: '生活场景自然', sort_order: 2,
  },
  {
    material_id: 'mat-04', filename: '产品细节特写.mp4', media_type: 'video',
    duration_seconds: 10, file_url: 'https://commondatastorage.googleapis.com/gtv-videos-bucket/sample/ForBiggerJoyrides.mp4', tags: ['细节', '质感', '近景'],
    recommended_section: 'hook', highlight_score: 0.91,
    highlight_reason: '质感极佳，适合高潮段落', sort_order: 3,
  },
]

const mockSections: AdaptedSection[] = [
  {
    section_id: 'sec-1', role: 'hook', theme: '悬念开场',
    content_description: '用特写镜头制造悬念，快速抓住注意力',
    source_section_indices: [0], source_shot_indices: [0, 1],
    order: 0, duration_seconds: 5,
    shots: [{ order: 0, subject: '手表局部', duration_seconds: 2.5, visual: '暗光下屏幕亮起的瞬间', narration: '' },
            { order: 1, subject: '包装盒', duration_seconds: 2.5, visual: '快速拉开包装盒', narration: '' }] as ShotPlan[],
  },
  {
    section_id: 'sec-2', role: 'feature', theme: '核心亮点',
    content_description: '展示手表最核心的功能亮点',
    source_section_indices: [1], source_shot_indices: [2, 3],
    order: 1, duration_seconds: 12,
    shots: [{ order: 0, subject: '触屏操作', duration_seconds: 6, visual: '手指滑动表盘，切换功能', narration: '' },
            { order: 1, subject: '健康监测', duration_seconds: 6, visual: '心率监测动画', narration: '' }] as ShotPlan[],
  },
  {
    section_id: 'sec-3', role: 'cta', theme: '立即行动',
    content_description: '用有力口号 + 产品定格画面收尾',
    source_section_indices: [2], source_shot_indices: [4],
    order: 2, duration_seconds: 8,
    shots: [{ order: 0, subject: '产品全貌', duration_seconds: 8, visual: '手表旋转展示 + 价格信息弹出', narration: '' }] as ShotPlan[],
  },
]

const mockScenes: Scene[] = [
  {
    scene_id: 'sc-0', section: 'hook', parent_section_id: 'sec-1', shot_order: 0,
    shot_subject: '手表局部', source: 'user_material', source_ref: 'mat-01',
    start: 0, duration: 3, in_point: 0,
    aigc_video_urls: ['https://commondatastorage.googleapis.com/gtv-videos-bucket/sample/ForBiggerBlazes.mp4'],
    narration: '你有没有想过，一款手表能改变你的生活方式？',
    scene_label: '悬念开场', needs_fill: false,
  },
  {
    scene_id: 'sc-1', section: 'hook', parent_section_id: 'sec-1', shot_order: 1,
    shot_subject: '包装盒', source: 'text_card', source_ref: 'text-card-fill-empty',
    start: 3, duration: 2, in_point: 0, aigc_video_urls: [],
    narration: '', scene_label: '悬念字卡', needs_fill: true,
  },
  {
    scene_id: 'sc-2', section: 'feature', parent_section_id: 'sec-2', shot_order: 0,
    shot_subject: '触屏操作', source: 'user_material', source_ref: 'mat-02',
    start: 5, duration: 6, in_point: 0,
    aigc_video_urls: ['https://commondatastorage.googleapis.com/gtv-videos-bucket/sample/ForBiggerEscapes.mp4'],
    narration: '1.5 寸 AMOLED 屏，抬手即亮，操作丝滑流畅',
    scene_label: '触屏演示', needs_fill: false,
  },
  {
    scene_id: 'sc-3', section: 'feature', parent_section_id: 'sec-2', shot_order: 1,
    shot_subject: '健康监测', source: 'user_material', source_ref: 'mat-03',
    start: 11, duration: 6, in_point: 0,
    aigc_video_urls: ['https://commondatastorage.googleapis.com/gtv-videos-bucket/sample/ForBiggerFun.mp4'],
    narration: '全天候心率血氧监测，你的私人健康管家',
    scene_label: '健康功能', needs_fill: false,
  },
  {
    scene_id: 'sc-4', section: 'cta', parent_section_id: 'sec-3', shot_order: 0,
    shot_subject: '产品全貌', source: 'text_card', source_ref: 'text-card-fill-empty',
    start: 17, duration: 8, in_point: 0, aigc_video_urls: [],
    narration: '', scene_label: '行动号召', needs_fill: true,
  },
]

const mockPackaging: PackagingItem[] = [
  { item_id: 'pkg-01', kind: 'title_bar', start: 0, end: 25, text: '智能手表开箱', style: { color: '#7c3aed' } },
  { item_id: 'pkg-02', kind: 'sticker', start: 2, end: 4, text: 'NEW', style: { emoji: true } },
  { item_id: 'pkg-03', kind: 'cover', start: 0, end: 2, text: '智能手表', style: { palette: ['#7c3aed', '#06b6d4'] } },
]

const mockBGM: BGMConfig = {
  asset_id: 'bgm-demo-01',
  track_url: null,
  volume: 0.7,
  fade_in: 0.5,
  fade_out: 1.0,
  duration_seconds: 30,
  peak_seconds: 12,
  video_anchor_seconds: 0,
  duck_with_voice: true,
  duck_attenuation_db: -12,
  analysis: {
    title_guess: 'Electronic Uplift',
    mood_tags: ['科技', '活力', '现代'],
    energy_shape: 'build_up',
    energy_shape_reason: '渐强推进，适合开场到高潮的节奏',
    theme_fit_score: 0.82,
    theme_fit_reason: '电子音色与数码产品开箱高度匹配',
    climaxes: [
      { at_seconds: 12, kind: 'climax', label: 'Drop 爆发', fit_with_video: '对齐核心功能展示段落' },
      { at_seconds: 22, kind: 'build_start', label: '再次蓄势', fit_with_video: '对齐 CTA 段落' },
    ],
    calm_segments: [
      { start: 0, end: 3, note: '前奏留白，适合压口播悬念开场' },
    ],
    overall_advice: '建议在 Drop 处对齐手表功能展示，前奏压口播制造悬念',
    backend: 'mock',
  },
}

const mockPlan: Plan = {
  plan_id: 'plan-demo',
  reference_versions: [
    { sample_id: 'demo-sample-01', slot_id: 'v1' },
  ] as ReferenceVersion[],
  project_id: 'demo',
  session_id: 'demo',
  brief: '30 秒介绍新款智能手表',
  video_goal: '快节奏开箱体验',
  adapted_sections: mockSections,
  variant: 'A',
  duration_seconds: 25,
  main_track: mockScenes,
  packaging_track: mockPackaging,
  bgm: mockBGM,
  settings: {
    target_duration_seconds: 30,
    target_platform: 'douyin',
    aspect_ratio: '9:16',
    tone: 'tight_hype',
    migration_preference: 'amp_emotion',
    cta: '立即下单',
    keywords: ['智能手表', '开箱', '性价比'],
    subtitle_enabled: true,
    voiceover_enabled: true,
    tts_voice: 'zh_male_jieshuo' as TTSVoice,
    packaging_prefs: {
      preset: 'energetic',
      allowed_transition_styles: ['hard_cut', 'dissolve', 'slide', 'zoom', 'whip', 'wipe'],
      max_transition_duration: 0.8,
      subtitle_font_size: 'medium',
      subtitle_position: 'bottom',
      subtitle_background: 'shadow',
      subtitle_bilingual: false,
      cover_text_source: 'auto',
      cover_custom_text: null,
      cover_duration: 1.2,
      cover_with_subtitle: true,
      llm_temperature: 0.7,
    },
    frame_design: {
      preset: 'creative-mode',
      palette: ['#7c3aed', '#06b6d4', '#f59e0b'],
      background_color: '#0f0f23',
      typography_display: 'Inter',
      typography_body: 'Inter',
      typography_mono: 'JetBrains Mono',
      motion_density: 'kinetic',
      grain_overlay: true,
      vignette: true,
      notes: '科技感强、节奏快、色彩鲜明',
    },
  },
}

const mockGaps: Gap[] = [
  {
    gap_id: 'gap-01', section: 'hook', section_id: 'sec-1',
    slot_index: 0, requirement: '手表细节特写镜头',
    status: 'ok', impact: 'high',
    matched_material_id: 'mat-01',
  },
  {
    gap_id: 'gap-02', section: 'hook', section_id: 'sec-1',
    slot_index: 1, requirement: '悬念字卡画面',
    status: 'warn', impact: 'high',
    note: '建议换一个更有冲击力的字卡',
  },
  {
    gap_id: 'gap-03', section: 'feature', section_id: 'sec-2',
    slot_index: 0, requirement: '触屏操作演示视频',
    status: 'ok', impact: 'medium',
    matched_material_id: 'mat-02',
  },
  {
    gap_id: 'gap-04', section: 'feature', section_id: 'sec-2',
    slot_index: 1, requirement: '佩戴场景实拍',
    status: 'ok', impact: 'medium',
    matched_material_id: 'mat-03',
  },
  {
    gap_id: 'gap-05', section: 'cta', section_id: 'sec-3',
    slot_index: 0, requirement: '产品全貌展示 + 价格标签',
    status: 'miss', impact: 'high',
    note: '还没填素材，可以用 AI 生成',
  },
]

const mockFills: FillResult[] = [
  {
    gap_id: 'gap-01', action: 'rerank', new_material_id: 'mat-01',
    narration: '你有没有想过，一款手表能改变你的生活方式？',
    alternatives: ['mat-01', 'mat-04'],
    video_urls: [], chunks_count: 0, chunk_task_ids: [],
    status: 'ok', section_id: 'sec-1',
  },
  {
    gap_id: 'gap-03', action: 'rerank', new_material_id: 'mat-02',
    narration: '1.5 寸 AMOLED 屏，抬手即亮',
    alternatives: ['mat-02'],
    video_urls: [], chunks_count: 0, chunk_task_ids: [],
    status: 'ok', section_id: 'sec-2',
  },
  {
    gap_id: 'gap-04', action: 'rerank', new_material_id: 'mat-03',
    narration: '全天候心率血氧监测，你的私人健康管家',
    alternatives: ['mat-03'],
    video_urls: [], chunks_count: 0, chunk_task_ids: [],
    status: 'ok', section_id: 'sec-2',
  },
]
