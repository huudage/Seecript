# Seecript

> 视频拆解与重组的助手（爆款结构迁移引擎）：从样例视频拆解 → 结构抽取 → 素材缺口补全 → 视频重组的 AI 创作平台。默认 mock 模式不依赖任何外部 API Key 即可端到端跑通。

> ⚠️ **重构进行中** — 上一版"创作者副驾"形态（人设/拆解/标题/评论/分镜五件套）已于阶段 0 全量退役；当前正按 [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) 重建。本 README 仅保留运行/部署/测试的稳定信息，技术架构以 `ARCHITECTURE.md` 为准。

---

## 1. 仓库结构（重构进行中）

```
seecript/
├── web/                                # 新前端：React 18 + Vite + TS（阶段 1 落地）
├── remotion/                           # 视频包装轨独立项目（阶段 3 落地）
│
├── server/
│   ├── app/
│   │   ├── main.py                     # FastAPI 入口（中间件 + 路由）
│   │   ├── config.py                   # Pydantic Settings（.env → 类型安全配置）
│   │   ├── schemas.py                  # I/O 契约（#8 待重写为新七模块 schema）
│   │   ├── routers/                    # 当前仅 asr；新七路由（library/decompose/material/gap/plan/render/edit）在 #8 落地
│   │   └── services/
│   │       ├── llm_client.py           # LLMClient 抽象 + Mock + DeepSeek（保留）
│   │       ├── asr_client.py           # ASRClient 抽象 + Mock + 火山豆包（保留）
│   │       └── prompts/                # #11/#12/#22 重新落地
│   ├── tests/                          # pytest（mock 路径）
│   ├── requirements.txt · requirements-dev.txt
│   └── .env.example                    # 配置模板
│
├── deploy/                             # systemd / nginx 模板
├── scripts/                            # deploy.sh · health-check.sh · install-on-medi-server.sh
├── docs/
│   ├── ARCHITECTURE.md                 # 当前架构权威文档（赛题交付物）
│   ├── PRD.md                          # 上一版 PRD（顶部已贴重构横幅）
│   └── INFRA.md                        # 生产服务器基础设施事实
├── run.ps1 · run.sh                    # 启动 uvicorn（含 venv 自举）
├── stop.ps1 · stop.sh                  # 优雅停止
└── CLAUDE.md                           # Claude Code 会话备忘
```

---

## 2. 本地运行

环境：**Python 3.10+**，命令行能调到 `python` 或 `py`。

### Windows

```powershell
.\run.ps1
```

首次运行自动 ① 创建 `server/venv` ② `pip install -r server/requirements.txt` ③ 从 `.env.example` 拷一份 `server/.env`（默认 `*_PROVIDER=mock`）④ 启动 uvicorn。

| 操作 | 命令 |
|---|---|
| 健康检查 | <http://127.0.0.1:8090/api/health> |
| API 文档 | <http://127.0.0.1:8090/docs> |
| 换端口 | `$env:PORT=8091; .\run.ps1` |
| 跳过 pip（依赖未变） | `$env:SKIP_INSTALL=1; .\run.ps1` |
| 停服 | `.\stop.ps1` |
| 日志 | `./logs/uvicorn.log` · `./logs/uvicorn.err.log` |

### Linux / macOS / WSL

```bash
chmod +x run.sh stop.sh
./run.sh
./stop.sh
```

### 切换到真实 provider

编辑 `server/.env`：

```ini
LLM_PROVIDER=deepseek
DEEPSEEK_API_KEY=sk-xxxxxxxxxxxx

ASR_PROVIDER=doubao
DOUBAO_API_KEY=<volc-uuid-key>
```

重启服务即生效。每个开关相互独立，可以只接其中一个。字段全集见 `server/.env.example`。

> 阶段 0 后前端已迁出后端进程；`run.*` 仅起 FastAPI。阶段 1 落地后会追加 `web/` 的 vite dev server。

---

## 3. 测试

```bash
cd server
python -m pytest -v
```

所有测试默认走 mock 路径，不消耗任何外部 API 配额。

---

## 4. 部署到生产

```bash
sudo REPO_URL=git@github.com:you/seecript.git \
     DOMAIN=seecript.example.com \
     bash scripts/install-on-medi-server.sh
```

脚本会交互式询问部署域名、仓库 URL、DeepSeek / 豆包 API Key（任一可空，对应 provider 自动降级 mock）。

后续升级：

```bash
bash scripts/deploy.sh
# 备份 → git pull → pip install → systemctl restart → 健康检查 → 失败自动回滚
```

端到端验收：

```bash
bash scripts/health-check.sh https://seecript.example.com
```

服务器基础设施约定见 [`docs/INFRA.md`](docs/INFRA.md)。手动步骤见 `deploy/` 模板。

---

## 5. 关联文档

- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — 爆款结构迁移引擎技术架构（赛题交付物，权威）
- [`docs/PRD.md`](docs/PRD.md) — 上一版"创作者副驾"PRD（已贴重构横幅）
- [`docs/INFRA.md`](docs/INFRA.md) — 生产服务器基础设施约定
- [`server/.env.example`](server/.env.example) — 所有配置项含义说明
- [`CLAUDE.md`](CLAUDE.md) — Claude Code 会话备忘
