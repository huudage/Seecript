# Seecript

> 视频拆解与重组平台：样例视频拆解 → 结构抽取 → 素材缺口补全 → 视频重组 → 自然语言编辑。

公网 demo：<http://47.117.167.183:8090/>

Seecript 卡在「**爆款结构 + 用户素材**」这个中间抽象上：上游不与 AI 生成竞争（Sora / 可灵），下游不与剪辑竞争（剪映 / Premiere），做的是把一条爆款视频拆成可复用的结构骨架，再把用户自己的素材对齐到骨架上，重组出结构对齐但内容原创的新视频。

「样例库 → 样例拆解 → 缺口识别与补全 → 视频重组 → 自然语言改片」这条链两版不变；变的是**交互范式与 AI 边界**。

## 两版形态：v1 → v2

| | v1 · 已上线（main / 公网 demo） | v2 · 重设计（`feat/v2-canvas` 开发中） |
|---|---|---|
| 交互 | 四轨时间线工作台 + 26 个 compose 组件，⌘K 对话框主导改片 | **结构画布**：段落块 × 素材槽 × 连线，拖拽直接操纵；⌘K 降级为兜底入口 |
| AI 边界 | 3 处「一键全自动」，AI 产物未经确认直接进 Plan | AI 出初稿 / 出建议 → **人确认或拖改后生效**；功能盘 = tool 白名单的可视化 |
| 渲染 | Remotion 子进程包装轨 + Seedance T2V 主链路 | ffmpeg 原生 filter + WebAV 浏览器端实时预览与轻量导出 |
| 外围 | 知识库 / 步骤状态机 / AB 对照 / TTS / BGM 燃点分析 / 素材自动识别 / 情绪曲线 | 整体裁剪；**补拍清单**替代 T2V 主链路 |

两版 PRD：[docs/PRD.md](docs/PRD.md)（v1.0）· [docs/PRD-v2.md](docs/PRD-v2.md)（v2.0 定稿，2026-09-23）。

## 为什么这么迭代 · 设计思考

v1 把链路跑通了，但跑通之后暴露的不是功能缺失，而是三个**形态**问题：

1. **直接操纵缺失**。换一个素材要走「选中 → 4-tab 弹窗 → 确认」四步，或者把意图翻译成一句话喂给对话框——用户在「自己本来就会做的事」上也被迫过 AI，AI 从辅助变成了必经入口。
2. **AI 主导时刻过多**。生成内容轨、自动重写口播、出片前自动补缺，三处 AI 产物未经确认直接进 Plan，被渲染机器链直接消费——AI 错了，用户只能在成片里发现，对「成片为什么变成这样」失去解释权。
3. **技术栈过重**。Remotion 是 Node 子进程（OOM 风险），自闭环要求下是渲染链上最重的一环；T2V 单次 30–90s 卡在用户主链路；demo 演示链路长、故障面大。

三个问题对应三条设计原则，构成 v2 全部功能取舍的裁决框架。

### 一 · AI 做 copilot，不做主导入口

剪辑是高度个性化的创作场景，AI 的价值是降低门槛、给出建议，而不是替用户拍板。判断的尺子是一条 badcase 成本公式：

> **badcase 成本 = 错误的消费者 × 级联深度**

AI 输出给人消费（报告 / 贴纸 / diff / 初稿），人当场可校验，成本可控；AI 输出给机器消费（自动进 Plan → 渲染），错误级联放大到成片。v2 由此立下总账：**所有 AI 产物的第一消费者是人，机器链只消费人确认后的东西**。

收编不是砍掉 AI，是改产物的消费方式：

| v1 一键全自动 | v2 收编后 |
|---|---|
| step1「生成内容轨」 | clarify 多轮聊天先确认结构 → 画布半透明初稿 +「AI 初稿」贴纸 → 拖改 →「定稿」 |
| step2→3 自动重写口播 + TTS | 功能盘内「配口播」动作：人触发、diff 确认后应用 |
| 出片前自动补缺 | 渲染确认清单只拦未定稿。空槽可以留着，不强制补满参考结构 |

### 二 · 能拖拽的地方，就不要对话框

v1 的四轨时间线是「轨道-参数」心智：素材落位没有直接操纵（拖拽只做网格排序，推荐落位只是卡片色条），改片只能钻弹窗或写对话。v2 用**结构画布**替代——把「爆款结构」这个抽象显性化为可拖拽的空间结构：

- **段落块**是叙事段落的容器（不是流程函数）：素材槽、字幕、标题条、口播稿同居一卡（块内层三轴），双击直改；
- **连线**既是叙事顺序，也是转场载体（6 风格白名单选择器，原生控件非 AI）；
- 点视频块从这段开始预览。画布外不再放锁定物体提示，也不再放底部视频块轴。时间轴和字幕 / 口播 / 音乐轨放在画布顶部，可以收起；
- **PlanPlayer** 点块即播该块，「播放整片」进入 WebAV 整体实时预览。

交互契约是「**即时生效 + 撤销兜底**」：任何画布操作直接落 `plan_store`（新 plan_id 入撤销栈），⌘Z 一步回滚。确认门只留给 AI 动作——人是权威源，人的操作不需要向自己确认。

AI 入口收敛为**功能盘**。空白画布右键：自然语言改片、生字卡、生图再渲染、添加用户素材。右键在捕获阶段打开，右键不再平移画布。生字卡和生图的悬停里可以选关联结构段，选了就用该段拆解结果预填时长和文案，不改原段。生图点下后先在画布末尾出现视频块，块内显示「生图中」，成图后再换成画面。视频块右键只放 step3 的包装（字幕、标题条、贴纸、封面），不含转场，且只作用这一块。转场用左键点两块之间的连线添加，连线没有右键菜单。左键视频块弹窗播放，并按时间轴裁剪用户素材；字卡不预览，只改参数。点块不再滚动页面。轨道仍在画布顶部，可收起。右下角对话和自进化蒸馏还在。

### 三 · 功能裁剪三问

功能的去留不凭感觉，每个都过三问：**① 真实需求与竞品 ② 技术重量与 demo 难度 ③ badcase 必要性**。三问全过才留，一问不过就砍或降级：

| 裁剪 / 降级 | 三问落点 |
|---|---|
| Remotion 渲染线整体移除 | 自闭环硬约束下它是技术最重一环（Node 子进程 / OOM）；ffmpeg 原生 filter（xfade / ASS / drawtext / overlay / zoompan）全覆盖；预览交给 WebAV 浏览器端管线 |
| T2V 出主链路 | 缺口的真实需求是「这段有画面」——用户自己拍是质量最高、成本最低的路，AI 替拍是最后手段；T2V 30–90s 卡链路，生成 badcase 由渲染直接消费。改为**补拍清单**（shot brief：拍什么 / 多长 / 什么情绪 / 参考哪段）+ 盘内「AI 合成视频」显式单点 |
| 素材自动识别与自动落位 | 超出 AI 能力边界——自动打标 / 槽位匹配的精度撑不起「默认生效」；且越界用户自主性——素材放哪段是创作决策，AI 不替用户拍板。降级为盘内按需「AI 理解」贴纸 |
| 情绪曲线 | 多信号打分达不到可信精度；剪辑过程没有消费场景、下游无引用；纯展示性炫技 |
| 其余外围（知识库 / 步骤状态机 / AB 对照 / TTS / BGM 燃点分析 / 自动落位 fill-all 等） | 逐项三问不过，整体移除（撤销栈底座保留） |

对能力不稳但仍有判断价值的环节（素材理解 / 素材裁剪），解法是**改消费方式**：理解降为按需贴纸（人点名才跑、不落 plan、可摘除），裁剪建议落成 diff、人确认后生效；对能力达不到且无消费场景的（自动识别落位 / 情绪曲线），直接砍。

### 不变的部分同样重要

用户问题、目标人群、「爆款结构 + 用户素材」差异化锚点、拆解 6 步流水线与确认门、三层兜底工程范式（Prompt / Router / Schema）——全部继承 v1。这次迭代不是推倒重来：真正验证过的资产是「拆解链路 + 结构数据」，要重写的是长在外围的交互外壳。

### 市场验证

画布 / 无时间线不是拍脑袋：Synthesia 场景卡家族验证「段落卡片 + 卡内分层」对小白创作者成立；Descript 验证按叙事段落（而非时间码）组织实拍素材的心智；2026 年节点画布交互已被大众化（Higgsfield / Runway），范式教育成本由先行者支付。「**节点画布 × 段落叙事 × 实拍素材重组**」在市场上无先例——这是差异化锚点，不是跟随。

## v1 · 核心能力（已上线）

| # | 模块 | 说明 |
|---|---|---|
| 1 | 样例库 | 内置营销 / 剪辑 / Motion Graph 三类样例，支持用户上传自有视频拆解 |
| 2 | 样例拆解 | PySceneDetect 切镜头 + librosa BGM 能量曲线 + VAD 门控 ASR + 多模态 LLM 帧打标 + LLM 段落结构 |
| 3 | 素材上传 + 缺口识别 | 多模态 LLM 给上传素材自动打标，槽位匹配算法对齐到样例段落（9 个 SectionKind） |
| 4 | 缺口三路补全 | ① 结构重排（rerank）② 文案补全（copy）③ AIGC 生图（Seedream）/ 生视频（Seedance T2V） |
| 5 | 视频重组 | FFmpeg concat 主轨 + Seedance 首尾帧串接长视频 + Remotion 包装轨透明 WebM + ffmpeg overlay 收尾 |
| 6 | 画面包装 | LLM 一次性给 6 种转场风格 + 封面方案，回写 `plan.packaging_track`，Remotion 渲染字幕 / 标题条 / 贴纸 / 转场 / 封面 |
| 7 | 自然语言编辑 | LLM tool calling 改 Plan JSON：双入口（Render 态三轨分流 + Compose 态 ⌘K 对话），每次生成新 plan_id 入撤销栈 |
| 8 | 情绪曲线 | LLM 多信号打分（角色 + BGM + 节奏 + 整片调性）→ 段落 anchor + peaks/valleys → 规则插值 60 点平滑曲线 |

## v2 · 功能版图（对 v1 八模块的处置）

v2 对 v1 八个模块逐个过「三问 + Copilot 三原则」：**砍两个**（超出 AI 能力边界，或越界用户自主性）、**收四个**（视频补全、重组、包装、自然语言编辑全部收进画布——交互自由度提升，产品形态创新）、**窄两个**（定位收窄）。产品形态从「四轨工作台 + 对话框」变为「结构画布 + 功能盘」。

| v1 模块 | 处置 | v2 说明 |
|---|---|---|
| 1 · 样例库 | 窄 | 上传收窄为 ≤60s / 三类型白名单，超域引导到系统样例 |
| 2 · 样例拆解 | 窄 | 6 步流水线不变；产物定位收窄为「给人看的结构报告 + 人工确认门」 |
| 3 · 素材上传 + 缺口识别 | **砍** | 多模态自动打标 + 槽位匹配落位整体移除——超出 AI 能力边界（识别 / 匹配精度撑不起默认生效），同时越界用户自主性（素材放哪段是创作决策，AI 不替用户拍板）。v2 落位回归拖拽直接操纵；缺口以画布空槽直接可见；不确定时右键「AI 理解」按需出贴纸（不落 plan，可摘除） |
| 4 · 缺口补全 | 重构 | 四路：rerank / 字卡 / AIGC 图（收窄为封面 · 底图 · 静态特写）/ **补拍清单**（新增：AI 出拍摄规格，而非替拍）；T2V 出主链路，收编为盘内「AI 合成视频」显式单点 |
| 5 · 视频重组 | 收 · 画布化 | 重组 = 拖拽重排 / 换槽 / 连线 / 块切分，直接操纵即时生效；渲染 Remotion → ffmpeg 原生 + WebAV 整体实时预览与浏览器端导出 |
| 6 · 画面包装 | 收 · 画布化 | 转场 = 连线样式（6 风格白名单）；字幕 / 标题条 = 块内层；封面 = AIGC 图动作；5 预设与推荐 agent 保留，入口收进功能盘 |
| 7 · 自然语言编辑 | 收 · 画布化 | 盘内「局部改片」（作用域限该段）+ ⌘K 全局兜底；diff 预览 → 确认 → 应用，未确认不落 plan |
| 8 · 情绪曲线 | **砍** | 多信号打分达不到可信精度（AI 能力边界）；剪辑过程没有消费场景、下游无引用；纯展示性炫技 |

## v2 · 变更明细与进度

### U1–U7 变更总览

| | v1 | v2 |
|---|---|---|
| U1 结构画布 | 四轨时间线工作台 | 画布：段落块可拖、点块预览。轨道在画布顶部，可折叠 |
| U2 功能盘 | ⌘K 对话主入口 | 右键鼠标跟随盘；自然语言改片在盘内，自进化蒸馏保留；生成项悬停展开；⌘K 仍在 |
| U3 AI 收编 | 3 处一键全自动 | 聊天确认结构 → 可拖改初稿；配口播走 diff；出片前渲染确认清单 |
| U4 AIGC 策略 | T2V 主链路 + Seedream 补图 | T2V 出主链路（盘内显式单点）；新增补拍清单；Seedream 收窄为封面 / 底图 / 静态特写 |
| U5 渲染减重 | Remotion 子进程 + 首尾帧串接 | ffmpeg 原生成片（xfade / ASS / drawtext / overlay / zoompan）+ WebAV 整体实时预览与浏览器端导出 |
| U6 外围裁剪 | 知识库 / 步骤状态机 / AB 对照 / TTS / 素材自动识别 / 情绪曲线 等 | 整体移除（撤销栈底座保留） |
| U7 输入收窄 | ≤3 分钟上传 | ≤60s + 三类型白名单；拆解产物定位为「给人看的结构报告 + 确认门」 |

### 开发里程碑（`feat/v2-canvas`）

| # | 里程碑 | 内容 | 状态 |
|---|---|---|---|
| D1 | 脚手架 | PRD-v2 入库 + React Flow 接入 + 画布骨架（四轨/画布切换 · 段落块×连线 · 总览条 · 选段联动） | ✅ `fe36178` |
| — | 盘交互前置 | CopilotDial 径向盘 v0：锚定命中测试。唤出键后改为右键（见 D8） | ✅ `9617dc0` |
| D2 | 画布核心 | 拖拽重排 · 换槽素材 · 块内层三轴（mini 时间条）· 连线转场选择器 · 块切分；D2.5 step2/3 合并两步导航 + 预览弹窗 + 可折叠 tab | ✅ `87bff77` |
| — | dev 演示模式 | `?demo` 用仓库样例素材直出全链路 mock（仅 DEV，产物树摇干净） | ✅ `1282acd` |
| D3 | 功能盘 | AI 8 + 结构 3 动作全量接入（白名单驱动） | 前端已接线：理解 / 裁剪 / 字卡 / 补图 / 合成视频 / 补拍 / 局部改片 / 配口播 / 包装 / 添加视频块 / 转场。未接入的不出现 |
| D4 | AI 收编 | clarify 确认结构（v1 门保留）+ 初稿模式 + 配口播 diff + 渲染确认清单 | ✅ 初稿 `structure_confirmed` + 盘内配口播按确认原文落盘 + 出片前清单（不再自动 copy 补缺） |
| D5 | 缺口 / 补拍 | 空槽即缺口 + 四路补全（rerank / 字卡 / AIGC 图 / 补拍清单） | 空槽右键打开字卡 / AIGC 图 / 补拍清单，表单在画布底边抽屉。实拍换槽仍是拖拽。T2V 只在盘内「AI 合成视频」 |
| D6 | 渲染线 | ffmpeg 原生成片 + 实时预览 + 一键导出 | 画布右侧实时预览随时间轴和点块播放。一键导出走服务端 ffmpeg 成片，完成后自动下载。当前公网是 HTTP，浏览器 WebCodecs 导出不可用 |
| D7 | 裁剪收窄 | 外围整体移除（含素材自动识别 / 情绪曲线）+ 上传收窄 + 回归 | 上传已收窄 ≤60s；自进化（创作偏好蒸馏）仍在导航；情绪曲线已从拆解、方案和页面拿掉。时间轴不再提供口播合成。步骤状态机后端仍在 |
| D8 | 画布收口 | 右键唤盘；撤销 / 帮助 / 结构迁移对照放在画布内部上部居中 | ✅ 已落地 |

## 仓库结构

```
seecript/
├── web/                                 React 19 + Vite + TS + Tailwind v4 + Zustand
│   └── src/{pages,components,stores,api,types}
│       └── components/compose/          v2 新增：StoryboardCanvas（结构画布）· CopilotDial（功能盘），feat/v2-canvas
├── remotion/                            包装轨独立 Node 项目（v1；v2 D6 移除）+ AnimatedImage
├── server/
│   ├── app/{main,config,schemas}.py     FastAPI 入口 + Pydantic Settings + 全模块契约
│   ├── app/routers/                     library · decompose · material · gap · plan · render · edit · asset · packaging · clarify · knowledge · project · step · voice · asr
│   ├── app/services/
│   │   ├── llm_client.py                LLMClient 抽象（Mock + DeepSeek + Doubao Ark 多模态）
│   │   ├── asr_client.py · t2v_client.py · seedream_client.py
│   │   ├── video/                       ffmpeg · scene_detect · aspect · bgm_analysis · voice_detect · ocr · remotion
│   │   ├── agent/                       decompose · plan · gap · packaging · clarify · copy_outline · aigc_prompt · compose_edit · emotion
│   │   ├── render/                      pipeline.py（6 步）+ seedance_chain.py + remotion_renderer.py
│   │   └── materials · assets · library · plans · projects · jobs · prompts · profile · tts
│   ├── samples/                         内置样例（video.mp4 + 预解析 manifest）
│   └── var/                             运行期产物（outputs / uploads / projects / aigc_*）
├── docs/                                ARCHITECTURE · AI-DESIGN · PRD · PRD-v2 · DEMO · CATALOG_FRAME
└── run.{ps1,sh} / stop.{ps1,sh}         本地启动 / 停止
```

## 技术栈

| 层 | 选型 | v2 变化 |
|---|---|---|
| 后端 | FastAPI + Pydantic v2 + Python 3.10+ | 不变 |
| 前端 | React 19 + Vite + TypeScript + Tailwind v4 + Zustand | 交互层重写（画布） |
| 结构画布 | — | 新增 React Flow（@xyflow/react） |
| 预览 / 导出 | — | 新增 WebAV（整体实时预览 + 浏览器端轻量导出） |
| 视频包装 | Remotion | v2 移除 → ffmpeg 原生 filter（xfade / ASS / drawtext / overlay / zoompan） |
| LLM | Doubao Seed-2.0-lite（多模态，OpenAI 兼容） | 不变 |
| ASR | 豆包 bigasr_auc_turbo + librosa VAD 门控 | 不变 |
| T2V | doubao-seedance-2-0-fast-260128 | 出主链路：盘内「AI 合成视频」显式单点 |
| T2I | doubao-seedream | 定位收窄：封面 / 底图 / 静态特写 |
| 镜头分割 | PySceneDetect | 不变 |
| 音频分析 | librosa（RMS energy + onset + tempo） | BGM 燃点分析裁剪，拆解侧维持 |
| 视频处理 | FFmpeg subprocess | filter graph 扩展 |

## 本地部署

> 以下为 v1（main）口径；v2 开发在 `feat/v2-canvas` 分支。

### 0 · 系统要求

- **Python 3.10+**（开发机用 3.12 已验证）
- **Node.js 18+** + **pnpm**（`npm i -g pnpm`）
- **ffmpeg + ffprobe** 必须在 `PATH`
  - Windows: `winget install Gyan.FFmpeg`
  - macOS: `brew install ffmpeg`
  - Linux: `apt install ffmpeg`

### 1 · 拿到代码

两种方式任选其一：

```bash
# 方式 A · git clone（推荐，能跟随后续更新）
git clone https://github.com/huudage/Seecript.git
cd Seecript

# 方式 B · 下载源码包（13 MB，已剔除 venv / node_modules / 大视频）
curl -LO https://seecript.zlhu.asia/release/seecript-source.zip
# 可选：校验完整性
curl -sL https://seecript.zlhu.asia/release/seecript-source.zip.sha256 | sha256sum -c -
unzip seecript-source.zip
cd seecript
```

源码包不含 `server/samples/*/video.mp4`（每个样例 4–20 MB，太大）。如需内置爆款样例视频，从公网 demo 拉：

```bash
mkdir -p server/samples/sample-marketing-01
curl -L -o server/samples/sample-marketing-01/video.mp4 https://seecript.zlhu.asia/samples/sample-marketing-01/video.mp4
# 同理：sample-vlog-01 / sample-motion-01
```

或者跳过爆款样例，直接在 UI 里上传你自己的视频跑全流程。

### 2 · 配 API Key（关键）

```bash
cp server/.env.example server/.env
```

打开 `server/.env`，按用途填上真值：

| 用途 | 关键变量 | 申请入口 |
|---|---|---|
| LLM（段落结构 / 文案 / 多模态理解） | `LLM_PROVIDER=doubao_ark` + `ARK_API_KEY` + `ARK_LLM_MODEL=doubao-seed-2-1-lite-260915`（`POST /responses`） | <https://console.volcengine.com/ark> |
| T2V（AIGC 缺口生成 + 长视频首尾帧扩展，可选） | `T2V_PROVIDER=doubao_ark` + `ARK_T2V_API_KEY`（可复用上面的 Key） | 同上 |
| 生图（字卡底图 / 参考图，Seedream） | `SEEDREAM_PROVIDER=doubao_ark` + `ARK_SEEDREAM_API_KEY` + `ARK_SEEDREAM_MODEL=doubao-seedream-5-0-260128` | 同上，接口 `POST /images/generations` |
| ASR（口播识别，可选；纯 BGM 视频会自动跳过） | `ASR_PROVIDER=doubao` + `DOUBAO_API_KEY` + `PUBLIC_AUDIO_BASE_URL` | 火山引擎 → 语音技术 |

> ⚠️ **没有 LLM Key 就跑不通**——本项目默认 `LLM_PROVIDER=mock`，只能让链路不报错，无法产生真实拆解结果。**生产路径不降级**，缺 Key 直接 5xx。
>
> 全部 mock 也能启服务，可以体验 UI 但所有 AI 输出都是占位字符串。

### 3 · 起服务

```bash
# 后端：自举 venv + 装 requirements + 起 127.0.0.1:8090
./run.ps1               # Windows PowerShell
./run.sh                # macOS / Linux

# 前端：另开一个终端
cd web
pnpm install
pnpm dev                # http://127.0.0.1:5173
```

健康检查：<http://127.0.0.1:8090/api/healthz>（应返回 `{"ok":true}`）

打开 <http://127.0.0.1:5173>，选一个内置样例点 "拆解" 或上传自己的视频，全链路即可跑通。

### 4 · 常见问题

| 现象 | 原因 / 解决 |
|---|---|
| `ffmpeg: command not found` | 装上 ffmpeg 并加进 PATH，重启终端 |
| 拆解卡在 "音频识别" | ASR 走异步 submit/query，火山服务端要拉取你的 `PUBLIC_AUDIO_BASE_URL` 公网音频；本地 dev 用 `cloudflared tunnel --url http://127.0.0.1:8090` 临时打洞 |
| 上传视频后 LLM 5xx | 检查 `ARK_API_KEY` 是否有效、`ARK_LLM_MODEL` 是否是有权访问的推理点 |
| 系统素材库为空 | 启动时从 `server/samples/{sample-*,sys-*}` 自动 seed；样例目录缺 `video.mp4` 不会被 seed |
| 端口冲突 | 改 `server/.env` 里的 `PORT`，前端改 `web/vite.config.ts` |

详细架构 / Agent 简报见 [AGENTS.md](AGENTS.md) 和 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)。

## 测试

```bash
# 后端
cd server && python -m pytest tests/ -v

# 前端类型检查 + 构建
cd web && npx tsc -p tsconfig.app.json --noEmit && npx vite build
```

## 进一步阅读

- [docs/PRD.md](docs/PRD.md) — v1 产品需求文档（十节结构，基于代码现状撰写）
- [docs/PRD-v2.md](docs/PRD-v2.md) — v2 重设计 PRD：结构画布 / 功能盘 / AI 收编 / 渲染减重，含 U1–U7 变更总览与决策记录
- [AGENTS.md](AGENTS.md) — 给代码 agent 的项目简报（硬约束 / 目录 / 部署）
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — 整体架构 / 数据流 / 工具协议 / 安全边界
- [docs/AI-DESIGN.md](docs/AI-DESIGN.md) — AI 干预点详解 + 三层兜底
- [docs/DEMO.md](docs/DEMO.md) — 5 分钟演示走查
