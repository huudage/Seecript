# Seecript · 爆款结构迁移引擎 — 技术架构

> 视频拆解与重组的助手。围绕赛题「爆款结构迁移引擎 — 从样例拆解、素材补全到视频重组的 AI 创作平台」构建。
>
> 本文档沉淀技术栈选型、数据流、任务拆解，作为赛题交付的「整体 AI 架构、工具协议和安全边界」依据。

## 1. 功能模块映射（赛题需求 → 技术实现）

| # | 模块 | 关键技术 |
|---|---|---|
| 1 | 素材库 | 静态 3 个内置样例（营销/剪辑/Motion Graph），预解析 manifest 缓存 |
| 2 | 样例拆解（BGM/整体结构） | PySceneDetect 镜头切分 + librosa BGM 能量曲线 + librosa VAD 门控 ASR（无口播跳过）+ 多模态 LLM 帧打标 + 多模态 LLM 段落结构（按 video_type 三选一：marketing=hook/body/cta · editing=opening/climax/closing · motion_graph=intro/build/drop/outro） |
| 3 | 新内容输入 + 缺口识别 + 补全 | 多模态 LLM 素材打标 + 槽位匹配算法 + 三种补全（结构重排 / 文案补全 / Seedance T2V 短片生成） |
| 4 | 迁移过程可视化 | React Flow 流程图（样例槽位 → 新方案分镜的连线，缺口红虚线 / 补全绿标）+ 三栏可调宽度布局 |
| 5 | 视频生成 | FFmpeg 主轨拼接 + doubao-seedance-2-0-fast-260128 首尾帧扩展长视频 + SSE 推进度 + AB 双版本 |
| 6 | 画面包装生成 | Remotion 包装轨道（字幕/标题条/转场/封面）独立子进程渲染 → ffmpeg overlay 叠加；packaging_agent LLM 一次性给出 6 种转场风格 + 封面方案，回写 `plan.packaging_track` |
| 7 | 自然语言编辑 | LLM tool calling 改 Plan JSON + 增量重渲染 + 撤销栈 + 双轨标注式编辑 |

## 2. 技术栈选型

### 2.1 前端

| 层 | 选型 | 理由 |
|---|---|---|
| 框架 | React 18 + Vite + TypeScript | 复杂时间轴/流程图/双轨编辑 vanilla 不可维护；Vite 启动 <1s |
| 状态 | Zustand | 比 Redux 轻 10×；3 个 store（session/plan/edit） |
| 路由 | React Router v6 | 纯 SPA |
| 样式 | Tailwind CSS + shadcn/ui | shadcn 是 copy-paste 组件不是包，无 UI 库 lock-in |
| 时间轴 | 自研 SVG + d3-scale | 核心交互必须自研可控 |
| 流程图（迁移映射） | @xyflow/react (React Flow) | 业界标准，节点/边/虚线开箱即用 |
| 图表 | Recharts | API 极简，比 ECharts 更适合 React |
| 视频播放器 | 原生 `<video>` + 自定义控件 | 不引 video.js（800KB） |
| 拖拽 | dnd-kit | 现代 API，比 react-dnd 轻 |

### 2.2 后端

| 层 | 选型 | 理由 |
|---|---|---|
| Web | FastAPI + Pydantic | 复用已有 middleware/路由/mock 机制 |
| 镜头分割 | PySceneDetect | 纯 Python、content-aware、MIT |
| 音频分析 | librosa | RMS energy + onset + tempo 一站式 |
| 字幕 OCR | PaddleOCR mobile 中文 | 中文最准，轻量版 <10MB |
| 视频处理 | FFmpeg (subprocess) | 拼接、抽帧、overlay |
| 任务编排 | BackgroundTasks + 内存 JobStore + SSE | 不引 Celery/Redis，比赛足够 |
| Agent 编排 | 自研 plan-act 循环 + Pydantic | 不引 LangGraph |

### 2.3 AI 模型

赛题原设 6 个 AI 干预点，落地时合并为 **3 类**（独立 VLM / T2I client 全部退役，画面理解全走多模态 LLM；图生静态画面这一步不再需要——缺口画面缺口直接走 T2V 生成 5-8s 短片）：

| 用途 | 模型 | 说明 |
|---|---|---|
| 段落结构 / 帧打标 / 缺口文案 / NL 编辑 tool call | Doubao-Seed-2.0-lite（多模态）| 同一模型四个调用点：①按 video_type 三选一 prompt 给段落结构；②关键帧缩略图批量打标；③缺口文案补全；④NL 编辑 tool call。OpenAI 兼容，赛题给的 EP |
| ASR 口播转写（VAD 门控） | 豆包 bigasr_auc_turbo | librosa 人声 VAD（300-3400Hz 频带能量阈值 0.35）先判定是否有口播；纯 BGM 视频跳过 ASR 直接走"靠画面+节奏"段落分析 |
| 视频生成（aigc 缺口 + 长视频首尾帧） | doubao-seedance-2-0-fast-260128 | submit + 轮询 query 两段式；gap_agent aigc 分支生成 5-8s 短片填补槽位；长视频用首尾帧模式串接 30-60s。Fast 版相比 1.0 pro 平均渲染时间从 90s 降到 30-60s，成本同时降低，比赛 demo 单镜头 5s 输出即可 |

### 2.4 视频合成（混合方案）

```
主轨   = FFmpeg concat (原素材剪切 + Seedance 生成片段) + ffmpeg drawtext 基础字幕
包装轨 = Remotion (React 组件描述字幕/标题条/贴纸/转场) → 透明 WebM 序列
最终  = ffmpeg overlay 主轨 + 包装轨 + BGM 音轨
```

## 3. 整体数据流

```
浏览器                                     后端                                    模型
─────────                                  ─────────                              ────────
1. 选样例 ─────────────────────────────►  GET /api/library
                                          GET /api/sample/{id}/manifest
                                          (静态返回 3 个样例的预解析结果)

2. 拆解（命中样例 → 复用缓存；新视频走完整链路）
                                          PySceneDetect ──► 镜头切分
                                          librosa ──────────► BGM 能量曲线
                                          librosa VAD ──────► 有口播？──►(是) ASR 转写
                                                                       └►(否) 跳过 ASR
                                          多模态 LLM (seed-2.0-lite) ──► 帧打标 + 按 video_type 三选一 prompt 给段落结构
                                          ↑ SSE /api/decompose/stream 推每一步进度

3. 上传新素材 ─────────────────────────►  POST /api/material/upload (multipart)
                                          多模态 LLM ──────► 素材打标 + 段落推荐

4. 缺口识别 ──────────────────────────►  POST /api/gap/detect
                                          槽位匹配算法 (Python 纯计算，支持 9 个 SectionKind)

5. 缺口补全 ──────────────────────────►  POST /api/gap/fill (action: rerank|copy|aigc)
                                          LLM 文案 / Seedance T2V (submit → 轮询) ──► 缺失画面

6. 方案生成 ──────────────────────────►  POST /api/plan/build
                                          组装分镜时间线 (Pydantic)

6b. 包装推荐 ─────────────────────────►  POST /api/packaging/recommend
                                          packaging_agent LLM ──► 转场风格 6 选 1 + 封面（标题/副标题/调色板/布局）
                                          落地：写入 plan.packaging_track 的 kind=transition / kind=cover items

7. 视频生成 ──────────────────────────►  POST /api/render/submit  (SSE 推进度)
                                          FFmpeg concat ──► 主轨 MP4
                                          Seedance 首尾帧 ──► 长视频段
                                          Remotion ──► 包装轨道透明 WebM（含 transition/cover 渲染）
                                          FFmpeg overlay ──► 最终 MP4 (AB 双版本)

8. 自然语言编辑 ──────────────────────►  POST /api/edit/apply
                                          LLM tool calling ──► 修改时间线 JSON ──► 增量重新渲染
```

## 4. 目标目录结构

```
seecript/
├── web/                                # 新前端（React + Vite + TS）
│   ├── src/
│   │   ├── pages/                      # Library / Decompose / Compose / Migrate / Render
│   │   ├── components/                 # timeline / rhythm-curve / flow-map / slot-list / material-card / nl-editor / ab-compare
│   │   ├── stores/                     # Zustand stores
│   │   ├── api/                        # 统一 API 客户端 + SSE
│   │   └── types/                      # 与后端 schemas 镜像的 TS 类型
│   ├── package.json
│   └── vite.config.ts
│
├── remotion/                           # Remotion 包装轨道（独立 Node 项目）
│   ├── src/
│   │   ├── PackagingTrack.tsx          # 主合成
│   │   ├── Subtitles.tsx
│   │   ├── TitleBar.tsx
│   │   ├── StickerOverlay.tsx
│   │   └── Transition.tsx
│   └── package.json
│
├── server/                             # 后端（FastAPI，大改）
│   ├── app/
│   │   ├── main.py
│   │   ├── config.py
│   │   ├── schemas.py                  # 重写：sample/decomp/material/slot/plan/render/edit
│   │   ├── routers/                    # library / decompose / material / gap / plan / render / edit
│   │   ├── services/
│   │   │   ├── llm_client.py           # LLMClient 抽象 + Doubao Seed-2.0-lite（多模态）
│   │   │   ├── asr_client.py           # 保留 + VAD gate（纯 BGM 跳过）
│   │   │   ├── t2v_client.py           # doubao-seedance-2-0-fast-260128 首尾帧 submit+poll
│   │   │   ├── video/                  # scene_detect / audio_analysis / voice_detect / ocr / ffmpeg / remotion
│   │   │   ├── agent/                  # decompose_agent / gap_agent
│   │   │   └── jobs/                   # 内存 JobStore + SSE 通道
│   │   └── prompts/                    # 重写所有 prompt
│   ├── samples/                        # 3 个内置样例（视频 + 预解析 JSON）
│   ├── var/uploads/                    # 用户上传素材
│   └── tests/
│
├── docs/
│   ├── ARCHITECTURE.md                 # 本文档
│   └── AI-DESIGN.md                    # 3 类 AI 干预点详解（多模态 LLM / VAD-门控 ASR / Seedance T2V）
│
└── run.ps1 / run.sh                    # 改：起后端 + 起前端 vite + 准备 remotion node_modules
```

## 5. 工具协议（赛题要求）

### 5.1 端点契约（FastAPI Pydantic）

| 端点 | 用途 | 请求/响应骨架 |
|---|---|---|
| `GET /api/library` | 列出 3 个内置样例 | `[{id, title, scene, duration, shot_count, cover_url}]` |
| `GET /api/sample/{id}/manifest` | 取样例预解析 manifest | `SampleManifest`（镜头列表 + 节奏曲线 + 包装指标 + 段落结构） |
| `POST /api/decompose` | 触发拆解任务 | `{ sample_id }` → `{ job_id }` |
| `GET /api/decompose/stream?job_id=...` | SSE 推拆解进度 | `event: progress` `data: {step, percent, payload}` |
| `POST /api/material/upload` | 上传新素材 | multipart → `Material[]`（含 VLM 标签 + 段落推荐） |
| `POST /api/gap/detect` | 槽位匹配 | `{ plan_id }` → `Gap[]`（含 ✅⚠️❌ 状态 + 影响等级） |
| `POST /api/gap/fill` | 补全单个缺口 | `{ gap_id, action: rerank \| copy \| aigc, params }` → `FillResult` |
| `POST /api/plan/build` | 组装最终 Plan | `Plan`（含 TimelineTrack 主轨 + 包装轨） |
| `GET /api/plan/{plan_id}` | 拉最新 Plan（包装推荐回写后前端用） | `Plan` |
| `POST /api/packaging/recommend` | LLM 给转场 + 封面，apply=true 写回 packaging_track | `{ plan_id, apply }` → `PackagingRecommendation` |
| `POST /api/render/submit` | 提交渲染任务 | `{ plan_id, variant: A \| B }` → `{ job_id }` |
| `GET /api/render/stream?job_id=...` | SSE 推渲染进度 | `event: progress` / `event: done` |
| `POST /api/edit/apply` | 自然语言改片 | `{ plan_id, instruction, marks[] }` → 新 `Plan` |

### 5.2 AI 客户端抽象

所有外部模型走统一的 client 抽象基类，保留 `Mock` 实现保证 mock 模式端到端跑通：

- `LLMClient`：`complete` / `complete_json` / `complete_with_tools` / `complete_multimodal`
  - `complete_multimodal(system, user_text, images)` 是新加的多模态入口，OpenAI 兼容的 `content: [{type:"text"}, {type:"image_url"}]` 结构。**取代了原先独立的 VLMClient 和 T2IClient**：画面理解（帧打标、段落分析、素材打标）全走这里。
- `T2VClient`：`submit(prompt, first_frame, last_frame=None, duration_seconds, size)` + `query(task_id)`
- `ASRClient`：`transcribe(audio_bytes, lang)`

### 5.3 Plan / Manifest 核心 schema（Pydantic v2）

```python
class SampleManifest(BaseModel):
    sample_id: str
    video_type: Literal["marketing", "editing", "motion_graph"]  # 决定段落 kind 三选一
    duration_seconds: float
    has_voice: bool                        # librosa VAD 判定，纯 BGM 视频为 False
    shots: list[Shot]                      # PySceneDetect 输出，含多模态 LLM 帧标签
    rhythm: RhythmCurve                    # 镜头切换频次 + BGM 能量
    sections: list[Section]                # marketing=hook/body/cta · editing=opening/climax/closing · motion_graph=intro/build/drop/outro
    packaging: PackagingProfile            # 字幕样式 / 标题条 / 转场统计 / 封面风格

class Plan(BaseModel):
    plan_id: str
    sample_id: str
    main_track: list[Scene]                # 主轨分镜（素材切片 + 时长 + 字幕），source ∈ {sample, user_material, aigc_t2v}
    packaging_track: list[PackagingItem]   # 包装轨：subtitle / title_bar / sticker / transition / cover
    bgm: BGMConfig
    variant: Literal["A", "B"]
```

### 5.4 Tool Calling Schema（自然语言编辑）

`POST /api/edit/apply` 走 `LLMClient.complete_with_tools()`，OpenAI 兼容的 `tools` 数组定义如下原子动作：

| tool name | 参数 | 语义 |
|---|---|---|
| `edit_scene_duration` | `{scene_id: str, duration_seconds: number}` | 改某 Scene 时长 |
| `edit_scene_narration` | `{scene_id: str, narration: str}` | 改某 Scene 口播 |
| `replace_scene_material` | `{scene_id: str, source_ref: str, source?: "user_material"\|"aigc_t2v"\|"sample"}` | 把某 Scene 的素材替换为另一条 |
| `edit_packaging_item_text` | `{item_id: str, text: str}` | 改包装轨某字幕/标题文字 |
| `set_bgm_volume` | `{volume: number}` | 改 BGM 音量（0-1） |

LLM 返回 `tool_calls[]` 后，后端按顺序原子地改 Plan JSON，再覆盖 `plan_store`，最终响应**新的完整 Plan**（不是 patch）——前端 diff 渲染。Mock provider 通过 user 文本关键字（"时长" / "字幕" / "替换"）猜 tool，保证 mock 模式跑通。

### 5.5 SSE 协议（拆解 / 渲染）

两条 SSE 端点（`/api/decompose/stream`、`/api/render/stream`）共用同一种事件协议：

```
event: progress
data: {"step": "scene_detect", "percent": 10, "payload": {"note": "PySceneDetect 切镜头"}}

event: done
data: {"job_id": "...", "payload": {...终态结果...}}

event: error
data: {"detail": "..."}
```

- `step` 列表（decompose）：`scene_detect → audio_analysis → voice_detect → asr_transcribe → vlm_tag → llm_section → done`
- `step` 列表（render）：`concat → seedance → remotion → overlay → cover → done`
- 客户端用 `EventSource`（前端 `@/api/sse` 薄封装）订阅，断线由浏览器自动重连
- `payload` 是 free-form dict，前端按 step 查需要的字段（如 voice_detect 的 `has_voice` / render 的 `timings_ms`）

## 6. 安全边界

| 维度 | 措施 |
|---|---|
| API Key 管理 | 所有 Key 走 `server/.env`，`chmod 600`；前端永不持有 Key；mock 模式不依赖任何 Key。`.env` 已加入 `.gitignore`；每次 commit 前 `git diff` 自查 `ark-[a-f0-9]` 等指纹，确认无泄漏 |
| 用户上传素材 | 仅落地到 `server/var/uploads/<session_id>/`；session 隔离；MIME 白名单（mp4/mov/webm/jpg/png/webp/mp3/wav）+ 单文件 50MB 上限；不入库；用户主动删除时同步清盘 |
| Prompt 注入 | LLM 输入侧对用户文本做长度上限（brief ≤ 500 字、edit instruction ≤ 1000 字）+ 结构化打包；输出侧强制 JSON schema 校验 + 单次重试；多模态 `complete_multimodal` 对图像路径做存在性检查，不存在时回落 1×1 占位 PNG |
| 视频/图像生成内容审核 | 调用豆包/Seedance 时透传 `trace_id`，厂商侧做内容审核；服务侧拒绝包含明显违规关键词的 brief |
| 跨域隔离 | 已有 COOP/COEP 中间件；`/api/*` 路径豁免（不影响 CORS）；ffmpeg.wasm 浏览器抽帧仍需 `crossOriginIsolated` |
| 任务隔离 | 每个 job 独立 `var/outputs/<job_id>/` 目录 + `trace_id`；失败回收临时文件；JobStore 进程内不跨用户；SSE 订阅鉴别 job_id 不存在则 404 |
| 模型降级 | 任何 provider 失败自动回落 mock 并通过 SSE `payload.note` 提示，绝不静默失败：scene_detect → 等分镜头；audio_analysis → 假节奏曲线；ASR → 占位 transcript；VLM → 空标签；LLM sections/packaging → 规则兜底 |
| Tool calling 安全 | edit 路径的 5 个原子 tool 走显式参数校验（Pydantic），LLM 返回未知 tool name 直接拒绝；改 Plan 走 `plan_store.replace()` 而非原地变更，便于撤销栈回滚 |
| 端到端可观测 | 每条请求带 `X-Trace-Id`；日志格式 `[trace_id] METHOD PATH -> STATUS (Nms)`；agent 内部按 `step` 推 SSE 进度，方便复盘哪一步降级 |

## 7. 任务路线图

参见会话期 TaskList。关键依赖：

```
阶段 0  · 清理废弃代码
        │
        ├──► 阶段 1 · web/ 前端骨架 ──► 模块 1-7 前端
        └──► 阶段 1 · 后端 schemas+路由骨架
                 │
        ┌────────┴────────────────┐
        ▼                         ▼
  阶段 2 · 视频处理底层      阶段 2 · AI 客户端
        │                         │
        └───────────┬─────────────┘
                    ▼
   ┌────────────┬─────────────┬──────────────┐
   ▼            ▼             ▼              ▼
拆解 Agent  缺口 Agent  Remotion 包装 ──► 视频渲染流水线 ──► 长视频拼接
                                                                │
                                                                ▼
                                                    阶段 5 · 联调 + 交付物
```

## 8. 历史 & 当前状态

- 仓库由 KOCopilot fork 改名而来（HEAD: ddea395，2026-05-22）
- 上一版形态为"创作者副驾"（人设/拆解/标题/评论/分镜五件套），已退役
- **2026-05-26：阶段 0–5 全部落地，7 模块 mock 模式端到端跑通；本日同步把 6 个 AI 干预点合并为 3 类（独立 VLM/T2I client 退役，多模态 LLM 接管画面理解；aigc 缺口由 Seedance T2V 直接生成短片）。**

### 8.1 落地清单（按模块）

| 模块 | 后端 | 前端 | 测试 |
|---|---|---|---|
| 1 · 素材库 | `routers/library.py` 静态 3 样例（marketing/editing/motion_graph 各一）+ manifest stub | `pages/Library.tsx` 卡片 + 选样例（带 video_type 标识） | `test_library_and_manifest`（覆盖 3 类型） |
| 2 · 样例拆解 | `routers/decompose.py` + `services/agent/decompose_agent.py`（PySceneDetect / librosa BGM+VAD / 条件 ASR / 多模态 LLM 打标 / 多模态 LLM 段落三选一 prompt） | `pages/Decompose.tsx` SSE 进度 + 节奏曲线 + 段落条（9 个 kind 着色）+ 镜头网格 + video_type radio | `test_agent_routing::test_decompose_routes_by_video_type` |
| 3 · 新内容 + 缺口 | `routers/material.py` 上传 + 多模态 LLM 标签；`routers/gap.py` detect/fill（rerank/copy/aigc=Seedance T2V） | `pages/Compose.tsx` UploadDropzone + Gap 列表 + 三种动作 | `test_material_upload_and_plan_build` + `test_agent_routing::test_gap_agent_aigc_*` |
| 4 · 迁移可视化 | 复用 `gap.detect` 输出 | `pages/Migrate.tsx` React Flow 双列 + 状态着色 | （前端集成验证） |
| 5 · 视频生成 | `services/render/pipeline.py` 6 步流水线 + 真实/mock fallback；`services/render/seedance_chain.py` 首尾帧拼接 | `pages/Render.tsx` 6 步进度条 + `<video>` 预览 + 分步耗时 | `test_render_submit_and_stream` |
| 6 · 画面包装 | `remotion/` 独立 Node 项目 + `services/render` 子进程调用 | `pages/Render.tsx` 包装轨横向时间线 | （Remotion 单独 `npm test`） |
| 7 · 自然语言编辑 | `routers/edit.py` 5 个原子 tool + `LLMClient.complete_with_tools` | `pages/Render.tsx` 底部 textarea + marks + 撤销/重做（`stores/edit.ts`） | `test_edit_apply_creates_new_plan` |

### 8.2 端到端验收

```bash
cd server
python -m pytest tests/ -v                # 49 passed（含 6 个 e2e + 10 个 agent 路由 + 19 个 LLM/Mock + 14 个 ASR）
```

详细演示剧本见 [`docs/DEMO.md`](DEMO.md)。


