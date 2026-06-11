import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'

import { api } from '@/api/client'
import { deletePlanBgm, patchPlanBgm } from '@/api/bgm'
import { patchPlanSettings } from '@/api/plan'
import { createSSE } from '@/api/sse'
import { commitStep, getStepSnapshot } from '@/api/steps'
import { deleteVoice, regenerateNarrations, synthesizeAll, synthesizeOne } from '@/api/voice'
import { BgmPickerDialog } from '@/components/compose/BgmPickerDialog'
import { BriefInput } from '@/components/compose/BriefInput'
import { ClarifyPanel } from '@/components/compose/ClarifyPanel'
import { ComposeCommandBar } from '@/components/compose/ComposeCommandBar'
import { ComposeSettingsPanel } from '@/components/compose/ComposeSettingsPanel'
import { DraggableCommandFab } from '@/components/compose/DraggableCommandFab'
import { EmotionCurveCard } from '@/components/compose/EmotionCurveCard'
import { FillAigcPanel } from '@/components/compose/FillAigcPanel'
import { FillCopyPanel } from '@/components/compose/FillCopyPanel'
import { FillRerankPanel } from '@/components/compose/FillRerankPanel'
import { FourTrackBoard } from '@/components/compose/FourTrackBoard'
import { MaterialGrid } from '@/components/compose/MaterialGrid'
import { PackagingItemEditDialog } from '@/components/compose/PackagingItemEditDialog'
import { ReferencePicker } from '@/components/compose/ReferencePicker'
import { RerankStrategyPicker } from '@/components/compose/RerankStrategyPicker'
import { SceneEditPanel } from '@/components/compose/SceneEditPanel'
import { SectionEditDialog } from '@/components/compose/SectionEditDialog'
import { ShotEditDialog } from '@/components/compose/ShotEditDialog'
import { StructureMapPanel } from '@/components/compose/StructureMapPanel'
import { SubtitleEditPopover } from '@/components/compose/SubtitleEditPopover'
import { SystemLibraryPicker } from '@/components/compose/SystemLibraryPicker'
import { TransitionStylePicker } from '@/components/compose/TransitionStylePicker'
import { VersionMenu } from '@/components/compose/VersionMenu'
import { PageShell } from '@/components/layout/PageShell'
import { PlanPlayer, type PlanPlayerHandle } from '@/components/preview/PlanPlayer'
import { cn } from '@/lib/utils'
import { useEditStore } from '@/stores/edit'
import { usePlanStore } from '@/stores/plan'
import { useProjectsStore } from '@/stores/projects'
import { useSessionStore } from '@/stores/session'
import type {
  AdaptedSection,
  FillAction,
  FillResult,
  Gap,
  GapDetectRequest,
  GapFillRequest,
  Material,
  MaterialUploadResponse,
  PackagingItem,
  Plan,
  PlanBuildRequest,
  RenderDonePayload,
  RenderSubmitResponse,
  SampleManifest,
  Scene,
  TransitionStyle,
} from '@/types/schemas'

const ACTION_TABS: { value: FillAction; label: string; hint: string }[] = [
  { value: 'rerank', label: '挑素材', hint: '从已上传素材里挑一个最匹配的填进本段画面' },
  { value: 'copy', label: '字卡画面', hint: 'AI 设计一张个性化字卡（字体/版式/颜色/动画）作为本段画面' },
  { value: 'aigc', label: 'AI 视频', hint: 'AI 视频生成，出 5-8 秒短片作为本段画面' },
  { value: 'aigc_image', label: 'AI 生图再渲染', hint: 'AI 生图后用动画引擎重渲染，成本/等待远低于视频；多主体自动拆分成多镜头故事板' },
]

const RENDER_STEP_LABELS: Record<string, string> = {
  prepare: '准备',
  ffmpeg_concat: '主轨拼接',
  seedance_extend: '主轨直通',
  remotion_render: '包装渲染',
  ffmpeg_overlay: '叠加输出',
  finalize: '收尾',
}
const RENDER_STEP_ORDER = [
  'prepare',
  'ffmpeg_concat',
  'seedance_extend',
  'remotion_render',
  'ffmpeg_overlay',
  'finalize',
] as const

export default function ComposePage() {
  const [searchParams, setSearchParams] = useSearchParams()
  const navigate = useNavigate()

  // session store
  const selectedReferences = useSessionStore((s) => s.selectedReferences)
  const videoType = useSessionStore((s) => s.videoType)
  const sessionId = useSessionStore((s) => s.sessionId)
  const manifest = useSessionStore((s) => s.manifest)
  const materials = useSessionStore((s) => s.materials)
  const brief = useSessionStore((s) => s.brief)
  const setBrief = useSessionStore((s) => s.setBrief)
  const settings = useSessionStore((s) => s.settings)
  const setSettings = useSessionStore((s) => s.setSettings)
  const setSession = useSessionStore((s) => s.setSession)
  const appendMaterials = useSessionStore((s) => s.appendMaterials)
  const setMaterials = useSessionStore((s) => s.setMaterials)
  const removeMaterial = useSessionStore((s) => s.removeMaterial)
  const reorderMaterials = useSessionStore((s) => s.reorderMaterials)

  // stage-15:Compose 不再读 selectedSampleIds。结构参考改用 selectedReferences
  // (1-2 个 (sample_id, slot_id))由顶部 ReferencePicker 写入。
  // A 位 = selectedReferences[0] 的 sample_id;manifest 取自 session(Decompose 页保留)或为 null
  const primaryReference = selectedReferences[0] ?? null
  const selectedSampleId = primaryReference?.sample_id ?? null

  // projects store（仅读 currentProjectId；后端 mark_planned/mark_rendered 已自动写回，前端无需 upsert）
  const currentProjectId = useProjectsStore((s) => s.currentProjectId)
  const refreshProjects = useProjectsStore((s) => s.refresh)

  // plan store
  const plan = usePlanStore((s) => s.plan)
  const gaps = usePlanStore((s) => s.gaps)
  const fills = usePlanStore((s) => s.fills)
  const selectedGapId = usePlanStore((s) => s.selectedGapId)
  const selectedSectionId = usePlanStore((s) => s.selectedSectionId)
  const setPlan = usePlanStore((s) => s.setPlan)
  const setGaps = usePlanStore((s) => s.setGaps)
  const setFills = usePlanStore((s) => s.setFills)
  const upsertFill = usePlanStore((s) => s.upsertFill)
  const setSelectedGapId = usePlanStore((s) => s.setSelectedGapId)
  const setSelectedSectionId = usePlanStore((s) => s.setSelectedSectionId)
  const variant = usePlanStore((s) => s.variant)
  // setVariant 之前用于 A/B 切换；现已统一用 VersionMenu 管理版本，不再需要写 variant。

  // edit store（撤销栈）—— 渲染流水线并入本页后，自然语言三轨编辑也搬过来
  const editHistory = useEditStore((s) => s.history)
  const editCursor = useEditStore((s) => s.cursor)
  const pushEdit = useEditStore((s) => s.push)
  const undoEdit = useEditStore((s) => s.undo)
  const redoEdit = useEditStore((s) => s.redo)

  // 命名快照已统一到顶部 VersionMenu 组件——它内部独立维护列表/保存/还原/删除。
  // 这里不再保留 snapshots 状态；undo/redo 由 useEditStore 单独管理。

  // UI state
  const [uploading, setUploading] = useState(false)
  const [analyzing, setAnalyzing] = useState(false)
  // A 位（refs[0]）primary manifest fallback：sessionStore.manifest 只在 Decompose 页才会 set。
  // 用户从 ReferencePicker 直接进 Compose 时 manifest=null，导致 StructureCompareSection 看不见。
  // 这里按 selectedReferences[0] 反查 /sample/{id}/manifest，作为 sessionStore.manifest 的兜底。
  const [primaryManifestFallback, setPrimaryManifestFallback] = useState<SampleManifest | null>(null)
  useEffect(() => {
    if (manifest) {
      // Decompose 页已经写入 sessionStore.manifest，不必重复 fetch
      setPrimaryManifestFallback(null)
      return
    }
    if (!primaryReference) {
      setPrimaryManifestFallback(null)
      return
    }
    let cancelled = false
    void (async () => {
      try {
        const mf = await api.get<SampleManifest>(
          `/sample/${primaryReference.sample_id}/manifest?slot=${primaryReference.slot_id}`,
        )
        if (!cancelled) setPrimaryManifestFallback(mf)
      } catch {
        if (!cancelled) setPrimaryManifestFallback(null)
      }
    })()
    return () => {
      cancelled = true
    }
  }, [manifest, primaryReference])
  const effectiveManifest: SampleManifest | null = manifest ?? primaryManifestFallback
  // B 位（refs[1]）样例 manifest:用于在内容轨上方平行显示第二条参考样例。
  // session store 的 manifest 字段只缓存 A 位;B 位独立请求 + 跟随 selectedReferences[1] 变化。
  const [secondaryManifest, setSecondaryManifest] = useState<SampleManifest | null>(null)
  const secondaryRef = selectedReferences[1] ?? null
  useEffect(() => {
    if (!secondaryRef) {
      setSecondaryManifest(null)
      return
    }
    let cancelled = false
    void (async () => {
      try {
        const mf = await api.get<SampleManifest>(
          `/sample/${secondaryRef.sample_id}/manifest?slot=${secondaryRef.slot_id}`,
        )
        if (!cancelled) setSecondaryManifest(mf)
      } catch {
        if (!cancelled) setSecondaryManifest(null)
      }
    })()
    return () => {
      cancelled = true
    }
  }, [secondaryRef])
  // stage-39：activeAction 改为按 section 隔离。原先单一 page-level state 会让用户
  // 在 A 段切到「字卡画面」、再点 B 段后 B 段顶部 tab 显示成「字卡画面」——用户感知
  // 上 N 段共享同一个工作台。改成 Map<section_id, FillAction>：每段记忆自己的 tab。
  const [actionBySection, setActionBySection] = useState<ReadonlyMap<string, FillAction>>(
    () => new Map(),
  )
  const activeAction: FillAction =
    (selectedSectionId && actionBySection.get(selectedSectionId)) || 'rerank'
  const setActiveAction = useCallback(
    (next: FillAction) => {
      if (!selectedSectionId) return
      setActionBySection((prev) => {
        if (prev.get(selectedSectionId) === next) return prev
        const map = new Map(prev)
        map.set(selectedSectionId, next)
        return map
      })
    },
    [selectedSectionId],
  )
  // 每条 (section_id, action) 独立工作台：切段或切动作时，旧 panel 只 display:none，
  // 本地 useState（form/思考态/seedance polling）不丢；切回时还在原现场。
  //
  // 关键：键用 section_id 而非 gap_id——gap_id 每次 silent rebuild 都会被后端
  // 加 plan_suffix 重写，旧 key 命中不到新 gaps 列表会导致 panel 卸载 → 进行中
  // 的 AI 出图状态全废。section_id 跨 rebuild 稳定（`sec-${order}` 不变）。
  //
  // rerank 不进 keepalive——它没有用户输入，重渲染廉价。
  const [visitedFillKeys, setVisitedFillKeys] = useState<ReadonlySet<string>>(() => new Set())
  // 每条 section 独立的 busy 锁：切到别的段不会还显示上一段的 loading 态。
  // 选用 Set<section_id> 而非 gap_id（gap_id 不稳）；用户在 A 段等 AI 出图（3 min+）
  // 期间应能切到 B 段并行做字卡/AI 出图，A 段完成后自动应用到内容轨。
  const [busySectionIds, setBusySectionIds] = useState<ReadonlySet<string>>(() => new Set())
  // stage-38：把每个 section_id 上次见过的 gap snapshot 缓存到 ref——silent rebuild
  // 期间 gaps 可能临时找不到目标 section（plan 已 setPlan 但 gap/detect 未回），
  // 这时直接用上次的 snapshot 兜底，避免 keepalive `<div key>` 因 find→null→return null
  // 导致 FillAigcPanel 整段卸载（→ Seedance polling/spec 状态全废）。
  const lastSeenGapBySectionRef = useRef<Map<string, Gap>>(new Map())
  // stage-37：原有 visitedSceneIds 多实例 SceneEditPanel keepalive 池已删除——
  // 段 / 单镜编辑迁到弹窗（SectionEditDialog / ShotEditDialog），不再需要保留草稿现场。
  const markBusy = useCallback((sectionId: string, busy: boolean) => {
    if (!sectionId) return
    setBusySectionIds((prev) => {
      const has = prev.has(sectionId)
      if (busy && has) return prev
      if (!busy && !has) return prev
      const next = new Set(prev)
      if (busy) next.add(sectionId)
      else next.delete(sectionId)
      return next
    })
  }, [])
  const [error, setError] = useState<string | null>(null)
  // ?tab=migrate (老链接) → 进 step 2 自动弹出结构对比放大模态；常驻 240px section 始终渲染
  const [structureZoomOpen, setStructureZoomOpen] = useState(() => searchParams.get('tab') === 'migrate')
  const [briefTouched, setBriefTouched] = useState(false)
  /** PR-F：强制至少一轮意图澄清。
   *  ClarifyPanel.onAdopt 被触发（无论 N 轮追问还是「跳过追问 1 键定稿」走的也是 handleAdopt）
   *  就置 true，「生成内容轨」按钮才解禁。state 仅活在当前会话内，刷页面会重置——
   *  这是有意的：换个 brief 重新跑应该重新走一次澄清。 */
  const [clarifiedOnce, setClarifiedOnce] = useState(false)
  // 「下一步」三阶段：补缺口 → 生成包装 → 跳渲染
  const [finalizing, setFinalizing] = useState<
    'idle' | 'filling-gaps' | 'done'
  >('idle')
  // 四轨板上的轨道动作 busy 锁（区别于 filling，避免与补全面板状态混淆）
  const [trackBusy, setTrackBusy] = useState(false)
  const [bgmPickerOpen, setBgmPickerOpen] = useState(false)
  // step1 素材库标题栏「+ 从素材库选取」按钮的开关——打开 SystemLibraryPicker 克隆共享素材到本项目
  const [systemLibraryOpen, setSystemLibraryOpen] = useState(false)
  const [editingSubtitleScene, setEditingSubtitleScene] = useState<Scene | null>(null)
  // step3 包装项：点击弹窗改文案/类型/归属段；时间轴上 left/right/move 拖动统一走 update_packaging_item_time 落盘；跨轴拖动不存在（packaging ↔ bgm 无语义）。转场节点同样点击改样式。
  const [editingPackagingItem, setEditingPackagingItem] = useState<PackagingItem | null>(null)
  const [editingTransition, setEditingTransition] = useState<{
    sceneId: string
    currentStyle: TransitionStyle | null
  } | null>(null)
  // stage-37：段块编辑 / 单镜编辑两套弹窗——替换 step2 keepalive SceneEditPanel
  const [editingSection, setEditingSection] = useState<{
    section: AdaptedSection
    firstScene: Scene
  } | null>(null)
  const [editingShot, setEditingShot] = useState<{
    scene: Scene
    section: AdaptedSection
  } | null>(null)
  // ⌘K 自然语言编辑（R6）：唤起 ComposeCommandBar，作用域由 activeStep 决定
  const [commandBarOpen, setCommandBarOpen] = useState(false)
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
        e.preventDefault()
        setCommandBarOpen((prev) => !prev)
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])
  // 四轨当前选中（内容/字幕/口播 共用 scene_id；包装走 PackagingItem.item_id）——驱动 SceneEditPanel。
  // 内容/字幕/口播 与 包装 互斥：选其一时另一个置 null，避免编辑面板上下文混淆。
  const [selectedSceneId, setSelectedSceneId] = useState<string | null>(null)
  const [selectedPackagingItemId, setSelectedPackagingItemId] = useState<string | null>(null)
  // 内容轨「确认」gate：未确认时不出 Player、不展开其它三轨。
  // 用户流：plan 出来 → 手动 / NL 编辑 → 补齐所有缺口 → 点「确认内容轨」 →
  //   这一刻才解锁 Remotion Player 实时预览 + 口播 / 包装 / BGM 三轨。
  // 切到新 plan_id 时自动重置，避免老确认状态串到新计划。
  const [contentConfirmed, setContentConfirmed] = useState(false)
  const lastConfirmedPlanIdRef = useRef<string | null>(null)
  useEffect(() => {
    if (!plan) {
      if (lastConfirmedPlanIdRef.current !== null) {
        setContentConfirmed(false)
        lastConfirmedPlanIdRef.current = null
      }
      return
    }
    if (plan.plan_id !== lastConfirmedPlanIdRef.current) {
      setContentConfirmed(false)
      lastConfirmedPlanIdRef.current = plan.plan_id
    }
  }, [plan])

  // 步骤 3 解锁 gate：plan 生成完了只解锁 step 2；用户在 step 2 点「进入第 3 步」才置 true。
  // plan_id 变更复位规则：
  //   - plan 整体被清掉（用户回 step1 重选样例 / 重置）→ 复位为锁定
  //   - plan_id 变了但用户当前正在 step3（批量补缺、step3 中上传素材等触发 /plan/build 重生）
  //     → 不复位，避免无关的 plan rebuild 把用户从 step3 弹回 step2（这是 PR-K 修复点）。
  //   - 其他情况（如 step1/2 中的 plan rebuild）→ 复位为锁定，老 step3 解锁状态不要串到新 plan。
  const [step3Unlocked, setStep3Unlocked] = useState(false)

  const lastStep3PlanIdRef = useRef<string | null>(null)
  // 通过 ref 读取当前 activeStep，避免把 activeStep 加入 useEffect 依赖
  // 而引发 plan_id 没变也跑这段重置逻辑的副作用
  const activeStepRef = useRef<1 | 2 | 3>(1)
  useEffect(() => {
    if (!plan) {
      if (lastStep3PlanIdRef.current !== null) {
        setStep3Unlocked(false)
        lastStep3PlanIdRef.current = null
      }
      return
    }
    if (plan.plan_id !== lastStep3PlanIdRef.current) {
      // 用户当前正在 step3 → 这次 plan rebuild 是 step3 内的增量行为（一键补缺 / 上传素材 /
      // 单段重生 narration 等），保持解锁；只更新 plan_id 跟踪，不复位 step3Unlocked。
      if (activeStepRef.current === 3 && step3Unlocked) {
        lastStep3PlanIdRef.current = plan.plan_id
      } else {
        setStep3Unlocked(false)
        lastStep3PlanIdRef.current = plan.plan_id
      }
    }
  }, [plan, step3Unlocked])

  // step 1 生成完成后的「✓ 内容轨已生成」全屏确认弹窗：
  // analyzing=true 期间显示 spinner；analyzing 结束 + planJustGenerated=true 显示预览 + 双按钮。
  // 用户点「进入第 2 步」/「重新澄清」其一才关闭并继续后续动作。
  const [planJustGenerated, setPlanJustGenerated] = useState(false)

  // 三步工作流（视频工坊拆分）：
  //   1 = 选参考样例 + 主题 + 设置
  //   2 = 内容轨生成与修改（随时上传素材重排结构）
  //   3 = 多轨（口播 / 包装 / BGM / 渲染）
  // 用 ?step=N URL 参数持久化；contentConfirmed 在步骤 3 自动视为 true。
  type WorkshopStep = 1 | 2 | 3
  const stepFromUrl = ((): WorkshopStep => {
    const v = searchParams.get('step')
    if (v === '2') return 2
    if (v === '3') return 3
    return 1
  })()
  const [activeStep, setActiveStepState] = useState<WorkshopStep>(stepFromUrl)
  // 持续把 activeStep 同步到 ref，给上面 plan_id 复位逻辑读取（不依赖 useEffect 依赖列表）
  useEffect(() => {
    activeStepRef.current = activeStep
  }, [activeStep])
  const setActiveStep = useCallback(
    (next: WorkshopStep) => {
      setActiveStepState(next)
      setSearchParams(
        (prev) => {
          const sp = new URLSearchParams(prev)
          sp.set('step', String(next))
          return sp
        },
        { replace: true },
      )
      if (next === 3) setContentConfirmed(true)
    },
    [setSearchParams],
  )
  // 进步骤 3 默认认为内容轨已确认（解锁 Player + 多轨完整体）
  useEffect(() => {
    if (activeStep === 3 && plan && !contentConfirmed) setContentConfirmed(true)
  }, [activeStep, plan, contentConfirmed])

  // URL 持久化的 ?step=2/3 在 plan/解锁 gate 不满足时必须降级，避免老 URL 串到新会话：
  //   - 选参考前 selectedSampleId=null（顶层 guard 拦截，渲染 ReferencePicker）；
  //     一旦点第一个参考解除 guard，若 URL 残留 step=3 会直接显示第 3 步——这就是 bug 现象。
  //   - 没 plan → 强制回 step 1；有 plan 但 step3Unlocked=false → step 3 降到 step 2。
  useEffect(() => {
    if (!plan && activeStep !== 1) {
      setActiveStep(1)
      return
    }
    if (activeStep === 3 && !step3Unlocked) {
      setActiveStep(2)
      return
    }
    // stage-26 PR-N.6：已经进入 step3 但批量补字卡或换源把内容轨打回了 needs_fill /
    // text-card-fill-empty 兜底——把用户拉回 step2 补齐再放行。
    // stage-61：user_edited=true 的 Scene 视作『已补齐』，无视 needs_fill / fill-empty 占位。
    const unfilled = plan
      ? plan.main_track.filter(
          (sc) =>
            sc.user_edited !== true &&
            (sc.needs_fill === true ||
              (sc.source_ref ?? '').startsWith('text-card-fill-empty')),
        ).length
      : 0
    if (activeStep === 3 && unfilled > 0) {
      setActiveStep(2)
    }
  }, [plan, activeStep, step3Unlocked, setActiveStep])

  /* --------------------- 渲染流水线（内联 · 无独立页面）--------------------- */
  // 设计：用户点「生成视频」之后，先补缺口 + 生成包装 + commit compose，再自动 POST /render/submit
  // 并接 SSE 流；进度状态只显示极简一行，结果视频直接落在本页底部。
  const [jobId, setJobId] = useState<string | null>(null)
  const [renderStep, setRenderStep] = useState<string>('idle')
  const [renderPercent, setRenderPercent] = useState(0)
  const [renderDone, setRenderDone] = useState<RenderDonePayload | null>(null)
  const [renderError, setRenderError] = useState<string | null>(null)
  const sseRef = useRef<ReturnType<typeof createSSE> | null>(null)
  useEffect(() => () => sseRef.current?.close(), [])

  // 实时预览：Remotion Player 与 FourTrackBoard 共享一条播放头
  const playerRef = useRef<PlanPlayerHandle>(null)
  const [playheadSeconds, setPlayheadSeconds] = useState(0)
  const seekPlayer = useCallback((seconds: number) => {
    playerRef.current?.seek(seconds)
  }, [])

  // 跨 plan 切换时重置撤销栈；同 plan_id 的 in-place 改动由各 handler 显式 pushEdit
  const lastPushedPlanIdRef = useRef<string | null>(null)
  useEffect(() => {
    if (!plan) return
    if (plan.plan_id === lastPushedPlanIdRef.current) return
    useEditStore.getState().reset()
    pushEdit(plan)
    lastPushedPlanIdRef.current = plan.plan_id
  }, [plan, pushEdit])

  // 包了一层的 setPlan：同时进撤销栈。用于 in-place 改动（synthesize / packaging / bgm / NLEdit / settings）
  const setPlanAndPush = useCallback(
    (next: Plan) => {
      setPlan(next)
      pushEdit(next)
    },
    [pushEdit, setPlan],
  )

  // 切版本（VersionMenu 还原快照）专用：重置撤销栈再以新 plan 为基线 push 一帧。
  // 否则切完版本按一下 ⌘Z 会"撤回切版本"，把用户拉回上一帧，UX 反直觉。
  const setPlanAfterRestore = useCallback(
    (next: Plan) => {
      setPlan(next)
      useEditStore.getState().reset()
      pushEdit(next)
      lastPushedPlanIdRef.current = next.plan_id
    },
    [pushEdit, setPlan],
  )

  const sortedMaterials = useMemo(
    () => materials.slice().sort((a, b) => a.sort_order - b.sort_order),
    [materials],
  )

  // 把素材库里 VLM 识别出的对象聚合成「已知主体」清单，传给 ClarifyPanel：
  // 用户上传素材后立刻能拉动 outline.content（典型：上传纸巾 → 澄清 LLM 必须在 content
  // 里点名「纸巾」）。dedupe + 长度限制 + 黑名单兜底，避免把噪声标签塞给 LLM。
  //
  // 优先级：material.subjects（VLM 单独要求的「具象名词」——青铜鼎/红色保温杯）
  // 用尽再 fallback 到 material.tags（旧素材没 subjects 字段，且 tags 里也藏着一些泛词）。
  const detectedSubjects = useMemo(() => {
    // VLM 失败兜底标 / 抽象类别词：扔进去除帮倒忙没别的；过滤掉。
    // 注意：subjects 字段在后端已经过黑名单清洗，这里二次过滤兜住 tags fallback 路径。
    const stop = new Set([
      '人', '物体', '场景', '画面', '视频', '图片', '素材', '内容', '主体',
      '构图', '光线', '风格', '氛围', '色彩', '色调', '镜头', '特写', '广角',
      '白色', '黑色', '红色', '蓝色', '绿色', '黄色',
      // 类别词（与后端 _CATEGORY_BLACKLIST 同步）：宁可空也别让「文物/杯子/狗」污染 outline。
      '文物', '杯子', '狗', '猫', '城市', '笔记本', '电脑', '古建筑', '食物',
      '动物', '植物', '建筑', '人物', '风景', '饮料', '家具',
    ])
    const seen = new Set<string>()
    const out: string[] = []
    const push = (raw: string): boolean => {
      const t = raw.trim()
      if (!t) return false
      if (t.startsWith('[auto]')) return false
      if (t.length < 2 || t.length > 12) return false
      if (stop.has(t)) return false
      if (seen.has(t)) return false
      seen.add(t)
      out.push(t)
      return out.length >= 20
    }
    // 先全 subjects 走一遍——质量最高
    for (const m of sortedMaterials) {
      for (const s of m.subjects ?? []) {
        if (push(s)) return out
      }
    }
    // 再 tags 兜底——旧素材 / VLM 未给 subjects 时仍能跑出一些粗略对象
    for (const m of sortedMaterials) {
      for (const tag of m.tags ?? []) {
        if (push(tag)) return out
      }
    }
    return out
  }, [sortedMaterials])

  // stage-15: 拦截 / 检测样例是否已拆解的逻辑下沉到 ReferencePicker
  // (它只列已落版本槽的 sample × slot,空仓库时会引导用户去 Decompose)

  // 选中 gap → 拿对应的 fill（如果已经做过）
  // stage-36：selectedSectionId 是稳定主键（section_id 跨 silent rebuild 不变）。
  // selectedGap 由 section_id 反查；老 gap_id 作为兜底（dialog / 老组件可能仍按 gap_id 传）。
  const selectedGap = useMemo(
    () => {
      if (selectedSectionId) {
        const bySec = gaps.find((g) => g.section_id === selectedSectionId)
        if (bySec) return bySec
      }
      return gaps.find((g) => g.gap_id === selectedGapId) ?? null
    },
    [gaps, selectedSectionId, selectedGapId],
  )
  // selectedFill 也按 section_id 优先：silent rebuild 后端会重写 gap_id，
  // 但 fill 对象保留的是 fill 时的旧 gap_id；通过 section_id 反查才能稳定锚定。
  const selectedFill = useMemo(
    () => {
      const secId = selectedGap?.section_id ?? selectedSectionId
      if (secId) {
        const bySec = fills.find((f) => f.section_id === secId)
        if (bySec) return bySec
      }
      return fills.find((f) => f.gap_id === selectedGapId) ?? null
    },
    [fills, selectedGap, selectedSectionId, selectedGapId],
  )
  const filledGapIds = useMemo(() => new Set(fills.map((f) => f.gap_id)), [fills])
  // 当前选中那条 gap 的 busy 状态——按 section_id 查（gap_id 在 silent rebuild 后会变）。
  // 仅用于左侧补全面板的 disabled / loading 标记。
  const gapBusy = selectedGap?.section_id ? busySectionIds.has(selectedGap.section_id) : false
  const anyGapBusy = busySectionIds.size > 0

  // selectedSectionId 变化或 gaps 重建后，把 selectedGapId 同步到当前 section 对应的真 gap_id——
  // FourTrackBoard / dialog 等下游仍按 gap_id 标记高亮。
  useEffect(() => {
    const target = selectedGap?.gap_id ?? null
    if (target !== selectedGapId) {
      setSelectedGapId(target)
    }
  }, [selectedGap, selectedGapId, setSelectedGapId])

  // 用户切到任何 section × (copy|aigc|aigc_image) → 推进 visitedFillKeys，加入 keepalive 池。
  // 用 selectedGap 直接 derive section_id（在它定义后才能跑，所以 useEffect 放这里）。
  useEffect(() => {
    if (!selectedGap?.section_id) return
    if (activeAction === 'rerank') return
    const key = `${selectedGap.section_id}::${activeAction}`
    setVisitedFillKeys((prev) => {
      if (prev.has(key)) return prev
      const next = new Set(prev)
      next.add(key)
      return next
    })
  }, [selectedGap?.section_id, activeAction])

  // stage-38：每次 gaps 列表更新，把当前每段最新的 Gap 快照刷进 ref。后续 keepalive 渲染
  // 时若 `gaps.find(section_id)` 临时未命中（plan 已 setPlan / gap/detect 未回），就回落
  // 到此 ref 里上次见过的快照——避免 panel 因 find→null→return null 整段卸载。
  useEffect(() => {
    for (const g of gaps) {
      if (g.section_id) lastSeenGapBySectionRef.current.set(g.section_id, g)
    }
  }, [gaps])

  // gap 列表换了之后，自动选第一个 miss/warn——但**只看 selectedSectionId 是否仍在 gaps 里**。
  // 不能看 selectedGapId 是否存在，因为后端每次 detect 都会重写 gap_id，
  // 那样会触发不必要的"重选第一段"，把用户当前选中的 section 跳走。
  //
  // stage-40：增加一条规则——若 selectedSectionId 指向 plan.adapted_sections 里**实际存在**
  // 但当前没 gap 的段（已填好），同样保留，不跳走。这样用户点已填好的「字卡画面」/「素材填好的段」
  // 工作台仍停在他点的段，不会被自动 fallback 推到下一个未填段。
  useEffect(() => {
    if (gaps.length === 0) {
      // 没 gap：plan 还没好；保留当前选段（plan adapted_sections 校验在下面 effect 里），
      // 不再清成 null——避免用户在 step3 浏览已完工 plan 时被清掉选段。
      return
    }
    if (
      selectedSectionId &&
      gaps.some((g) => g.section_id === selectedSectionId)
    ) {
      return
    }
    // selectedSectionId 已指向 plan 里实际存在的段（即使没 gap，例如已填好）——保留
    if (
      selectedSectionId &&
      plan?.adapted_sections.some((s) => s.section_id === selectedSectionId)
    ) {
      return
    }
    const first = gaps.find((g) => g.status !== 'ok') ?? gaps[0]
    setSelectedSectionId(first.section_id ?? null)
  }, [gaps, plan, selectedSectionId, setSelectedSectionId])

  // 内容轨选段：用户没点过、或选的段在新 plan 里已不存在 → 回落到 sc-0；
  // 用 derive-during-render 避免 setState-in-effect 级联渲染。
  const effectiveSelectedSceneId: string | null = (() => {
    if (!plan || plan.main_track.length === 0) return null
    if (selectedSceneId && plan.main_track.some((s) => s.scene_id === selectedSceneId)) {
      return selectedSceneId
    }
    return plan.main_track[0].scene_id
  })()

  // 包装段选中：包装项被点中时取出当前 item，让右侧编辑面板让位给 PackagingItemEditDialog。
  const selectedPackagingItem = useMemo(
    () =>
      selectedPackagingItemId
        ? (plan?.packaging_track.find((it) => it.item_id === selectedPackagingItemId) ?? null)
        : null,
    [plan, selectedPackagingItemId],
  )

  // stage-37：原有 visitedSceneIds 推进 useEffect 已删除（弹窗模型不再需要保留草稿现场）。

  // 渲染流水线衍生态：jobId 存在 + 未 done + 无错误 = 进行中
  const isRendering = jobId !== null && !renderDone && !renderError
  const canUndo = editCursor > 0
  const canRedo = editCursor >= 0 && editCursor < editHistory.length - 1

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

        // Bug 修复：之前刷新页面 / 切项目回 Compose 时，fills 始终为 []。
        // 这导致：① 一键补全的 skipGapIds 永远是空 → 后端把所有 gap 重做一遍（覆盖已有字卡 / AIGC）；
        // ② pendingGapsCount 把已经填好的段也算成"待办"。
        // 解决：从 plan.main_track 反推已采纳的 fill——
        //   text_card 段 ↔ copy fill；aigc_t2v 段 ↔ aigc fill。
        // 拿 scene.scene_id (`sc-{order}`) → AdaptedSection.section_id → 在 freshGaps 里找 gap_id。
        const sectionByOrder = new Map<number, string>()
        for (const sec of freshPlan.adapted_sections ?? []) {
          sectionByOrder.set(sec.order, sec.section_id)
        }
        const gapBySection = new Map<string, Gap>()
        for (const g of freshGaps) {
          if (g.section_id) gapBySection.set(g.section_id, g)
        }
        const hydrated: FillResult[] = []
        for (const scene of freshPlan.main_track ?? []) {
          // stage-43：scene_id 支持单镜 `sc-N` 与多分镜 `sc-N-sh-M`，两种都按 section index N 反查 section。
          const m = scene.scene_id.match(/^sc-(\d+)(?:-sh-\d+)?$/)
          if (!m) continue
          const sectionId = sectionByOrder.get(Number(m[1]))
          if (!sectionId) continue
          const gap = gapBySection.get(sectionId)
          if (!gap) continue
          if (scene.source === 'text_card' && scene.text_card_spec) {
            hydrated.push({
              gap_id: gap.gap_id,
              section_id: sectionId,
              action: 'copy',
              status: 'ok',
              narration: scene.narration ?? null,
              voiceover_url: scene.voiceover_url ?? null,
              text_card_spec: scene.text_card_spec,
              alternatives: [],
              video_urls: [],
              chunks_count: 0,
              chunk_task_ids: [],
              note: '已采纳（从历史 plan 恢复）',
            })
          } else if (scene.source === 'aigc_t2v' && scene.aigc_video_urls.length > 0) {
            hydrated.push({
              gap_id: gap.gap_id,
              section_id: sectionId,
              action: 'aigc',
              status: 'ok',
              narration: scene.narration ?? null,
              voiceover_url: scene.voiceover_url ?? null,
              video_urls: scene.aigc_video_urls,
              cover_url: scene.aigc_video_urls[0] ?? null,
              alternatives: [],
              chunks_count: scene.aigc_video_urls.length,
              chunk_task_ids: [],
              note: '已采纳（从历史 plan 恢复）',
            })
          } else if (scene.source === 'aigc_image' && scene.aigc_image_url) {
            // stage-43：AI 单图换源也要让 step3 看到（否则前端从 plan 重建时丢掉这个 fill，
            // step3 仍按旧 fill 渲染——用户痛报：『针对一个三分镜片段生成字卡后换源单镜 step3 还是字卡』）。
            hydrated.push({
              gap_id: gap.gap_id,
              section_id: sectionId,
              action: 'aigc_image',
              status: 'ok',
              narration: scene.narration ?? null,
              voiceover_url: scene.voiceover_url ?? null,
              aigc_image_url: scene.aigc_image_url,
              animation_spec: scene.animation_spec ?? null,
              alternatives: [],
              video_urls: [],
              chunks_count: 0,
              chunk_task_ids: [],
              note: '已采纳（从历史 plan 恢复）',
            })
          }
        }
        if (hydrated.length > 0) setFills(hydrated)
      } catch {
        /* 没快照或拉取失败时让用户重新跑分析 */
      }
    })()
    return () => {
      cancelled = true
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [currentProjectId])

  // mount：把后端已存的素材回灌进 zustand（材料 store 是 in-memory，刷浏览器还在但进程重启清空）
  const refreshMaterials = useCallback(async () => {
    if (!currentProjectId) return
    try {
      const items = await api.get<Material[]>(`/material?project_id=${encodeURIComponent(currentProjectId)}`)
      setMaterials(items)
    } catch {
      /* 没素材或网络抖动不影响主流程 */
    }
  }, [currentProjectId, setMaterials])

  useEffect(() => {
    if (!currentProjectId) return
    let cancelled = false
    void (async () => {
      try {
        const items = await api.get<Material[]>(`/material?project_id=${encodeURIComponent(currentProjectId)}`)
        if (cancelled) return
        setMaterials(items)
      } catch {
        /* 没素材或网络抖动不影响主流程 */
      }
    })()
    return () => {
      cancelled = true
    }
  }, [currentProjectId, setMaterials])

  // forward-ref for runAnalyze:handlePickFiles 在 runAnalyze 之前定义,但需要在上传完成后触发它
  const runAnalyzeRef = useRef<
    ((extra?: FillResult[], opts?: { silent?: boolean }) => Promise<Plan | null>) | null
  >(null)
  /** 并发 plan/build 防串号：每次 runAnalyze 拿一个 epoch；返回前比对 latest，
   *  若已被更新的请求覆盖（用户连点多段补全），本次结果直接丢弃。 */
  const analyzeEpochRef = useRef(0)
  /** 并发 plan/build 序列化：所有 runAnalyze 通过这个 promise 链排队，
   *  避免后端拿到两次 effectiveFills 不同的 plan/build 请求引起 plan 抖动。 */
  const analyzeChainRef = useRef<Promise<Plan | null>>(Promise.resolve(null))

  /* ------------------------------ 上传 ------------------------------ */

  const handlePickFiles = useCallback(
    async (files: FileList | null) => {
      if (!files || files.length === 0) return
      if (!currentProjectId) {
        setError('请先在首页新建/选择一个项目，再上传素材')
        return
      }
      setError(null)
      setUploading(true)
      // 乐观占位：图片/视频上传 + LLM 打标可能耗时 30s+。立刻插入占位 Material 让用户看到
      // 「正在处理 N 条」，否则在响应回来前 grid 像没动。
      const fileArr = Array.from(files)
      const stamp = Date.now()
      const pendingIds = fileArr.map((_, i) => `__pending_${stamp}_${i}`)
      const placeholders: Material[] = fileArr.map((f, i) => ({
        material_id: pendingIds[i],
        filename: f.name,
        media_type: f.type.startsWith('video/') ? 'video' : f.type.startsWith('audio/') ? 'audio' : 'image',
        tags: [],
        subjects: [],
        highlight_score: 0,
        sort_order: 9_000_000 + i,
        preprocess_status: 'running',
      }))
      appendMaterials(placeholders)
      try {
        const fd = new FormData()
        fileArr.forEach((f) => fd.append('files', f))
        // project_id 是后端唯一隔离键；session_id 字段保留为别名（已等于 project_id）
        fd.append('project_id', currentProjectId)
        fd.append('video_type', videoType)
        const resp = await api.post<MaterialUploadResponse>('/material/upload', fd)
        setSession(resp.session_id)
        // 移除占位，再以服务端返回为准重新载入；防御性 GET 兜底——保证服务端真值与
        // grid 一致（避免 sort_order 错位 / 项目切换 / 多 tab 并发上传时漏条）。
        for (const id of pendingIds) removeMaterial(id)
        appendMaterials(resp.materials)
        try {
          const fresh = await api.get<Material[]>(`/material?project_id=${encodeURIComponent(currentProjectId)}`)
          setMaterials(fresh)
        } catch {
          /* GET 失败不阻断；上面 appendMaterials 已经把新素材插进去了 */
        }
        // step 2 中上传 = 用户希望立刻把新素材纳入排列；自动跑一次 plan/build + gap/detect 重新计算缺口
        if (plan && runAnalyzeRef.current) {
          void runAnalyzeRef.current(fills)
        }
      } catch (err) {
        // 失败：清掉占位
        for (const id of pendingIds) removeMaterial(id)
        setError(err instanceof Error ? err.message : '上传失败')
      } finally {
        setUploading(false)
      }
    },
    [appendMaterials, currentProjectId, fills, plan, removeMaterial, setMaterials, setSession, videoType],
  )

  /* -------------------- 删素材：DELETE 后端 + 同步 store + 触发重算 -------------------- */
  // MaterialCard 上的 × 按钮 / step2 onRemove 都走这里。后端调用失败仍同步本地（让用户看到效果），
  // 但 setError 提示一下,不然进程重启后这条素材会"诈尸"回来,用户更困惑。
  const handleDeleteMaterial = useCallback(
    async (materialId: string) => {
      // 占位条目还没 POST 到后端,直接走本地清理,不要 DELETE
      if (materialId.startsWith('__pending_')) {
        removeMaterial(materialId)
        return
      }
      if (!currentProjectId) {
        removeMaterial(materialId)
        return
      }
      try {
        await api.delete(`/material/${encodeURIComponent(materialId)}?project_id=${encodeURIComponent(currentProjectId)}`)
      } catch (err) {
        console.warn('[material] delete failed; UI state still updated:', err)
        setError(err instanceof Error ? `删除失败：${err.message}` : '删除失败')
      }
      removeMaterial(materialId)
    },
    [currentProjectId, removeMaterial],
  )

  /* -------------------- 智能分析（plan/build + gap/detect） -------------------- */

  const runAnalyze = useCallback(
    async (extraFills?: FillResult[], opts?: { silent?: boolean }) => {
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
        setError('请先输入主题——AI 需要它作为方向锚点，否则段落推断会偏。')
        return null
      }
      // silent=true：补全工作台触发的增量重建，不动全局 analyzing —— 让用户继续操作
      // 其它段的工作台，实现真正的并行补全（每段独立 busyGapIds 锁即可）。
      const silent = opts?.silent ?? false
      // 序列化：concurrent 调用排队（plan/build 是单例后端，并发请求会互相覆盖 plan 状态）
      const prev = analyzeChainRef.current
      const job = (async (): Promise<Plan | null> => {
        try {
          await prev
        } catch {
          /* 上一次失败不应阻塞这次 */
        }
        const myEpoch = ++analyzeEpochRef.current
        setError(null)
        if (!silent) setAnalyzing(true)
        try {
          // 「重新分析」（无 extraFills）→ 旧 plan 的 fills 对新 plan_id 不再有效，整体清空
          // 避免后端 fill_by_section 路由把上一版的 narration / aigc_video_urls 错塞进新段落。
          const effectiveFills: FillResult[] = extraFills ?? []
          const isIncremental = extraFills !== undefined
          if (extraFills === undefined) {
            setFills([])
          }
          const planReq: PlanBuildRequest = {
            reference_versions: selectedReferences,
            project_id: currentProjectId,
            session_id: currentProjectId,
            brief: brief.trim() || null,
            video_goal: null,
            settings,
            selected_materials: sortedMaterials.map((m) => m.material_id),
            fills: effectiveFills,
            // 增量重建：fill 触发的 runAnalyze 不应让 LLM 重排段落（5→4 抖动 bug）。
            // 仅当 plan 已存在 & 是 incremental rebuild 时透传旧 sections。
            reuse_sections: isIncremental && plan?.adapted_sections ? plan.adapted_sections : undefined,
            variant: 'A',
          }
          const builtPlan = await api.post<Plan>('/plan/build', planReq)
          // 并发守卫：若本次响应回来时已有更新的 runAnalyze 在跑，丢弃本次的应用，
          // 让最新一次的结果落地（最后赢家原则，避免老结果覆盖新结果）。
          if (myEpoch !== analyzeEpochRef.current) return null
          setPlan(builtPlan)

          const detectReq: GapDetectRequest = {
            plan_id: builtPlan.plan_id,
            project_id: currentProjectId,
            session_id: currentProjectId,
          }
          const detected = await api.post<Gap[]>('/gap/detect', detectReq)
          if (myEpoch !== analyzeEpochRef.current) return null
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
              f.action === 'copy'
                ? '字卡画面'
                : f.action === 'aigc'
                  ? 'AI 视频'
                  : f.action === 'aigc_image'
                    ? 'AI 生图再渲染'
                    : '已挑素材'
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
              video_goal: null,
              settings,
            })
            .catch(() => {
              /* 回写失败不阻塞分析主流程 */
            })

          return builtPlan
        } catch (err) {
          if (myEpoch === analyzeEpochRef.current) {
            setError(err instanceof Error ? err.message : '智能分析失败')
          }
          return null
        } finally {
          if (!silent) setAnalyzing(false)
        }
      })()
      analyzeChainRef.current = job
      return job
    },
    [brief, currentProjectId, selectedReferences, selectedSampleId, setFills, setGaps, setPlan, settings, sortedMaterials, plan],
  )

  // 把 runAnalyze 挂到 ref:supports handlePickFiles 在 step 2 上传后自动重排
  useEffect(() => {
    runAnalyzeRef.current = runAnalyze
  }, [runAnalyze])

  /* ----------------------------- 补全动作 ----------------------------- */

  const runFill = useCallback(
    async (gap: Gap, action: FillAction, params: Record<string, unknown> = {}) => {
      const sectionId = gap.section_id ?? gap.gap_id  // 老 gap 没 section_id 兜底
      markBusy(sectionId, true)
      setError(null)
      try {
        const body: GapFillRequest = { gap_id: gap.gap_id, action, params }
        const result = await api.post<FillResult>('/gap/fill', body)
        upsertFill(result)
        // stage-58：AIGC 产物会被后端 _record_aigc_to_library 写进素材库，立刻拉一次
        // /material 让左侧素材库出现新条目。runFill 当前只被 rerank 用，rerank 不
        // 增加素材；但保留这个保险——未来若 runFill 被复用为通用入口仍能正确刷新。
        if (result.status === 'ok' && (result.action === 'aigc' || result.action === 'aigc_image')) {
          void refreshMaterials()
        }
        // 关键：从 store 拿最新 fills 而非 closure 中的 stale 快照。
        // 并行补全场景（A、B 两段同时点 fill）下，closure 里的 fills 只包含本次发起
        // 时的状态，B 提交时已不含 A 的结果——`[...fills.filter, B] 把 A 抹掉了`。
        // 改用 getState 在 await 后取最新 snapshot（已含 upsertFill 写入的 A、B）。
        const latest = usePlanStore.getState().fills
        const nextFills = [...latest.filter((f) => f.gap_id !== gap.gap_id), result]
        // silent=true：本次重建不触发全局 analyzing 锁，其它 section 工作台保持可操作
        // （每段 busySectionIds 单独锁就够了，真正的并发补全在这里实现）。
        await runAnalyze(nextFills, { silent: true })
        return result
      } catch (err) {
        setError(err instanceof Error ? err.message : '补全失败')
        return null
      } finally {
        markBusy(sectionId, false)
      }
    },
    [markBusy, refreshMaterials, runAnalyze, upsertFill],
  )

  const handleRerankApply = useCallback(async () => {
    if (!selectedGap) return
    await runFill(selectedGap, 'rerank')
  }, [runFill, selectedGap])

  // copy fill 已迁移到 FillCopyPanel 内部状态机（T5），不再在 Compose 这一层触发或采纳。

  // stage-70：gap 待补数必须以 scene 级状态为最终依据。
  // 用户报障：『所有镜都换源审过了（"已审"），但仍显示 N 段缺口待补、无法进 step3』
  // 根因：旧版本只看 gap.status / fills 数组。后端 /gap/detect 重新分析时不知道用户在
  //       SwapSourceDialog 里做过手动换源（这条路径不写 fill 记录，只翻 scene.user_edited
  //       和 plan.main_track），于是 gap 状态停留在 miss/warn，pendingGapsCount 永远 >0。
  // 新语义：一个 gap "pending" 只有当它所在 section 还有镜既不是 user_edited，又是 needs_fill
  //         / text-card-fill-empty 占位时才算。这与 mainTrackUnfilledCount 的 scene 级口径
  //         保持一致，避免两个数字打架。
  const pendingGapsCount = useMemo(() => {
    if (!plan) return 0
    const sectionHasUnfilled = new Map<string, boolean>()
    for (const sc of plan.main_track) {
      const sid = sc.parent_section_id
      if (!sid) continue
      if (!sectionHasUnfilled.has(sid)) sectionHasUnfilled.set(sid, false)
      if (sc.user_edited === true) continue
      const unfilled =
        sc.needs_fill === true ||
        (sc.source_ref ?? '').startsWith('text-card-fill-empty')
      if (unfilled) sectionHasUnfilled.set(sid, true)
    }
    return gaps.filter((g) => {
      if (g.status === 'ok') return false
      if (fills.some((f) => f.gap_id === g.gap_id && f.status === 'ok')) return false
      // 老 plan / 老 gap 没有 section_id → 退回旧语义防止漏门控
      if (!g.section_id) return true
      return sectionHasUnfilled.get(g.section_id) === true
    }).length
  }, [gaps, fills, plan])

  // stage-26 PR-N.6：内容轨『还未补齐』的 Scene 数。两类都算：
  //   - 后端 PR-N.2 标记 needs_fill=true（匹配 weak/missing 物化时落下的兜底）
  //   - PR-L.3 兜底字卡（source_ref 以 text-card-fill-empty 开头）—— 这是真实的
  //     『某段 fill 跑空了，临时塞了文字卡占位』的场景，没补齐前内容轨残缺
  // 用于：
  //   a) 「进入第 3 步」按钮 disabled — 内容轨没补完不许进
  //   b) WorkshopStepNav step3 tab disabled — 顶部 tab 也跟着锁
  // 不再用 pendingGapsCount 单独门控 step3（那是 gap 模型层面的"待补"，乐观更新会瞬间归零）；
  // 真正能反映轨道更新进度的是 plan.main_track 实际状态。
  const mainTrackUnfilledCount = useMemo(() => {
    // stage-61: 用户原话『step2 的补齐缺口检查以分镜为单位』+『手动调整过的分镜无论如何视作已补齐』
    // → 按 Scene 数计（不再按段聚合），且把 user_edited=true 的 Scene 排除。
    if (!plan) return 0
    let count = 0
    for (const sc of plan.main_track) {
      if (sc.user_edited === true) continue
      const isUnfilled =
        sc.needs_fill === true ||
        (sc.source_ref ?? '').startsWith('text-card-fill-empty')
      if (isUnfilled) count++
    }
    return count
  }, [plan])

  /* --------------------- 四轨：口播 / 包装 / BGM 动作 --------------------- */

  const refetchPlan = useCallback(
    async (planId: string) => {
      try {
        const fresh = await api.get<Plan>(`/plan/${planId}`)
        setPlanAndPush(fresh)
      } catch {
        /* 拉新版失败由上层 error 兜底；不阻塞当前动作 */
      }
    },
    [setPlanAndPush],
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
        setPlanAndPush(fresh)
      } catch (err) {
        setError(err instanceof Error ? err.message : `清除口播失败：${sceneId}`)
      } finally {
        setTrackBusy(false)
      }
    },
    [plan, setPlanAndPush],
  )

  /**
   * 进入 step3 时的一次性筹备：先重写口播 → 若开了配音则一键 TTS 全片 →
   * 全部跑完才 setStep3Unlocked(true) + setActiveStep(3) + 释放 trackBusy。
   *
   * stage-63 起：包装项一律由用户手动「✨ 添加组件」单加，不再做自动批量包装/转场推荐。
   * 用户原话：『step3的一键生成包装轨和重新生成包装轨的功能取消吧，乱七八糟的』『只支持添加组件生成』。
   */
  const handleEnterStep3 = useCallback(async () => {
    if (!plan) return
    setTrackBusy(true)
    setError(null)
    let landedOk = false
    try {
      // 1) 重写口播（关键路径：失败就不进 step3）
      const ren = await regenerateNarrations(plan.plan_id)
      setPlanAndPush(ren.plan)
      landedOk = true

      // 2) 若开了配音且有更新口播，自动一键 TTS 全片（失败只 setError 不阻塞）
      if (ren.plan.settings.voiceover_enabled && ren.updated_scene_ids.length > 0) {
        try {
          const tts = await synthesizeAll(plan.plan_id)
          if (tts.failures.length > 0) {
            setError(`部分段落配音失败（${tts.failures.length} 段）；可在口播轨手动重试`)
          }
          // 把 TTS 写入的 voiceover_url 拉回前端
          await refetchPlan(plan.plan_id)
        } catch (err) {
          setError(err instanceof Error ? `配音失败：${err.message}` : '配音失败')
        }
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : '进入 step3 准备失败')
    } finally {
      // 所有筹备跑完（成功/失败都算）→ 才切换并释放 busy，
      // 用户落到 step3 的瞬间口播条、字幕都已就绪。
      if (landedOk) {
        setStep3Unlocked(true)
        setActiveStep(3)
      }
      setTrackBusy(false)
    }
  }, [plan, refetchPlan, setPlanAndPush])

  const handleBgmAnchorChange = useCallback(
    async (newAnchor: number) => {
      if (!plan) return
      setTrackBusy(true)
      setError(null)
      try {
        const fresh = await patchPlanBgm(plan.plan_id, { video_anchor_seconds: newAnchor })
        setPlanAndPush(fresh)
      } catch (err) {
        setError(err instanceof Error ? err.message : '更新 BGM 锚点失败')
      } finally {
        setTrackBusy(false)
      }
    },
    [plan, setPlanAndPush],
  )

  const handleClearBgm = useCallback(async () => {
    if (!plan) return
    setTrackBusy(true)
    setError(null)
    try {
      const fresh = await deletePlanBgm(plan.plan_id)
      setPlanAndPush(fresh)
    } catch (err) {
      setError(err instanceof Error ? err.message : '清除 BGM 失败')
    } finally {
      setTrackBusy(false)
    }
  }, [plan, setPlanAndPush])

  const handleBgmVolumeChange = useCallback(
    async (volume: number) => {
      if (!plan) return
      setError(null)
      try {
        const fresh = await patchPlanBgm(plan.plan_id, { volume })
        setPlanAndPush(fresh)
      } catch (err) {
        setError(err instanceof Error ? err.message : '更新 BGM 音量失败')
      }
    },
    [plan, setPlanAndPush],
  )

  /** 包装项剪映式拉伸：start/end 同时改（move 时也走这条路径，保持单 op 落盘）。 */
  const handleResizePackagingItem = useCallback(
    async (itemId: string, newStart: number, newEnd: number, newLaneIndex: number | null) => {
      if (!plan) return
      setError(null)
      try {
        const op: Record<string, unknown> = {
          op: 'update_packaging_item_time',
          item_id: itemId,
          start: newStart,
          end: newEnd,
        }
        if (newLaneIndex !== null) op.lane_index = newLaneIndex
        const laneTail = newLaneIndex !== null ? ` · 落在第 ${newLaneIndex + 1} 轨` : ''
        const body = {
          plan_id: plan.plan_id,
          step: 'step3' as const,
          instruction: `拉伸包装项 ${itemId} 到 [${newStart.toFixed(1)},${newEnd.toFixed(1)}]s${laneTail}`,
          apply: true,
          confirmed_ops: [op],
        }
        const resp = await api.post<{ plan?: Plan }>('/edit/compose', body)
        if (resp.plan) setPlanAndPush(resp.plan)
      } catch (err) {
        setError(err instanceof Error ? err.message : '包装项调整失败')
      }
    },
    [plan, setPlanAndPush],
  )

  const handleDeletePackagingItem = useCallback(
    async (itemId: string) => {
      if (!plan) return
      setError(null)
      try {
        const fresh = await api.delete<Plan>(
          `/packaging/items/${encodeURIComponent(plan.plan_id)}/${encodeURIComponent(itemId)}`,
        )
        setPlanAndPush(fresh)
        setSelectedPackagingItemId((curr) => (curr === itemId ? null : curr))
      } catch (err) {
        setError(err instanceof Error ? err.message : '删除包装项失败')
      }
    },
    [plan, setPlanAndPush],
  )

  const handleRecommendPackagingForScene = useCallback(
    async (sceneId: string, kind: 'title_bar' | 'sticker' | 'cover') => {
      if (!plan) return
      setError(null)
      setTrackBusy(true)
      try {
        // recommend-for-scene → place 链路：LLM 按段+类型出单个 PackagingItem 草稿，立即落进轨道。
        const draft = await api.post<import('@/types/schemas').PackagingItemDraftResponse>(
          '/packaging/recommend-for-scene',
          { plan_id: plan.plan_id, scene_id: sceneId, kind },
        )
        const placeReq: import('@/types/schemas').PackagingItemPlaceRequest = {
          plan_id: plan.plan_id,
          item: draft.item,
        }
        const fresh = await api.post<Plan>('/packaging/items/place', placeReq)
        setPlanAndPush(fresh)
        setSelectedPackagingItemId(draft.item.item_id)
      } catch (err) {
        setError(err instanceof Error ? err.message : '智能添加包装组件失败')
      } finally {
        setTrackBusy(false)
      }
    },
    [plan, setPlanAndPush],
  )

  // 字幕开关：同时改 plan.settings + session.settings，让本次 plan 立刻生效，
  // 同时下次「重新分析」也保留用户偏好。
  const handleToggleSubtitle = useCallback(
    async (enabled: boolean) => {
      setSettings({ subtitle_enabled: enabled })
      if (!plan) return
      setTrackBusy(true)
      setError(null)
      try {
        const fresh = await patchPlanSettings(plan.plan_id, { subtitle_enabled: enabled })
        setPlanAndPush(fresh)
      } catch (err) {
        setError(err instanceof Error ? err.message : '切换字幕开关失败')
      } finally {
        setTrackBusy(false)
      }
    },
    [plan, setPlanAndPush, setSettings],
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
        setPlanAndPush(fresh)
      } catch (err) {
        setError(err instanceof Error ? err.message : '切换口播开关失败')
      } finally {
        setTrackBusy(false)
      }
    },
    [plan, setPlanAndPush, setSettings],
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
        setPlanAndPush(fresh)
      } catch (err) {
        setError(err instanceof Error ? err.message : '切换配音音色失败')
      } finally {
        setTrackBusy(false)
      }
    },
    [plan, setPlanAndPush, setSettings],
  )

  /* --------------------- 下一步：补缺口 → 生成包装 → 渲染 --------------------- */

  /* --------------------- 一键收尾：补缺口 → 包装 → 渲染（全部内联） --------------------- */

  const handleProceedToRender = useCallback(async () => {
    if (!plan) return
    setError(null)
    setRenderError(null)
    setRenderDone(null)
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

      // 阶段 2 · 包装项不再自动批量生成（stage-63 起，用户手动单加）。直接进 commit。

      // 阶段 3 · commit compose 步骤快照（顶部 nav 标 saved + current_step 推进）
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

      // 阶段 4 · 提交渲染（内联，不跳页）：接 SSE，进度 / 结果都落在本页底部
      setRenderStep('submit')
      setRenderPercent(0)
      const submitResp = await api.post<RenderSubmitResponse>('/render/submit', {
        plan_id: activePlanId,
        variant,
      })
      setJobId(submitResp.job_id)
      sseRef.current?.close()
      sseRef.current = createSSE<{ job_id: string; payload: RenderDonePayload }>(
        `/render/stream?job_id=${submitResp.job_id}`,
        {
          onProgress: (p) => {
            setRenderStep(p.step)
            setRenderPercent(p.percent)
          },
          onDone: (d) => {
            setRenderDone(d.payload)
            setRenderStep('done')
            setRenderPercent(100)
            // 后端 _do_render 完成时已自动 mark_rendered + 落盘；这里刷项目列表 + commit render 步骤
            if (currentProjectId) {
              void refreshProjects()
              void commitStep(currentProjectId, 'render', { job_id: submitResp.job_id }).catch(
                () => {
                  /* commit 失败不阻断结果展示 */
                },
              )
            }
          },
          onError: (e) => setRenderError(e.detail),
        },
      )
    } catch (err) {
      setError(err instanceof Error ? err.message : '渲染收尾失败')
      setFinalizing('idle')
      setRenderStep('idle')
    }
  }, [
    currentProjectId,
    fills,
    gaps,
    plan,
    refreshProjects,
    runAnalyze,
    setPlanAndPush,
    upsertFill,
    variant,
  ])

  const handleUndo = useCallback(() => {
    const p = undoEdit()
    if (p) setPlan(p)
  }, [setPlan, undoEdit])

  const handleRedo = useCallback(() => {
    const p = redoEdit()
    if (p) setPlan(p)
  }, [redoEdit, setPlan])

  // 命名快照的保存/恢复/删除已统一由 VersionMenu 组件内部维护——
  // 它每次打开都会拉一次最新列表，不需要在 Compose 这边镜像状态。

  /* ----------------------------- guard ----------------------------- */

  if (!selectedSampleId) {
    return (
      <PageShell title="视频工坊" subtitle="先从资产库挑 1–2 个拆解版本作为参考，再来这儿写主题、配素材、出片。">
        <div className="mb-3 rounded-lg border border-dashed border-border bg-card p-4 text-xs text-muted-foreground">
          下面会列出资产库里所有已拆解的样例。挑 1–2 个作为本次的结构参考；如果还没有，先去
          <Link to="/library" className="ml-1 text-primary underline-offset-4 hover:underline">
            资产库
          </Link>
          找一支样例进「样例拆解」跑一次并保存。
        </div>
        <ReferencePicker />
      </PageShell>
    )
  }

  /* ------------------------------ 渲染 ------------------------------ */

  return (
    <PageShell
      title="视频工坊"
      subtitle="第 1 步选参考 + 写主题 → 第 2 步生成内容轨 → 第 3 步出片。"
    >
      <div className="flex items-start justify-between gap-3">
        <WorkshopStepNav
          activeStep={activeStep}
          hasReferences={selectedReferences.length > 0}
          briefFilled={brief.trim().length > 0}
          hasPlan={!!plan}
          step3Unlocked={step3Unlocked}
          pendingGapsCount={pendingGapsCount}
          mainTrackUnfilledCount={mainTrackUnfilledCount}
          onChange={setActiveStep}
        />
        {plan && (
          <div className="shrink-0 pt-1">
            <VersionMenu plan={plan} onPlanRestored={setPlanAfterRestore} />
          </div>
        )}
      </div>

      {error && (
        <div className="mb-3 rounded-md border border-destructive/40 bg-destructive/5 px-4 py-2 text-sm text-destructive">
          {error}
        </div>
      )}

      {/* 外部素材一律走拖拽,所以删除原"+追加素材"按钮配套的 hidden file input. */}

      {/* stage-15: ReferencePicker —— 从资产库挑 1-2 个 (sample, slot) 作为结构参考 */}
      {activeStep === 1 && (
        <div className="mb-3">
          <ReferencePicker />
        </div>
      )}

      {/* 结构迁移示意：step 2 起常驻——内容轨生成后，每一步都让用户能扫一眼"新方案 vs 样例" */}
      {(activeStep === 2 || activeStep === 3) && effectiveManifest && plan && (
        <div className="mb-3">
          <StructureCompareSection
            manifest={effectiveManifest}
            secondaryManifest={secondaryManifest}
            plan={plan}
            gaps={gaps}
            onZoom={() => setStructureZoomOpen(true)}
          />
        </div>
      )}

      {/* ============ Row 1：步骤 1 = 主题输入 + 设置 + 上传素材 ============ */}
      {activeStep === 1 && (
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
            <ClarifyPanel
              initialBrief={brief}
              onAdopt={(t) => {
                setBrief(t)
                setBriefTouched(false)
                setClarifiedOnce(true)
              }}
              disabled={analyzing}
              clarified={clarifiedOnce}
              detectedSubjects={detectedSubjects}
            />
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
                  没有素材也能跑：仅凭主题分析 → 所有段落都标为缺素材 → 用「字卡画面 / AI 视频」逐个补齐。
                </p>
              )}
              <UploadDropzone
                uploading={uploading}
                onDrop={(f) => void handlePickFiles(f)}
              />
              {/* hidden file input 仍保留:仅供 Step3 内部其它路径(如 packaging fill)使用,
                  外部素材一律走拖拽. */}
            </div>

            <div className="space-y-2">
              <div className="flex items-center justify-between">
                <label className="text-xs font-semibold">素材库（拖拽可排序）</label>
                <div className="flex items-center gap-2">
                  <button
                    type="button"
                    onClick={() => setSystemLibraryOpen(true)}
                    disabled={!currentProjectId}
                    className="rounded border border-border bg-background px-2 py-0.5 text-[10px] hover:bg-secondary disabled:opacity-50"
                    title="从系统素材库挑选 → 克隆到本项目"
                  >
                    + 从素材库选取
                  </button>
                  <span className="text-[10px] text-muted-foreground">{sortedMaterials.length} 条</span>
                </div>
              </div>
              <MaterialGrid
                materials={sortedMaterials}
                onReorder={reorderMaterials}
                onRemove={(id) => void handleDeleteMaterial(id)}
              />
            </div>
          </section>
        </div>
      )}

      {/* ============ 步骤 1 → 步骤 2：生成内容轨按钮 ============ */}
      {activeStep === 1 && (
        <div className="mt-3 flex flex-col gap-2 sm:flex-row">
          <button
            onClick={async () => {
              const built = await runAnalyze()
              if (built) setPlanJustGenerated(true)
            }}
            disabled={analyzing || brief.trim().length === 0 || !clarifiedOnce}
            title={
              brief.trim().length === 0
                ? '请先输入主题/卖点'
                : !clarifiedOnce
                  ? '请先在上方完成一轮「意图澄清」（点澄清面板的开始澄清按钮）'
                  : undefined
            }
            className={cn(
              'flex-1 rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground transition-colors',
              (analyzing || brief.trim().length === 0 || !clarifiedOnce) && 'cursor-not-allowed opacity-60',
            )}
          >
            {analyzing
              ? '生成内容轨中…'
              : !clarifiedOnce
                ? '请先完成意图澄清'
                : plan
                  ? '重新生成内容轨'
                  : '生成内容轨'}
          </button>
          {plan && !analyzing && (
            <button
              onClick={() => setActiveStep(2)}
              className="rounded-md border border-border bg-card px-4 py-2 text-sm font-medium hover:bg-secondary"
            >
              直接进入第 2 步（不重生成）
            </button>
          )}
        </div>
      )}

      {/* ============ Step 1 全屏覆盖层：analyzing 时 spinner；analyzing 结束 + planJustGenerated 时 ✓ 内容轨已生成 + 预览 + 双按钮 ============ */}
      {activeStep === 1 && (analyzing || (planJustGenerated && plan)) && (
        <div
          role="dialog"
          aria-modal="true"
          aria-live="polite"
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/55 backdrop-blur-sm p-4"
        >
          {analyzing ? (
            <div className="flex max-w-md flex-col items-center gap-3 rounded-lg border border-border bg-card px-8 py-6 text-center shadow-2xl">
              <div className="h-12 w-12 animate-spin rounded-full border-2 border-primary/30 border-t-primary" />
              <div className="text-base font-semibold text-foreground">正在生成内容轨…</div>
              <div className="text-xs leading-relaxed text-muted-foreground">
                结构改编 + 段落识别 + 缺口分析需要 5–15s 左右。
              </div>
            </div>
          ) : plan ? (
            <div className="flex max-h-[90vh] w-full max-w-5xl flex-col gap-3 overflow-hidden rounded-lg border border-border bg-card p-5 shadow-2xl">
              <div className="flex items-center justify-between">
                <div>
                  <h3 className="text-base font-semibold text-foreground">✓ 内容轨已生成</h3>
                  <p className="text-xs text-muted-foreground">
                    {plan.adapted_sections.length} 段 · {plan.main_track.length} 镜头 · 共 {plan.duration_seconds.toFixed(1)}s · 缺口 {gaps.length}
                  </p>
                </div>
                <div className="flex items-center gap-2">
                  {(plan.kb_rules_applied ?? 0) > 0 && (
                    <button
                      type="button"
                      onClick={() => navigate('/knowledge')}
                      title="点击去个性知识库管理"
                      className="flex items-center gap-1 rounded-full border border-primary/30 bg-primary/10 px-2 py-0.5 text-[11px] font-medium text-primary hover:bg-primary/20"
                    >
                      已应用 {plan.kb_rules_applied} 条规则 · 去管理 →
                    </button>
                  )}
                  <span className="text-[10px] text-muted-foreground">{videoType}</span>
                </div>
              </div>
              <div className="min-h-0 flex-1 overflow-auto rounded-md border border-border bg-background/30 p-2">
                {effectiveManifest && (
                  <div className="mb-2 h-[200px] overflow-hidden rounded-md border border-border bg-card">
                    <StructureMapPanel
                      className="h-full"
                      manifests={[effectiveManifest, secondaryManifest].filter(
                        (m): m is SampleManifest => !!m,
                      )}
                      plan={plan}
                      gaps={gaps}
                    />
                  </div>
                )}
                <FourTrackBoard
                  plan={plan}
                  gaps={gaps}
                  filledGapIds={filledGapIds}
                  selectedGapId={null}
                  selectedSceneId={null}
                  selectedPackagingItemId={null}
                  materials={sortedMaterials}
                  fills={fills}
                  referenceManifests={[effectiveManifest, secondaryManifest].filter(
                    (m): m is SampleManifest => !!m,
                  )}
                  onSelectScene={() => {}}
                  onSelectVoice={() => {}}
                  onSelectPackaging={() => {}}
                  onSynthesizeScene={async () => {}}
                  onSynthesizeAll={async () => {}}
                  onClearVoice={async () => {}}
                  onPickBgm={() => {}}
                  onBgmAnchorChange={async () => {}}
                  onClearBgm={async () => {}}
                  onBgmVolumeChange={async () => {}}
                  onToggleSubtitle={async () => {}}
                  onToggleVoiceover={async () => {}}
                  onChangeTtsVoice={async () => {}}
                  busy={false}
                  phase="content-only"
                  playheadSeconds={0}
                />
              </div>
              <div className="flex flex-col gap-2 sm:flex-row">
                <button
                  type="button"
                  onClick={() => {
                    setPlanJustGenerated(false)
                    setActiveStep(2)
                  }}
                  className="flex-1 rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground transition-colors hover:opacity-90"
                >
                  进入第 2 步 → 编辑内容轨 / 补缺口
                </button>
                <button
                  type="button"
                  onClick={() => setPlanJustGenerated(false)}
                  className="rounded-md border border-border bg-card px-4 py-2 text-sm font-medium hover:bg-secondary"
                >
                  重新澄清主题再生成
                </button>
              </div>
            </div>
          ) : null}
        </div>
      )}

      {/* ============ Row 2：步骤 2 = 样例 ↔ 新内容轨（顶部）+ 适配概要 + 补缺口 + 段落编辑 + 素材库（底） ============ */}
      {activeStep === 2 && plan && (
        <section className="mt-4 space-y-3 rounded-lg border border-border bg-card p-4">
          {/* stage-76（2026-06-12）：用户原话「step2 一开始不要直接填入素材了，都让用户自己去选择」。
              内容轨现在只产 plan 规划占位（text_card + needs_fill），真实素材 / 已切片画面需在
              分镜卡上点击换源逐个挑选。第一次进 step2 弹一次说明，之后用 localStorage 静音。 */}
          <Step2PlaceholderHint />
          {/* 顶部：左 sticky 实时预览 + 右 内容轨（step3 同款布局）
              用户在 step2 也能边补缺口边看整片预览；选中片段后会自动 seek 到段首。
              phase=content-only：只展示内容轨，口播 / 包装 / BGM 留到 step3 解锁。 */}
          <div className="grid items-start gap-3 md:grid-cols-[minmax(0,300px)_1fr]">
            <div className="rounded-lg border border-border bg-card p-2 md:sticky md:top-4 md:self-start">
              <div className="mb-1.5 flex items-center justify-between px-1 text-[11px] text-muted-foreground">
                <span className="font-medium">实时预览</span>
                <span className="font-mono">
                  {playheadSeconds.toFixed(1)}s / {plan.duration_seconds.toFixed(1)}s
                </span>
              </div>
              <PlanPlayer
                ref={playerRef}
                plan={plan}
                materials={sortedMaterials}
                onTimeUpdate={setPlayheadSeconds}
              />
            </div>
            <div className="space-y-1.5">
              <div className="flex items-center justify-between">
                <h2 className="text-sm font-semibold">样例视频轨道 ↔ 新内容轨</h2>
                <span className="text-[10px] text-muted-foreground">{videoType}</span>
              </div>
              <FourTrackBoard
                plan={plan}
                gaps={gaps}
                filledGapIds={filledGapIds}
                selectedGapId={selectedGapId}
                selectedSceneId={effectiveSelectedSceneId}
                selectedPackagingItemId={selectedPackagingItemId}
                materials={sortedMaterials}
                fills={fills}
                referenceManifests={[effectiveManifest, secondaryManifest].filter(
                  (m): m is SampleManifest => !!m,
                )}
                onSelectScene={(scene, gap) => {
                  setSelectedSceneId(scene.scene_id)
                  setSelectedPackagingItemId(null)
                  // stage-36：用 section_id（跨 silent rebuild 稳定）作为选段主键。
                  // selectedGapId 由 sync useEffect 自动跟随，避免与 silent rebuild 抢写。
                  // stage-40：用 scene.parent_section_id 作为主键源——填好的段（如字卡画面
                  // 或已采纳的素材）没 gap，gap?.section_id 会是 undefined→null，再被 L597
                  // 的自动 fallback 误推到「第一个 miss/warn 段」，导致用户感觉点这段、
                  // 工作台显示别的段。
                  setSelectedSectionId(
                    scene.parent_section_id ?? gap?.section_id ?? null,
                  )
                  seekPlayer(scene.start)
                }}
                onSelectVoice={(scene) => {
                  setSelectedSceneId(scene.scene_id)
                  setSelectedPackagingItemId(null)
                  seekPlayer(scene.start)
                }}
                onSelectPackaging={(item) => {
                  setSelectedPackagingItemId(item.item_id)
                  setSelectedSceneId(null)
                  seekPlayer(item.start)
                }}
                onSynthesizeScene={handleSynthesizeScene}
                onSynthesizeAll={handleSynthesizeAll}
                onClearVoice={handleClearVoice}
                onDeletePackagingItem={handleDeletePackagingItem}
                onRecommendPackagingForScene={handleRecommendPackagingForScene}
                onPickBgm={() => setBgmPickerOpen(true)}
                onBgmAnchorChange={handleBgmAnchorChange}
                onClearBgm={handleClearBgm}
                onBgmVolumeChange={handleBgmVolumeChange}
                onToggleSubtitle={handleToggleSubtitle}
                onToggleVoiceover={handleToggleVoiceover}
                onChangeTtsVoice={handleChangeTtsVoice}
                busy={trackBusy}
                phase="content-only"
                contentTrackMode="sections"
                playheadSeconds={playheadSeconds}
                onSeek={seekPlayer}
                onResizePackagingItem={handleResizePackagingItem}
                onEditPackagingItem={(item) => {
                  setEditingPackagingItem(item)
                  setSelectedPackagingItemId(item.item_id)
                }}
                onEditTransition={(sceneId, currentStyle) =>
                  setEditingTransition({ sceneId, currentStyle })
                }
                onEditSection={(section, firstScene) =>
                  setEditingSection({ section, firstScene })
                }
                onEditShot={(scene, section) =>
                  setEditingShot({ scene, section })
                }
              />

          {/* stage-36：缺口补全工作台移到右列 FourTrackBoard 正下方。
              这样左侧实时预览 sticky 不滚走，用户切段后视线在 [选段 → 工作台 → 预览]
              一条短弧上，不用全宽来回滚——也避免了"切几次就找不到当前段在跑什么"。
              keepalive 多实例 display:none 切换，跨段后台跑。
              stage-38：keepalive 池**总是**渲染（不再被外层 `selectedGap ?` 包住），
              即使当前没选段或处于 silent rebuild 中间态，已访问过的 panel 也不会卸载，
              Seedance polling / spec / prompt 状态稳定保留。tabs / rerank / 空状态提示
              则按 selectedGap 走条件分支。 */}
          <div className="mt-3 space-y-2 border-t border-border pt-3">
            <p className="rounded-md border border-primary/30 bg-primary/5 px-2 py-1.5 text-[11px] leading-relaxed text-foreground">
              💡 这里只关心<strong>画面 + 字幕</strong>——四种方式都是给本段生成画面（挑素材 / 字卡画面 / AI 视频 / AI 生图再渲染）；字幕轨开关默认关闭，开启后 AI 自动按段落生成可编辑字幕。口播留到第 3 步再切换音色合成。
            </p>
            {selectedGap && (
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
                      <RerankStrategyPicker
                        gapBusy={gapBusy}
                        materials={sortedMaterials}
                        targetSection={selectedGap.section}
                        onPickManual={(materialId) =>
                          void runFill(selectedGap, 'rerank', {
                            strategy: 'manual',
                            target_material_id: materialId,
                          })
                        }
                      />
                    )}
                    {selectedFill && selectedFill.action === 'rerank' && (
                      <>
                        <RerankStrategyPicker
                          gapBusy={gapBusy}
                          materials={sortedMaterials}
                          targetSection={selectedGap.section}
                          currentMaterialId={selectedFill.new_material_id ?? null}
                          onPickManual={(materialId) =>
                            void runFill(selectedGap, 'rerank', {
                              strategy: 'manual',
                              target_material_id: materialId,
                            })
                          }
                        />
                        <FillRerankPanel
                          plan={plan}
                          fill={selectedFill}
                          materials={sortedMaterials}
                          onApply={handleRerankApply}
                          loading={gapBusy}
                        />
                      </>
                    )}
                  </>
                )}
              </>
            )}

            {/* keepalive 工作台：每个 (section_id, action) 一份 panel，留在 DOM
                不卸载。切段或切动作只切 display；本地 useState（生成阶段、
                seedance polling、表单草稿）跨切换不丢。
                关键：键用 section_id（rebuild 后稳定），运行时按 section_id 反查
                最新 gaps 拿到最新 gap_id——这样 silent rebuild 重写 gap_id
                后，panel 不会因为 find 失败而卸载（→ AI 出图 polling 不丢）。
                stage-38：lastSeenGapBySectionRef 兜底——`gaps.find` 临时未命中也仍能渲染。 */}
            {visitedFillKeys.size === 0 && !selectedGap && !selectedSectionId && (
              <p className="rounded-md border border-dashed border-border bg-background/30 px-3 py-2 text-[11px] text-muted-foreground">
                点上方内容轨任意一段——这里出现「挑素材 / 字卡画面 / AI 视频 / AI 生图再渲染」四个画面补全选项。
              </p>
            )}
            {selectedSectionId && !selectedGap && (
              <p className="rounded-md border border-dashed border-emerald-500/30 bg-emerald-500/5 px-3 py-2 text-[11px] leading-relaxed text-foreground">
                ✅ <strong>本段已填好</strong>——内容轨这一段没有空缺，不需要再用工作台补画面。
                <br />
                <span className="text-muted-foreground">
                  想替换本段画面？点上方内容轨该段的小镜（▾ 展开）打开「单镜编辑」弹窗，
                  里面可改 subject/visual/narration，或<strong>换源</strong>到用户素材 / AI 单图 / AI 视频 / 字卡。
                </span>
              </p>
            )}
            {Array.from(visitedFillKeys).map((key) => {
              const [sectionId, action] = key.split('::') as [string, FillAction]
              const gapForKey =
                gaps.find((g) => g.section_id === sectionId) ??
                gaps.find((g) => g.gap_id === sectionId) ?? // 老 gap 兜底（没 section_id）
                lastSeenGapBySectionRef.current.get(sectionId) ?? // stage-38：silent rebuild 兜底
                null
              if (!gapForKey) {
                // 首次进入还没见过这个 section 的 gap——render 一个隐藏占位 div
                // 保持 React key 稳定，避免后续命中时整段重新 mount。
                return <div key={key} style={{ display: 'none' }} aria-hidden />
              }
              const isActive =
                selectedGap?.section_id === sectionId && activeAction === action
              const fillForKey =
                fills.find(
                  (f) =>
                    (f.section_id === sectionId || f.gap_id === gapForKey.gap_id) &&
                    f.action === action,
                ) ?? null
              const onResult = async (f: FillResult) => {
                // 标记本段 busy（用 section_id 而非 gap_id，rebuild 后仍能定位）
                const sid = f.section_id ?? gapForKey.section_id ?? gapForKey.gap_id
                markBusy(sid, true)
                upsertFill(f)
                // stage-58：AIGC 生成产物（aigc_image/aigc_video）会被后端
                // _record_aigc_to_library 写进 material_store——必须立刻拉一次
                // /material 让左侧素材库出现新条目，否则用户要刷页面才能看到。
                if (f.status === 'ok' && (f.action === 'aigc' || f.action === 'aigc_image')) {
                  void refreshMaterials()
                }
                // 取 store 最新 snapshot（含其它并发面板刚 upsert 的 fill），避免
                // 用 closure 中的 stale `fills` 把别的段的成果覆盖掉。
                const latest = usePlanStore.getState().fills
                const nextFills = [...latest.filter((x) => x.gap_id !== f.gap_id), f]
                try {
                  await runAnalyze(nextFills, { silent: true })
                } finally {
                  markBusy(sid, false)
                }
              }
              return (
                <div
                  key={key}
                  style={{ display: isActive ? 'block' : 'none' }}
                  aria-hidden={!isActive}
                >
                  {action === 'copy' && (
                    <FillCopyPanel
                      gap={gapForKey}
                      fill={fillForKey}
                      plan={plan}
                      onResult={onResult}
                    />
                  )}
                  {action === 'aigc' && (
                    <FillAigcPanel
                      gap={gapForKey}
                      fill={fillForKey}
                      plan={plan}
                      onResult={onResult}
                    />
                  )}
                  {action === 'aigc_image' && (
                    <FillAigcPanel
                      gap={gapForKey}
                      fill={fillForKey}
                      plan={plan}
                      mode="image"
                      onResult={onResult}
                    />
                  )}
                </div>
              )
            })}
          </div>
            </div>
          </div>

          {/* 素材库（提升到中段，提升上传感受）：上传 / 拖拽排序 / 删除 → 自动重排并刷新缺口 */}
          <div className="space-y-2 border-t border-border pt-3">
            <div className="flex items-center justify-between gap-2">
              <h2 className="text-sm font-semibold">
                素材库（{sortedMaterials.length}）
                <span className="ml-2 text-[10px] font-normal text-muted-foreground">
                  上传或拖拽排序会自动重排并刷新缺口
                </span>
              </h2>
              {/* "+ 从素材库选取" / "+ 追加素材" 已移除——外部素材一律走下方拖拽区，避免歧义 */}
            </div>
            <UploadDropzone
              uploading={uploading}
              onDrop={(f) => void handlePickFiles(f)}
            />
            {sortedMaterials.length > 0 && (
              <MaterialGrid
                materials={sortedMaterials}
                onReorder={(orderedIds) => {
                  reorderMaterials(orderedIds)
                  if (runAnalyzeRef.current) void runAnalyzeRef.current(fills)
                }}
                onRemove={(id) => {
                  void handleDeleteMaterial(id).then(() => {
                    if (runAnalyzeRef.current) void runAnalyzeRef.current(fills)
                  })
                }}
              />
            )}
          </div>

          <div className="flex items-center justify-between gap-2 border-t border-border pt-3">
            <h2 className="text-sm font-semibold">
              适配结构（{plan.adapted_sections.length} 段 / 缺口 {gaps.length}
              {pendingGapsCount > 0 && (
                <span
                  className="ml-2 text-amber-500"
                  title="待补按镜级状态算：该段还有镜没被人工换源（badge 显示「已审」）也没被工作台采纳；换源或采纳后此数字自动归零。"
                >
                  待补 {pendingGapsCount}
                </span>
              )}
              ）
            </h2>
            <div className="flex items-center gap-2">
              {fills.length > 0 && (
                <span
                  className="text-[10px] text-muted-foreground"
                  title="工作台采纳记录：通过下方「补全工作台」点过「采纳」的 fill 数量。手动换源（SwapSourceDialog）不进这里——那走镜级 user_edited 标记。"
                >
                  工作台采纳 {fills.length}
                </span>
              )}
            </div>
          </div>

          {/* 两栏：左 段落编辑（补全的依据） / 右 缺口补全 tabs。
              v34：把右侧补全工作台从两栏抽出，挪到了 FourTrackBoard 正下方
              （一行宽，方便用户切段后直接看到所有四种补全方式）。
              stage-37：段落 / 单镜编辑迁到弹窗（点段块 ✏ 或展开后点小镜），
              彻底去掉这里多实例 keepalive 池——「前 N 段共享工作台」的痛源即此。
              这里仅剩包装段编辑——按 selection 自动展开。 */}
          <div className="grid gap-3">
            {selectedPackagingItem && (
              <SceneEditPanel
                key={`pkg-${selectedPackagingItem.item_id}`}
                plan={plan}
                selectedSceneId={effectiveSelectedSceneId}
                selectedPackagingItem={selectedPackagingItem}
                materials={sortedMaterials}
                onSaved={setPlanAndPush}
                disabled={analyzing || anyGapBusy || trackBusy}
              />
            )}

          </div>

          {/* 步骤 2 → 步骤 3 转换按钮（与步骤 1 → 步骤 2 同形式：主按钮 + 可选辅按钮） */}
          <div className="mt-3 flex flex-col gap-2 border-t border-border pt-3 sm:flex-row">
            <button
              type="button"
              onClick={() => {
                void handleEnterStep3()
              }}
              disabled={
                pendingGapsCount > 0 ||
                trackBusy ||
                mainTrackUnfilledCount > 0 ||
                analyzing
              }
              className={cn(
                'flex-1 rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground transition-colors',
                (pendingGapsCount > 0 ||
                  trackBusy ||
                  mainTrackUnfilledCount > 0 ||
                  analyzing) &&
                  'cursor-not-allowed opacity-60',
              )}
            >
              {analyzing
                ? '内容轨重排中…'
                : mainTrackUnfilledCount > 0
                  ? `内容轨还有 ${mainTrackUnfilledCount} 镜未补完 · 补齐后进入第 3 步`
                  : pendingGapsCount > 0
                    ? `还有 ${pendingGapsCount} 段缺口待补 · 补齐后进入第 3 步`
                    : trackBusy
                      ? '准备中…（重写口播 / 配音 / 转场推荐）'
                      : '进入第 3 步 → 解锁口播 / 包装 / BGM 与实时预览'}
            </button>
            {pendingGapsCount === 0 && mainTrackUnfilledCount === 0 && (
              <button
                type="button"
                onClick={() => setActiveStep(1)}
                className="rounded-md border border-border bg-card px-4 py-2 text-sm font-medium hover:bg-secondary"
              >
                ← 返回第 1 步重生成
              </button>
            )}
          </div>
        </section>
      )}

      {/* ============ Row 3：四轨工作台 —— 步骤 3 专属（全部三轨 + 实时预览）============ */}
      {activeStep === 3 && (
      <section className="mt-4">
        {plan ? (
          <>
            <div className="mb-2 flex items-center gap-2 rounded-md border border-border bg-background/40 px-3 py-1.5 text-[11px] text-muted-foreground">
              <span>第 3 步 · 实时预览与三轨已展开。包装方案入口已移到下方"包装轨"右上角。</span>
              <button
                type="button"
                onClick={() => setActiveStep(2)}
                className="ml-auto rounded-md border border-border bg-background px-2 py-0.5 text-[10px] hover:bg-secondary"
              >
                ← 返回第 2 步
              </button>
            </div>
            <EmotionCurveCard
              plan={plan}
              playheadSeconds={playheadSeconds}
              onPlanUpdate={setPlanAndPush}
              className="mb-2"
            />
            {plan.subject_anchors && plan.subject_anchors.length > 0 ? (
              <div className="mb-2 flex flex-wrap items-center gap-1.5 rounded-md border border-emerald-500/40 bg-emerald-50/60 px-2 py-1.5 text-[11px] dark:bg-emerald-950/30">
                <span className="font-medium text-emerald-900 dark:text-emerald-200">
                  🎯 锁定可拍物体
                </span>
                <span className="text-emerald-700/80 dark:text-emerald-300/80">
                  （澄清阶段已确认，每个物体在视频里至少出现 1 次）
                </span>
                {plan.subject_anchors.map((a) => (
                  <span
                    key={a}
                    className="inline-flex items-center rounded-full border border-emerald-600/50 bg-white px-2 py-0.5 text-[10px] font-medium text-emerald-800 dark:bg-emerald-950 dark:text-emerald-200"
                  >
                    {a}
                  </span>
                ))}
              </div>
            ) : null}
            <div className="mb-3 grid items-start gap-3 md:grid-cols-[minmax(0,280px)_1fr]">
              <div className="rounded-lg border border-border bg-card p-2 md:sticky md:top-4 md:self-start">
                <div className="mb-1.5 flex items-center justify-between px-1 text-[11px] text-muted-foreground">
                  <span className="font-medium">实时预览（无需等渲染）</span>
                  <span className="font-mono">{playheadSeconds.toFixed(1)}s / {plan.duration_seconds.toFixed(1)}s</span>
                </div>
                <PlanPlayer
                  ref={playerRef}
                  plan={plan}
                  materials={sortedMaterials}
                  onTimeUpdate={setPlayheadSeconds}
                />
              </div>
              <FourTrackBoard
                plan={plan}
                gaps={gaps}
                filledGapIds={filledGapIds}
                selectedGapId={selectedGapId}
                selectedSceneId={effectiveSelectedSceneId}
                selectedPackagingItemId={selectedPackagingItemId}
                materials={sortedMaterials}
                fills={fills}
                referenceManifests={[effectiveManifest, secondaryManifest].filter(
                  (m): m is SampleManifest => !!m,
                )}
                onSelectScene={(scene, gap) => {
                  setSelectedSceneId(scene.scene_id)
                  setSelectedPackagingItemId(null)
                  // stage-36：用 section_id 作为选段主键，selectedGapId 由 sync useEffect 跟随。
                  // stage-40：parent_section_id 优先——填好的段没 gap，避免落到 null 被 fallback 推走。
                  setSelectedSectionId(
                    scene.parent_section_id ?? gap?.section_id ?? null,
                  )
                  seekPlayer(scene.start)
                }}
                onSelectVoice={(scene) => {
                  setSelectedSceneId(scene.scene_id)
                  setSelectedPackagingItemId(null)
                  setEditingSubtitleScene(scene)
                  seekPlayer(scene.start)
                }}
                onEditSubtitle={(scene) => {
                  setEditingSubtitleScene(scene)
                  seekPlayer(scene.start)
                }}
                onSelectPackaging={(item) => {
                  setSelectedPackagingItemId(item.item_id)
                  setSelectedSceneId(null)
                  seekPlayer(item.start)
                }}
                onSynthesizeScene={handleSynthesizeScene}
                onSynthesizeAll={handleSynthesizeAll}
                onClearVoice={handleClearVoice}
                onDeletePackagingItem={handleDeletePackagingItem}
                onRecommendPackagingForScene={handleRecommendPackagingForScene}
                onPickBgm={() => setBgmPickerOpen(true)}
                onBgmAnchorChange={handleBgmAnchorChange}
                onClearBgm={handleClearBgm}
                onBgmVolumeChange={handleBgmVolumeChange}
                onToggleSubtitle={handleToggleSubtitle}
                onToggleVoiceover={handleToggleVoiceover}
                onChangeTtsVoice={handleChangeTtsVoice}
                busy={trackBusy}
                phase={pendingGapsCount === 0 ? 'full' : 'content-only'}
                playheadSeconds={playheadSeconds}
                onSeek={seekPlayer}
                onResizePackagingItem={handleResizePackagingItem}
                onEditPackagingItem={(item) => {
                  setEditingPackagingItem(item)
                  setSelectedPackagingItemId(item.item_id)
                  seekPlayer(item.start)
                }}
                onEditTransition={(sceneId, currentStyle) =>
                  setEditingTransition({ sceneId, currentStyle })
                }
              />
            </div>
          </>
        ) : (
          <div className="rounded-md border border-dashed border-border bg-background/30 p-6 text-center text-xs text-muted-foreground">
            还没生成 plan。回到第 1 步填写主题，点「生成内容轨」开始。
          </div>
        )}
      </section>
      )}

      {/* ============ Row 4：（已移除）原本的「分镜预览」与 FourTrackBoard 主轨展示重复，
          统一由 FourTrackBoard 内容轨承载，结构对照改用顶部常驻的 StructureMapPanel。 ============ */}

      {/* ============ Row 5：（已移除）原本的全局 NLEditPanel 已下沉到 SceneEditPanel 内、跟随段落选择。
          渲染流程里 Row 9 那块 NLEditPanel（lockedTracks=['main']）保留，用于成片后改包装 / 口播。 ============ */}

      {/* ============ Row 6：一键生成视频（步骤 3 专属）============ */}
      {activeStep === 3 && plan && (
        <section className="mt-6 space-y-3 rounded-lg border border-border bg-card p-4">
          <div className="flex flex-wrap items-center gap-3">
            <h2 className="text-sm font-semibold">生成视频</h2>
            <button
              onClick={() => void handleProceedToRender()}
              disabled={
                analyzing ||
                anyGapBusy ||
                isRendering ||
                finalizing === 'filling-gaps'
              }
              title="先用文案补全所有未补缺口，再直接渲染成片"
              className={cn(
                'rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90',
                (analyzing ||
                  anyGapBusy ||
                  isRendering ||
                  finalizing === 'filling-gaps') &&
                  'cursor-not-allowed opacity-60',
              )}
            >
              {finalizing === 'filling-gaps' && '补全剩余缺口中…'}
              {isRendering && `渲染中 · ${renderPercent}%`}
              {!isRendering &&
                finalizing !== 'filling-gaps' &&
                (renderDone ? '重新生成视频' : '一键生成视频')}
            </button>
            <button
              onClick={() => setActiveStep(2)}
              disabled={finalizing === 'filling-gaps'}
              className="rounded-md border border-border bg-card px-3 py-2 text-xs font-medium hover:bg-secondary disabled:opacity-60"
            >
              ← 返回第 2 步调整内容
            </button>
            {!isRendering && finalizing === 'idle' && !renderDone && (
              <span className="text-[11px] text-muted-foreground">
                {pendingGapsCount > 0
                  ? `还有 ${pendingGapsCount} 段缺口未补，请回到第 2 步逐段补齐再渲染`
                  : '所有缺口已补，将直接渲染成片'}
              </span>
            )}
          </div>

          {renderError && (
            <div className="rounded-md border border-destructive/40 bg-destructive/5 px-3 py-2 text-xs text-destructive">
              {renderError}
            </div>
          )}

          {/* 渲染进度（极简内联条）+ 结果视频 */}
          {(isRendering || renderDone) && (
            <RenderProgress step={renderStep} percent={renderPercent} />
          )}
          {renderDone && <RenderResult done={renderDone} />}
        </section>
      )}

      {/* ============ Row 7：撤销 / 重做（步骤 2 / 3）——保存版本已统一到顶部 VersionMenu ============ */}
      {(activeStep === 2 || activeStep === 3) && plan && (
        <section className="mt-4 flex flex-col items-end gap-2">
          <div className="flex items-center gap-2">
            <span className="text-xs text-muted-foreground">
              步操作 {Math.max(editCursor + 1, 0)}/{editHistory.length}
            </span>
            <button
              onClick={handleUndo}
              disabled={!canUndo}
              className={cn(
                'rounded-md border border-border bg-background px-3 py-1 text-xs hover:bg-secondary',
                !canUndo && 'cursor-not-allowed opacity-40',
              )}
            >
              ↶ 撤销
            </button>
            <button
              onClick={handleRedo}
              disabled={!canRedo}
              className={cn(
                'rounded-md border border-border bg-background px-3 py-1 text-xs hover:bg-secondary',
                !canRedo && 'cursor-not-allowed opacity-40',
              )}
            >
              重做 ↷
            </button>
          </div>
        </section>
      )}

      {/* 样例截图弹窗已废弃：选中内容轨改为直接切换右侧编辑区，不再弹窗。 */}

      {/* 系统素材库选择器 —— step1 素材库标题栏「+ 从素材库选取」按钮触发，
          从共享 __system__ 项目挑素材克隆到当前项目，扩充本项目素材库。 */}
      <SystemLibraryPicker
        open={systemLibraryOpen}
        projectId={currentProjectId}
        onClose={() => setSystemLibraryOpen(false)}
        onCloned={(materials) => appendMaterials(materials)}
      />

      {/* BGM 选择 / 上传弹窗 */}
      {plan && currentProjectId && (
        <BgmPickerDialog
          open={bgmPickerOpen}
          onClose={() => setBgmPickerOpen(false)}
          projectId={currentProjectId}
          planId={plan.plan_id}
          onPlanUpdated={setPlanAndPush}
        />
      )}

      {/* 字幕浮窗（R3）：step3 字幕轨某段被点击 → 手动改 narration */}
      {plan && (
        <SubtitleEditPopover
          open={!!editingSubtitleScene}
          scene={editingSubtitleScene}
          planId={plan.plan_id}
          onClose={() => setEditingSubtitleScene(null)}
          onPlanUpdated={setPlanAndPush}
        />
      )}

      {/* PR-I.2 包装组件编辑弹窗：点击包装轨上 title_bar/sticker/cover → 改文案/时间/样式 */}
      {plan && (
        <PackagingItemEditDialog
          open={!!editingPackagingItem}
          item={editingPackagingItem}
          planId={plan.plan_id}
          onClose={() => setEditingPackagingItem(null)}
          onPlanUpdated={setPlanAndPush}
        />
      )}

      {/* PR-I.2 转场样式弹窗：点击包装轨上分镜之间的 ⇆ 节点 */}
      {plan && (
        <TransitionStylePicker
          open={!!editingTransition}
          sceneId={editingTransition?.sceneId ?? null}
          currentStyle={editingTransition?.currentStyle ?? null}
          planId={plan.plan_id}
          onClose={() => setEditingTransition(null)}
          onPlanUpdated={setPlanAndPush}
        />
      )}

      {/* stage-37 段编辑弹窗：内容轨段块上 ✏ 按钮 → 改 theme + content_description */}
      {plan && (
        <SectionEditDialog
          plan={plan}
          section={editingSection?.section ?? null}
          firstScene={editingSection?.firstScene ?? null}
          onClose={() => setEditingSection(null)}
          onSaved={(fresh) => {
            setPlanAndPush(fresh)
            setEditingSection(null)
          }}
          disabled={analyzing || anyGapBusy || trackBusy}
        />
      )}

      {/* stage-37 单镜编辑弹窗：段块展开 ▾ 后点分镜小块 → 改 subject / visual / narration */}
      {plan && (
        <ShotEditDialog
          plan={plan}
          scene={editingShot?.scene ?? null}
          section={editingShot?.section ?? null}
          materials={sortedMaterials}
          onClose={() => setEditingShot(null)}
          onSaved={(fresh) => {
            setPlanAndPush(fresh)
            setEditingShot(null)
          }}
          disabled={analyzing || anyGapBusy || trackBusy}
        />
      )}

      {/* ⌘K 自然语言编辑（R6）：step2/step3 各自的可编辑范围由后端决定 */}
      {plan && (activeStep === 2 || activeStep === 3) && (
        <ComposeCommandBar
          open={commandBarOpen}
          onClose={() => setCommandBarOpen(false)}
          planId={plan.plan_id}
          step={activeStep === 2 ? 'step2' : 'step3'}
          projectId={currentProjectId ?? ''}
          onApplied={setPlanAndPush}
        />
      )}

      {/* ⌘K 浮动入口（R6 可发现性）：step2/step3 可见，可拖动并把位置写 localStorage。 */}
      {plan && (activeStep === 2 || activeStep === 3) && !commandBarOpen && (
        <DraggableCommandFab onClick={() => setCommandBarOpen(true)} />
      )}

      {/* 结构迁移示意 · 放大模态：全屏看 react-flow */}
      {structureZoomOpen && effectiveManifest && plan && (
        <div
          role="dialog"
          aria-modal="true"
          onClick={() => setStructureZoomOpen(false)}
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4"
        >
          <div
            onClick={(e) => e.stopPropagation()}
            className="flex h-[80vh] w-full max-w-6xl flex-col overflow-hidden rounded-lg border border-border bg-card shadow-xl"
          >
            <div className="flex items-center justify-between border-b border-border px-4 py-2">
              <h3 className="text-sm font-semibold">结构迁移：样例 → 新方案</h3>
              <button
                onClick={() => setStructureZoomOpen(false)}
                className="rounded text-muted-foreground hover:text-foreground"
                aria-label="关闭"
              >
                ✕
              </button>
            </div>
            <div className="flex-1 min-h-0 p-3">
              <StructureMapPanel
                className="h-full"
                manifests={[effectiveManifest, secondaryManifest].filter(
                  (m): m is SampleManifest => !!m,
                )}
                plan={plan}
                gaps={gaps}
              />
            </div>
          </div>
        </div>
      )}

    </PageShell>
  )
}

/* ---------- 子组件 ---------- */

type WorkshopStep = 1 | 2 | 3

function StructureCompareSection({
  manifest,
  secondaryManifest,
  plan,
  gaps,
  onZoom,
}: {
  manifest: SampleManifest
  secondaryManifest: SampleManifest | null
  plan: Plan
  gaps: Gap[]
  onZoom: () => void
}) {
  const [expanded, setExpanded] = useState(true)
  const manifests = secondaryManifest ? [manifest, secondaryManifest] : [manifest]
  return (
    <section className="rounded-lg border border-border bg-card">
      <div className="flex items-center justify-between border-b border-border px-4 py-2">
        <div className="flex flex-wrap items-center gap-2">
          <h3 className="text-sm font-semibold">
            结构迁移：{secondaryManifest ? '样例1 → 新方案 ← 样例2' : '样例 → 新方案'}
          </h3>
          <span className="text-[11px] font-normal text-muted-foreground">
            {secondaryManifest
              ? '左右两个样例同时对位中间新方案，连线颜色对应同一段'
              : '左边样例段落、右边新方案段落，字段类目对齐'}
          </span>
        </div>
        <div className="flex items-center gap-1">
          <button
            type="button"
            onClick={() => setExpanded((v) => !v)}
            className="rounded-md border border-border bg-background px-2 py-1 text-[11px] hover:bg-secondary"
            title={expanded ? '收起结构迁移' : '展开结构迁移'}
          >
            {expanded ? '▾ 收起' : '▸ 展开'}
          </button>
          {expanded && (
            <button
              type="button"
              onClick={onZoom}
              className="rounded-md border border-border bg-background px-2 py-1 text-[11px] hover:bg-secondary"
            >
              放大 ⤢
            </button>
          )}
        </div>
      </div>
      {expanded && (
        <div className="h-[280px] p-2">
          <StructureMapPanel className="h-full" manifests={manifests} plan={plan} gaps={gaps} />
        </div>
      )}
    </section>
  )
}

function WorkshopStepNav({
  activeStep,
  hasReferences,
  briefFilled,
  hasPlan,
  step3Unlocked,
  pendingGapsCount,
  mainTrackUnfilledCount,
  onChange,
}: {
  activeStep: WorkshopStep
  hasReferences: boolean
  briefFilled: boolean
  hasPlan: boolean
  step3Unlocked: boolean
  pendingGapsCount: number
  /** stage-26 PR-N.6：plan.main_track 里还有几段 needs_fill / fill-empty 占位。>0 时禁止进 step3。 */
  mainTrackUnfilledCount: number
  onChange: (step: WorkshopStep) => void
}) {
  const step2Reason = !hasReferences
    ? '请先在第 1 步选择参考样例'
    : !briefFilled
      ? '请先在第 1 步填写主题'
      : !hasPlan
        ? '先点「生成内容轨」生成 plan'
        : ''
  const step3Reason = !hasPlan
    ? '先在第 2 步生成内容轨'
    : mainTrackUnfilledCount > 0
      ? `内容轨还有 ${mainTrackUnfilledCount} 镜未补完，请先在第 2 步补齐再进入第 3 步`
      : !step3Unlocked
        ? '请在第 2 步底部点「进入第 3 步」解锁'
        : pendingGapsCount > 0
          ? `还有 ${pendingGapsCount} 个缺口未补，可继续进入第 3 步自动补全`
          : ''

  const step3Disabled =
    !hasPlan || !step3Unlocked || mainTrackUnfilledCount > 0

  const steps: { id: WorkshopStep; title: string; sub: string; disabled: boolean; tip: string }[] = [
    {
      id: 1,
      title: '1 · 选参考 + 写主题',
      sub: '挑样例、填主题、上传素材',
      disabled: false,
      tip: '',
    },
    {
      id: 2,
      title: '2 · 生成内容轨',
      sub: '改编结构、补缺口、随时上传素材重排',
      disabled: !hasReferences || !briefFilled || !hasPlan,
      tip: step2Reason,
    },
    {
      id: 3,
      title: '3 · 多轨 + 出片',
      sub: '包装轨 + 口播配音 + 一键生成视频',
      disabled: step3Disabled,
      tip: step3Reason,
    },
  ]

  return (
    <nav className="mb-4 grid grid-cols-3 gap-2">
      {steps.map((s) => {
        const active = s.id === activeStep
        const clickable = !s.disabled || active
        return (
          <button
            key={s.id}
            onClick={() => !s.disabled && onChange(s.id)}
            disabled={!clickable}
            title={s.disabled ? s.tip : ''}
            className={cn(
              'rounded-lg border px-3 py-2 text-left transition-colors',
              active
                ? 'border-primary bg-primary/10'
                : clickable
                  ? 'border-border bg-card hover:bg-secondary/60'
                  : 'cursor-not-allowed border-dashed border-border bg-background/30 opacity-60',
            )}
          >
            <div
              className={cn(
                'text-sm font-semibold',
                active ? 'text-primary' : 'text-foreground',
              )}
            >
              {s.title}
            </div>
            <div className="mt-0.5 text-[11px] text-muted-foreground">{s.sub}</div>
            {s.disabled && s.tip && (
              <div className="mt-1 text-[10px] text-amber-600 dark:text-amber-400">{s.tip}</div>
            )}
          </button>
        )
      })}
    </nav>
  )
}

function RenderProgress({ step, percent }: { step: string; percent: number }) {
  const currentIdx = RENDER_STEP_ORDER.indexOf(step as (typeof RENDER_STEP_ORDER)[number])
  return (
    <div>
      <div className="mb-1 flex items-center justify-between text-xs">
        <span className="font-mono text-muted-foreground">
          {step === 'idle' ? '待命' : step === 'done' ? '完成' : (RENDER_STEP_LABELS[step] ?? step)}
        </span>
        <span className="font-mono text-muted-foreground">{percent}%</span>
      </div>
      <div className="h-1.5 overflow-hidden rounded-full bg-secondary">
        <div
          className="h-full bg-primary transition-all duration-300"
          style={{ width: `${Math.min(100, percent)}%` }}
        />
      </div>
      <ol className="mt-2 grid grid-cols-3 gap-1 text-[10px] sm:grid-cols-6">
        {RENDER_STEP_ORDER.map((s, i) => (
          <li
            key={s}
            className={cn(
              'rounded border px-1 py-0.5 text-center',
              i < currentIdx
                ? 'border-emerald-500/40 bg-emerald-500/10 text-emerald-700 dark:text-emerald-300'
                : i === currentIdx
                  ? 'border-primary bg-primary/10 text-primary'
                  : 'border-border bg-background text-muted-foreground',
            )}
          >
            {RENDER_STEP_LABELS[s]}
          </li>
        ))}
      </ol>
    </div>
  )
}

function RenderResult({ done }: { done: RenderDonePayload }) {
  // 文件名规则：seecript-{plan-12位}-{variant}.{ext}——保证用户从浏览器下载下来一眼能认出
  // 哪个 plan、哪个版本，本地多版本对比时不会撞名。
  const safePlanId = done.plan_id.replace(/[^a-zA-Z0-9_-]/g, '').slice(0, 12) || 'plan'
  const videoFilename = `seecript-${safePlanId}-${done.variant}.mp4`
  const coverFilename = `seecript-${safePlanId}-${done.variant}-cover.jpg`
  const [copied, setCopied] = useState(false)
  const copyVideoLink = async () => {
    try {
      // 拼成绝对 URL，方便用户贴到别处直接下载
      const absUrl = new URL(done.video_url, window.location.origin).toString()
      await navigator.clipboard.writeText(absUrl)
      setCopied(true)
      setTimeout(() => setCopied(false), 1800)
    } catch {
      /* 复制失败静默——保留视频与新标签按钮作为兜底 */
    }
  }
  return (
    <div className="mt-2 space-y-3">
      <video
        controls
        poster={done.cover_url}
        src={done.video_url}
        className="w-full rounded-md border border-border bg-black"
      />
      <div className="flex flex-wrap items-center gap-2">
        <a
          href={done.video_url}
          download={videoFilename}
          className={cn(
            'inline-flex items-center gap-1 rounded-md bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground',
            'hover:bg-primary/90',
          )}
        >
          ⬇ 下载 MP4
        </a>
        {done.cover_url && (
          <a
            href={done.cover_url}
            download={coverFilename}
            className="inline-flex items-center gap-1 rounded-md border border-border bg-background px-3 py-1.5 text-xs hover:bg-secondary/40"
          >
            ⬇ 下载封面
          </a>
        )}
        <button
          type="button"
          onClick={copyVideoLink}
          className="inline-flex items-center gap-1 rounded-md border border-border bg-background px-3 py-1.5 text-xs hover:bg-secondary/40"
        >
          {copied ? '✓ 已复制' : '复制视频链接'}
        </button>
        <a
          href={done.video_url}
          target="_blank"
          rel="noreferrer"
          className="inline-flex items-center gap-1 rounded-md border border-border bg-background px-3 py-1.5 text-xs hover:bg-secondary/40"
        >
          新标签打开
        </a>
      </div>
      <div className="grid grid-cols-2 gap-2 text-xs sm:grid-cols-4">
        <Stat label="时长" value={`${done.duration_seconds.toFixed(1)}s`} />
        <Stat label="版本" value={done.variant} />
        <Stat label="方案编号" value={done.plan_id} mono />
        <Stat
          label="总耗时"
          value={
            done.timings_ms
              ? `${Math.round(Object.values(done.timings_ms).reduce((a, b) => a + b, 0))} ms`
              : '—'
          }
        />
      </div>
      {done.timings_ms && (
        <details className="text-xs">
          <summary className="cursor-pointer text-muted-foreground">分步耗时</summary>
          <ul className="mt-2 grid grid-cols-2 gap-1 font-mono">
            {Object.entries(done.timings_ms).map(([k, v]) => (
              <li key={k} className="flex justify-between rounded bg-background/50 px-2 py-1">
                <span>{RENDER_STEP_LABELS[k] ?? k}</span>
                <span>{Math.round(v)} ms</span>
              </li>
            ))}
          </ul>
        </details>
      )}
      {done.notes && done.notes.length > 0 && (
        <details className="text-xs">
          <summary className="cursor-pointer text-muted-foreground">
            流水线日志（{done.notes.length}）
          </summary>
          <ul className="mt-2 space-y-0.5 font-mono text-[11px] text-muted-foreground">
            {done.notes.map((n, i) => (
              <li key={i}>· {n}</li>
            ))}
          </ul>
        </details>
      )}
    </div>
  )
}

function Stat({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  return (
    <div className="rounded-md border border-border bg-background/40 px-2 py-1.5">
      <div className="text-[10px] uppercase tracking-wider text-muted-foreground">{label}</div>
      <div className={cn('truncate text-sm', mono && 'font-mono')}>{value}</div>
    </div>
  )
}

function UploadDropzone({
  onDrop,
  uploading,
}: {
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
      className={cn(
        'flex h-24 items-center justify-center rounded-md border-2 border-dashed text-xs transition-colors',
        hover ? 'border-primary bg-primary/5' : 'border-border bg-background/40',
        uploading && 'pointer-events-none opacity-60',
      )}
    >
      <span className="text-muted-foreground">
        {uploading ? '上传中…' : '拖拽 video / image / audio 到这里上传（≤ 50MB / 个）'}
      </span>
    </div>
  )
}

// 未拆解样例拦截弹窗已移除——stage-15 用 ReferencePicker 取代该 gate:
// 资产库为空时 ReferencePicker 直接引导去素材库,不再有"选了样例又没拆解"的中间态。

/**
 * stage-76（2026-06-12）说明卡：
 * 用户原话「step2 一开始不要直接填入素材了，都让用户自己去选择」+
 * 「我现在只让你对真实素材做切片，不要做其他处理」。
 * 第一次进 step2 弹出，用户点"知道了"后写 localStorage，之后不再出现。
 */
const STEP2_PLACEHOLDER_HINT_KEY = 'seecript.step2.placeholder.dismissed.v1'

function Step2PlaceholderHint() {
  const [dismissed, setDismissed] = useState<boolean>(() => {
    try {
      return localStorage.getItem(STEP2_PLACEHOLDER_HINT_KEY) === '1'
    } catch {
      return false
    }
  })
  if (dismissed) return null
  return (
    <div className="flex items-start gap-3 rounded-md border border-amber-300 bg-amber-50 px-3 py-2 text-xs text-amber-900 dark:border-amber-700 dark:bg-amber-900/30 dark:text-amber-100">
      <span className="select-none text-base leading-none">💡</span>
      <div className="flex-1 leading-relaxed">
        <p className="font-medium">真实素材已解析，请在分镜上手动挑选切片</p>
        <p className="mt-0.5 text-amber-900/80 dark:text-amber-100/80">
          内容轨当前显示的是按样例规划的占位卡片；点击任一分镜卡 →「换源」即可挑选你的素材切片，
          所选切片的时长会直接覆盖该分镜（其它分镜自动顺移）。
        </p>
      </div>
      <button
        type="button"
        onClick={() => {
          try {
            localStorage.setItem(STEP2_PLACEHOLDER_HINT_KEY, '1')
          } catch {
            /* localStorage 不可用时仍允许本次会话内关闭 */
          }
          setDismissed(true)
        }}
        className="shrink-0 rounded border border-amber-400/60 bg-white/60 px-2 py-0.5 text-[11px] font-medium hover:bg-white dark:border-amber-500/40 dark:bg-amber-950/40 dark:hover:bg-amber-950/60"
      >
        知道了
      </button>
    </div>
  )
}
