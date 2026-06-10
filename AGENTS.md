# Seecript · AI Agent Briefing

> 给 Claude Code / Cursor / 任何代码 agent 接手本项目前必读。
> 公开 README 是给人看的；本文是给写代码的 agent 看的——硬约束、硬规矩、硬目录。

## 1. 项目身份

Seecript 是把**爆款视频拆成可复用的结构骨架**，再把**用户自己的素材**对齐到该骨架上**重组**成新视频的 AI 创作工坊。

四阶段链路：
```
样例库 → 样例拆解 → 缺口补全（结构重排 / 文案补全 / AIGC 生图生视频） → 视频重组 → 自然语言编辑
```

核心能力：
- PySceneDetect 镜头切分 + librosa BGM 能量曲线 + librosa VAD 门控 ASR
- 多模态 LLM（Doubao Seed-2.0-lite）：帧打标 / 段落结构 / 缺口文案 / NL 编辑 tool calling
- Doubao Seedance T2V 生成 5-8s 短片填补缺口；首尾帧串接长视频
- Doubao Seedream T2I 生静图，Remotion 渲 ken-burns / keyframe_morph 动效
- Remotion 包装轨（透明 WebM）+ ffmpeg overlay 主轨 + BGM
- LLM 多信号情绪曲线（stage-28，emotion_agent）

## 2. 技术栈

| 层 | 选型 |
|---|---|
| 后端 | FastAPI + Pydantic v2 + Python 3.10+ |
| 前端 | React 19 + Vite + TypeScript + Tailwind v4 + Zustand |
| 视频包装 | Remotion（独立 `remotion/` Node 项目） |
| LLM | Doubao Seed-2.0-lite（多模态，OpenAI 兼容） |
| ASR | 豆包 bigasr_auc_turbo + librosa VAD 门控 |
| T2V | doubao-seedance-2-0-fast-260128（submit + 轮询 query） |
| T2I | doubao-seedream（多镜头 storyboard） |
| 任务编排 | FastAPI BackgroundTasks + 内存 JobStore + SSE |

## 3. 目录速查

```
seecript/
├── web/src/                              React 前端
│   ├── pages/                            Library / Decompose / Compose / Render
│   ├── components/{compose,edit,...}     业务组件
│   ├── stores/                           Zustand stores（session/plan/edit/projects）
│   ├── api/{client,sse}.ts               fetch 封装 + EventSource
│   └── types/schemas.ts                  ★ 必须与后端 schemas.py 镜像同步
│
├── server/app/
│   ├── main.py                           FastAPI 入口 + CORS + 静态挂载（/samples /uploads /assets /aigc-images /aigc-videos）
│   ├── config.py                         Pydantic Settings + provider 开关
│   ├── schemas.py                        ★ 全模块 Pydantic v2 契约
│   ├── routers/                          路由层（一个文件一个业务模块）
│   └── services/
│       ├── llm_client.py                 LLMClient 抽象 + Doubao Ark + Mock + 多模态
│       ├── asr_client.py                 ASR 抽象 + VAD 门控
│       ├── t2v_client.py                 Seedance submit + poll
│       ├── seedream_client.py            Seedream T2I 多镜头
│       ├── video/                        ffmpeg / scene_detect / aspect / bgm_analysis / voice_detect / ocr / remotion
│       ├── agent/                        decompose / plan / gap / packaging / clarify / copy_outline / aigc_prompt / compose_edit / emotion / shot_matcher
│       ├── render/                       pipeline.py（6 步）+ seedance_chain.py（首尾帧）+ remotion_renderer.py
│       ├── materials/ assets/ library/   素材 / 资产 / 样例库
│       ├── plans/ projects/ jobs/        Plan / Project / Job 持久化
│       └── tts/ prompts/ profile/        TTS / 提示词 / 用户偏好
│
├── remotion/src/                         Cover / Subtitles / TitleBar / StickerOverlay / Transition / AnimatedImage / PackagingTrack
├── server/samples/<sys-id>/              内置样例（video.mp4 + 预解析 manifest）
├── server/var/                           运行期产物（outputs / projects / uploads / aigc_videos / aigc_images）
└── docs/                                 ARCHITECTURE / AI-DESIGN / PRD / DEMO / CATALOG_FRAME
```

## 4. 启动 / 测试

```bash
# 本地开发
./run.ps1                                  # Windows，自举 venv + 起 :8090 后端
./run.sh                                   # macOS/Linux
cd web && pnpm install && pnpm dev         # :5173 前端

# 后端测试
cd server && python -m pytest tests/ -v

# 前端类型检查 + 构建
cd web && npx tsc -p tsconfig.app.json --noEmit && npx vite build

# 健康检查
curl http://127.0.0.1:8090/api/health      # 看 5 个 *_provider 状态
```

## 5. 给 agent 的硬规矩

### 5.1 后端优先

任何新 feature 必须**后端真实装**，不能只摆前端壳。前端只是后端能力的 UI 投影。

### 5.2 真链路原则

所有任务必须能在真 LLM / Seedance / ASR / ffmpeg 下跑通。**Mock 仅用于单测**——生产路径不允许 mock 兜底，缺 API key 必须硬失败 5xx，绝不静默降级到 mock。

历史已删的 mock 兜底：`_stub_manifest` / `_stub_sections` / render placeholder。**不要再加回来。**

保留的 LLM fallback：`_fallback_outline` / `_fallback_adaptation` / `_role_mood_value` 规则版（emotion_agent 在 LLM 失败时用）——这些是**规则降级**，不是 mock 数据，是允许的。

### 5.3 schemas 双向镜像

`server/app/schemas.py` 改了 → `web/src/types/schemas.ts` 必须同步改。前后端契约靠 Pydantic v2 + TS interface 双边绑定。

### 5.4 SSE 协议

所有 SSE 端点（`/api/decompose/stream` / `/api/render/stream` / `/api/conversation/stream` / 等）共用同一种事件协议：

```
event: progress
data: {"step": "scene_detect", "percent": 10, "payload": {...}}

event: done
data: {...终态结果...}

event: error
data: {"detail": "..."}
```

JobStore 在 `services/jobs/`，订阅靠 `asyncio.Queue`。

### 5.5 三层兜底范式

每个 LLM 调用都按这个范式：

```
Layer 1 · Prompt（软约束，system message 写约束）
Layer 2 · Router（硬约束，路由层 HTTPException 拦截）
Layer 3 · Schema（终判约束，Pydantic 校验，畸形 422）
```

通用兜底（所有 LLM 调用共享）：
- JSON 解析自愈：`_extract_json` 剥离 markdown 代码栅栏
- JSON 失败重试一次：拼更严格 system 再调
- HTTP 状态码透传：5xx → LLMError → 路由 502
- 结构化日志：`[trace_id] module ok | provider | elapsed_ms | tokens`

### 5.6 LLM tool calling 模式

NL 编辑走 `LLMClient.complete_with_tools()`：

- **Render 态**（`/api/edit/apply`）：按 `track ∈ {main, packaging, voice}` 分流，每个 track 一个 system prompt + 子集 tools
- **Compose 态**（`/api/edit/compose`）：按 `step ∈ {step2, step3}` 分流；step2 全开但禁 aigc_image/t2v；step3 禁内容轨

工具集中定义在 `services/agent/compose_edit_agent.py` 与 `routers/edit.py`。**新增 tool 必须**：① schema 写在 `_TOOL_*` 常量里；② mutator 函数返回 `ComposeEditDiff`；③ apply=False 时只算 diff，apply=True 才落盘。

### 5.7 Plan 不可变更新

改 Plan 走 `plan_store.replace()` 而非原地变更。每次都生成新 `plan_id`，旧 plan 留在 `PlanSnapshotStore` 供撤销栈回滚。前端 `stores/edit.ts` 维护 undo/redo。

### 5.8 静态资源挂载

URL 前缀 → 物理目录映射（`main.py` 已挂载，新增资源类型同此模式）：

| URL | 物理路径 |
|---|---|
| `/samples/<id>/...` | `server/samples/<id>/` |
| `/uploads/decompose/<id>/...` | `server/var/uploads/decompose/<id>/`（用户上传待拆解视频） |
| `/uploads/<project_id>/...` | `server/var/uploads/<project_id>/`（项目素材） |
| `/assets/<project_id>/<kind>/...` | `server/var/assets/<project_id>/<kind>/` |
| `/aigc-images/...` | `server/var/aigc_images/` |
| `/aigc-videos/...` | `server/var/aigc_videos/`（解决 TOS 跨域） |

CSS `background-image: url()` 不渲 mp4——卡片封面**必须**抽 jpg。

### 5.9 部署

生产：`root@47.239.58.145:5002`，`/opt/seecript`，`systemctl restart seecript-server`，外网 `https://seecript.zlhu.asia`。

repo 是 private → 服务器**不能 git pull**，必须 tar 推：

```bash
tar 打包排除：.env web/node_modules server/venv __pycache__ .git var server/var server/samples/*/video.mp4 logs .scratch
tar 打包包含：web/dist
scp → /opt/seecript → chown seecript:seecript → systemctl restart seecript-server
```

服务器 `.env` 必须有 `SEEDREAM_PROVIDER=doubao_ark`、`PUBLIC_AUDIO_BASE_URL=https://seecript.zlhu.asia`（豆包 ASR 2.0 要求 audio.url 公网可达）。

### 5.10 文档/注释纪律

- 默认不写注释。只在 WHY 非显而易见时才写一行（隐藏约束 / 历史 bug 的 workaround）。
- 不写 `//removed` / `// added for X` 这种活注释——属于 PR 描述。
- 不创建 PRD / 决策 / 分析文档除非用户明说要。
- `docs/*.md` 改完**只本地 commit，不推 GitHub / 不部署**——除非同轮也改了代码。

### 5.11 对话 / 编辑 agent 的"不脑补"原则

`compose_edit_agent` 的 `_QA_RULES`：

- **A) 编辑**（tool_calls）：用户**明确说**改什么+改成什么才用
- **B) 讲解**（直接 1-3 句中文）：基于当前 Plan 概览，不编造
- **C) 追问**（1 句反问）：意图模糊（"调整一下第二段"）必须反问

**禁止把单一指令拆成多个 diff**——一句"调整第二段"不能产生"改文案+改时长"两个 tool_call。

## 6. 关键事实

- **不存在的工具**：删字幕（只能 `subtitle_enabled` 全关或改文字）、单段情绪强化（只有全局 `migration_preference=amp_emotion`）
- **延迟生效字段**：`migration_preference` 改了不会立即重算 timeline，要 `regenerate_fill` / `recompute-emotion` 才生效
- **Section role 系统**：`marketing=hook/body/cta` · `editing=opening/climax/closing` · `motion_graph=intro/build/drop/outro`，9 个 SectionKind
- **Sample id 命名空间**：`sys-*` 是内置样例（`server/samples/`）；`user-*` 是用户上传（`server/var/uploads/decompose/`）

## 7. 进一步阅读

- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — 整体架构 + 数据流 + 工具协议 + 安全边界
- [`docs/AI-DESIGN.md`](docs/AI-DESIGN.md) — AI 干预点详解 + 三层兜底
- [`docs/PRD.md`](docs/PRD.md) — 产品需求文档（基于代码现状）
- [`docs/CATALOG_FRAME.md`](docs/CATALOG_FRAME.md) — HyperFrames catalog + frame.md 设计系统
- [`docs/DEMO.md`](docs/DEMO.md) — 5 分钟演示走查
