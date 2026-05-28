# Seecript · 交付物清单

> 工程训练营赛题「爆款结构迁移引擎 — 从样例拆解、素材补全到视频重组的 AI 创作平台」交付索引。
> 评委验收按本清单逐项核对即可。

---

## 1. 文档交付物

| # | 文件 | 用途 | 状态 |
|---|---|---|---|
| 1 | [`docs/ARCHITECTURE.md`](ARCHITECTURE.md) | 整体 AI 架构 · 工具协议 · 安全边界（赛题要求权威） | ✅ |
| 2 | [`docs/AI-DESIGN.md`](AI-DESIGN.md) | 3 类 AI 干预点详解：多模态 LLM / VAD-门控 ASR / Seedance T2V | ✅ |
| 3 | [`docs/DEMO.md`](DEMO.md) | 5 分钟演示剧本 + 联调验收脚本 | ✅ |
| 4 | [`docs/INFRA.md`](INFRA.md) | 生产服务器基础设施约定 | ✅ |
| 5 | [`docs/PRD.md`](PRD.md) | 上一版 PRD（已贴重构横幅，作为变更轨迹保留） | ✅ |
| 6 | [`docs/screencast-script.md`](screencast-script.md) | 录屏分镜脚本（每一帧讲什么） | ✅ |
| 7 | [`docs/DELIVERY.md`](DELIVERY.md) | 本文件：所有交付物索引 | ✅ |
| 8 | [`README.md`](../README.md) | 仓库门面：模块速览 + 本地运行 + 测试 + 部署 | ✅ |

---

## 2. 代码交付物（按赛题 7 个功能模块）

| # | 模块 | 后端落点 | 前端落点 | 测试 |
|---|---|---|---|---|
| 1 | 素材库 | `server/app/routers/library.py` + `server/samples/` 3 个内置样例 | `web/src/pages/Library.tsx` | `test_library_and_manifest` |
| 2 | 样例拆解 | `routers/decompose.py` + `services/agent/decompose_agent.py`（PySceneDetect / librosa BGM+VAD / 条件 ASR / 多模态 LLM 段落 + 帧打标 / `_compute_climax` 高潮位置） | `pages/Decompose.tsx`（SSE 进度 + 节奏曲线含高潮 `ReferenceLine` + 段落条 + 镜头网格） | `test_agent_routing::test_decompose_routes_by_video_type` |
| 3 | 新内容 + 缺口补全 | `routers/material.py`（多模态 LLM 含 `highlight_score`）+ `routers/gap.py` + `services/agent/gap_agent.py`（rerank / copy / aigc=Seedance T2V，高 impact 段先吃高光素材） | `pages/Compose.tsx` + `components/compose/PackagingPanel.tsx`（封面预览） | `test_material_upload_and_plan_build` + `test_agent_routing::test_gap_agent_aigc_*` |
| 4 | 迁移可视化 | 复用 `gap.detect` 输出 | `pages/Migrate.tsx`（React Flow 双列 + 状态着色） | 前端集成验证 |
| 5 | 视频生成 | `services/render/pipeline.py`（6 步）+ `services/render/seedance_chain.py`（首尾帧串接，doubao-seedance-2-0-fast-260128） | `pages/Render.tsx`（6 步进度条 + `<video>` 预览 + 分步耗时表） | `test_render_submit_and_stream` |
| 5b | 包装推荐 | `routers/packaging.py` + `services/agent/packaging_agent.py`（LLM 一次性给 6 种转场 + 封面，回写 `plan.packaging_track` + 时间轴对齐 + 兜底） | `components/compose/PackagingPanel.tsx`（一键推荐 → 自动 refetch plan） | mock 路径冒烟（mock LLM provider 路由命中 packaging） |
| 6 | 画面包装 | `remotion/`（独立 Node 项目，含 `Cover.tsx` + 6 风格 `Transition.tsx` + `Subtitles.tsx` 等）+ `services/render` 子进程调用 → ffmpeg overlay | `pages/Render.tsx` 包装轨横向时间线 | Remotion 单独 `npm test` |
| 7 | 自然语言编辑 | `routers/edit.py` + `LLMClient.complete_with_tools`（5 个原子 tool） | `pages/Render.tsx` 底部 textarea + marks + 撤销/重做（`stores/edit.ts`） | `test_edit_apply_creates_new_plan` |

> 多版本（A/B）一项**未实现**，已在本届交付范围外。其余 7 个模块 + 包装推荐子模块 5b 全部落地。

---

## 3. API 端点清单（赛题「工具协议」交付）

| 端点 | 方法 | 用途 |
|---|---|---|
| `/api/health` | GET | 健康检查（5 个 provider 状态） |
| `/api/library` | GET | 列出 3 个内置样例 |
| `/api/sample/{id}/manifest` | GET | 取样例预解析 manifest |
| `/api/decompose` | POST | 触发拆解任务 |
| `/api/decompose/stream` | GET (SSE) | 推拆解进度 |
| `/api/material/upload` | POST | 上传新素材（多模态 LLM 自动打标） |
| `/api/gap/detect` | POST | 槽位匹配（9 个 SectionKind） |
| `/api/gap/fill` | POST | 补全单个缺口（rerank / copy / aigc） |
| `/api/gap/aigc-refresh` | POST | 前端轮询 Seedance task_id |
| `/api/plan/build` | POST | 组装最终 Plan |
| `/api/plan/{plan_id}` | GET | 拉最新 Plan（包装推荐回写后前端用） |
| `/api/packaging/recommend` | POST | LLM 给转场 + 封面，apply=true 写回 `packaging_track` |
| `/api/render/submit` | POST | 提交渲染任务 |
| `/api/render/stream` | GET (SSE) | 推渲染进度 |
| `/api/edit/apply` | POST | 自然语言改片（LLM tool calling） |
| `/api/asr` | POST | ASR 直调（调试用） |

完整 schema 见 `server/app/schemas.py`（Pydantic v2 契约）+ `http://127.0.0.1:8090/docs`（OpenAPI 自动生成）。

---

## 4. AI 模型清单（赛题「整体 AI 架构」交付）

| 用途 | 模型 | 调用点 |
|---|---|---|
| 段落结构 / 帧打标 / 缺口文案 / 包装推荐 / NL 编辑 tool call | Doubao-Seed-2.0-lite（多模态） | `LLMClient.complete_multimodal` / `complete_json` / `complete_with_tools` |
| ASR 口播转写（VAD 门控） | 豆包 bigasr_auc_turbo | `ASRClient`，librosa VAD 先判定有口播才调用 |
| 视频生成（aigc 缺口 + 长视频首尾帧） | doubao-seedance-2-0-fast-260128 | `T2VClient.submit/query`，30-60s 渲染中位数 |

> 原 6 个 AI 干预点合并为 3 类，独立 VLM / T2I client 全部退役，画面理解全走多模态 LLM；aigc 缺口由 T2V 直接生成 5-8s 短片。

---

## 5. 安全边界交付（赛题要求）

详见 [`ARCHITECTURE.md` §6](ARCHITECTURE.md#6-安全边界)。要点：

- **API Key 管理**：所有 Key 走 `server/.env` + `chmod 600`，前端永不持有，mock 模式不依赖任何 Key
- **用户上传素材**：MIME 白名单（mp4/mov/webm/jpg/png/webp/mp3/wav）+ 50MB 上限 + session 隔离 + 用户删除时同步清盘
- **Prompt 注入**：brief ≤ 500 字 / edit instruction ≤ 1000 字 + JSON schema 校验 + 单次重试
- **模型降级**：任何 provider 失败自动回落 mock 并 SSE 提示，绝不静默失败
- **Tool calling**：edit 路径 5 个原子 tool 走 Pydantic 严格校验，未知 tool name 拒绝
- **可观测**：每条请求 `X-Trace-Id`，agent 内部按 step 推 SSE 进度

---

## 6. 演示输出物（交给评委的物料）

| 物料 | 文件 | 说明 |
|---|---|---|
| 录屏脚本 | [`docs/screencast-script.md`](screencast-script.md) | 每帧讲什么，5 分钟剧本 |
| 演示截图 | [`docs/screenshots/`](screenshots/) | 关键界面静态截图（备份） |
| Mock 端到端测试 | `cd server && python -m pytest tests/ -v` | 49 passed，全部走 mock 路径不耗 API |
| 真实模型联调脚本 | [`docs/DEMO.md`](DEMO.md#2--联调验收脚本) | 切到真 provider 后的验收命令 |

---

## 7. 演示走查流程（评委 5 分钟版）

按以下顺序点击即可走完 7 模块：

1. `/library` → 选「营销-护肤产品测评」样例
2. `/decompose` → 点「开始拆解」→ 看 SSE 进度 → 看节奏曲线（含红色虚线高潮位置）+ 段落条
3. `/compose` → 拖 2-3 个 mp4/jpg → 填创作者主题（强制） → 点「开始构建」
4. 缺口列表里：
   - 点一个 `ok` 缺口的「采纳文案」按钮 → 看 LLM 文案补全
   - 点一个 `miss` 缺口的「AIGC 生成」按钮 → 看 Seedance T2V 调用（mock 即时返回）
   - 点「包装推荐」按钮 → 看 6 风格转场 + 封面预览
5. `/migrate` → 看 React Flow 双列连线（绿 / 黄 / 红虚线）
6. `/render` → 点「提交渲染」→ 看 6 步进度条 → 视频预览
7. 在 `/render` 底部输入「把开场改得更口语化」→ 应用 → 看 Plan diff + 撤销栈

详细操作清单见 [`docs/DEMO.md`](DEMO.md)。

---

## 8. 版本与变更轨迹

- **2026-05-22**：从 KOCopilot fork 改名为 Seecript（HEAD ddea395）
- **2026-05-26**：阶段 0–5 全部落地，7 模块 mock 模式端到端跑通；6 个 AI 干预点合并为 3 类
- **2026-05-27**：高光评分驱动槽位排序 + 高潮时间点可视化 + 转场/封面 LLM 推荐回写 `packaging_track`；模型升级到 doubao-seedance-2-0-fast-260128
