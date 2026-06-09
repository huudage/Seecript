import { useCallback, useEffect, useMemo, useRef, useState } from 'react'

import { api } from '@/api/client'
import type {
  AigcImageSpecResponse,
  AigcPromptResponse,
  AigcSeedreamResponse,
  AigcTailFrameResponse,
  Asset,
  AssetSaveFromUrlRequest,
  FillResult,
  Gap,
  GapFillRequest,
  ImageSpec,
  Plan,
} from '@/types/schemas'
import { cn } from '@/lib/utils'
import { ThinkingSteps } from './ThinkingSteps'

/**
 * AIGC 补全 · Agent 化（stage-18）：把"分析参考图 → 写提示词 → 调 Seedance"包装成
 * 可视化的 agent 流程，每步都有思考链和加载态。
 *
 *   idle             → 用户点『开始分析 ✨』触发
 *   analyzing-spec   → 调 /aigc/aigc-image-spec，展示思考过程
 *   spec             → 用户对每张图选『上传 / Seedream / 保存到素材库』
 *   analyzing-prompt → 调 /aigc/aigc-prompt，展示思考过程
 *   prompt           → 编辑视频 prompt + 尾帧承接 + 一键 run
 *
 * Seedream 出图返回临时 CDN（1h-7d 有效），用户点"保存到素材库"通过
 * /asset/save-from-url 永久落盘。上传方式天然就在素材库里。
 */
const AUTO_POLL_INTERVAL_MS = 8000
const AUTO_POLL_MAX_ATTEMPTS = 30

type Phase = 'idle' | 'analyzing-spec' | 'spec' | 'analyzing-prompt' | 'prompt'

interface ImageSlot {
  url: string
  source: 'upload' | 'seedream'
  /** 已落入素材库的 asset_id；上传方式天然有，Seedream 走 save-from-url 才有。 */
  assetId?: string
}

export function FillAigcPanel({
  gap,
  fill,
  plan,
  onResult,
  mode = 'video',
}: {
  gap: Gap
  fill: FillResult | null
  plan: Plan | null
  onResult: (fill: FillResult) => void
  /** 'video' = Seedance T2V（默认）；'image' = Seedream 文生图。两种模式共享 spec/prompt 准备链，
   *  只在最后一步 handleRun 调不同 action（aigc vs aigc_image）。 */
  mode?: 'video' | 'image'
}) {
  const [phase, setPhase] = useState<Phase>('idle')

  // -- spec / thinking 状态 --
  const [imageSpecs, setImageSpecs] = useState<ImageSpec[]>([])
  const [specThinking, setSpecThinking] = useState<string[]>([])
  const [specErr, setSpecErr] = useState<string | null>(null)
  const [imageSlots, setImageSlots] = useState<Record<string, ImageSlot>>({})
  const [slotPrompts, setSlotPrompts] = useState<Record<string, string>>({})
  const [slotBusy, setSlotBusy] = useState<string | null>(null)
  const [slotErr, setSlotErr] = useState<Record<string, string | null>>({})
  const [slotSaving, setSlotSaving] = useState<string | null>(null)
  const [slotSaveOk, setSlotSaveOk] = useState<Record<string, boolean>>({})

  // -- prompt / thinking 状态 --
  const [prompt, setPrompt] = useState<string>('')
  const [promptThinking, setPromptThinking] = useState<string[]>([])
  const [promptErr, setPromptErr] = useState<string | null>(null)
  const [promptLoading, setPromptLoading] = useState(false)

  // -- 多镜头 (path B：仅 image 模式可用) --
  // stage-25：直接从 plan.adapted_sections[gap.section_id].shots 读取本段已规划的分镜清单作为
  // n_shots/subjects 来源——分镜数已是 plan_agent 决策的产物，无需让用户再填。前端只展示一个
  // read-only chip 列表方便用户确认。

  // -- run --
  const [loading, setLoading] = useState(false)
  const [refreshing, setRefreshing] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const [autoPolling, setAutoPolling] = useState(false)
  const [autoPollAttempts, setAutoPollAttempts] = useState(0)

  // -- 尾帧承接 --
  const [useTailFrame, setUseTailFrame] = useState(false)
  const [tailFrameDataUrl, setTailFrameDataUrl] = useState<string | null>(null)
  const [tailFrameLoading, setTailFrameLoading] = useState(false)
  const [tailFrameErr, setTailFrameErr] = useState<string | null>(null)

  // -- gap 切换：重置一切，回到 idle --
  // stage-36：依赖 section_id 而非 gap_id。后端 silent /gap/detect 会重写 gap_id（plan-scoped
  // 唯一性），但同一段的 section_id 跨 silent rebuild 稳定；这里若依赖 gap_id，会被无关重算
  // 误触发，清掉正在跑的 Seedance 任务/已生成图片选择/口播。
  useEffect(() => {
    setPhase('idle')
    setImageSpecs([])
    setSpecThinking([])
    setSpecErr(null)
    setImageSlots({})
    setSlotPrompts({})
    setSlotErr({})
    setSlotSaveOk({})
    setPrompt('')
    setPromptThinking([])
    setPromptErr(null)
    setUseTailFrame(false)
    setTailFrameDataUrl(null)
    setTailFrameErr(null)
    setErr(null)
  }, [gap.section_id])

  // gap.section_id → plan.main_track 的 scene_id + 本段已规划分镜清单
  const sceneInfo = useMemo(() => {
    if (!plan || !gap.section_id) {
      return { sceneId: null, hasPrev: false, prevReady: false, sectionDuration: 0, plannedSubjects: [] as string[] }
    }
    const sec = plan.adapted_sections.find((s) => s.section_id === gap.section_id)
    if (!sec) {
      return { sceneId: null, hasPrev: false, prevReady: false, sectionDuration: 0, plannedSubjects: [] as string[] }
    }
    const sceneId = `sc-${sec.order}`
    const prevSceneId = sec.order > 0 ? `sc-${sec.order - 1}` : null
    const prevScene = prevSceneId ? plan.main_track.find((s) => s.scene_id === prevSceneId) : null
    // 读 ShotPlan.subject 作为多镜主体清单；空 subject 兜底跳过。
    const plannedSubjects = (sec.shots ?? [])
      .map((sh) => (sh.subject ?? '').trim())
      .filter((s) => s.length > 0)
      .slice(0, 4)
    return {
      sceneId,
      hasPrev: !!prevSceneId,
      prevReady: !!prevScene && prevScene.aigc_video_urls.length > 0,
      sectionDuration: Number(sec.duration_seconds) || 0,
      plannedSubjects,
    }
  }, [plan, gap.section_id])

  const isSlotReady = useCallback(
    (slotId: string) => !!imageSlots[slotId]?.url,
    [imageSlots],
  )
  const allSlotsReady = useMemo(
    () => imageSpecs.length > 0 && imageSpecs.every((s) => isSlotReady(s.slot_id)),
    [imageSpecs, isSlotReady],
  )

  // -- Step 1: 触发分析 --
  const handleStartAnalyze = useCallback(async () => {
    setPhase('analyzing-spec')
    setSpecErr(null)
    setSpecThinking([])
    setImageSpecs([])
    try {
      const resp = await api.post<AigcImageSpecResponse>('/gap/aigc-image-spec', {
        gap_id: gap.gap_id,
        // PR-O：把 ShotPlan.subject 清单作为强制锚点带过去，后端会用它覆盖
        // section.shots 上的旧 subject（避免编辑未同步），并在 spec.prompt 上
        // post-validation 注入前缀，保证下游 Seedream 不丢主体。
        subjects: sceneInfo.plannedSubjects,
      })
      setSpecThinking(resp.thinking ?? [])
      setImageSpecs(resp.specs)
      const init: Record<string, string> = {}
      resp.specs.forEach((s) => { init[s.slot_id] = s.prompt })
      setSlotPrompts(init)
      // 等思考链动画跑完一轮再切到 spec 阶段（每条 ~600ms）
      const delayMs = Math.max(800, (resp.thinking?.length ?? 0) * 600)
      window.setTimeout(() => {
        if (resp.specs.length === 0) {
          // 段落不需要图，自动进 prompt 阶段
          void handleEnterPromptStage(true)
        } else {
          setPhase('spec')
        }
      }, delayMs)
    } catch (e) {
      setSpecErr(e instanceof Error ? e.message : '分析失败')
      setPhase('idle')
    }
  }, [gap.gap_id, sceneInfo.plannedSubjects])

  // -- Step 2: spec → prompt --
  const handleEnterPromptStage = useCallback(async (auto = false) => {
    setPhase('analyzing-prompt')
    setPromptErr(null)
    setPromptThinking([])
    setPromptLoading(true)
    try {
      const resp = await api.post<AigcPromptResponse>('/gap/aigc-prompt', {
        gap_id: gap.gap_id,
      })
      setPromptThinking(resp.thinking ?? [])
      setPrompt(resp.prompt)
      const delayMs = Math.max(700, (resp.thinking?.length ?? 0) * 600)
      window.setTimeout(() => {
        setPhase('prompt')
        setPromptLoading(false)
      }, auto ? Math.max(delayMs, 1200) : delayMs)
    } catch (e) {
      setPromptErr(e instanceof Error ? e.message : '提示词生成失败')
      setPhase('prompt')
      setPromptLoading(false)
    }
  }, [gap.gap_id])

  const handleRegeneratePrompt = useCallback(async () => {
    setPromptLoading(true)
    setPromptErr(null)
    setPromptThinking([])
    try {
      const resp = await api.post<AigcPromptResponse>('/gap/aigc-prompt', {
        gap_id: gap.gap_id,
      })
      setPromptThinking(resp.thinking ?? [])
      setPrompt(resp.prompt)
    } catch (e) {
      setPromptErr(e instanceof Error ? e.message : '提示词生成失败')
    } finally {
      setPromptLoading(false)
    }
  }, [gap.gap_id])

  // -- spec 阶段：单张上传 --
  const handleUploadSlot = useCallback(
    async (slotId: string, file: File) => {
      setSlotBusy(slotId)
      setSlotErr((m) => ({ ...m, [slotId]: null }))
      try {
        const form = new FormData()
        form.append('file', file)
        form.append('kind', 'reference_image')
        const projectId = plan?.project_id || gap.project_id || ''
        if (projectId) form.append('project_id', projectId)
        const asset = await api.post<Asset>('/asset/upload', form)
        setImageSlots((m) => ({
          ...m,
          [slotId]: { url: asset.file_url, source: 'upload', assetId: asset.asset_id },
        }))
        setSlotSaveOk((m) => ({ ...m, [slotId]: true }))
      } catch (e) {
        setSlotErr((m) => ({
          ...m,
          [slotId]: e instanceof Error ? e.message : '上传失败',
        }))
      } finally {
        setSlotBusy(null)
      }
    },
    [plan?.project_id, gap.project_id],
  )

  // 按 spec_id 顺序与 plannedSubjects 对齐：第 i 个 spec 配第 i 个主体（一镜一图原则）。
  const subjectForSpec = useCallback(
    (spec: ImageSpec): string => {
      const idx = imageSpecs.findIndex((s) => s.slot_id === spec.slot_id)
      if (idx >= 0 && idx < sceneInfo.plannedSubjects.length) {
        return sceneInfo.plannedSubjects[idx]
      }
      return ''
    },
    [imageSpecs, sceneInfo.plannedSubjects],
  )

  // -- spec 阶段：单张 Seedream 生图 --
  const handleSeedreamSlot = useCallback(
    async (spec: ImageSpec) => {
      const slotId = spec.slot_id
      const promptText = (slotPrompts[slotId] || spec.prompt || '').trim()
      if (!promptText) {
        setSlotErr((m) => ({ ...m, [slotId]: '请填写图片描述后再生成' }))
        return
      }
      setSlotBusy(slotId)
      setSlotErr((m) => ({ ...m, [slotId]: null }))
      setSlotSaveOk((m) => ({ ...m, [slotId]: false }))
      try {
        // 已存在 slot.url → 这是「换一张」/「再渲染」单图：把它作为视觉参考传给 Seedream，
        // 让二次出图延续用户已确认画面的主体/构图/色调，而不是凭空重抽。
        const prevUrl = imageSlots[slotId]?.url
        const resp = await api.post<AigcSeedreamResponse>('/gap/aigc-seedream', {
          prompt: promptText,
          ratio: spec.ratio,
          n: 1,
          subject: subjectForSpec(spec),
          ...(prevUrl ? { reference_image_url: prevUrl } : {}),
        })
        const first = resp.images[0]
        if (!first) throw new Error('AI 出图未返回图片')
        setImageSlots((m) => ({ ...m, [slotId]: { url: first.url, source: 'seedream' } }))
      } catch (e) {
        setSlotErr((m) => ({
          ...m,
          [slotId]: e instanceof Error ? e.message : 'AI 出图失败',
        }))
      } finally {
        setSlotBusy(null)
      }
    },
    [imageSlots, slotPrompts, subjectForSpec],
  )

  // -- spec 阶段：一键 Seedream 生成 slot --
  // 串行调避免撞配额。force=true 时不跳过已就绪 slot——给「再渲染」按钮用，
  // 因为用户明确要求"AI 生图再渲染不能跳过生图"。再渲染时把已有 slot.url 作为
  // Seedream 的 reference_image_url 传入，保持视觉一致性。
  const [seedreamAllBusy, setSeedreamAllBusy] = useState(false)
  const handleSeedreamAllSlots = useCallback(async (opts?: { force?: boolean }) => {
    if (seedreamAllBusy) return
    const force = opts?.force === true
    const todo = force ? imageSpecs : imageSpecs.filter((s) => !imageSlots[s.slot_id])
    if (todo.length === 0) return
    setSeedreamAllBusy(true)
    try {
      for (const spec of todo) {
        const slotId = spec.slot_id
        const promptText = (slotPrompts[slotId] || spec.prompt || '').trim()
        if (!promptText) {
          setSlotErr((m) => ({ ...m, [slotId]: '空提示词已跳过' }))
          continue
        }
        setSlotBusy(slotId)
        setSlotErr((m) => ({ ...m, [slotId]: null }))
        try {
          // 已有 slot.url → 用它作为视觉参考（再渲染场景），从已确认画面延续
          const prevUrl = imageSlots[slotId]?.url
          const resp = await api.post<AigcSeedreamResponse>('/gap/aigc-seedream', {
            prompt: promptText,
            ratio: spec.ratio,
            n: 1,
            subject: subjectForSpec(spec),
            ...(prevUrl ? { reference_image_url: prevUrl } : {}),
          })
          const first = resp.images[0]
          if (!first) throw new Error('AI 出图未返回图片')
          setImageSlots((m) => ({ ...m, [slotId]: { url: first.url, source: 'seedream' } }))
        } catch (e) {
          setSlotErr((m) => ({
            ...m,
            [slotId]: e instanceof Error ? e.message : 'AI 出图失败',
          }))
        }
      }
    } finally {
      setSlotBusy(null)
      setSeedreamAllBusy(false)
    }
  }, [imageSlots, imageSpecs, seedreamAllBusy, slotPrompts, subjectForSpec])

  // -- spec 阶段：把 Seedream 临时 CDN 图保存到素材库 --
  const handleSaveSlotToLibrary = useCallback(
    async (spec: ImageSpec) => {
      const slotId = spec.slot_id
      const slot = imageSlots[slotId]
      if (!slot || slot.source !== 'seedream') return
      const projectId = plan?.project_id || gap.project_id || ''
      if (!projectId) {
        setSlotErr((m) => ({ ...m, [slotId]: '当前没有项目 ID，无法保存到素材库' }))
        return
      }
      setSlotSaving(slotId)
      setSlotErr((m) => ({ ...m, [slotId]: null }))
      try {
        const body: AssetSaveFromUrlRequest = {
          project_id: projectId,
          url: slot.url,
          kind: 'reference_image',
          title: spec.caption,
          tags: ['seedream', gap.section_id || gap.section || ''].filter(Boolean) as string[],
        }
        const asset = await api.post<Asset>('/asset/save-from-url', body)
        setImageSlots((m) => ({
          ...m,
          [slotId]: { ...slot, assetId: asset.asset_id, url: asset.file_url },
        }))
        setSlotSaveOk((m) => ({ ...m, [slotId]: true }))
      } catch (e) {
        setSlotErr((m) => ({
          ...m,
          [slotId]: e instanceof Error ? e.message : '保存到素材库失败',
        }))
      } finally {
        setSlotSaving(null)
      }
    },
    [imageSlots, plan?.project_id, gap.project_id, gap.section, gap.section_id],
  )

  const handleClearSlot = useCallback((slotId: string) => {
    setImageSlots((m) => {
      const copy = { ...m }
      delete copy[slotId]
      return copy
    })
    setSlotErr((m) => ({ ...m, [slotId]: null }))
    setSlotSaveOk((m) => ({ ...m, [slotId]: false }))
  }, [])

  // -- 尾帧抽取 --
  const handleTailFrameToggle = useCallback(
    async (next: boolean) => {
      setUseTailFrame(next)
      setTailFrameErr(null)
      if (!next) {
        setTailFrameDataUrl(null)
        return
      }
      if (!plan || !sceneInfo.sceneId) {
        setTailFrameErr('当前没有可用 plan / 场景')
        setUseTailFrame(false)
        return
      }
      if (!sceneInfo.hasPrev) {
        setTailFrameErr('本段是第一段，没有可承接的前段')
        setUseTailFrame(false)
        return
      }
      if (!sceneInfo.prevReady) {
        setTailFrameErr('前一段尚未补全，请先生成前段再勾选')
        setUseTailFrame(false)
        return
      }
      setTailFrameLoading(true)
      try {
        const resp = await api.post<AigcTailFrameResponse>('/gap/aigc-tail-frame', {
          plan_id: plan.plan_id,
          scene_id: sceneInfo.sceneId,
        })
        setTailFrameDataUrl(resp.frame_data_url)
      } catch (e) {
        setTailFrameErr(e instanceof Error ? e.message : '尾帧抽取失败')
        setUseTailFrame(false)
        setTailFrameDataUrl(null)
      } finally {
        setTailFrameLoading(false)
      }
    },
    [plan, sceneInfo],
  )

  const handleRun = useCallback(async () => {
    setLoading(true)
    setErr(null)
    try {
      const refImages = Object.values(imageSlots).map((s) => s.url)
      const params: Record<string, unknown> = {
        prompt: prompt.trim() || gap.requirement,
      }
      // 显式把高级设置里的画面比例带上——后端虽然有 fallback，但传一遍更直白可审计。
      const aspect = plan?.settings?.aspect_ratio
      if (aspect) params.ratio = aspect
      // L1: video 模式显式带本段 duration_seconds，避免后端 fallback 用 5s 默认值
      if (mode === 'video' && sceneInfo.sectionDuration > 0) {
        params.duration_seconds = Math.min(60, Math.max(2, sceneInfo.sectionDuration))
      }
      if (refImages.length > 0) params.reference_images = refImages
      // 尾帧承接只对视频模式有意义；图片模式是单帧静图，没有"承接前段"概念。
      if (mode === 'video' && useTailFrame && tailFrameDataUrl) {
        params.first_frame_url = tailFrameDataUrl
      }
      // AI 生图再渲染：subjects/n_shots 不再让用户输入，直接从 plan.adapted_sections.shots（plan_agent
      // 已决策的分镜清单）读取。前端不再硬塞——为空时让后端走 content_description 兜底自动抽取。
      if (mode === 'image') {
        if (sceneInfo.plannedSubjects.length > 0) {
          params.subjects = sceneInfo.plannedSubjects
          params.n_shots = sceneInfo.plannedSubjects.length
        }
      }
      const body: GapFillRequest = {
        gap_id: gap.gap_id,
        action: mode === 'image' ? 'aigc_image' : 'aigc',
        params,
      }
      const result = await api.post<FillResult>('/gap/fill', body)
      onResult(result)
    } catch (e) {
      setErr(e instanceof Error ? e.message : mode === 'image' ? 'AI 生图再渲染失败' : 'AI 视频生成失败')
    } finally {
      setLoading(false)
    }
  }, [gap.gap_id, gap.requirement, imageSlots, mode, onResult, plan, prompt, sceneInfo.plannedSubjects, sceneInfo.sectionDuration, tailFrameDataUrl, useTailFrame])

  // image 模式 Seedream 是同步出图，没有 Seedance 任务队列概念——不要拿 new_material_id 当 task_id 去
  // 调 /gap/aigc-refresh（那是 Seedance 视频专用），否则会用 img-xxxx 当 task_id 查 Seedance 任务，
  // 必然 404 → 把刚 ok 的 fill 覆盖成 warn → UI 显示『失败』。
  const firstTaskId = mode === 'image'
    ? null
    : (fill?.chunk_task_ids?.[0] ?? fill?.new_material_id ?? extractTaskId(fill?.note))
  const canRefresh = !!fill && fill.status !== 'ok' && !!firstTaskId && mode === 'video'
  const expectedChunks = extractExpectedChunks(fill?.note) ?? fill?.chunks_count ?? 0
  // image 模式：只要有 aigc_image_url 就算预览就绪——即便 status 不是 ok（如 persist 失败但 CDN url
  // 仍可用），也得让用户能看到图，避免『图都出来了却显示生成失败』。
  const hasPreview = mode === 'image'
    ? !!fill?.aigc_image_url
    : !!fill?.video_urls && fill.video_urls.length > 0

  const handleRefresh = useCallback(async () => {
    if (!fill || !firstTaskId) return
    setRefreshing(true)
    setErr(null)
    try {
      const result = await api.post<FillResult>('/gap/aigc-refresh', {
        gap_id: fill.gap_id,
        task_id: firstTaskId,
      })
      onResult(result)
    } catch (e) {
      setErr(e instanceof Error ? e.message : '刷新失败')
    } finally {
      setRefreshing(false)
    }
  }, [fill, firstTaskId, onResult])

  // -- 自动轮询 --
  const onResultRef = useRef(onResult)
  useEffect(() => { onResultRef.current = onResult }, [onResult])

  useEffect(() => {
    if (!fill || fill.status === 'ok' || !firstTaskId) {
      setAutoPolling(false)
      setAutoPollAttempts(0)
      return
    }
    setAutoPolling(true)
    setAutoPollAttempts(0)
    let cancelled = false
    let attempts = 0

    const tick = async () => {
      if (cancelled) return
      attempts += 1
      setAutoPollAttempts(attempts)
      try {
        const result = await api.post<FillResult>('/gap/aigc-refresh', {
          gap_id: fill.gap_id,
          task_id: firstTaskId,
        })
        if (cancelled) return
        onResultRef.current(result)
      } catch {
        // 静默忽略
      }
    }

    const handle = window.setInterval(() => {
      if (attempts >= AUTO_POLL_MAX_ATTEMPTS) {
        window.clearInterval(handle)
        setAutoPolling(false)
        return
      }
      void tick()
    }, AUTO_POLL_INTERVAL_MS)

    return () => {
      cancelled = true
      window.clearInterval(handle)
      setAutoPolling(false)
    }
  }, [fill, firstTaskId])

  // === Render ===
  return (
    <div className="space-y-2 rounded-md border border-border bg-background/40 p-3">
      <div className="flex items-center justify-between">
        <h4 className="text-xs font-semibold">{mode === 'image' ? 'AI 生图再渲染' : 'AI 视频生成'}</h4>
        <span className="text-xs text-muted-foreground">
          {mode === 'image' ? '单帧静图，按段落时长定格' : '时长跟随段落规划自动分段'}
        </span>
      </div>

      {/* 进度链 */}
      <PhasePill phase={phase} hasFill={!!fill} mode={mode} />

      {phase === 'idle' && (
        <IdleStage onStart={handleStartAnalyze} />
      )}

      {phase === 'analyzing-spec' && (
        <ThinkingStage
          title="AI 正在分析这一段需要什么参考图…"
          steps={specThinking}
          err={specErr}
        />
      )}

      {phase === 'spec' && (
        <SpecStage
          specs={imageSpecs}
          thinking={specThinking}
          slots={imageSlots}
          slotPrompts={slotPrompts}
          slotErr={slotErr}
          slotBusy={slotBusy}
          slotSaving={slotSaving}
          slotSaveOk={slotSaveOk}
          allReady={allSlotsReady}
          seedreamAllBusy={seedreamAllBusy}
          onSlotPromptChange={(slotId, value) =>
            setSlotPrompts((m) => ({ ...m, [slotId]: value.slice(0, 300) }))
          }
          onUpload={handleUploadSlot}
          onSeedream={handleSeedreamSlot}
          onSeedreamAll={handleSeedreamAllSlots}
          onSaveToLibrary={handleSaveSlotToLibrary}
          onClear={handleClearSlot}
          onSkip={() => void handleEnterPromptStage()}
          onNext={() => void handleEnterPromptStage()}
        />
      )}

      {phase === 'analyzing-prompt' && (
        <ThinkingStage
          title="AI 正在写视频生成提示词…"
          steps={promptThinking}
          err={promptErr}
        />
      )}

      {phase === 'prompt' && (
        <PromptStage
          mode={mode}
          prompt={prompt}
          onPromptChange={(v) => setPrompt(v.slice(0, 300))}
          thinking={promptThinking}
          promptLoading={promptLoading}
          promptErr={promptErr}
          imageSpecs={imageSpecs}
          imageSlots={imageSlots}
          onBackToSpec={imageSpecs.length > 0 ? () => setPhase('spec') : undefined}
          onRegenerate={handleRegeneratePrompt}
          useTailFrame={useTailFrame}
          tailFrameDataUrl={tailFrameDataUrl}
          tailFrameLoading={tailFrameLoading}
          tailFrameErr={tailFrameErr}
          sceneHasPrev={sceneInfo.hasPrev}
          scenePrevReady={sceneInfo.prevReady}
          onTailFrameToggle={handleTailFrameToggle}
          loading={loading}
          err={err}
          onRun={handleRun}
          fillExists={!!fill}
          plannedSubjects={sceneInfo.plannedSubjects}
        />
      )}

      {fill && phase === 'prompt' && (
        <FillStatusCard
          fill={fill}
          mode={mode}
          firstTaskId={firstTaskId}
          expectedChunks={expectedChunks}
          hasPreview={hasPreview}
          autoPolling={autoPolling}
          autoPollAttempts={autoPollAttempts}
          refreshing={refreshing}
          canRefresh={canRefresh}
          onRefresh={handleRefresh}
          onReapply={() => onResult(fill)}
          projectId={plan?.project_id || gap.project_id || ''}
          saveTitle={gap.section || gap.section_id || (mode === 'image' ? 'AI 图片' : 'AI 视频')}
        />
      )}
    </div>
  )
}

// ============================================================================
// Sub-components
// ============================================================================

function PhasePill({ phase, hasFill, mode }: { phase: Phase; hasFill: boolean; mode: 'video' | 'image' }) {
  const step1Active = phase === 'analyzing-spec' || phase === 'spec'
  const step2Active = phase === 'analyzing-prompt' || phase === 'prompt'
  const step1Done = step2Active || hasFill
  const step2Done = hasFill

  const cls = (active: boolean, done: boolean) =>
    cn(
      'rounded px-1.5 py-0.5',
      active
        ? 'bg-primary text-primary-foreground'
        : done
          ? 'bg-emerald-500/20 text-emerald-700 dark:text-emerald-300'
          : 'bg-secondary text-muted-foreground',
    )

  return (
    <div className="flex items-center gap-1 text-xs font-medium">
      <span className={cls(step1Active, step1Done)}>1. 参考图</span>
      <span className="text-muted-foreground">→</span>
      <span className={cls(step2Active, step2Done)}>
        2. {mode === 'image' ? '图片提示词' : '视频提示词'}
      </span>
      <span className="text-muted-foreground">→</span>
      <span
        className={cn(
          'rounded px-1.5 py-0.5',
          hasFill ? 'bg-primary text-primary-foreground' : 'bg-secondary text-muted-foreground',
        )}
      >
        3. 生成
      </span>
    </div>
  )
}

function IdleStage({ onStart }: { onStart: () => void }) {
  return (
    <div className="space-y-2 rounded border border-dashed border-primary/40 bg-primary/5 p-3">
      <p className="text-[12px] leading-relaxed">
        点击下方按钮，AI 助手会先看完整段落上下文，
        判断本段需要哪些参考图，再写一条专业的视频生成提示词，最后调用视频生成模型出片。
        每一步都可以看 AI 的思考过程并干预。
      </p>
      <ul className="space-y-0.5 text-xs text-muted-foreground">
        <li>① 分析参考图清单（可上传 / 让 AI 出图 / 跳过）</li>
        <li>② 自动撰写视频提示词（可手动改）</li>
        <li>③ AI 视频生成（含尾帧承接，让前后段画面连得上）</li>
      </ul>
      <button
        type="button"
        onClick={onStart}
        className="w-full rounded-md bg-primary px-3 py-2 text-xs font-semibold text-primary-foreground hover:bg-primary/90"
      >
        开始分析 ✨
      </button>
    </div>
  )
}

function ThinkingStage({
  title,
  steps,
  err,
}: {
  title: string
  steps: string[]
  err: string | null
}) {
  return (
    <div className="space-y-2 rounded border border-border bg-background/60 p-3">
      <div className="flex items-center gap-2">
        <span className="relative flex h-2 w-2">
          <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-primary/60" />
          <span className="relative inline-flex h-2 w-2 rounded-full bg-primary" />
        </span>
        <span className="text-xs font-medium">{title}</span>
      </div>
      <ThinkingSteps steps={steps} animated />
      {err && <p className="text-xs text-destructive">{err}</p>}
    </div>
  )
}

/** 思考链已抽到 `./ThinkingSteps`，多个 Agent 面板共享。 */

function SpecStage({
  specs,
  thinking,
  slots,
  slotPrompts,
  slotErr,
  slotBusy,
  slotSaving,
  slotSaveOk,
  allReady,
  seedreamAllBusy,
  onSlotPromptChange,
  onUpload,
  onSeedream,
  onSeedreamAll,
  onSaveToLibrary,
  onClear,
  onSkip,
  onNext,
}: {
  specs: ImageSpec[]
  thinking: string[]
  slots: Record<string, ImageSlot>
  slotPrompts: Record<string, string>
  slotErr: Record<string, string | null>
  slotBusy: string | null
  slotSaving: string | null
  slotSaveOk: Record<string, boolean>
  allReady: boolean
  seedreamAllBusy: boolean
  onSlotPromptChange: (slotId: string, value: string) => void
  onUpload: (slotId: string, file: File) => void
  onSeedream: (spec: ImageSpec) => void
  onSeedreamAll: (opts?: { force?: boolean }) => void
  onSaveToLibrary: (spec: ImageSpec) => void
  onClear: (slotId: string) => void
  onSkip: () => void
  onNext: () => void
}) {
  const pendingCount = specs.filter((s) => !slots[s.slot_id]).length
  return (
    <div className="space-y-2">
      {/* 思考链总结：折叠展示，让用户知道 AI 怎么决定的 */}
      {thinking.length > 0 && (
        <details className="rounded border border-border bg-secondary/30 px-2 py-1.5 text-xs" open>
          <summary className="cursor-pointer font-medium">
            AI 思路（{thinking.length} 步）
          </summary>
          <div className="mt-1.5">
            <ThinkingSteps steps={thinking} />
          </div>
        </details>
      )}

      <div className="flex items-center justify-between text-xs">
        <span className="text-muted-foreground">
          AI 建议本段配 <span className="font-semibold text-foreground">{specs.length}</span> 张参考图：
        </span>
        <button
          type="button"
          onClick={onSkip}
          className="text-xs text-primary underline-offset-2 hover:underline"
        >
          跳过参考图 →
        </button>
      </div>

      {/* 一键 Seedream 全部出图：串行调，已就绪 slot 跳过 */}
      {pendingCount > 0 && (
        <button
          type="button"
          onClick={() => onSeedreamAll()}
          disabled={seedreamAllBusy}
          title={`串行调 Seedream 为剩余 ${pendingCount} 张未就绪的参考图出图（已上传 / 已生成的会跳过）`}
          className={cn(
            'flex w-full items-center justify-center gap-1 rounded-md border border-primary/60 bg-primary/10 px-2 py-1.5 text-xs font-medium text-primary transition-colors hover:bg-primary/20',
            seedreamAllBusy && 'cursor-not-allowed opacity-60',
          )}
        >
          {seedreamAllBusy
            ? `🪄 一键出图中…（${pendingCount} 张待生成）`
            : `🪄 一键 AI 出图全部参考图（${pendingCount} 张）`}
        </button>
      )}

      {/* 强制重渲染：所有 slot 已就绪后仍可点，覆盖现有图。
          用户痛报：『AI 生图再渲染不能跳过生图』；改了 subject 后必须能逐 slot 重出。 */}
      {pendingCount === 0 && specs.length > 0 && (
        <button
          type="button"
          onClick={() => onSeedreamAll({ force: true })}
          disabled={seedreamAllBusy}
          title={`强制重新渲染全部 ${specs.length} 张参考图（覆盖现有；改了主体后必跑生图）`}
          className={cn(
            'flex w-full items-center justify-center gap-1 rounded-md border border-amber-500/60 bg-amber-500/10 px-2 py-1.5 text-xs font-medium text-amber-700 transition-colors hover:bg-amber-500/20 dark:text-amber-300',
            seedreamAllBusy && 'cursor-not-allowed opacity-60',
          )}
        >
          {seedreamAllBusy
            ? `🔄 重渲染中…（${specs.length} 张）`
            : `🔄 按当前主体强制重新生图（${specs.length} 张）`}
        </button>
      )}

      <div className="space-y-2">
        {specs.map((spec) => {
          const slot = slots[spec.slot_id]
          const busy = slotBusy === spec.slot_id
          const saving = slotSaving === spec.slot_id
          const saveOk = !!slotSaveOk[spec.slot_id]
          const slotPrompt = slotPrompts[spec.slot_id] ?? spec.prompt
          const errMsg = slotErr[spec.slot_id]
          return (
            <div key={spec.slot_id} className="space-y-1.5 rounded border border-border bg-background/50 p-2">
              <div className="flex items-center justify-between gap-2">
                <span className="text-xs font-semibold">{spec.caption}</span>
                <span className="rounded bg-secondary px-1.5 py-0.5 text-xs font-mono text-muted-foreground">
                  {spec.ratio}
                </span>
              </div>

              {slot ? (
                <div className="flex items-start gap-2">
                  <img
                    src={slot.url}
                    alt={spec.caption}
                    className="h-20 w-20 flex-shrink-0 rounded border border-border object-cover"
                  />
                  <div className="flex-1 space-y-1 text-xs">
                    <p className="text-muted-foreground">
                      已就绪 · {slot.source === 'upload' ? '用户上传' : 'AI 出图'}
                      {saveOk && (
                        <span className="ml-1 text-emerald-600 dark:text-emerald-300">· 已入库</span>
                      )}
                    </p>
                    <div className="flex flex-wrap items-center gap-2">
                      {slot.source === 'seedream' && !saveOk && (
                        <button
                          type="button"
                          onClick={() => onSaveToLibrary(spec)}
                          disabled={saving}
                          className={cn(
                            'rounded border border-emerald-500/40 bg-emerald-500/10 px-1.5 py-0.5 text-xs text-emerald-700 hover:bg-emerald-500/20 dark:text-emerald-300',
                            saving && 'cursor-not-allowed opacity-60',
                          )}
                        >
                          {saving ? '保存中…' : '保存到素材库'}
                        </button>
                      )}
                      <button
                        type="button"
                        onClick={() => onClear(spec.slot_id)}
                        disabled={busy || saving}
                        className="text-primary underline-offset-2 hover:underline"
                      >
                        换一张
                      </button>
                    </div>
                  </div>
                </div>
              ) : (
                <>
                  <textarea
                    value={slotPrompt}
                    onChange={(e) => onSlotPromptChange(spec.slot_id, e.target.value)}
                    rows={2}
                    disabled={busy}
                    className={cn(
                      'w-full resize-y rounded-md border border-border bg-background px-2 py-1 text-xs outline-none focus:border-primary',
                      busy && 'cursor-wait opacity-60',
                    )}
                    placeholder="描述这张图（用于 AI 出图；上传方式可忽略）"
                  />
                  <div className="flex items-center gap-2">
                    <label className={cn(
                      'flex-1 cursor-pointer rounded-md border border-border bg-secondary px-2 py-1 text-center text-xs hover:bg-secondary/80',
                      busy && 'pointer-events-none opacity-60',
                    )}>
                      上传图片
                      <input
                        type="file"
                        accept="image/*"
                        className="hidden"
                        onChange={(e) => {
                          const f = e.target.files?.[0]
                          if (f) onUpload(spec.slot_id, f)
                          e.target.value = ''
                        }}
                      />
                    </label>
                    <button
                      type="button"
                      onClick={() => onSeedream(spec)}
                      disabled={busy || !slotPrompt.trim()}
                      className={cn(
                        'flex-1 rounded-md bg-primary px-2 py-1 text-xs font-medium text-primary-foreground',
                        (busy || !slotPrompt.trim()) && 'cursor-not-allowed opacity-60',
                      )}
                    >
                      {busy ? '生成中…' : 'AI 出图'}
                    </button>
                  </div>
                </>
              )}
              {errMsg && <p className="text-xs text-destructive">{errMsg}</p>}
            </div>
          )
        })}
      </div>

      <button
        type="button"
        onClick={onNext}
        disabled={!allReady}
        className={cn(
          'w-full rounded-md bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground',
          !allReady && 'cursor-not-allowed opacity-60',
        )}
      >
        {allReady ? '下一步：让 AI 写视频提示词 →' : '请先准备好所有参考图（或点上方"跳过参考图"）'}
      </button>
    </div>
  )
}

function PromptStage({
  mode,
  prompt,
  onPromptChange,
  thinking,
  promptLoading,
  promptErr,
  imageSpecs,
  imageSlots,
  onBackToSpec,
  onRegenerate,
  useTailFrame,
  tailFrameDataUrl,
  tailFrameLoading,
  tailFrameErr,
  sceneHasPrev,
  scenePrevReady,
  onTailFrameToggle,
  loading,
  err,
  onRun,
  fillExists,
  plannedSubjects,
}: {
  mode: 'video' | 'image'
  prompt: string
  onPromptChange: (v: string) => void
  thinking: string[]
  promptLoading: boolean
  promptErr: string | null
  imageSpecs: ImageSpec[]
  imageSlots: Record<string, ImageSlot>
  onBackToSpec?: () => void
  onRegenerate: () => void
  useTailFrame: boolean
  tailFrameDataUrl: string | null
  tailFrameLoading: boolean
  tailFrameErr: string | null
  sceneHasPrev: boolean
  scenePrevReady: boolean
  onTailFrameToggle: (next: boolean) => void
  loading: boolean
  err: string | null
  onRun: () => void
  fillExists: boolean
  plannedSubjects: string[]
}) {
  return (
    <>
      {/* 已选参考图缩略图 */}
      {Object.keys(imageSlots).length > 0 && (
        <div className="space-y-1">
          <div className="flex items-center justify-between">
            <span className="text-xs font-semibold text-muted-foreground">
              本段参考图（{Object.keys(imageSlots).length} 张）
            </span>
            {onBackToSpec && (
              <button
                type="button"
                onClick={onBackToSpec}
                className="text-xs text-primary underline-offset-2 hover:underline"
              >
                ← 返回调整参考图
              </button>
            )}
          </div>
          <div className="flex flex-wrap gap-1.5">
            {imageSpecs.map((spec) => {
              const slot = imageSlots[spec.slot_id]
              if (!slot) return null
              return (
                <div key={spec.slot_id} className="relative">
                  <img
                    src={slot.url}
                    alt={spec.caption}
                    title={spec.caption}
                    className="h-14 w-14 rounded border border-border object-cover"
                  />
                  <span className="absolute -bottom-1 -right-1 rounded bg-secondary px-1 text-[8px] text-muted-foreground">
                    {slot.source === 'upload' ? '上传' : 'AI'}
                  </span>
                </div>
              )
            })}
          </div>
        </div>
      )}

      {Object.keys(imageSlots).length === 0 && imageSpecs.length > 0 && onBackToSpec && (
        <div className="rounded border border-dashed border-amber-500/40 bg-amber-500/5 px-2 py-1.5 text-xs text-amber-700 dark:text-amber-300">
          已跳过参考图，本段将仅按文字提示词生成视频。
          <button
            type="button"
            onClick={onBackToSpec}
            className="ml-2 underline-offset-2 hover:underline"
          >
            返回上传 / 生成
          </button>
        </div>
      )}

      {/* Agent 思考链 */}
      {thinking.length > 0 && !promptLoading && (
        <details className="rounded border border-border bg-secondary/30 px-2 py-1.5 text-xs">
          <summary className="cursor-pointer font-medium">
            AI 怎么写出这条提示词的（{thinking.length} 步）
          </summary>
          <div className="mt-1.5">
            <ThinkingSteps steps={thinking} />
          </div>
        </details>
      )}

      <div>
        <div className="mb-1 flex items-center justify-between">
          <label className="text-xs font-semibold text-muted-foreground">
            视频生成提示词（AI 视频生成）
          </label>
          <button
            type="button"
            onClick={onRegenerate}
            disabled={promptLoading || loading}
            className={cn(
              'text-xs text-primary underline-offset-2 hover:underline',
              (promptLoading || loading) && 'cursor-not-allowed opacity-60',
            )}
          >
            {promptLoading ? '生成中…' : '↻ 让 AI 再写一版'}
          </button>
        </div>
        <textarea
          value={prompt}
          onChange={(e) => onPromptChange(e.target.value)}
          rows={4}
          placeholder={promptLoading ? 'AI 正在为这一段写视频生成提示词…' : '描述画面/风格；为空则用素材需求文字'}
          disabled={promptLoading}
          className={cn(
            'w-full resize-y rounded-md border border-border bg-background px-2 py-1.5 text-sm outline-none focus:border-primary',
            promptLoading && 'cursor-wait opacity-60',
          )}
        />
        <div className="mt-0.5 flex items-center justify-between text-xs">
          <span className="text-muted-foreground">
            {promptErr ? <span className="text-destructive">{promptErr}</span> : '可手动修改后再点开始生成'}
          </span>
          <span className="font-mono text-muted-foreground">{prompt.length}/300</span>
        </div>
        <p className="mt-1 text-xs text-muted-foreground">
          输出分辨率：720p（比例随高级设置走）
        </p>
      </div>

      {/* AI 生图再渲染：分镜数 / 主体清单都从 plan_agent 已经决策好的 ShotPlan 读，
          用户不再需要手动填——这里只 read-only 展示一下让用户知道会出几张图。 */}
      {mode === 'image' && (
        <div className="space-y-1.5 rounded border border-border bg-secondary/30 p-2">
          <div className="flex items-center justify-between text-xs">
            <span className="font-medium">
              本段分镜 · {plannedSubjects.length > 0 ? `${plannedSubjects.length} 张图` : '1 张图（单镜头）'}
            </span>
            <span className="text-xs text-muted-foreground">由内容轨自动决定</span>
          </div>
          {plannedSubjects.length > 0 ? (
            <div className="flex flex-wrap gap-1">
              {plannedSubjects.map((sub, i) => (
                <span
                  key={`${i}-${sub}`}
                  className="rounded-full border border-border bg-background/70 px-2 py-0.5 text-xs"
                  title={sub}
                >
                  镜 {i + 1}：{sub.slice(0, 20)}{sub.length > 20 ? '…' : ''}
                </span>
              ))}
            </div>
          ) : (
            <p className="text-xs text-muted-foreground">
              本段没有分镜规划 · 走单图 ken-burns 动效
            </p>
          )}
        </div>
      )}

      {/* 尾帧承接前段 */}
      <div className="space-y-1 rounded border border-border bg-secondary/30 p-2">
        <label className="flex items-start gap-2 text-xs">
          <input
            type="checkbox"
            checked={useTailFrame}
            disabled={tailFrameLoading || !sceneHasPrev || !scenePrevReady}
            onChange={(e) => onTailFrameToggle(e.target.checked)}
            className="mt-0.5"
          />
          <span>
            <span className="font-medium">尾帧承接前段</span>
            <span className="ml-1 text-muted-foreground">
              （把上一段最后一帧作为本段首帧参考，画面更连贯）
            </span>
          </span>
        </label>
        {!sceneHasPrev && (
          <p className="pl-5 text-xs text-muted-foreground">本段是第一段，没有可承接的前段。</p>
        )}
        {sceneHasPrev && !scenePrevReady && (
          <p className="pl-5 text-xs text-muted-foreground">前一段尚未补全，请先生成前段。</p>
        )}
        {tailFrameLoading && (
          <p className="pl-5 text-xs text-muted-foreground">正在抽取前段尾帧…</p>
        )}
        {tailFrameErr && (
          <p className="pl-5 text-xs text-destructive">{tailFrameErr}</p>
        )}
        {tailFrameDataUrl && (
          <div className="pl-5">
            <img
              src={tailFrameDataUrl}
              alt="前段尾帧"
              className="h-14 rounded border border-border object-cover"
            />
          </div>
        )}
      </div>

      <button
        onClick={onRun}
        disabled={loading || promptLoading || !prompt.trim()}
        className={cn(
          'w-full rounded-md bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground',
          (loading || promptLoading || !prompt.trim()) && 'cursor-not-allowed opacity-60',
        )}
      >
        {loading ? '生成中…（可能要 3 分钟以上）' : fillExists ? '改完提示词重新生成' : '开始生成'}
      </button>

      {err && <p className="text-xs text-destructive">{err}</p>}
    </>
  )
}

function FillStatusCard({
  fill,
  mode,
  firstTaskId,
  expectedChunks,
  hasPreview,
  autoPolling,
  autoPollAttempts,
  refreshing,
  canRefresh,
  onRefresh,
  onReapply,
  projectId,
  saveTitle,
}: {
  fill: FillResult
  mode: 'video' | 'image'
  firstTaskId: string | null
  expectedChunks: number
  hasPreview: boolean
  autoPolling: boolean
  autoPollAttempts: number
  refreshing: boolean
  canRefresh: boolean
  onRefresh: () => void
  /** 重新把当前 fill 推回父级 → runAnalyze 重建 plan，强制本段画面同步到内容轨。
   *  正常路径下 fill 完成时已自动应用过一次，这个按钮供出错回滚 / 跨段反复确认时手动重灌。 */
  onReapply: () => void
  projectId: string
  saveTitle: string
}) {
  const [savingIdx, setSavingIdx] = useState<number | null>(null)
  const [savedIdx, setSavedIdx] = useState<Set<number>>(new Set())
  const [saveErr, setSaveErr] = useState<Record<number, string>>({})

  const handleSave = useCallback(
    async (url: string, idx: number) => {
      if (!projectId) {
        setSaveErr((m) => ({ ...m, [idx]: '缺少 project_id' }))
        return
      }
      setSavingIdx(idx)
      setSaveErr((m) => ({ ...m, [idx]: '' }))
      try {
        const abs = url.startsWith('http') ? url : `${window.location.origin}${url}`
        const body: AssetSaveFromUrlRequest = {
          project_id: projectId,
          url: abs,
          kind: mode === 'image' ? 'reference_image' : 'reference_video',
          title:
            mode === 'image'
              ? `${saveTitle}-AI 图片`
              : `${saveTitle}-AI 视频${idx + 1}`,
          tags: ['aigc', saveTitle].filter(Boolean) as string[],
        }
        await api.post<Asset>('/asset/save-from-url', body)
        setSavedIdx((s) => new Set(s).add(idx))
      } catch (e) {
        setSaveErr((m) => ({ ...m, [idx]: e instanceof Error ? e.message : '保存失败' }))
      } finally {
        setSavingIdx(null)
      }
    },
    [mode, projectId, saveTitle],
  )
  return (
    <div className="space-y-2 rounded border border-border bg-secondary/50 p-2 text-xs">
      <div className="flex items-center justify-between">
        <span>
          状态：
          <span
            className={cn(
              'ml-1 font-medium',
              fill.status === 'ok'
                ? 'text-emerald-600 dark:text-emerald-300'
                : 'text-amber-600 dark:text-amber-300',
            )}
          >
            {fill.status === 'ok'
              ? '完成'
              : mode === 'image'
                ? '生图未完成'
                : '生成中 / 异常'}
          </span>
        </span>
        <span className="text-muted-foreground">
          {mode === 'image'
            ? fill.aigc_image_urls && fill.aigc_image_urls.length > 1
              ? `${fill.aigc_image_urls.length} 张图`
              : '1 张图'
            : `${fill.chunks_count}/${expectedChunks || '?'} 段`}
        </span>
      </div>
      {fill.note && <p className="text-muted-foreground">{fill.note}</p>}

      {/* 应用到轨道：fill 完成后自动跑过一次 runAnalyze（在父级 onResult 里），
          这里给一个显式标记 + 手动重灌按钮——用户改了 prompt 重生成后，UI
          上更明确地知道"这版结果已经落到了内容轨" / 出问题时能一键重新应用。 */}
      {hasPreview && (
        <div className="flex items-center justify-between gap-2 rounded-md border border-emerald-500/30 bg-emerald-500/5 px-2 py-1">
          <span className="text-xs text-emerald-700 dark:text-emerald-300">
            ✓ 已应用到本镜内容轨
            <span className="ml-1 text-[10px] font-normal text-muted-foreground">
              （生成后自动写入；改完可再点右侧重新应用）
            </span>
          </span>
          <button
            type="button"
            onClick={onReapply}
            className="shrink-0 rounded border border-emerald-500/50 bg-emerald-500/10 px-1.5 py-0.5 text-[11px] font-medium text-emerald-700 hover:bg-emerald-500/20 dark:text-emerald-200"
            title="把本结果重新写回内容轨（runAnalyze 重建 plan）"
          >
            ↻ 应用到轨道
          </button>
        </div>
      )}

      {!hasPreview && (
        <div className="rounded-md border border-amber-500/40 bg-amber-500/10 p-2">
          <p className="text-xs font-medium text-amber-700 dark:text-amber-300">
            {mode === 'image' ? '图片还没就绪' : '还没有可预览的视频'}
            {autoPolling && (
              <span className="ml-2 text-xs font-normal text-amber-600/80">
                · 自动刷新中 {autoPollAttempts}/{AUTO_POLL_MAX_ATTEMPTS}
              </span>
            )}
          </p>
          <p className="mt-0.5 text-xs text-muted-foreground">
            {fill.status === 'ok'
              ? mode === 'image'
                ? '生成完成但暂时拿不到图片链接，可以重新点开始生成再试。'
                : '生成完成但暂时拿不到视频链接，可以点刷新再试。'
              : autoPolling
                ? `视频还在生成（排队 / 渲染 / 上传中）。每 ${AUTO_POLL_INTERVAL_MS / 1000} 秒自动查一次，最长 ${(AUTO_POLL_INTERVAL_MS * AUTO_POLL_MAX_ATTEMPTS) / 60000} 分钟。`
                : mode === 'image'
                  ? `Seedream 生图未返回图片：${fill.note || '请检查提示词或重试'}`
                  : '视频还没出来（超时 / 排队中 / 失败）。点下方刷新可以再查一次。'}
          </p>
          {firstTaskId && (
            <p className="mt-1 font-mono text-xs text-muted-foreground">
              任务号：{firstTaskId}
            </p>
          )}
          {firstTaskId && (
            <button
              onClick={onRefresh}
              disabled={refreshing}
              className={cn(
                'mt-1.5 w-full rounded-md border border-amber-500/60 bg-amber-500/20 px-2 py-1 text-xs font-medium text-amber-700 transition-colors hover:bg-amber-500/30 dark:text-amber-200',
                refreshing && 'cursor-not-allowed opacity-60',
              )}
            >
              {refreshing ? '查询中…' : autoPolling ? '马上刷新一次' : '刷新进度'}
            </button>
          )}
        </div>
      )}

      {hasPreview && mode === 'image' && fill.aigc_image_url && (
        <div className="space-y-1.5">
          {/* 多镜头模式（path B）：N 张图横向预览，每张一个保存按钮。
              预览压窄到 max-w-md (~448px) 避免占满整列；多镜头继续走 grid-cols-2。 */}
          {fill.aigc_image_urls && fill.aigc_image_urls.length > 1 ? (
            <>
              <div className="text-xs text-muted-foreground">
                Seedream 故事板 · 本段拆 {fill.aigc_image_urls.length} 个子镜头（视觉一致）
              </div>
              <div className="grid max-w-md grid-cols-2 gap-1.5">
                {fill.aigc_image_urls.map((url, i) => (
                  <div key={`${url}-${i}`} className="space-y-0.5">
                    <div className="flex items-center justify-between text-xs text-muted-foreground">
                      <span>镜头 {i + 1}</span>
                      <div className="flex items-center gap-1.5">
                        <button
                          type="button"
                          onClick={() => handleSave(url, i)}
                          disabled={savingIdx === i || savedIdx.has(i) || !projectId}
                          className={cn(
                            'rounded border px-1 py-0.5 text-xs font-medium transition-colors',
                            savedIdx.has(i)
                              ? 'cursor-default border-emerald-500/50 bg-emerald-500/10 text-emerald-700 dark:text-emerald-300'
                              : savingIdx === i
                                ? 'cursor-wait border-border bg-secondary/60 text-muted-foreground'
                                : 'border-primary/50 bg-primary/10 text-primary hover:bg-primary/20',
                            !projectId && 'cursor-not-allowed opacity-60',
                          )}
                          title={!projectId ? '缺少项目信息，无法入库' : savedIdx.has(i) ? '已存入素材库' : '保存为本项目的参考图素材'}
                        >
                          {savedIdx.has(i) ? '✓ 入库' : savingIdx === i ? '保存…' : '存库'}
                        </button>
                        <a
                          href={url}
                          target="_blank"
                          rel="noreferrer"
                          className="font-mono text-primary underline-offset-2 hover:underline"
                        >
                          ↗
                        </a>
                      </div>
                    </div>
                    {saveErr[i] && <p className="text-xs text-destructive">{saveErr[i]}</p>}
                    <img
                      src={url}
                      alt={`${saveTitle} 镜头 ${i + 1}`}
                      className="w-full rounded-md border border-border bg-black"
                    />
                  </div>
                ))}
              </div>
            </>
          ) : (
            <div className="max-w-xs space-y-0.5">
              <div className="flex items-center justify-between text-xs text-muted-foreground">
                <span>Seedream 出图</span>
                <div className="flex items-center gap-2">
                  <button
                    type="button"
                    onClick={() => handleSave(fill.aigc_image_url!, 0)}
                    disabled={savingIdx === 0 || savedIdx.has(0) || !projectId}
                    className={cn(
                      'rounded border px-1.5 py-0.5 text-xs font-medium transition-colors',
                      savedIdx.has(0)
                        ? 'cursor-default border-emerald-500/50 bg-emerald-500/10 text-emerald-700 dark:text-emerald-300'
                        : savingIdx === 0
                          ? 'cursor-wait border-border bg-secondary/60 text-muted-foreground'
                          : 'border-primary/50 bg-primary/10 text-primary hover:bg-primary/20',
                      !projectId && 'cursor-not-allowed opacity-60',
                    )}
                    title={!projectId ? '缺少项目信息，无法入库' : savedIdx.has(0) ? '已存入素材库' : '保存为本项目的参考图素材'}
                  >
                    {savedIdx.has(0) ? '✓ 已入库' : savingIdx === 0 ? '保存中…' : '保存到素材库'}
                  </button>
                  <a
                    href={fill.aigc_image_url}
                    target="_blank"
                    rel="noreferrer"
                    className="font-mono text-primary underline-offset-2 hover:underline"
                  >
                    新窗打开 ↗
                  </a>
                </div>
              </div>
              {saveErr[0] && <p className="text-xs text-destructive">{saveErr[0]}</p>}
              <img
                src={fill.aigc_image_url}
                alt={saveTitle}
                className="w-full rounded-md border border-border bg-black"
              />
            </div>
          )}
        </div>
      )}

      {hasPreview && mode === 'video' && (
        <div className="space-y-1.5">
          {fill.video_urls.map((url, i) => (
            <div key={`${url}-${i}`} className="max-w-md space-y-0.5">
              <div className="flex items-center justify-between text-xs text-muted-foreground">
                <span>第 {i + 1} 段</span>
                <div className="flex items-center gap-2">
                  <button
                    type="button"
                    onClick={() => handleSave(url, i)}
                    disabled={savingIdx === i || savedIdx.has(i) || !projectId}
                    className={cn(
                      'rounded border px-1.5 py-0.5 text-xs font-medium transition-colors',
                      savedIdx.has(i)
                        ? 'cursor-default border-emerald-500/50 bg-emerald-500/10 text-emerald-700 dark:text-emerald-300'
                        : savingIdx === i
                          ? 'cursor-wait border-border bg-secondary/60 text-muted-foreground'
                          : 'border-primary/50 bg-primary/10 text-primary hover:bg-primary/20',
                      !projectId && 'cursor-not-allowed opacity-60',
                    )}
                    title={
                      !projectId
                        ? '缺少项目信息，无法入库'
                        : savedIdx.has(i)
                          ? '已存入素材库'
                          : '保存为本项目的参考视频素材'
                    }
                  >
                    {savedIdx.has(i)
                      ? '✓ 已入库'
                      : savingIdx === i
                        ? '保存中…'
                        : '保存到素材库'}
                  </button>
                  <a
                    href={url}
                    target="_blank"
                    rel="noreferrer"
                    className="font-mono text-primary underline-offset-2 hover:underline"
                  >
                    新窗打开 ↗
                  </a>
                </div>
              </div>
              {saveErr[i] && (
                <p className="text-xs text-destructive">{saveErr[i]}</p>
              )}
              <video
                src={url}
                controls
                preload="metadata"
                poster={i === 0 ? fill.cover_url ?? undefined : undefined}
                className="w-full rounded-md border border-border bg-black"
              />
            </div>
          ))}
        </div>
      )}

      {fill.chunk_task_ids && fill.chunk_task_ids.length > 0 && (
        <details className="text-xs text-muted-foreground">
          <summary className="cursor-pointer">任务号列表（{fill.chunk_task_ids.length}）</summary>
          <ul className="mt-1 space-y-0.5 font-mono">
            {fill.chunk_task_ids.map((t) => (
              <li key={t}>{t}</li>
            ))}
          </ul>
        </details>
      )}

      {hasPreview && canRefresh && (
        <button
          onClick={onRefresh}
          disabled={refreshing}
          className={cn(
            'rounded-md border border-primary/60 bg-primary/10 px-2 py-1 text-xs font-medium text-primary transition-colors hover:bg-primary/20',
            refreshing && 'cursor-not-allowed opacity-60',
          )}
        >
          {refreshing ? '查询中…' : '刷新进度（仅第一段）'}
        </button>
      )}
    </div>
  )
}

function extractTaskId(note: string | null | undefined): string | null {
  if (!note) return null
  const m = note.match(/task=([\w-]+)/)
  return m?.[1] ?? null
}

function extractExpectedChunks(note: string | null | undefined): number | null {
  if (!note) return null
  const partial = note.match(/(\d+)\s*\/\s*(\d+)\s*段/)
  if (partial) return Number(partial[2])
  const full = note.match(/链式生成完成（(\d+)\s*段/)
  if (full) return Number(full[1])
  return null
}
