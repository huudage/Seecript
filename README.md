# Seecript

> 视频拆解与重组的助手（爆款结构迁移引擎）：从样例视频拆解 → 结构抽取 → 素材缺口补全 → 视频重组的 AI 创作平台。默认 mock 模式不依赖任何外部 API Key 即可端到端跑通。

> **当前状态（2026-05-26）**：阶段 0–5 全部落地，7 模块 mock 模式端到端跑通。技术架构以 [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) 为准；演示剧本见 [`docs/DEMO.md`](docs/DEMO.md)。

---

## 1. 模块速览

| # | 模块 | 路由 / 路径 | 关键技术 |
|---|---|---|---|
| 1 | 素材库 | `/library` · `GET /api/library` | 3 个内置样例 + 预解析 manifest |
| 2 | 样例拆解 | `/decompose` · `POST /api/decompose` SSE | PySceneDetect + librosa + ASR + VLM + LLM 段落 |
| 3 | 新内容 + 缺口补全 | `/compose` · `POST /api/material/upload` `gap/detect` `gap/fill` | VLM 标签 + 槽位匹配 + 三种补全（rerank/copy/aigc） |
| 4 | 迁移可视化 | `/migrate` | React Flow 双列 + 状态着色（绿命中 / 黄勉强 / 红虚线缺口） |
| 5 | 视频生成 | `/render` · `POST /api/render/submit` SSE | FFmpeg 主轨 + Seedance 首尾帧扩展 + 6 步流水线 |
| 6 | 画面包装 | 同上 | Remotion 透明 WebM + ffmpeg overlay |
| 7 | 自然语言编辑 | 同上 · `POST /api/edit/apply` | LLM tool calling 5 原子 tool + 撤销栈 |

---

## 2. 仓库结构

```
seecript/
├── web/                                # React 19 + Vite + TS + Tailwind v4 + Zustand
│   └── src/
│       ├── pages/                      # Library / Decompose / Compose / Migrate / Render
│       ├── stores/                     # session / plan / edit
│       ├── api/                        # client.ts (fetch) + sse.ts (EventSource)
│       └── types/schemas.ts            # 与后端 schemas 镜像
│
├── remotion/                           # 包装轨独立 Node 项目（透明 WebM）
│
├── server/
│   ├── app/
│   │   ├── main.py                     # FastAPI + middleware + 8 路由
│   │   ├── schemas.py                  # 7 模块完整 Pydantic v2 契约
│   │   ├── routers/                    # asr / library / decompose / material / gap / plan / render / edit
│   │   └── services/
│   │       ├── llm_client.py           # LLMClient 抽象 + Mock + DeepSeek + Doubao Ark
│   │       ├── vlm_client.py · t2i_client.py · t2v_client.py · asr_client.py
│   │       ├── video/                  # scene_detect / audio / ocr / ffmpeg
│   │       ├── agent/                  # decompose_agent / gap_agent
│   │       ├── render/                 # pipeline.py（6 步）+ seedance_chain.py（首尾帧拼接）
│   │       ├── jobs/                   # 内存 JobStore + asyncio.Queue + SSE
│   │       └── plans/                  # PlanStore（plan_id → Plan）
│   ├── tests/                          # 35 用例（mock 路径）
│   ├── samples/                        # 3 个内置样例（视频 + 预解析 JSON）
│   └── var/uploads/                    # 用户上传（session 隔离）
│
├── docs/
│   ├── ARCHITECTURE.md                 # 技术架构（赛题交付物，权威）
│   ├── DEMO.md                         # 5 分钟串讲剧本 + 联调验收
│   ├── PRD.md                          # 上一版 PRD（已贴重构横幅）
│   └── INFRA.md                        # 生产服务器约定
└── deploy/ · scripts/ · run.* · stop.*
```

---

## 3. 本地运行

环境：**Python 3.10+** · **Node 20+**（前端）。

### 后端

```powershell
.\run.ps1                                # Windows
# 或
./run.sh                                 # Linux / macOS / WSL
```

首次：自举 venv → 装依赖 → 拷 `.env`（默认 `*_PROVIDER=mock`）→ 起 uvicorn。

| 操作 | 命令 |
|---|---|
| 健康检查 | <http://127.0.0.1:8090/api/health> |
| API 文档 | <http://127.0.0.1:8090/docs> |
| 换端口 | `$env:PORT=8091; .\run.ps1` |
| 跳过 pip | `$env:SKIP_INSTALL=1; .\run.ps1` |
| 停服 | `.\stop.ps1` / `./stop.sh` |
| 日志 | `./logs/uvicorn.log` |

### 前端

```bash
cd web
pnpm install                             # 或 npm i
pnpm dev                                 # http://127.0.0.1:5173，/api/* 走 vite proxy → 8090
```

### 切换到真实 provider

编辑 `server/.env`：

```ini
LLM_PROVIDER=doubao_ark
DOUBAO_ARK_API_KEY=ark-xxxxxxxx
DOUBAO_ARK_LLM_ENDPOINT=ep-xxxxxxxx

VLM_PROVIDER=doubao_ark
T2I_PROVIDER=seedream
T2V_PROVIDER=seedance

ASR_PROVIDER=doubao
DOUBAO_API_KEY=<volc-uuid-key>
```

每个开关独立，可以只接其中一个。字段全集见 `server/.env.example`。**Key 文件须 `chmod 600`，前端永不持有 Key。**

---

## 4. 测试

```bash
cd server
python -m pytest tests/ -v               # 35 passed
```

包括：
- `test_e2e_pipeline.py` — 7 模块端到端联调
- `test_llm_client.py` / `test_asr_client.py` — provider 抽象 + mock fallback
- `test_asr_endpoint.py` — multipart + 大小/格式校验

所有测试默认走 mock 路径，不消耗任何外部 API 配额。

---

## 5. 部署到生产

```bash
sudo REPO_URL=git@github.com:you/seecript.git \
     DOMAIN=seecript.example.com \
     bash scripts/install-on-medi-server.sh
```

后续升级：

```bash
bash scripts/deploy.sh                   # 备份 → git pull → pip install → restart → health-check → 失败回滚
bash scripts/health-check.sh https://seecript.example.com
```

服务器基础设施约定见 [`docs/INFRA.md`](docs/INFRA.md)。

---

## 6. 关联文档

- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — 7 模块技术架构（赛题交付物，权威）
- [`docs/DEMO.md`](docs/DEMO.md) — 演示走查手册 + 联调命令
- [`docs/INFRA.md`](docs/INFRA.md) — 生产基础设施约定
- [`server/.env.example`](server/.env.example) — 所有配置项
- [`CLAUDE.md`](CLAUDE.md) — Claude Code 会话备忘
