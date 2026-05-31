import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'

import { api } from '@/api/client'
import { deletePlanBgm, patchPlanBgm } from '@/api/bgm'
import { patchPlanSettings } from '@/api/plan'
import { commitStep, getStepSnapshot } from '@/api/steps'
import { deleteVoice, synthesizeAll, synthesizeOne } from '@/api/voice'
import { BatchAigcButton } from '@/components/compose/BatchAigcButton'
import { BatchCopyButton } from '@/components/compose/BatchCopyButton'
import { BgmPickerDialog } from '@/components/compose/BgmPickerDialog'
import { BriefInput } from '@/components/compose/BriefInput'
import { ComposeSettingsPanel } from '@/components/compose/ComposeSettingsPanel'
import { FillAigcPanel } from '@/components/compose/FillAigcPanel'
import { FillCopyPanel } from '@/components/compose/FillCopyPanel'
import { FillRerankPanel } from '@/components/compose/FillRerankPanel'
import { FourTrackBoard } from '@/components/compose/FourTrackBoard'
import { GapPreviewDialog } from '@/components/compose/GapPreviewDialog'
import { MaterialGrid } from '@/components/compose/MaterialGrid'
import { SceneEditPanel } from '@/components/compose/SceneEditPanel'
import { StoryboardPreview } from '@/components/compose/StoryboardPreview'
import { VideoGoalInput } from '@/components/compose/VideoGoalInput'
import { NLEditPanel } from '@/components/edit/NLEditPanel'
import { PageShell } from '@/components/layout/PageShell'
import { SECTION_BG, SECTION_SHORT } from '@/lib/sections'
import { cn } from '@/lib/utils'
import { usePlanStore } from '@/stores/plan'
import { useProjectsStore } from '@/stores/projects'
import { useSessionStore } from '@/stores/session'
import type {
  FillAction,
  FillResult,
  Gap,
  GapDetectRequest,
  GapFillAllResponse,
  GapFillRequest,
  MaterialUploadResponse,
  PackagingRecommendRequest,
  Plan,
  PlanBuildRequest,
  SampleManifest,
} from '@/types/schemas'

const ACTION_TABS: { value: FillAction; label: string; hint: string }[] = [
  { value: 'rerank', label: '结构重排', hint: '从已上传素材里挑一个最匹配的填进 slot' },
  { value: 'copy', label: '文案补全', hint: 'LLM 写一段画外口播，可编辑+三选一' },
  { value: 'aigc', label: 'AIGC 生成', hint: 'Seedance T2V 生成 5-8s 短片填补 slot' },
]

export default function ComposePage() {
  const navigate = useNavigate()

  // session store
  const selectedSampleIds = useSessionStore((s) => s.selectedSampleIds)
  const selectedSampleTitles = useSessionStore((s) => s.selectedSampleTitles)
  const selectedSampleId = selectedSampleIds[0] ?? null
  const videoType = useSessionStore((s) => s.videoType)
  const sessionId = useSessionStore((s) => s.sessionId)
  const manifest = useSessionStore((s) => s.manifest)
  const materials = useSessionStore((s) => s.materials)
  const brief = useSessionStore((s) => s.brief)
  const setBrief = useSessionStore((s) => s.setBrief)
  const videoGoal = useSessionStore((s) => s.videoGoal)
  const setVideoGoal = useSessionStore((s) => s.setVideoGoal)
  const settings = useSessionStore((s) => s.settings)
  const setSettings = useSessionStore((s) => s.setSettings)
  const setSession = useSessionStore((s) => s.setSession)
  const appendMaterials = useSessionStore((s) => s.appendMaterials)
  const removeMaterial = useSessionStore((s) => s.removeMaterial)
  const reorderMaterials = useSessionStore((s) => s.reorderMaterials)

  // projects store（仅读 currentProjectId；后端 mark_planned/mark_rendered 已自动写回，前端无需 upsert）
  const currentProjectId = useProjectsStore((s) => s.currentProjectId)

  // plan store
  const plan = usePlanStore((s) => s.plan)
  const gaps = usePlanStore((s) => s.gaps)
  const fills = usePlanStore((s) => s.fills)
  const selectedGapId = usePlanStore((s) => s.selectedGapId)
  const setPlan = usePlanStore((s) => s.setPlan)
  const setGaps = usePlanStore((s) => s.setGaps)
  const setFills = usePlanStore((s) => s.setFills)
  const upsertFill = usePlanStore((s) => s.upsertFill)
  const setSelectedGapId = usePlanStore((s) => s.setSelectedGapId)

  // UI state
  const fileInputRef = useRef<HTMLInputElement | null>(null)
  const [uploading, setUploading] = useState(false)
  const [analyzing, setAnalyzing] = useState(false)
  const [activeAction, setActiveAction] = useState<FillAction>('rerank')
  // 每条 gap 独立的 busy 锁：切到别的 gap 不会还显示上一段的 loading 态。
  // 选用 Set<gap_id> 而非全局 boolean——AIGC 链式生成可能 >3 分钟，用户在等待期间
  // 完全有理由切到别的段先写文案、看分镜，不该被全局 spinner 锁死。
  const [busyGapIds, setBusyGapIds] = useState<ReadonlySet<string>>(() => new Set())
  const markBusy = useCallback((gapId: string, busy: boolean) => {
    setBusyGapIds((prev) => {
      const has = prev.has(gapId)
      if (busy && has) return prev
      if (!busy && !has) return prev
      const next = new Set(prev)
      if (busy) next.add(gapId)
      else next.delete(gapId)
      return next
    })
  }, [])
  const [error, setError] = useState<string | null>(null)
  const [previewGapId, setPreviewGapId] = useState<string | null>(null)
  const [briefTouched, setBriefTouched] = useState(false)
  // 「下一步」三阶段：补缺口 → 生成包装 → 跳渲染
  const [finalizing, setFinalizing] = useState<
    'idle' | 'filling-gaps' | 'packaging' | 'done'
  >('idle')
  // 四轨板上的轨道动作 busy 锁（区别于 filling，避免与补全面板状态混淆）
  const [trackBusy, setTrackBusy] = useState(false)
  const [bgmPickerOpen, setBgmPickerOpen] = useState(false)
  // 内容轨当前选中的 scene_id——驱动 SceneEditPanel
  const [selectedSceneId, setSelectedSceneId] = useState<string | null>(null)

  const sortedMaterials = useMemo(
    () => materials.slice().sort((a, b) => a.sort_order - b.sort_order),
    [materials],
  )

  // 选中 gap → 拿对应的 fill（如果已经做过）
  const selectedGap = useMemo(
    () => gaps.find((g) => g.gap_id === selectedGapId) ?? null,
    [gaps, selectedGapId],
  )
  const selectedFill = useMemo(
    () => fills.find((f) => f.gap_id === selectedGapId) ?? null,
    [fills, selectedGapId],
  )
  const filledGapIds = useMemo(() => new Set(fills.map((f) => f.gap_id)), [fills])
  // 当前选中那条 gap 的 busy 状态——仅用于左侧补全面板的 disabled / loading 标记。
  // 全局动作锁还是用 analyzing（plan/build 是单例不并发）。
  const gapBusy = selectedGap ? busyGapIds.has(selectedGap.gap_id) : false
  const anyGapBusy = busyGapIds.size > 0

  // gap 列表换了之后，自动选第一个 miss/warn
  useEffect(() => {
    if (gaps.length === 0) {
      setSelectedGapId(null)
      return
    }
    if (selectedGapId && gaps.some((g) => g.gap_id === selectedGapId)) return
    const first = gaps.find((g) => g.status !== 'ok') ?? gaps[0]
    setSelectedGapId(first.gap_id)
  }, [gaps, selectedGapId, setSelectedGapId])

  // 内容轨选段：用户没点过、或选的段在新 plan 里已不存在 → 回落到 sc-0；
  // 用 derive-during-render 避免 setState-in-effect 级联渲染。
  const effectiveSelectedSceneId: string | null = (() => {
    if (!plan || plan.main_track.length === 0) return null
    if (selectedSceneId && plan.main_track.some((s) => s.scene_id === selectedSceneId)) {
      return selectedSceneId
    }
    return plan.main_track[0].scene_id
  })()

  // mount：若 compose 步骤已 commit 过，拉对应 plan + gaps 回 store——切项目/刷新后回到本页能看到上次结果
  useEffect(() => {
    if (!currentProjectId) return
    let cancelled = false
    void (async () => {
      try {
        const snap = await getStepSnapshot(currentProjectId, 'compose')
        if (cancelled || !snap) return
        const savedPlanId = snap.payload?.plan_id as string | undefined
        if (!savedPlanId) return
        // 已有相同 plan_id → 不重复拉
        if (plan?.plan_id === savedPlanId) return
        const [freshPlan, freshGaps] = await Promise.all([
          api.get<Plan>(`/plan/${savedPlanId}`),
          api.get<Gap[]>(`/gap?plan_id=${savedPlanId}`),
        ])
        if (cancelled) return
        setPlan(freshPlan)
        setGaps(freshGaps)
      } catch {
        /* 没快照或拉取失败时让用户重新跑分析 */
      }
    })()
    return () => {
      cancelled = true
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [currentProjectId])

  /* ----------------------------- 上传 ----------------------------- */

  const handlePickFiles = useCallback(
    async (files: FileList | null) => {
      if (!files || files.length === 0) return
      if (!currentProjectId) {
        setError('请先在首页新建/选择一个项目，再上传素材')
        return
      }
      setError(null)
      setUploading(true)
      try {
        const fd = new FormData()
        Array.from(files).forEach((f) => fd.append('files', f))
        // project_id 是后端唯一隔离键；session_id 字段保留为别名（已等于 project_id）
        fd.append('project_id', currentProjectId)
        fd.append('video_type', videoType)
        const resp = await api.post<MaterialUploadResponse>('/material/upload', fd)
        setSession(resp.session_id)
        appendMaterials(resp.materials)
      } catch (err) {
        setError(err instanceof Error ? err.message : '上传失败')
      } finally {
        setUploading(false)
      }
    },
    [appendMaterials, currentProjectId, setSession, videoType],
  )

  /* -------------------- 智能分析（plan/build + gap/detect） -------------------- */

  const runAnalyze = useCallback(
    async (extraFills?: FillResult[]) => {
      if (!selectedSampleId) {
        setError('请先在素材库挑一个样例')
        return null
      }
      if (!currentProjectId) {
        setError('请先在首页新建/选择一个项目')
        return null
      }
      if (brief.trim().length === 0) {
        setBriefTouched(true)
        setError('请先输入主题——LLM 需要它作为语义锚定，否则缺口推断会失真。')
        return null
      }
      setError(null)
      setAnalyzing(true)
      try {
        // 「重新分析」（无 extraFills）→ 旧 plan 的 fills 对新 plan_id 不再有效，整体清空
        // 避免后端 fill_by_section 路由把上一版的 narration / aigc_video_urls 错塞进新段落。
        const effectiveFills: FillResult[] = extraFills ?? []
        if (extraFills === undefined) {
          setFills([])
        }
        const planReq: PlanBuildRequest = {
          sample_ids: selectedSampleIds,
          project_id: currentProjectId,
          session_id: currentProjectId,
          brief: brief.trim() || null,
          video_goal: videoGoal.trim() || null,
          settings,
          selected_materials: sortedMaterials.map((m) => m.material_id),
          fills: effectiveFills,
          variant: 'A',
        }
        const builtPlan = await api.post<Plan>('/plan/build', planReq)
        setPlan(builtPlan)

        const detectReq: GapDetectRequest = {
          plan_id: builtPlan.plan_id,
          project_id: currentProjectId,
          session_id: currentProjectId,
          // 用户没传素材时关掉 mock 回退，让所有 gap 真实地显示 miss，引导走 copy/aigc。
          allow_mock: sortedMaterials.length > 0,
        }
        const detected = await api.post<Gap[]>('/gap/detect', detectReq)
        // 把已采纳的 fill 叠加到 gap 状态上：后端 detect 只看 materials，不知道
        // 用户刚采纳的 copy/aigc/rerank。这里在前端做合并，让红色 ❌ 立刻变 ✅。
        //
        // Bug 修复：后端每次 detect 会用新 plan_id 后缀重写 gap_id（plan-scoped 唯一性需要），
        // 老 fill 的 gap_id 与新 detect 的 gap_id 永远对不上，merge 用 gap_id 必失败 → 看似"应用失败"。
        // 改用 section_id（稳定）作为兜底匹配键；同时把 fills 的 gap_id 改写成新的，
        // 下一轮 runAnalyze 透传时不再积累陈旧记录。
        const fillByGapId = new Map(effectiveFills.map((f) => [f.gap_id, f]))
        const fillBySection = new Map<string, FillResult>()
        for (const f of effectiveFills) {
          if (f.section_id) fillBySection.set(f.section_id, f)
        }
        const merged = detected.map((g): Gap => {
          const f =
            fillByGapId.get(g.gap_id) ??
            (g.section_id ? fillBySection.get(g.section_id) : undefined)
          if (!f || f.status !== 'ok') return g
          const label =
            f.action === 'copy' ? '文案补全' : f.action === 'aigc' ? 'AIGC 生成' : '已重排'
          return {
            ...g,
            status: 'ok',
            note: f.note ?? `已采纳 ${label}`,
            matched_material_id: f.new_material_id ?? g.matched_material_id,
          }
        })
        setGaps(merged)

        // 把 store 里的 fills 重新映射到本轮 detect 给出的新 gap_id 上：
        // 否则下次 handleCopyAdopt / runFill 透传时 fillMap 还是用老 gap_id，永远命中不了。
        if (effectiveFills.length > 0) {
          const sectionToNewGapId = new Map<string, string>()
          for (const g of detected) {
            if (g.section_id) sectionToNewGapId.set(g.section_id, g.gap_id)
          }
          const remapped = effectiveFills.map((f) => {
            if (!f.section_id) return f
            const newGapId = sectionToNewGapId.get(f.section_id)
            return newGapId && newGapId !== f.gap_id ? { ...f, gap_id: newGapId } : f
          })
          // 仅当真的有重写时才 setFills，避免不必要的 re-render 触发本组件 effect
          if (remapped.some((f, i) => f.gap_id !== effectiveFills[i].gap_id)) {
            setFills(remapped)
          }
        }

        // brief/goal/settings 回写到后端 Project（首页卡片显示 + 重进项目恢复上下文）；
        // plan 状态由后端 plan/build 内部 mark_planned 自动更新，前端不再手动 upsert。
        void api
          .patch('/project/' + currentProjectId, {
            brief: brief.trim() || null,
            video_goal: videoGoal.trim() || null,
            settings,
          })
          .catch(() => {
            /* 回写失败不阻塞分析主流程 */
          })

        return builtPlan
      } catch (err) {
        setError(err instanceof Error ? err.message : '智能分析失败')
        return null
      } finally {
        setAnalyzing(false)
      }
    },
    [brief, currentProjectId, selectedSampleId, selectedSampleIds, setFills, setGaps, setPlan, settings, sortedMaterials, videoGoal],
  )

  const handleAnalyze = useCallback(() => void runAnalyze(), [runAnalyze])

  /* ----------------------------- 补全动作 ----------------------------- */

  const runFill = useCallback(
    async (gap: Gap, action: FillAction, params: Record<string, unknown> = {}) => {
      markBusy(gap.gap_id, true)
      setError(null)
      try {
        const body: GapFillRequest = { gap_id: gap.gap_id, action, params }
        const result = await api.post<FillResult>('/gap/fill', body)
        upsertFill(result)
        // 自动用最新 fills 重发 plan/build + gap/detect → 刷新右侧 + 底部
        const nextFills = [...fills.filter((f) => f.gap_id !== gap.gap_id), result]
        await runAnalyze(nextFills)
        return result
      } catch (err) {
        setError(err instanceof Error ? err.message : '补全失败')
        return null
      } finally {
        markBusy(gap.gap_id, false)
      }
    },
    [fills, markBusy, runAnalyze, upsertFill],
  )

  const handleRerankApply = useCallback(async () => {
    if (!selectedGap) return
    await runFill(selectedGap, 'rerank')
  }, [runFill, selectedGap])

  const handleCopyAdopt = useCallback(
    async (finalNarration: string) => {
      if (!selectedGap) return
      // 用 prompt_hint 触发后端再写一次，但我们其实只要回写本地——简单走 upsertFill+rebuild
      const baseFill: FillResult = selectedFill ?? {
        gap_id: selectedGap.gap_id,
        action: 'copy',
        alternatives: [],
        video_urls: [],
        chunks_count: 0,
        chunk_task_ids: [],
        status: 'ok',
      }
      const merged: FillResult = {
        ...baseFill,
        action: 'copy',
        narration: finalNarration,
        status: 'ok',
      }
      upsertFill(merged)
      const nextFills = [...fills.filter((f) => f.gap_id !== selectedGap.gap_id), merged]
      await runAnalyze(nextFills)
    },
    [fills, runAnalyze, selectedFill, selectedGap, upsertFill],
  )

  const handleCopyTrigger = useCallback(async () => {
    if (!selectedGap) return
    await runFill(selectedGap, 'copy', { prompt_hint: selectedGap.requirement })
  }, [runFill, selectedGap])

  const pendingGapsCount = useMemo(
    () =>
      gaps.filter(
        (g) => g.status !== 'ok' && !fills.some((f) => f.gap_id === g.gap_id && f.status === 'ok'),
      ).length,
    [gaps, fills],
  )

  const handleBatchDone = useCallback(
    async (resp: GapFillAllResponse) => {
      if (!resp.fills.length) {
        if (resp.stopped_reason) setError(resp.stopped_reason)
        return
      }
      // 把所有批量 fills 合并进 store，然后重跑分析刷新一次
      const merged = [
        ...fills.filter((f) => !resp.fills.some((r) => r.gap_id === f.gap_id)),
        ...resp.fills,
      ]
      resp.fills.forEach(upsertFill)
      await runAnalyze(merged)
      if (resp.failed_gap_id && resp.stopped_reason) {
        setError(`批量生成在 ${resp.failed_gap_id} 停止：${resp.stopped_reason}`)
      }
    },
    [fills, runAnalyze, upsertFill],
  )

  /* --------------------- 四轨：口播 / 包装 / BGM 动作 --------------------- */

  const refetchPlan = useCallback(
    async (planId: string) => {
      try {
        const fresh = await api.get<Plan>(`/plan/${planId}`)
        setPlan(fresh)
      } catch {
        /* 拉新版失败由上层 error 兜底；不阻塞当前动作 */
      }
    },
    [setPlan],
  )

  const handleSynthesizeScene = useCallback(
    async (sceneId: string) => {
      if (!plan) return
      setTrackBusy(true)
      setError(null)
      try {
        await synthesizeOne({ plan_id: plan.plan_id, scene_id: sceneId })
        await refetchPlan(plan.plan_id)
      } catch (err) {
        setError(err instanceof Error ? err.message : `单段口播合成失败：${sceneId}`)
      } finally {
        setTrackBusy(false)
      }
    },
    [plan, refetchPlan],
  )

  const handleSynthesizeAll = useCallback(async () => {
    if (!plan) return
    setTrackBusy(true)
    setError(null)
    try {
      const resp = await synthesizeAll(plan.plan_id)
      await refetchPlan(plan.plan_id)
      if (resp.failures.length > 0) {
        setError(
          `${resp.synthesized.length} 段已合成；${resp.failures.length} 段失败：` +
            resp.failures.map((f) => f.scene_id).join(', '),
        )
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : '一键合成失败')
    } finally {
      setTrackBusy(false)
    }
  }, [plan, refetchPlan])

  const handleClearVoice = useCallback(
    async (sceneId: string) => {
      if (!plan) return
      setTrackBusy(true)
      setError(null)
      try {
        const fresh = await deleteVoice(plan.plan_id, sceneId)
        setPlan(fresh)
      } catch (err) {
        setError(err instanceof Error ? err.message : `清除口播失败：${sceneId}`)
      } finally {
        setTrackBusy(false)
      }
    },
    [plan, setPlan],
  )

  const handleRecommendPackaging = useCallback(async () => {
    if (!plan) return
    setTrackBusy(true)
    setError(null)
    try {
      const body: PackagingRecommendRequest = { plan_id: plan.plan_id, apply: true }
      await api.post('/packaging/recommend', body)
      await refetchPlan(plan.plan_id)
    } catch (err) {
      setError(err instanceof Error ? err.message : '包装推荐失败')
    } finally {
      setTrackBusy(false)
    }
  }, [plan, refetchPlan])

  const handleBgmAnchorChange = useCallback(
    async (newAnchor: number) => {
      if (!plan) return
      setTrackBusy(true)
      setError(null)
      try {
        const fresh = await patchPlanBgm(plan.plan_id, { video_anchor_seconds: newAnchor })
        setPlan(fresh)
      } catch (err) {
        setError(err instanceof Error ? err.message : '更新 BGM 锚点失败')
      } finally {
        setTrackBusy(false)
      }
    },
    [plan, setPlan],
  )

  const handleClearBgm = useCallback(async () => {
    if (!plan) return
    setTrackBusy(true)
    setError(null)
    try {
      const fresh = await deletePlanBgm(plan.plan_id)
      setPlan(fresh)
    } catch (err) {
      setError(err instanceof Error ? err.message : '清除 BGM 失败')
    } finally {
      setTrackBusy(false)
    }
  }, [plan, setPlan])

  const handleBgmVolumeChange = useCallback(
    async (volume: number) => {
      if (!plan) return
      setError(null)
      try {
        const fresh = await patchPlanBgm(plan.plan_id, { volume })
        setPlan(fresh)
      } catch (err) {
        setError(err instanceof Error ? err.message : '更新 BGM 音量失败')
      }
    },
    [plan, setPlan],
  )

  // 口播开关：同时改 plan.settings + session.settings，让本次 plan 立刻生效，
  // 同时下次「重新分析」也保留用户偏好（plan/build 把 sessionStore.settings 当输入）。
  const handleToggleVoiceover = useCallback(
    async (enabled: boolean) => {
      setSettings({ voiceover_enabled: enabled })
      if (!plan) return
      setTrackBusy(true)
      setError(null)
      try {
        const fresh = await patchPlanSettings(plan.plan_id, { voiceover_enabled: enabled })
        setPlan(fresh)
      } catch (err) {
        setError(err instanceof Error ? err.message : '切换口播开关失败')
      } finally {
        setTrackBusy(false)
      }
    },
    [plan, setPlan, setSettings],
  )

  // 音色切换：同步 plan + session，下次合成与重新分析都生效。
  const handleChangeTtsVoice = useCallback(
    async (voice: import('@/types/schemas').TTSVoice) => {
      setSettings({ tts_voice: voice })
      if (!plan) return
      setTrackBusy(true)
      setError(null)
      try {
        const fresh = await patchPlanSettings(plan.plan_id, { tts_voice: voice })
        setPlan(fresh)
      } catch (err) {
        setError(err instanceof Error ? err.message : '切换 TTS 音色失败')
      } finally {
        setTrackBusy(false)
      }
    },
    [plan, setPlan, setSettings],
  )

  /* --------------------- 下一步：补缺口 → 生成包装 → 渲染 --------------------- */

  const handleProceedToRender = useCallback(async () => {
    if (!plan) return
    setError(null)
    try {
      // 阶段 1 · 缺口检查：把还没补上的 gap（status≠ok 且无 ok fill）顺序用文案补全。
      // 顺序而非并发——copy 的 LLM prompt 依赖段落上下文，串行更稳，也避免配额抖动。
      const pending = gaps.filter(
        (g) => g.status !== 'ok' && !fills.some((f) => f.gap_id === g.gap_id && f.status === 'ok'),
      )
      // runAnalyze 内部 plan/build 会签发一个新 plan_id，必须用 rebuilt.plan_id 做后续 packaging，
      // 否则包装会落到上一版 plan 上、新版没 packaging_track，渲染端拿到的就是裸 main 轨。
      let activePlanId = plan.plan_id
      if (pending.length > 0) {
        setFinalizing('filling-gaps')
        const fresh: FillResult[] = []
        for (const gap of pending) {
          const body: GapFillRequest = {
            gap_id: gap.gap_id,
            action: 'copy',
            params: { prompt_hint: gap.requirement },
          }
          const result = await api.post<FillResult>('/gap/fill', body)
          upsertFill(result)
          fresh.push(result)
        }
        const nextFills = [
          ...fills.filter((f) => !fresh.some((r) => r.gap_id === f.gap_id)),
          ...fresh,
        ]
        // 用补全后的 fills 重建 plan（口播写进 scene.narration → 字幕轨能拿到）
        const rebuilt = await runAnalyze(nextFills)
        if (!rebuilt) {
          setFinalizing('idle')
          return
        }
        activePlanId = rebuilt.plan_id
      }

      // 阶段 2 · 包装生成：基于定稿 plan 写转场 + 封面到 packaging_track（apply=true 服务端落盘）
      setFinalizing('packaging')
      const pkgBody: PackagingRecommendRequest = { plan_id: activePlanId, apply: true }
      await api.post('/packaging/recommend', pkgBody)
      // 拉最新 plan，让 packaging_track 进 store，渲染页直接用定稿版本
      try {
        const fresh = await api.get<Plan>(`/plan/${activePlanId}`)
        setPlan(fresh)
      } catch {
        /* 拉新版失败不阻塞跳转，渲染端按 plan_id 仍能取到落盘版本 */
      }

      // 阶段 3 · 进入渲染：先提交 compose 步骤快照，让顶部 nav 标 saved + current_step=render
      try {
        await commitStep(currentProjectId!, 'compose', {
          plan_id: activePlanId,
          fill_ids: fills.map((f) => f.gap_id),
        })
      } catch (err) {
        setError(err instanceof Error ? err.message : '保存 compose 步骤失败')
        setFinalizing('idle')
        return
      }
      setFinalizing('done')
      navigate('/render')
    } catch (err) {
      setError(err instanceof Error ? err.message : '进入渲染前的收尾失败')
      setFinalizing('idle')
    }
  }, [fills, gaps, navigate, plan, runAnalyze, setPlan, upsertFill])

  /* ----------------------------- guard ----------------------------- */

  if (!selectedSampleId) {
    return (
      <PageShell title="新素材 / 缺口补全" subtitle="先去素材库挑一个样例。">
        <div className="rounded-lg border border-dashed border-border bg-card p-8 text-sm text-muted-foreground">
          <Link to="/library" className="text-primary underline-offset-4 hover:underline">
            返回素材库 →
          </Link>
        </div>
      </PageShell>
    )
  }

  /* ------------------------------ 渲染 ------------------------------ */

  return (
    <PageShell
      title="新素材 / 缺口补全"
      subtitle="输入主题（可选上传素材）→ 生成内容轨 → 缺口补全 → 一键生成包装 / 字幕 / BGM。"
    >
      {error && (
        <div className="mb-3 rounded-md border border-destructive/40 bg-destructive/5 px-4 py-2 text-sm text-destructive">
          {error}
        </div>
      )}

      {/* ====== 参考样例 chips（最多 2 个）====== */}
      <div className="mb-3 flex flex-wrap items-center gap-2 text-xs">
        <span className="text-muted-foreground">参考样例：</span>
        {selectedSampleIds.map((sid, i) => (
          <span
            key={sid}
            className="inline-flex items-center gap-1 rounded-full border border-primary/40 bg-primary/5 px-2 py-0.5 text-primary"
          >
            <span className="rounded-sm bg-primary px-1 text-[10px] font-bold text-primary-foreground">
              {String.fromCharCode(65 + i)}
            </span>
            <span className="font-medium">{selectedSampleTitles[i] ?? sid}</span>
          </span>
        ))}
        {selectedSampleIds.length === 2 && (
          <span className="text-[10px] text-muted-foreground">· 两份结构会被合并喂给 LLM 改编</span>
        )}
      </div>

      {/* ============ Row 1：输入（左）+ 上传素材（右）—— 左右排开 ============ */}
      <div className="grid gap-4 xl:grid-cols-2">
        {/* ----- 左 · 主题 / 视频目标 / 设置 ----- */}
        <section className="space-y-3 rounded-lg border border-border bg-card p-4">
          <BriefInput
            value={brief}
            onChange={(v) => {
              setBrief(v)
              if (v.trim().length > 0) setBriefTouched(false)
            }}
            required
            showError={briefTouched}
          />
          <VideoGoalInput value={videoGoal} onChange={setVideoGoal} />
          <ComposeSettingsPanel value={settings} onChange={setSettings} />
        </section>

        {/* ----- 右 · 上传 + 素材库 ----- */}
        <section className="space-y-3 rounded-lg border border-border bg-card p-4">
          <div className="space-y-2">
            <div className="flex items-center justify-between">
              <label className="text-xs font-semibold">
                上传素材 <span className="font-normal text-muted-foreground">（可选）</span>
              </label>
              <span className="text-[10px] text-muted-foreground">
                session <span className="font-mono">{sessionId ?? '尚未分配'}</span>
              </span>
            </div>
            {sortedMaterials.length === 0 && (
              <p className="rounded-md bg-muted/40 px-2 py-1 text-[11px] text-muted-foreground">
                没有素材也能跑：仅凭主题分析 → 缺口全部 miss → 用 文案 / AIGC 逐个补齐。
              </p>
            )}
            <UploadDropzone
              uploading={uploading}
              onPick={() => fileInputRef.current?.click()}
              onDrop={(f) => void handlePickFiles(f)}
            />
            <input
              ref={fileInputRef}
              type="file"
              multiple
              hidden
              accept="video/*,image/*,audio/*"
              onChange={(e) => void handlePickFiles(e.target.files)}
            />
          </div>

          <div className="space-y-2">
            <div className="flex items-center justify-between">
              <label className="text-xs font-semibold">素材库（拖拽可排序）</label>
              <span className="text-[10px] text-muted-foreground">{sortedMaterials.length} 条</span>
            </div>
            <MaterialGrid
              materials={sortedMaterials}
              onReorder={reorderMaterials}
              onRemove={removeMaterial}
            />
          </div>
        </section>
      </div>

      {/* ============ 智能分析按钮（横跨左右两栏）============ */}
      <div className="mt-3">
        <button
          onClick={handleAnalyze}
          disabled={analyzing || brief.trim().length === 0}
          title={brief.trim().length === 0 ? '请先输入主题/卖点' : undefined}
          className={cn(
            'w-full rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground transition-colors',
            (analyzing || brief.trim().length === 0) && 'cursor-not-allowed opacity-60',
          )}
        >
          {analyzing ? '生成内容轨中…' : plan ? '重新生成内容轨' : '生成内容轨'}
        </button>
      </div>

      {/* ============ Row 2：样例轨 + 适配概要 + 补全功能键 + 段落编辑（时间轴之上） ============ */}
      {plan && (
        <section className="mt-4 space-y-3 rounded-lg border border-border bg-card p-4">
          <div className="space-y-2">
            <div className="flex items-center justify-between">
              <h2 className="text-sm font-semibold">样例结构（参考）</h2>
              <span className="text-[10px] text-muted-foreground">{videoType}</span>
            </div>
            <SectionsBar manifest={manifest} />
          </div>

          <div className="flex items-center justify-between gap-2 border-t border-border pt-3">
            <h2 className="text-sm font-semibold">
              适配结构（{plan.adapted_sections.length} 段 / 缺口 {gaps.length}
              {pendingGapsCount > 0 && (
                <span className="ml-2 text-amber-500">待补 {pendingGapsCount}</span>
              )}
              ）
            </h2>
            <div className="flex items-center gap-2">
              {fills.length > 0 && (
                <span className="text-[10px] text-muted-foreground">已采纳 {fills.length}</span>
              )}
              <BatchCopyButton
                planId={plan.plan_id}
                pendingCount={pendingGapsCount}
                onDone={handleBatchDone}
              />
              <BatchAigcButton
                planId={plan.plan_id}
                pendingCount={pendingGapsCount}
                onDone={handleBatchDone}
              />
            </div>
          </div>

          {/* 两栏：左缺口补全 tabs / 右段落内容编辑 */}
          <div className="grid gap-3 lg:grid-cols-[1.2fr_1fr]">
            {/* 左 · 缺口补全（依赖选中 gap） */}
            <div className="space-y-2">
              {selectedGap ? (
                <>
                  <div className="flex flex-wrap items-center gap-1 text-xs">
                    {ACTION_TABS.map((tab) => (
                      <button
                        key={tab.value}
                        onClick={() => setActiveAction(tab.value)}
                        title={tab.hint}
                        className={cn(
                          'rounded-md border px-2 py-1 transition-colors',
                          activeAction === tab.value
                            ? 'border-primary bg-primary/10 text-primary'
                            : 'border-border bg-background hover:bg-secondary',
                        )}
                      >
                        {tab.label}
                      </button>
                    ))}
                  </div>

                  {activeAction === 'rerank' && (
                    <>
                      {!selectedFill && (
                        <button
                          onClick={() => void runFill(selectedGap, 'rerank')}
                          disabled={gapBusy}
                          className={cn(
                            'w-full rounded-md bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground',
                            gapBusy && 'cursor-not-allowed opacity-60',
                          )}
                        >
                          {gapBusy ? '生成候选中…' : '让 LLM 挑一个素材填进来'}
                        </button>
                      )}
                      {selectedFill && selectedFill.action === 'rerank' && (
                        <FillRerankPanel
                          plan={plan}
                          fill={selectedFill}
                          materials={sortedMaterials}
                          onApply={handleRerankApply}
                          loading={gapBusy}
                        />
                      )}
                    </>
                  )}

                  {activeAction === 'copy' && (
                    <>
                      {!selectedFill || selectedFill.action !== 'copy' ? (
                        <button
                          onClick={handleCopyTrigger}
                          disabled={gapBusy}
                          className={cn(
                            'w-full rounded-md bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground',
                            gapBusy && 'cursor-not-allowed opacity-60',
                          )}
                        >
                          {gapBusy ? '生成文案中…' : '让 LLM 写一段口播'}
                        </button>
                      ) : (
                        <FillCopyPanel
                          fill={selectedFill}
                          onAdopt={handleCopyAdopt}
                          loading={gapBusy}
                        />
                      )}
                    </>
                  )}

                  {activeAction === 'aigc' && (
                    <FillAigcPanel
                      key={selectedGap.gap_id}
                      gap={selectedGap}
                      fill={selectedFill?.action === 'aigc' ? selectedFill : null}
                      onResult={(f) => {
                        upsertFill(f)
                        const nextFills = [...fills.filter((x) => x.gap_id !== f.gap_id), f]
                        void runAnalyze(nextFills)
                      }}
                    />
                  )}
                </>
              ) : (
                <p className="rounded-md border border-dashed border-border bg-background/30 px-3 py-2 text-[11px] text-muted-foreground">
                  点下方内容轨任意一段——这里出现 rerank / copy / aigc 的补全面板。
                </p>
              )}
            </div>

            {/* 右 · 段落内容编辑（迁移结构内容 + 口播） */}
            <SceneEditPanel
              key={effectiveSelectedSceneId ?? 'none'}
              plan={plan}
              selectedSceneId={effectiveSelectedSceneId}
              onSaved={setPlan}
              disabled={analyzing || anyGapBusy || trackBusy}
            />
          </div>
        </section>
      )}

      {/* ============ Row 3：四轨工作台（content-only → 缺口补齐 → full） ============ */}
      <section className="mt-4">
        {plan ? (
          <>
            {pendingGapsCount > 0 && (
              <p className="mb-2 rounded-md border border-amber-400/30 bg-amber-500/5 px-3 py-1.5 text-[11px] text-amber-700 dark:text-amber-300">
                内容轨先行：还有 {pendingGapsCount} 段缺口待补。补齐后口播 / 包装 / BGM 三轨会自动展开。
              </p>
            )}
            <FourTrackBoard
              plan={plan}
              gaps={gaps}
              filledGapIds={filledGapIds}
              selectedGapId={selectedGapId}
              onSelectScene={(scene, gap) => {
                setSelectedSceneId(scene.scene_id)
                if (gap) {
                  setSelectedGapId(gap.gap_id)
                  setPreviewGapId(gap.gap_id)
                }
              }}
              onSynthesizeScene={handleSynthesizeScene}
              onSynthesizeAll={handleSynthesizeAll}
              onClearVoice={handleClearVoice}
              onRecommendPackaging={handleRecommendPackaging}
              onPickBgm={() => setBgmPickerOpen(true)}
              onBgmAnchorChange={handleBgmAnchorChange}
              onClearBgm={handleClearBgm}
              onBgmVolumeChange={handleBgmVolumeChange}
              onToggleVoiceover={handleToggleVoiceover}
              onChangeTtsVoice={handleChangeTtsVoice}
              busy={trackBusy}
              phase={pendingGapsCount > 0 ? 'content-only' : 'full'}
            />
          </>
        ) : (
          <div className="rounded-md border border-dashed border-border bg-background/30 p-6 text-center text-xs text-muted-foreground">
            点上方「生成内容轨」开始；plan 构建好后这里会先出现内容轨；补齐缺口后口播 / 包装 / BGM 三轨自动展开。
          </div>
        )}
      </section>

      {/* ============ Row 4：分镜预览 ============ */}
      <section className="mt-4 rounded-lg border border-border bg-card p-4">
        <h2 className="mb-3 text-sm font-semibold">分镜预览</h2>
        {plan ? (
          <StoryboardPreview plan={plan} />
        ) : (
          <div className="rounded-md border border-dashed border-border bg-background/30 p-6 text-center text-xs text-muted-foreground">
            plan 构建好后这里会显示分镜带。
          </div>
        )}
      </section>

      {/* ============ Row 5：自然语言编辑（仅内容轨——Compose 阶段改大思路） ============ */}
      {plan && (
        <section className="mt-4">
          <NLEditPanel
            plan={plan}
            projectStep="compose"
            onApplied={setPlan}
            selectedSceneId={selectedSceneId}
            lockedTracks={['packaging', 'voice']}
            defaultTrack="main"
          />
        </section>
      )}

      {/* 底部 next steps */}
      {plan && (
        <div className="mt-4 flex flex-col gap-2 sm:flex-row sm:items-center">
          <button
            onClick={() => navigate('/migrate')}
            disabled={finalizing !== 'idle' && finalizing !== 'done'}
            className="rounded-md border border-border bg-card px-4 py-2 text-sm font-medium hover:bg-secondary disabled:opacity-60"
          >
            下一步 · 迁移映射 →
          </button>
          <button
            onClick={() => void handleProceedToRender()}
            disabled={
              analyzing || anyGapBusy || finalizing === 'filling-gaps' || finalizing === 'packaging'
            }
            title="先用文案补全所有未补缺口，再生成包装轨（转场 + 封面），最后进入渲染"
            className={cn(
              'rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90',
              (analyzing ||
                anyGapBusy ||
                finalizing === 'filling-gaps' ||
                finalizing === 'packaging') &&
                'cursor-not-allowed opacity-60',
            )}
          >
            {finalizing === 'filling-gaps' && '补全剩余缺口中…'}
            {finalizing === 'packaging' && '生成包装轨中…'}
            {(finalizing === 'idle' || finalizing === 'done') && '下一步 · 补缺口 + 包装 → 渲染'}
          </button>
          {finalizing === 'idle' && (
            <span className="text-[11px] text-muted-foreground sm:ml-2">
              {pendingGapsCount > 0
                ? `还有 ${pendingGapsCount} 个缺口将用文案自动补上，再生成包装`
                : '所有缺口已补，将直接生成包装并进入渲染'}
            </span>
          )}
        </div>
      )}

      {/* 样例截图弹窗 */}
      <GapPreviewDialog
        gap={previewGapId ? (gaps.find((g) => g.gap_id === previewGapId) ?? null) : null}
        onClose={() => setPreviewGapId(null)}
      />

      {/* BGM 选择 / 上传弹窗 */}
      {plan && currentProjectId && (
        <BgmPickerDialog
          open={bgmPickerOpen}
          onClose={() => setBgmPickerOpen(false)}
          projectId={currentProjectId}
          planId={plan.plan_id}
          onPlanUpdated={setPlan}
        />
      )}
    </PageShell>
  )
}

/* ---------- 子组件 ---------- */

function UploadDropzone({
  onPick,
  onDrop,
  uploading,
}: {
  onPick: () => void
  onDrop: (files: FileList) => void
  uploading: boolean
}) {
  const [hover, setHover] = useState(false)
  return (
    <div
      onDragOver={(e) => {
        e.preventDefault()
        setHover(true)
      }}
      onDragLeave={() => setHover(false)}
      onDrop={(e) => {
        e.preventDefault()
        setHover(false)
        onDrop(e.dataTransfer.files)
      }}
      onClick={onPick}
      className={cn(
        'flex h-24 cursor-pointer items-center justify-center rounded-md border-2 border-dashed text-xs transition-colors',
        hover ? 'border-primary bg-primary/5' : 'border-border bg-background/40',
        uploading && 'pointer-events-none opacity-60',
      )}
    >
      <span className="text-muted-foreground">
        {uploading ? '上传中…' : '点击或拖拽 video / image / audio（≤ 50MB / file）'}
      </span>
    </div>
  )
}

function SectionsBar({ manifest }: { manifest: SampleManifest | null }) {
  // 没拆解过就用 4 元骨架占位；拆过就按真实 section 时长比例画。
  if (!manifest || manifest.sections.length === 0) {
    const fallback: Array<{ role: 'opening' | 'development' | 'climax' | 'closing'; theme: string }> = [
      { role: 'opening', theme: '开场' },
      { role: 'development', theme: '发展' },
      { role: 'climax', theme: '高潮' },
      { role: 'closing', theme: '收尾' },
    ]
    return (
      <div className="flex h-8 overflow-hidden rounded-md border border-border">
        {fallback.map((f) => (
          <div
            key={f.role}
            className={cn(
              'flex flex-1 items-center justify-center text-[11px] font-medium text-white',
              SECTION_BG[f.role],
            )}
          >
            {SECTION_SHORT[f.role]}
          </div>
        ))}
      </div>
    )
  }

  const total = manifest.duration_seconds || 1
  return (
    <div className="flex h-8 overflow-hidden rounded-md border border-border">
      {manifest.sections.map((sec, i) => {
        const widthPct = ((sec.end - sec.start) / total) * 100
        return (
          <div
            key={i}
            className={cn(
              'flex items-center justify-center px-1 text-[11px] font-medium text-white',
              SECTION_BG[sec.role],
            )}
            style={{ width: `${widthPct}%` }}
            title={`${SECTION_SHORT[sec.role]} · ${sec.theme}`}
          >
            <span className="truncate">{sec.theme || SECTION_SHORT[sec.role]}</span>
          </div>
        )
      })}
    </div>
  )
}
