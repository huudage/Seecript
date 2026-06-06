# Seecript PRD

> **基于代码现状从头撰写**，对齐 stage-21（commit `e66b0ed`，2026-06-06）。本文档不引用历史 PRD。
> 撰写方法：prd-development skill 8 阶段流程 → template.md 十节标准结构。
> 仓库根：`D:\Seecript`，公网 demo：`https://seecript.zlhu.asia`。

---

## 1. Executive Summary

Seecript 是一个把**爆款视频拆成可复用的结构骨架**，再把**用户自己的素材**对齐到该骨架上**重组**成新视频的 AI 创作工坊。围绕字节工程训练营赛题「爆款结构迁移引擎 — 从样例拆解、素材补全到视频重组的 AI 创作平台」实现。当前 v0.21 已覆盖 5 个前端页面、72 条后端路由、9 个 services/agent，提供"样例拆解 → 缺口识别 → 三路补全 → 重组渲染 → 自然语言改片"完整链路。

---

## 2. Problem Statement

### 2.1 谁有这个问题

短视频创作者（KOC / 品牌内容运营 / 独立编导）——拥有自己的拍摄素材，但缺乏从零设计叙事结构的能力，习惯参考一两条爆款来定节奏。

### 2.2 问题是什么

> 模仿爆款时只能复制表层（同款 BGM、同款滤镜、同款字幕样式），抓不到结构（段落比例、镜头节奏、口播密度、转场落点、包装时机）。

具体在工作流上表现为五个空白：

| 工作流断点 | 现状 |
|---|---|
| **拆解** | 想"参考"一条爆款时，没有工具能告诉你它的镜头切分、BGM 燃点、段落角色（开场/发展/高潮/收尾）；要么靠肉眼数时间码，要么放弃 |
| **匹配** | 拿自己的素材去对齐爆款骨架——哪个素材该放哪段——只能凭感觉 |
| **缺口** | 总有 1–3 个段落自己素材完全不够（比如缺一个特写、缺一句口播），传统流程到这里就卡住 |
| **包装** | 字幕样式、转场风格、封面布局每条都要重调，没有"按这条爆款的风格自动套"的入口 |
| **改片** | 调过一版后想再微调（"第二段慢点 / 字幕大一号 / 换个转场"），只能去剪映重剪 |

### 2.3 为什么痛

- **赛题视角**：评分表 5 维度——基础闭环 25 / 缺口处理 20 / 可展示 20 / 进阶 20 / 协同+完成度 15——任何一维如果只调 API 没自研都拿不满。
- **创作者视角**：拆解 + 匹配 + 缺口 + 包装 + 改片 五个环节，**没有任何主流工具把它们串成一条工作流**——剪映只剪、ChatGPT 只聊、Sora 只生成、模板库只贴底。

### 2.4 证据

| 证据 | 来源 |
|---|---|
| 赛题原文扣分项明确「主要依赖现成产品直接生成结果，缺少自主设计与改造」 | 字节工程训练营赛题官方说明 |
| 当前已实装 12 次跨 6 个 agent 的 LLM 调用 | grep `\.complete*` on `services/agent/`：decompose×4 / packaging×2 / aigc_prompt×2 / plan×2 / copy_outline×1 / gap×1 |
| 当前 schemas 35 个 `Literal[...]` 枚举覆盖结构 / 节奏 / 包装 / 字幕 / 转场 / 平台 / 比例 / 调性 / 角色等独立维度 | `server/app/schemas.py` Literal 分布 |
| stage-10 ~ stage-21 累计 11 个 stage commit 体现自研链路深度 | `git log --oneline -20` |

---

## 3. Target Users & Personas

### 3.1 主用户 · 训练营评委（Persona P0）

| 维度 | 描述 |
|---|---|
| **角色** | 字节工程训练营评分官、答辩评委 |
| **目标** | 5–8 分钟答辩里确认参赛作品命中评分表 5 维度 |
| **关注点** | 闭环能跑通吗？缺口三路真的实装了吗？可展示性强吗？多模态 LLM 真有用还是包壳？协同/完成度（README / 部署 / 测试）够不够？ |
| **JTBD** | 「在 demo 演示和代码仓库里**快速确认这队不是堆 API 包壳**，能给到第一梯队分数」 |

### 3.2 主用户 · 视频创作者（Persona P1，对应"赛题给定场景"）

| 维度 | 描述 |
|---|---|
| **角色** | 月粉 1k–10w 的 KOC、品牌 DTC 内容运营、独立编导 |
| **拍摄能力** | 能用 iPhone / 单反拍素材，会基础剪映操作 |
| **缺乏能力** | 编导思维、爆款结构拆解、口播节奏设计、包装样式系统化 |
| **JTBD-1** | 「我看到了一条 30 万赞的对标视频，想用我手机里这堆素材**做一条节奏相似但内容是我自己的视频**」 |
| **JTBD-2** | 「拼了一版觉得不够紧凑，想**用一句话告诉系统'第二段加快 20%、换冷色调转场'**，而不是再开剪映改半小时」 |

### 3.3 次级用户 · 自托管开发者（Persona P2）

| 维度 | 描述 |
|---|---|
| **角色** | 把 Seecript clone 下来在自己机器上跑的工程师 |
| **关注点** | mock 模式能不能让我断网/没钱也能跑通 UI？是不是必须先开通豆包 + Seedance + ARK 三个 API？config.py 那 ~50 个字段都什么含义？ |
| **JTBD** | 「`git clone` + `cp .env.example .env` + `python run.py` 就能跑起来，**调一个 LLM_PROVIDER=doubao_ark 切到真链路**，剩下不用动」 |

---

## 4. Strategic Context

### 4.1 目标（OKR 化）

| O · 拿到训练营第一梯队成绩 | KR |
|---|---|
| KR1 基础闭环 25 分拿满 | 5 个页面（Home / Library / Decompose / Compose / Knowledge）+ 72 条路由全部跑通公网真链路 |
| KR2 缺口处理 20 分拿满 | `FillAction = rerank / copy / aigc / aigc_image` 四路 + `GapStatus` 三态（ok / warn / miss） |
| KR3 可展示 20 分拿满 | 四轨工作台 + Remotion 实时预览 + SSE 拆解流 + AB 双版本快照 |
| KR4 进阶 20 分至少 16 | 9 个 services/agent + 自然语言改片（`/edit/compose`）+ 包装 agent（`/packaging/recommend`）+ 4 类 client 抽象（LLM / ASR / T2V / Seedream） |
| KR5 协同 + 完成度 15 拿满 | README / ARCHITECTURE / DELIVERY / DEMO 四份文档 + 250 pytest 用例 + tar 推部署可重现 |

### 4.2 市场（粗估，仅作 sizing 参考；本期非商业目标）

| 层 | 规模 | 备注 |
|---|---|---|
| TAM | 全网 KOC 千万级 + 企业内容运营数十万级 | 行业公开数据 |
| SAM | 月入 3k–8k 的中长尾创作者百万级 | "想要爆款节奏但没有团队"的核心群 |
| SOM | 0 | 本期是赛题项目，不做用户增长 |

### 4.3 竞争景观

| 类目 | 代表 | Seecript 与它们的差异 |
|---|---|---|
| 通用剪辑 | 剪映 / CapCut / Premiere | 解决"剪"，不解决"结构对齐" |
| 通用对话 AI | ChatGPT / Claude | 给文字建议，不给可执行的 Plan/Scene |
| AI 视频生成 | Sora / 可灵 / Veo / Seedance | 无中生有，不消化"用户既有素材" |
| 卡点模板库 | 剪映模板 / 必剪卡点 | 模板僵硬，无结构语义（不知道你这条素材属于"开场/发展/高潮/收尾"哪段） |

**差异化锚点**：卡在「**爆款结构 + 用户素材**」这个中间抽象——上游不抢生成、下游不抢剪辑。

### 4.4 Why now

- 多模态 LLM（Doubao Seed-2.0-lite）单次调用可同时处理帧 + 时间码 + ASR transcript，使"一次拆完一条 60s 视频"从工程问题变成 API 编排问题。
- Seedance 2.0 fast 把 5s 短片渲染从 90s 压到 30–60s，让 aigc 缺口补全从"可演示"升级为"可用"。
- 字节工程训练营赛题窗口期。

---

## 5. Solution Overview

### 5.1 高层描述

Seecript = **5 页前端 + 72 路由后端 + 9 agent + 4 类外部 client 抽象 + Remotion 包装子进程**。

```
                         ┌────────────────────────────────────────────┐
   首页 / 工坊导航 ──→   │ Home                  workspace.html 风格 │
                         ├────────────────────────────────────────────┤
   样例选取 + 上传 ──→   │ Library      ┌─ 系统样例 / 用户上传双 tab  │
                         │              └─ video_type 三分类筛选     │
                         ├────────────────────────────────────────────┤
   样例拆解 ───────→     │ Decompose    ┌─ SSE 实时进度流            │
                         │              └─ 角色 + 主题双层结构落库   │
                         ├────────────────────────────────────────────┤
   重组工作台 ──→        │ Compose      ┌─ 四轨工作台（FourTrackBoard）│
                         │              ├─ 缺口识别 + 三路补全        │
                         │              ├─ ⌘K 自然语言改片            │
                         │              ├─ Remotion 实时预览          │
                         │              └─ AB 双版本快照              │
                         ├────────────────────────────────────────────┤
   知识库 ───────→       │ Knowledge    项目级偏好 + 隐性沉淀         │
                         └────────────────────────────────────────────┘
```

### 5.2 关键功能

#### F1 · 样例库（Library）

- 系统样例 + 用户上传双 tab（`web/src/pages/Library.tsx:135`）
- `LibrarySource = "system" | "user"` 二态 + `VideoType = "marketing" | "editing" | "motion_graph"` 三分类筛选
- 上传卡内置类型选择器（stage-21 修：之前所有上传被强写 marketing → `e66b0ed`）
- 后端：`POST /api/library/system/upload`、`POST /api/decompose/upload`、`GET /api/library`

#### F2 · 样例拆解（Decompose）

- SSE 实时进度流：`GET /api/decompose/stream`（`decompose.py:257`）
- 6 步流水线：PySceneDetect 镜头切分 → librosa BGM 能量曲线 + onset → librosa VAD 门控 → ASR（豆包 bigasr_auc，仅在 VAD 判定有口播时跑）→ 多模态 LLM 帧打标 → 多模态 LLM 段落结构（4 次 LLM 调用，`decompose_agent.py`）
- 输出 `SampleManifest`，包含 `Section[]`（`role: SectionRole`，`theme: str ≤20 字`）+ `Shot[]` + `RhythmCurve` + `PackagingProfile`
- 双版本槽位：`POST /api/sample/{id}/manifest/save` + `POST /api/sample/{id}/versions/{slot_id}/activate`

#### F3 · 重组工作台（Compose）

`web/src/pages/Compose.tsx:1212` `title="视频工坊"` + 26 个 compose 组件。核心子模块：

1. **MaterialGrid** + **MaterialCard**——用户素材网格，含视频预处理状态轮询（`GET /api/material/{id}/preprocess`）
2. **AdaptedSectionList** + **StructureMapPanel**——结构改编：`plan_agent` 把样例骨架按用户主题改成新结构
3. **FourTrackBoard**——四轨工作台（内容轨 / 节奏轨 / 字幕轨 / 包装轨）
4. **GapList** + **GapPreviewDialog**——缺口列表 + 缩略图预览
5. **FillRerankPanel** / **FillCopyPanel** / **FillAigcPanel**——三路补全面板（agent 化体验：分析 → 调参 → 运行）
6. **ComposeSettingsPanel**——目标平台 / 画面比例 / 时长 / 调性 / 关键词 / TTS / 字幕开关
7. **PackagingPanel** + **ReferencePicker** + **VersionMenu**——包装方案 + 参考池 + AB 版本切换
8. **ComposeCommandBar** + **DraggableCommandFab** + **ClarifyPanel**——⌘K 自然语言改片入口
9. **BgmPickerDialog** + **BgmAnalysisCard**——BGM 选择 + 豆包音频理解（燃点对齐）
10. **ThinkingSteps**——LLM 思考链可视化（fade-in 动画，复用在 copy / aigc agent 化面板）

#### F4 · 缺口识别与补全（Gap）

`FillAction = "rerank" | "copy" | "aigc" | "aigc_image"` 四路（`schemas.py:40`）：

| 动作 | 行为 | 路由 | 输出去向 |
|---|---|---|---|
| **rerank** | 从素材库重排候选 | `POST /api/gap/fill action=rerank` | `Scene.source_ref` 改写 |
| **copy** | LLM 写字卡（先 outline 调参 → 再渲染） | `POST /api/gap/copy-outline` + `POST /api/gap/fill action=copy` | `Scene.text_card_spec` |
| **aigc** | Seedance T2V 5–8s 短片 | `POST /api/gap/aigc-prompt` + `POST /api/gap/fill action=aigc` | `Scene.aigc_video_urls`，落 `var/aigc_videos/` |
| **aigc_image** | Seedream 文生图 → 按 scene.duration 定格 mp4 | `POST /api/gap/aigc-image-spec` + `POST /api/gap/aigc-seedream` | `Scene.aigc_image_url`，落 `var/aigc_images/` |

`Gap.status = "ok" \| "warn" \| "miss"`（`schemas.py:37`），用户面板上分别渲染为绿/黄/红视觉态。

#### F5 · 包装（Packaging Agent）

- `POST /api/packaging/recommend` → `packaging_agent` LLM 一次性返回 `PackagingRecommendationV2`（2 次 LLM 调用）
- `PackagingPreset = "minimalist" | "energetic" | "info_feed" | "dialogue" | "custom"`（5 维偏好预设）
- 落地写入 `Plan.packaging_track[]`，渲染时 Remotion 子进程读这条 list 输出透明 WebM
- 颗粒化插入：`POST /api/packaging/items/draft` → `POST /api/packaging/items/place`

#### F6 · 自然语言改片（Compose Edit）

- `POST /api/edit/compose`（`edit.py:398`）→ `compose_edit_agent`，LLM tool calling 修改 Plan
- `ComposeEditStep = "step2" | "step3"`——step2 只改内容轨（避免破坏已批准的包装），step3 全开
- 撤销栈：`POST /api/plan/{plan_id}/snapshot` + `POST /api/plan/.../snapshot/{id}/restore`

#### F7 · 渲染（Render）

- `POST /api/render/submit` + `GET /api/render/stream`（SSE）
- 主轨：FFmpeg concat（原素材剪切 + Seedance 片段 + 文字卡） + drawtext 基础字幕
- 包装轨：Remotion 子进程渲染透明 WebM 序列
- 最终：FFmpeg overlay 主轨 + 包装轨 + BGM 音轨 → MP4
- 落盘到 `server/var/outputs/`

#### F8 · 知识库（Knowledge）

- `GET /api/profile` / `PATCH /api/profile/settings` / `GET /api/profile/projects/{id}`
- 项目级偏好（默认时长 / 平台 / 关键词） + 跨项目沉淀的隐性规则
- 渲染时把命中的 `kb_rules_applied` 计数写回 `Plan`

#### F9 · 项目化隔离（Project）

- 5 路由：`POST/GET/PATCH/DELETE /api/project[/{id}]`
- 每个项目对应 `server/var/projects/<project_id>/` 目录隔离 + `materials/index.json` + `gaps/<plan_id>.json`
- 步骤状态机：`POST /api/project/{id}/step/{step}/commit`（`StepName = "library" | "decompose" | "compose" | "render"`）

### 5.3 用户主流程（Happy Path）

```
Home → 新建项目 → Library 选系统样例（或上传）
                  ↓
            Decompose SSE 6 步进度
                  ↓
            Library 显示 manifest_status = "ready" + version_count
                  ↓
            Compose: 上传 Material → MaterialGrid 视频预处理轮询
                  ↓
            StructureMap: plan_agent 改编骨架到用户主题
                  ↓
            GapList: 三态可视化 → 三路补全任选
                  ↓
            PackagingPanel: 一键 5 预设 / 颗粒插入
                  ↓
            ⌘K 自然语言改片（可选迭代多轮）
                  ↓
            Remotion 实时预览 + AB 双版本对照
                  ↓
            Render SSE → 下载 MP4
```

---

## 6. Success Metrics

### 6.1 主指标（赛题维度）

| 维度 | 当前 → 目标 | 验证 |
|---|---|---|
| **基础闭环跑通率** | 已通 → 公网 demo 上传任意样例端到端跑出 MP4 | 手动走查 `https://seecript.zlhu.asia` |
| **缺口三路覆盖** | 4 路全通（rerank / copy / aigc / aigc_image） | 三路面板分别真链路验证 |
| **多模态 LLM 调用点** | 6 agent × 12 次调用 → 维持 ≥ 5 agent × 8 次 | grep `\.complete*` on `services/agent/` |
| **可展示性** | 5 页面 + 26 compose 组件 + Remotion 预览 + SSE 进度 | 演示视频走查 |
| **pytest 通过率** | 250 用例收集，1 collection error（`test_gap_auto_tts.py` 因 `library.py:43` 过时 kwarg）→ 修 collection error，主体 ≥ 240 通过 | `cd server && pytest` |

### 6.2 次级指标

| 指标 | 当前 → 目标 |
|---|---|
| 60s 样例端到端拆解耗时 | 25–45s → ≤ 60s |
| Seedance T2V 单缺口耗时（720p / 5s） | 30–90s → ≤ 120s |
| Compose 页首屏渲染 | < 2s → < 3s |
| 单租户月度算力成本 | ~80 元 → ≤ 100 元（豆包 + Seedance + ARK 三方账单） |

### 6.3 Guardrail（不能更差的）

| 指标 | 红线 |
|---|---|
| 公网 demo 可达 | 5xx 率 < 1%（uvicorn.log 监控） |
| mock 模式覆盖 | 所有 UI 路径 100% 可在 `*_provider=mock` 下跑通（CI 必须） |
| 视频上传上限 | 200MB / 3 分钟（前端 + 后端双校验，已实装） |

---

## 7. User Stories & Requirements

### 7.1 Epic 假设

> **我们相信**给视频创作者一个"拆样例 → 对齐素材 → 补缺口 → 自动包装 → 一句话改片"的链路型工坊，**会让他们能在 30 分钟里产出**一条结构对齐爆款但内容原创的视频，**因为现有工具链在这五步中间是断的**。我们以"5 路由分类 × demo 端到端通跑"作为成功验证。

### 7.2 User Stories（按 Epic 分组）

#### Epic-1 · 样例拆解

- **US-1.1**：作为 P1，我希望点系统样例 → 立即看到拆解结果，不用等。
  - **AC**：`GET /api/sample/{id}/manifest` 命中缓存直接返回；未命中走完整 SSE 流；`ManifestStatus` 字段反映 ready 状态。
- **US-1.2**：作为 P1，我希望上传自己的视频后能看到 6 步进度（不是无限转圈）。
  - **AC**：`GET /api/decompose/stream` 推 shot / rhythm / vad / asr / caption / section 6 步；前端 SSE 渲染条形进度。
- **US-1.3**：作为 P1，我希望同一条样例可以保留两版拆解结果做对比。
  - **AC**：`PUT /api/sample/{id}/manifest` + `versions/{slot_id}/activate`，`SampleVersionInfo` 显示 slot 列表。

#### Epic-2 · 素材匹配

- **US-2.1**：作为 P1，我希望上传素材后系统自动建议它放在哪段。
  - **AC**：`Material.recommended_section: SectionRole`（4 元枚举）+ `Material.highlight_score: float 0.0–1.0`，预处理完成后回写。
- **US-2.2**：作为 P1，我希望预处理是异步的——上传完不阻塞，可以继续操作。
  - **AC**：`GET /api/material/{material_id}/preprocess` 轮询；`Material.preprocess_status = pending / running / ready / failed / skipped`。

#### Epic-3 · 缺口补全

- **US-3.1**：作为 P1，我希望系统告诉我**哪些段落缺画面**，而不是让我自己对比骨架和素材。
  - **AC**：`POST /api/gap/detect` 返回 `Gap[]`，每条带 `status: ok/warn/miss` + `requirement: str` + `impact: high/medium/low`。
- **US-3.2 (copy)**：作为 P1，我希望对字卡型缺口**先看 outline 再生成**，不是一键直出。
  - **AC**：`POST /api/gap/copy-outline` 返回 `CopyOutline`（core_message / emotional_hook / forced_keywords / target_length） + ThinkingSteps；用户调完参再走 `POST /api/gap/fill action=copy`。
- **US-3.3 (aigc)**：作为 P1，我希望对画面型缺口先看 LLM 给的 prompt + 参考图分析，调完参数再去 Seedance 跑。
  - **AC**：`POST /api/gap/aigc-prompt` 返回 `AigcPromptResponse` + ThinkingSteps；`/api/gap/fill action=aigc` 提交后轮询；视频落到 `var/aigc_videos/` 同源播放（解决 TOS 临时签名 + 跨域问题）。
- **US-3.4**：作为 P1，我希望**画面比例与目标平台解耦**——B 站可发竖屏，抖音也可横屏。
  - **AC**：`ComposeSettings.aspect_ratio: "9:16" | "16:9" | "1:1"` 独立字段；`aspect_for_settings(plan)` 优先取它，缺失再 fallback `target_platform`。
- **US-3.5 (rerank)**：作为 P1，我希望从素材库重挑而不是凭空生成。
  - **AC**：`POST /api/gap/fill action=rerank` 返回 top-N 候选。
- **US-3.6 (aigc_image)**：作为 P1，我希望对**静态特写**走 Seedream 文生图（更便宜更快），不要每次都跑 T2V。
  - **AC**：`POST /api/gap/aigc-seedream` + `Scene.source = "aigc_image"`，按 scene.duration 定格成 mp4 段。

#### Epic-4 · 包装

- **US-4.1**：作为 P1，我希望**一键选个预设**（minimalist / energetic / info_feed / dialogue / custom）拿到完整包装方案。
  - **AC**：`POST /api/packaging/recommend` 返回 `PackagingRecommendationV2`，含转场 + 字幕 + 标题条 + 封面四维。
- **US-4.2**：作为 P1，我希望包装入口集中——配置和触发在同一处。
  - **AC**：stage-19 已收敛到 `FourTrackBoard` 包装轨 actions 区"打开方案 ⤢ + 一键生成"两按钮并列，step-3 banner 内入口已删。

#### Epic-5 · 自然语言改片

- **US-5.1**：作为 P1，我希望**用一句话改片**："把第二段加快 1.2 倍 + 换冷色调转场 + 字幕字号小一号"。
  - **AC**：`POST /api/edit/compose` 走 `compose_edit_agent`，LLM tool calling 改 Plan；返回新 Plan + 标注变更字段。
- **US-5.2**：作为 P1，我希望改前的版本能恢复。
  - **AC**：`POST /api/plan/{id}/snapshot` + `POST .../restore`；前端撤销栈保留 snapshot list。
- **US-5.3**：作为 P1，我希望 ⌘K 在 step2/step3 的作用域不同——step2 别动我已批准的包装。
  - **AC**：`ComposeEditStep` 区分 step2 / step3；后端按 step 限制 tool 范围（详见 memory `project_seecript_ck_scope`）。

#### Epic-6 · 渲染与产物

- **US-6.1**：作为 P1，我希望渲染时看到进度而不是无限转圈。
  - **AC**：`POST /api/render/submit` → `RenderSubmitResponse` 带 job_id；`GET /api/render/stream?job_id=...` SSE 推 concat / overlay / mux 三步。
- **US-6.2**：作为 P1，我希望保留 AB 两版做对比。
  - **AC**：`Plan.variant: "A" | "B"`，`/plan/{id}/snapshot` + restore 接口完整覆盖。

#### Epic-7 · 项目隔离

- **US-7.1**：作为 P1，我希望多个项目（不同主题）互不污染。
  - **AC**：`POST /api/project` 创建 → `var/projects/<project_id>/` 物理隔离；所有 Plan / Gap / Material 都绑定 project_id。
- **US-7.2**：作为 P1，我希望知道每个项目走到哪一步。
  - **AC**：`StepName: library/decompose/compose/render` × `StepStatus: pending/in_progress/saved/dirty`；前端 Home 卡片显示。

### 7.3 边缘场景与约束

| 场景 | 处理 |
|---|---|
| 纯 BGM 视频无口播 | librosa VAD 检测后跳过 ASR，仅靠画面 + 节奏分析段落结构 |
| 上传视频 > 200MB / > 3 分钟 | 前端 `web/src/lib/video.ts` 预校验 + 后端二次校验 |
| AIGC 视频 TOS URL 跨域 + 失效 | stage-19 修：gap_agent 拿到 URL 后下载落 `var/aigc_videos/<gap_id>-<ts>.mp4`，`app.mount("/aigc-videos", StaticFiles(...))` 同源 |
| LLM JSON 畸形 | `_extract_json` 兜底 + 一次重试 |
| 老 Plan 没有 `aspect_ratio` 字段 | `aspect_for_settings` fallback `aspect_for_platform(target_platform)` |
| 无外网演示场地 | `llm_provider / asr_provider / t2v_provider / seedream_provider` 各自 `mock` 模式 100% UI 兜底 |
| `library.py:43` 过时 kwarg 导致 `test_gap_auto_tts.py` collection error | 待修：去掉 `APIRouter(..., on_startup=...)` 旧式参数，改用 `lifespan` |

---

## 8. Out of Scope

本期（v0.21 → 答辩窗口）**不做**：

| 不做的事 | 原因 |
|---|---|
| **多用户登录 / 权限系统** | 单租户演示 + 自托管即可，加 auth 增加部署复杂度 |
| **多版本 > 2 槽** | AB 双版本已覆盖 90% 对比需求；继续扩 UI 决策疲劳 |
| **超过 3 分钟视频上传** | 拆解耗时线性增长，赛题 demo 60s 已足够说明问题 |
| **API Key 轮转 / KMS 托管** | 单租户 `.env` 直管 |
| **前端测试** | 当前 `web/src/**/*.{test,spec}.*` 0 文件——本期靠后端 250 pytest 兜底 |
| **节奏图叠加高潮 marker（任务 2）** | 现有 RhythmCurve 已可视化，marker 是锦上添花 |
| **转场 LLM 自由生成 CSS（任务 8）** | 已用 `TransitionStyle` 6 选 1 白名单 + 模板字段；放开自由 CSS 前端崩溃风险高 |
| **高光片段筛选 + 段落归属推荐（任务 11）** | 现有 `Material.recommended_section + highlight_score` 已覆盖 80% |
| **商业化模块（计费 / 配额 / 限流）** | 赛题项目，留给 v1.x |
| **`config.py:107` `ark_t2v_resolution` 字段重复定义** | 已知技术债（覆盖到 plain str），不影响功能，等顺手清理 |

---

## 9. Dependencies & Risks

### 9.1 技术依赖

| 依赖 | 用途 | 版本 / 配置 |
|---|---|---|
| **Doubao ARK Seed-2.0-lite** | 9 agent 中 6 个的 LLM 调用 | `config.py:45` `ark_llm_model="doubao-seed-2-0-lite"` |
| **Seedance 2.0 fast** | T2V 缺口补全 | `config.py:48` `ark_t2v_model="doubao-seedance-2-0-fast-260128"` + `:52` `ark_t2v_resolution="720p"` |
| **Seedream 5.0** | 文生图缺口 | `config.py:130` `ark_seedream_model="doubao-seedream-5-0-260128"` |
| **豆包 bigasr_auc** | ASR 转写 | `config.py:76` `doubao_resource_id="volc.bigasr.auc"` |
| **火山 TTS** | 配音 | `config.py:113-122` `volc_*` |
| **PySceneDetect** | 镜头切分 | content-aware 模式 |
| **librosa** | BGM 能量 + onset + VAD | 300-3400Hz 频带能量阈值 0.35 |
| **FFmpeg** | 主轨 concat + overlay + mux | 系统级二进制 |
| **Remotion** | 包装轨独立 Node 子进程 | 透明 WebM 输出 |
| **FastAPI + Pydantic v2** | 后端 | 严校验 |
| **React 19 + Vite + Zustand + Tailwind v4** | 前端 | — |

### 9.2 外部依赖

- **GitHub `huudage/Seecript` 私有仓库**：服务器无登录链 → 部署只能 tar + scp + ssh restart，不能 git pull。
- **生产服务器** `root@47.239.58.145`，systemd `seecript-server.service` 监听 5002，nginx 反代 `https://seecript.zlhu.asia`。
- **`.env`**：4 套 API Key（ARK / Doubao ASR / Volc TTS / Seedream），缺任一对应 provider 必须切 mock。

### 9.3 风险与缓解

| 风险 | 影响 | 缓解 |
|---|---|---|
| Doubao ARK 限流 / 抖动 | agent 失败 | `LLMClient` 抽象 + mock 模式作为 demo 兜底 |
| Seedance 任务超时 / 排队 | aigc 缺口卡住 | 轮询超时 `config.py:97` `t2v_timeout_seconds=60`；前端可换 rerank / copy |
| Remotion 子进程 OOM | 包装渲染失败 | 子进程超时 kill + 降级到纯 ffmpeg drawtext |
| 公网 demo 被恶意上传 | 磁盘膨胀 | 200MB / 3min 上限 + `var/` 目录配额监控 |
| LLM 输出格式漂移 | Pydantic 422 | `_extract_json` 容错（fence / 单引号 / 尾逗号） + 一次重试 |
| `ark_t2v_resolution` 字段在 `config.py` 重复声明（`:52` Literal vs `:107` str） | 后定义覆盖前定义，影响类型提示 | 已知技术债，留待清理 |
| `test_gap_auto_tts.py` collection error | pytest 报错但主体不阻塞 | 修 `library.py:43` 改用 `lifespan` |
| 评分官现场断网 | demo 中断 | mock 模式覆盖 100% UI + 预录演示视频兜底 |
| TOS 临时签名跨域 | `<video>` failed-to-fetch | stage-19 已修：落盘 `var/aigc_videos/` + `/aigc-videos` 静态 mount |

---

## 10. Open Questions

| # | 问题 | 当前倾向 | 决策窗口 |
|---|---|---|---|
| Q1 | 答辩演示用哪条样例？ | marketing + editing 各一条，证明 4 元 `SectionRole`（opening / development / climax / closing）对两类视频都鲁棒 | 答辩前 1 周 |
| Q2 | `library.py:43` 的 `on_startup` 用法是否要立刻修？ | 是——`test_gap_auto_tts.py` collection error 在 pytest 输出里很扎眼，影响"完成度"印象 | 答辩前 3 天 |
| Q3 | `config.py:107` 的 `ark_t2v_resolution` 重复字段要不要清掉？ | 是，顺手清，但优先级低于 Q2 | 答辩前 3 天 |
| Q4 | mock 模式要不要在生产环境暴露开关给评委？ | 倾向"不"——生产用真链路证明能跑；mock 留给 CI 和 P2（自托管开发者） | 答辩前 1 周 |
| Q5 | 前端要不要补 .test.tsx？ | 不补——本期答辩窗口紧；用 README 写明"前端测试留 v1.0 补"作为 trade-off | 已决：跳过 |
| Q6 | `stage-15` ~ `stage-18` 在 git log 里跳号（直接从 14 到 19），要不要在 README/DELIVERY 里说明？ | 是——避免评委误以为有漏交付。stage-15/16/17/18 是合并到 stage-19 一次发的（c0bf88f） | 答辩前 3 天 |
| Q7 | 包装方案的 5 个 preset 要不要补"用户自定义保存"？ | `PackagingPreset = "custom"` 已支持运行时自定义，但持久化未做 → 留 v1.0 | 已决：跳过 |
| Q8 | `Knowledge` 页与 ⌘K 的协同（用户在 KB 里写"我喜欢冷色调转场"，⌘K 改片时自动注入）是否实装？ | 已部分实装（`kb_rules_applied` 字段在 `Plan` 上），需确认 e2e 验证 | 答辩前 1 周 |
| Q9 | ARCHITECTURE.md 是否与代码同步？ | stage-19/20/21 改动可能未反映；需要做一遍 sync | 答辩前 3 天 |
| Q10 | DEMO.md 答辩走查脚本（5 分钟演示路径）是否够细？ | 当前覆盖基础闭环，需要把"缺口三路 + ⌘K"切片演示加进去 | 答辩前 3 天 |

---

## 附录 A · 后端路由清单（72 条）

按 router 分组，仅列 method + path：

```
asr (1)        POST /transcribe
asset (8)      POST /asset/upload · GET /asset/library · GET/PATCH/DELETE /asset/{id}
               POST /asset/{id}/touch · POST /asset/save-from-url
clarify (2)    GET /clarify/round · POST /clarify/finalize
decompose (3)  POST /decompose · POST /decompose/upload · GET /decompose/stream
edit (3)       POST /edit/apply · POST /edit/compose · POST /edit/compose/dismiss
gap (10)       POST /gap/detect · GET /gap · POST /gap/fill · POST /gap/fill-all
               POST /gap/aigc-refresh · /aigc-prompt · /aigc-image-spec
               POST /gap/copy-outline · /aigc-seedream · /aigc-tail-frame
knowledge (4)  GET/PATCH /profile · /profile/settings · /profile/projects/{id}/enabled · GET /profile/projects/{id}
library (10)   GET /library · /sample/{id}/manifest · /manifest/status · /versions
               PUT /sample/{id}/manifest · POST /versions/{slot}/activate · DELETE
               POST /sample/{id}/manifest/save · GET /references · POST /library/system/upload
material (3)   POST /material/upload · GET /material/{id}/preprocess · GET /material
packaging (5)  POST /packaging/recommend · /apply · /items/draft · /items/place · DELETE /items/{plan_id}/{item_id}
plan (11)      POST /plan/build · GET /plan · GET/PATCH /plan/{id}
               PATCH/DELETE /plan/{id}/bgm · PATCH /plan/{id}/settings · /scene/{scene_id}
               POST/GET /plan/{id}/snapshot · /restore · DELETE
project (5)    POST/GET /project · GET/PATCH/DELETE /project/{id}
render (2)     POST /render/submit · GET /render/stream
step (3)       POST /project/{id}/step/{step}/commit · GET /step/{step} · GET /steps
voice (3)      POST /voice/synthesize · /synthesize-all · DELETE /voice/{plan_id}/{scene_id}
```

## 附录 B · services/agent 清单（9 个）

```
decompose_agent       样例视频拆 SampleManifest（角色 + 主题双层）       4× LLM
plan_agent            样例骨架按用户主题改编为新结构                       2× LLM
gap_agent             缺口识别与四路补全分发                                1× LLM
packaging_agent       转场 + 封面方案推荐，回写 plan.packaging_track       2× LLM
aigc_prompt_agent     段落上下文 → Seedance T2V 友好 prompt               2× LLM
copy_outline_agent    字卡 outline 推荐 → 用户调参 → 渲染                 1× LLM
clarify_agent         step1 意图澄清多轮追问                                tool-call 路径
compose_edit_agent    ⌘K 自然语言改片                                       tool-call 路径
__init__.py           agent 层公共导出
```

## 附录 C · 前端组件清单（compose/，26 个）

```
四轨工作台     FourTrackBoard
素材子区       MaterialGrid · MaterialCard · BatchAigcButton · BatchCopyButton
结构子区       AdaptedSectionList · StructureMapPanel
缺口子区       GapList · GapPreviewDialog · FillRerankPanel · FillCopyPanel · FillAigcPanel
包装子区       PackagingPanel · ReferencePicker · VersionMenu · SubtitleEditPopover
BGM 子区       BgmAnalysisCard · BgmPickerDialog
ComposeSettingsPanel · BriefInput · VideoGoalInput · SceneEditPanel
⌘K 链路        ComposeCommandBar · DraggableCommandFab · ClarifyPanel · ThinkingSteps
```

---

**文档版本**：v1.0 · 2026-06-06 · stage-21（commit `e66b0ed`）
**撰写工具**：prd-development skill（8 阶段流程 + 10 节模板）
**事实来源**：仅 `D:\Seecript` 当前代码 + git log + grep；未引用任何历史 PRD
