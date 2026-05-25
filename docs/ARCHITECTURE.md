# Seecript · 爆款结构迁移引擎 — 技术架构

> 视频拆解与重组的助手。围绕赛题「爆款结构迁移引擎 — 从样例拆解、素材补全到视频重组的 AI 创作平台」构建。
>
> 本文档沉淀技术栈选型、数据流、任务拆解，作为赛题交付的「整体 AI 架构、工具协议和安全边界」依据。

## 1. 功能模块映射（赛题需求 → 技术实现）

| # | 模块 | 关键技术 |
|---|---|---|
| 1 | 素材库 | 静态 3 个内置样例（营销/剪辑/Motion Graph），预解析 manifest 缓存 |
| 2 | 样例拆解（BGM/整体结构） | PySceneDetect 镜头切分 + librosa BGM 能量曲线 + ASR 口播 + VLM 帧打标 + LLM 段落结构（Hook/Body/CTA） |
| 3 | 新内容输入 + 缺口识别 + 补全 | VLM 素材打标 + 槽位匹配算法 + 三种补全（结构重排 / 文案补全 / Seedream AIGC） |
| 4 | 迁移过程可视化 | React Flow 流程图（样例槽位 → 新方案分镜的连线，缺口红虚线 / 补全绿标）+ 三栏可调宽度布局 |
| 5 | 视频生成 | FFmpeg 主轨拼接 + doubao-seedance-1.0-pro 首尾帧扩展长视频 + SSE 推进度 + AB 双版本 |
| 6 | 画面包装生成 | Remotion 包装轨道（字幕/标题条/转场/封面）独立子进程渲染 → ffmpeg overlay 叠加 |
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

| 用途 | 模型 | 说明 |
|---|---|---|
| 段落结构/Hook/CTA/补全文案 | Doubao-Seed-2.0-lite | 赛题给的 EP，OpenAI 兼容 |
| 视频内容理解 | Doubao-Seed-1.6-vision (VLM) | 关键帧抽样后批量打标：封面风格 / 转场类型 / 字幕样式 / 物体场景 |
| ASR 口播转写 | 豆包 bigasr_auc_turbo | 复用已有客户端 |
| 文生图（缺口补全 + 首尾帧） | Seedream 4.0 | 配合 seedance 做长视频首尾帧 |
| 视频生成 | doubao-seedance-1.0-pro | 图生视频-首尾帧模式，单段 2-12s，前段尾帧 → 下一段首帧 → 拼 30-60s |

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
                                          ASR ──────────────► 口播文本 ──► LLM ──► Hook/Body/CTA
                                          VLM 抽帧 ─────────► 封面风格/转场/字幕样式
                                          ↑ SSE /api/decompose/stream 推每一步进度

3. 上传新素材 ─────────────────────────►  POST /api/material/upload (multipart)
                                          VLM ──────────────► 素材打标 + 段落推荐

4. 缺口识别 ──────────────────────────►  POST /api/gap/detect
                                          槽位匹配算法 (Python 纯计算)

5. 缺口补全 ──────────────────────────►  POST /api/gap/fill (action: rerank|copy|aigc)
                                          LLM 文案 / Seedream 文生图 ──► 缺失画面

6. 方案生成 ──────────────────────────►  POST /api/plan/build
                                          组装分镜时间线 (Pydantic)

7. 视频生成 ──────────────────────────►  POST /api/render/submit  (SSE 推进度)
                                          FFmpeg concat ──► 主轨 MP4
                                          Seedance 首尾帧 ──► 长视频段
                                          Remotion ──► 包装轨道透明 WebM
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
│   │   │   ├── llm_client.py           # 保留 LLMClient 抽象 + 加 Doubao Seed-2.0-lite
│   │   │   ├── asr_client.py           # 保留
│   │   │   ├── vlm_client.py           # 新：Doubao Seed-1.6-vision
│   │   │   ├── t2i_client.py           # 新：Seedream 4.0
│   │   │   ├── t2v_client.py           # 改：doubao-seedance-1.0-pro 首尾帧
│   │   │   ├── video/                  # scene_detect / audio_analysis / ocr / ffmpeg / remotion
│   │   │   ├── agent/                  # decompose_agent / gap_agent
│   │   │   └── jobs/                   # 内存 JobStore + SSE 通道
│   │   └── prompts/                    # 重写所有 prompt
│   ├── samples/                        # 3 个内置样例（视频 + 预解析 JSON）
│   ├── var/uploads/                    # 用户上传素材
│   └── tests/
│
├── docs/
│   ├── ARCHITECTURE.md                 # 本文档
│   └── AI-DESIGN.md                    # 6 个 LLM/VLM 干预点详解
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
| `POST /api/render/submit` | 提交渲染任务 | `{ plan_id, variant: A \| B }` → `{ job_id }` |
| `GET /api/render/stream?job_id=...` | SSE 推渲染进度 | `event: progress` / `event: done` |
| `POST /api/edit/apply` | 自然语言改片 | `{ plan_id, instruction, marks[] }` → 新 `Plan` |

### 5.2 AI 客户端抽象

所有外部模型走统一的 client 抽象基类，保留 `Mock` 实现保证 mock 模式端到端跑通：

- `LLMClient`：`complete` / `complete_json` / `complete_with_tools`
- `VLMClient`：`tag_frames(images, taxonomy)` / `describe(image)`
- `T2IClient`：`generate(prompt, ref_image=None, size, style)`
- `T2VClient`：`submit(prompt, first_frame, last_frame=None, duration_seconds, size)` + `query(task_id)`
- `ASRClient`：`transcribe(audio_bytes, lang)`

### 5.3 Plan / Manifest 核心 schema（Pydantic v2）

```python
class SampleManifest(BaseModel):
    sample_id: str
    duration_seconds: float
    shots: list[Shot]                      # PySceneDetect 输出
    rhythm: RhythmCurve                    # 镜头切换频次 + BGM 能量
    sections: list[Section]                # Hook / Body / CTA 三段
    packaging: PackagingProfile            # 字幕样式 / 标题条 / 转场统计 / 封面风格

class Plan(BaseModel):
    plan_id: str
    sample_id: str
    main_track: list[Scene]                # 主轨分镜（素材切片 + 时长 + 字幕）
    packaging_track: list[PackagingItem]   # 包装轨（字幕/标题条/贴纸/转场）
    bgm: BGMConfig
    variant: Literal["A", "B"]
```

## 6. 安全边界

| 维度 | 措施 |
|---|---|
| API Key 管理 | 所有 Key 走 `server/.env`，`chmod 600`；前端永不持有 Key；mock 模式不依赖任何 Key |
| 用户上传素材 | 仅落地到 `server/var/uploads/<session_id>/`；session 隔离；MIME / 大小校验；不入库 |
| Prompt 注入 | LLM 输入侧对用户文本做长度上限 + 结构化 brief；输出侧强制 JSON schema 校验 + 单次重试 |
| 视频/图像生成内容审核 | 调智谱/豆包侧自带审核；额外把 `user_id` 透传给厂商做风控关联 |
| 跨域隔离 | 已有 COOP/COEP 中间件；ffmpeg.wasm 浏览器抽帧仍需 `crossOriginIsolated` |
| 任务隔离 | 每个 job 独立目录 + trace_id；失败回收临时文件；JobStore 进程内不跨用户 |
| 模型降级 | 任何 provider 失败自动回落 mock 并 toast 提示，绝不静默失败 |

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
- 当前正按本文档的栈与路线图重构


