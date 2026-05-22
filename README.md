# Seecript

> 创作者内容工作流的 AI 副驾。前端静态页 + FastAPI 后端单进程一体；默认 mock 模式不依赖任何外部 API Key 即可端到端跑通。

---

## 1. 仓库结构

```
seecript/
├── index.html · workspace.html · feature-1..5.html   # 6 个前端页面
├── styles.css · app-screens.css                       # 全站样式（CSS 变量集中换皮）
├── api.js                                             # 前端 API 客户端（fetch + loading + toast）
├── interactions.js                                    # feature-1..4 表单提交-渲染逻辑
├── t2v.js                                             # feature-5 文生视频专属轮询状态机
├── seecript-history.js                                # localStorage 历史 + 工作台看板渲染
├── seecript-persona-editor.js                         # 全局 modal 人设编辑器（SRP）
├── asr-uploader.js                                    # ffmpeg.wasm 抽轨 + 调用后端 ASR
├── vendor/ffmpeg/                                     # ffmpeg.wasm 0.12 同源资产
│
├── run.ps1 · run.sh                                   # 启动 uvicorn（含 venv 自举 + pip 安装）
├── stop.ps1 · stop.sh                                 # 优雅停止
│
├── server/
│   ├── app/
│   │   ├── main.py                                    # FastAPI 入口（中间件 + 路由 + 静态挂载）
│   │   ├── config.py                                  # Pydantic Settings（.env → 类型安全配置）
│   │   ├── schemas.py                                 # 所有请求/响应的 Pydantic 模型
│   │   ├── routers/                                   # 8 个业务端点（详见 §3）
│   │   └── services/
│   │       ├── llm_client.py                          # LLMClient 抽象 + Mock + DeepSeek
│   │       ├── asr_client.py                          # ASRClient 抽象 + Mock + 火山豆包
│   │       ├── t2v_client.py                          # T2VClient 抽象 + Mock + 智谱清影
│   │       ├── t2v_shot_prompts.py                    # 分镜演示模式固定 system prompt
│   │       └── prompts/                               # 6 个 LLM 模块的 system prompt 模板
│   ├── tests/                                         # pytest（54 通过）
│   ├── requirements.txt · requirements-dev.txt
│   └── .env.example                                   # 配置模板
│
├── deploy/
│   ├── seecript-server.service                        # systemd 单元（占位符 → sed 替换）
│   └── nginx.conf.example                             # nginx 站点配置（占位符 → sed 替换）
│
├── scripts/
│   ├── deploy.sh                                      # 备份 → git pull → 重启 → 健康检查 → 失败回滚
│   ├── install-on-medi-server.sh                      # 一键服务器落地
│   └── health-check.sh                                # 端到端冲烟
│
├── docs/
│   ├── INFRA.md                                       # 服务器基础设施事实
│   └── AI-DESIGN.md                                   # 6 个 LLM 干预点的 prompt / schema 详解
│
└── CLAUDE.md                                          # Claude Code 会话备忘
```

---

## 2. 技术架构

### 2.1 整体形态

```
浏览器 ──┐
         ├── 静态 HTML/JS/CSS  ◄── FastAPI StaticFiles  ────┐
         └── fetch /api/*      ──► /api/{persona, skeleton, ── FastAPI Routers
                                     qa, script, seo,            │
                                     comments, asr, t2v}         │
                                                                 ▼
                                                       LLMClient / ASRClient / T2VClient
                                                                 │
                                                ┌────────────────┼────────────────┐
                                                ▼                ▼                ▼
                                             Mock           DeepSeek         火山豆包 / 智谱
                                          （默认）        （真实付费）        （真实付费）
```

**单进程**：FastAPI 同时挂载 8 个 `/api/*` 路由与根目录的静态前端；不需要额外的前端 dev server / nginx 反代即可本地跑通。

### 2.2 后端模块

| 路径 | 行数 | 职责 |
|---|---|---|
| `server/app/main.py` | 170 | FastAPI 实例工厂；CORS / trace-id / COOP·COEP 三层中间件；路由注册；静态挂载 |
| `server/app/config.py` | 144 | Pydantic Settings 单例，读 `.env`；包含 LLM/ASR/T2V 三套 provider 切换与各路由 max_tokens 独立可调 |
| `server/app/schemas.py` | 377 | 所有请求/响应的 Pydantic 模型（含 `TRANSCRIPT_MAX_CHARS = 50000` 等业务常量） |
| `server/app/routers/*.py` | 67–193 | 8 个端点，每个文件单一职责 |
| `server/app/services/llm_client.py` | 462 | LLM 抽象基类 + `MockLLMClient` + `DeepSeekLLMClient`（OpenAI 兼容 `/chat/completions`） |
| `server/app/services/asr_client.py` | 288 | ASR 抽象基类 + `MockASRClient` + `DoubaoBigmodelASRClient`（火山极速版） |
| `server/app/services/t2v_client.py` | 471 | T2V 抽象基类 + `MockT2VClient` + `ZhipuT2VClient`（CogVideoX-3/2 自动模型差异处理） |

### 2.3 HTTP 端点

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/api/health` | 健康检查；返回当前 3 个 provider 选型 |
| POST | `/api/persona/generate` | 生成 3 套差异化人设方案 |
| POST | `/api/skeleton/extract` | 把台词文本拆成结构化骨架（hook / body / cta / template） |
| POST | `/api/qa/next` | 引导式问答，≤3 轮硬收敛 |
| POST | `/api/script/generate` | 基于骨架 + 问答 + brief 出原创脚本（scenes + full_text） |
| POST | `/api/seo/titles` | 抖音标题/简介/标签 |
| POST | `/api/comments/classify` | 高/中/低三栏分拣 + 三种语气回复 |
| POST | `/api/asr/transcribe` | multipart 上传音频 → 转写文本 |
| POST | `/api/t2v/submit` | 提交文生视频任务（返回 task_id） |
| GET | `/api/t2v/query/{task_id}` | 轮询任务状态 |

### 2.4 SOLID 落地

| 原则 | 体现 |
|---|---|
| **SRP** | 每个 router 只管一个端点；`prompts/` 一文件一模块；`schemas.py` 只放 I/O 契约 |
| **OCP** | 加新 LLM/ASR/T2V 提供商只需在对应 `*_client.py` 写新子类 + 注册到 `_PROVIDERS` 字典 |
| **LSP** | `MockXxxClient` 与真实实现完全可替换；测试默认走 mock |
| **ISP** | `LLMClient` 仅暴露 `complete` / `complete_json` 两个方法 |
| **DIP** | 业务代码只 `from .services.llm_client import get_llm_client`，不直接 new 具体实现 |

### 2.5 防御性

- LLM 返回非 JSON 时自动重试一次，并在 system prompt 强化约束；连续两次失败映射为 502 而非 500
- 所有 `httpx` 调用包裹超时 + 网络错误捕获 + 状态码白名单
- 前端 `SeecriptApi` 把 422 / 502 / 500 映射成中文 toast
- Pydantic Settings 在 `.env` 缺 key 时自动回退到 mock，并打 warning 日志

### 2.6 可观测

- 每个 HTTP 请求生成 12 位 `trace_id`，回写 `X-Trace-Id` header
- 每次 LLM 调用打印 `provider / 耗时 / prompt tokens / completion tokens`
- 启停脚本通过 `.server.pid` + `logs/` 统一管理

### 2.7 跨域隔离（COOP / COEP）

ffmpeg.wasm 0.12 使用 SharedArrayBuffer 跨线程传数据，浏览器要求页面 `crossOriginIsolated`。`main.py` 中间件给每个 HTML 响应自动加：

- `Cross-Origin-Opener-Policy: same-origin`
- `Cross-Origin-Embedder-Policy: credentialless`

`credentialless` 允许加载第三方 CDN（jsdelivr / Google Fonts），代价是这些请求不带 cookies——本项目不需要。

### 2.8 ASR 链路

```
浏览器                                后端                            火山引擎
file input ─→ ffmpeg.wasm ─→ mp3 ─→ /api/asr/transcribe (multipart)
                                  ─→ base64 内联 ─→ /recognize/flash ─→
                                                              ←─ X-Api-Status-Code 20000000
                                                              ←─ result.text  (1-5s)
              transcript ←──────────
```

资源 ID 必须为 `volc.bigasr.auc_turbo`（极速版）；标准版的 submit/query 异步轮询路径已废弃。

### 2.9 T2V 链路

```
浏览器 feature-5.html               后端                           智谱开放平台
prompt ─→ POST /api/t2v/submit  ─→ T2VClient.submit() ─→ POST /paas/v4/videos/generations
                                   ↓ < 2s 返回 task_id              (异步，30s-3min)
        ←── { task_id, pending } ─
每 5s 轮询 → GET /api/t2v/query/{id} → T2VClient.query() → GET /paas/v4/async-result/{id}
                                                            ↓ task_status: SUCCESS
        ←── { status: succeeded, video_url, cover_image_url }
```

`ZhipuT2VClient` 在模型为 `cogvideox-3` 时自动附加 `fps`/`duration` 字段，避免对 `cogvideox-2` 误传导致 400。Prompt 硬上限 500 字符（智谱官方 512，留 12 字安全余量）。

### 2.10 前端结构

| 文件 | 职责 |
|---|---|
| `api.js` | 全局 `SeecriptApi`：fetch 封装、错误码 → 中文 toast、loading 状态机 |
| `interactions.js` | feature-1..4 的表单提交 → 调端点 → 渲染结果 |
| `t2v.js` | feature-5 的 4 阶段状态机（input/loading/result/error） + 8 分钟硬超时 + 单飞控制 |
| `seecript-history.js` | localStorage 持久化 + 工作台看板渲染；保留上限 30 条 |
| `seecript-persona-editor.js` | 全局 modal 编辑器，feature-2 / workspace 复用 |
| `asr-uploader.js` | ffmpeg.wasm 抽 16kHz 单声道 mp3 + 进度回调 + 自动填 textarea |

CSS 类前缀统一 `.seecript-*`；主题色集中在 `styles.css` 顶部 `:root`，改几个变量即可全站换皮。

---

## 3. 本地运行

环境：**Python 3.10+**，命令行能调到 `python` 或 `py`。

### Windows

```powershell
.\run.ps1
```

首次运行自动 ① 创建 `server/venv` ② `pip install -r server/requirements.txt` ③ 从 `.env.example` 拷一份 `server/.env`（默认 `*_PROVIDER=mock`）④ 启动 uvicorn。

| 操作 | 命令 |
|---|---|
| 访问 | <http://127.0.0.1:8090/> |
| API 文档 | <http://127.0.0.1:8090/docs> |
| 健康检查 | <http://127.0.0.1:8090/api/health> |
| 换端口 | `$env:PORT=8091; .\run.ps1` |
| 跳过 pip（依赖未变） | `$env:SKIP_INSTALL=1; .\run.ps1` |
| 停服 | `.\stop.ps1` |
| 日志 | `./logs/uvicorn.log` · `./logs/uvicorn.err.log` |

### Linux / macOS / WSL

```bash
chmod +x run.sh stop.sh
./run.sh
# PORT=8091 ./run.sh
# SKIP_INSTALL=1 ./run.sh
./stop.sh
```

### 切换到真实 provider

编辑 `server/.env` 三个开关之一：

```ini
LLM_PROVIDER=deepseek
DEEPSEEK_API_KEY=sk-xxxxxxxxxxxx

ASR_PROVIDER=doubao
DOUBAO_API_KEY=<volc-uuid-key>

T2V_PROVIDER=zhipu
ZHIPU_API_KEY=<zhipu-api-key>
```

重启服务即生效。每个开关相互独立，可以只接其中一个。

字段全集见 `server/.env.example`；模型语义见 `server/app/config.py`。

---

## 4. 测试

```bash
cd server
.\venv\Scripts\python.exe -m pip install -r requirements-dev.txt --quiet   # Win
# source venv/bin/activate; pip install -r requirements-dev.txt           # Unix
.\venv\Scripts\python.exe -m pytest -v                                    # Win
# python -m pytest -v                                                     # Unix
```

预期：**54 passed**。测试默认全部在 mock 路径上跑，不会消耗任何外部 API 配额。

### 烟测

```bash
curl http://127.0.0.1:8090/api/health
curl -X POST http://127.0.0.1:8090/api/persona/generate \
     -H "Content-Type: application/json" \
     -d '{"background":"PM 8 years","interests":"home","resources":"6h/week"}'
```

---

## 5. 部署到生产

### 5.1 一键脚本

```bash
sudo REPO_URL=git@github.com:you/seecript.git \
     DOMAIN=seecript.example.com \
     bash scripts/install-on-medi-server.sh
```

脚本会交互式询问（已通过环境变量传入的字段会跳过）：

1. 部署域名
2. Git 仓库 URL（若 `/opt/seecript` 已存在可留空）
3. DeepSeek / 豆包 / 智谱 三个 API Key（任一可空，对应 provider 自动降级 mock）

完成后状态：

- ✅ systemd `seecript-server.service` 已起，监听 `127.0.0.1:5001`
- ✅ nginx 站点已装（HTTP only）
- ✅ `/opt/seecript/server/.env` 已写、`chmod 600`
- ⚠️ HTTPS 需手动跑 `certbot --nginx -d <domain>` —— ffmpeg.wasm 在浏览器需要 HTTPS 才能拿到 SharedArrayBuffer

### 5.2 手动步骤（兜底）

```bash
# 1. 准备目录与用户
sudo useradd -m -s /bin/bash seecript
sudo mkdir -p /opt/seecript && sudo chown seecript:seecript /opt/seecript
sudo -u seecript -i
cd /opt && git clone <repo> seecript && cd seecript

# 2. 后端依赖
cd server
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
deactivate
cd ..

# 3. 配 .env
cp server/.env.example server/.env
chmod 600 server/.env
# 编辑：LLM_PROVIDER=deepseek + DEEPSEEK_API_KEY + PORT=5001 等

# 4. systemd
sudo cp deploy/seecript-server.service /etc/systemd/system/
sudo sed -i 's|__PROJECT_DIR__|/opt/seecript|g; s|__RUN_USER__|seecript|g' \
    /etc/systemd/system/seecript-server.service
sudo mkdir -p /opt/seecript/var/logs
sudo chown -R seecript:seecript /opt/seecript/var
sudo systemctl daemon-reload
sudo systemctl enable --now seecript-server
curl http://127.0.0.1:5001/api/health   # 应返回 healthy

# 5. nginx
sudo cp deploy/nginx.conf.example /etc/nginx/sites-available/seecript.conf
sudo sed -i \
    -e 's|__DOMAIN__|seecript.example.com|g' \
    -e 's|__FRONTEND_DIR__|/opt/seecript|g' \
    -e 's|__BACKEND_PORT__|5001|g' \
    /etc/nginx/sites-available/seecript.conf
sudo ln -s /etc/nginx/sites-available/seecript.conf /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx

# 6. HTTPS
sudo certbot --nginx -d seecript.example.com
```

### 5.3 后续升级

```bash
bash scripts/deploy.sh
# 备份当前版本 → git pull → pip install → systemctl restart → 健康检查 → 失败自动回滚
```

### 5.4 端到端验收

```bash
bash scripts/health-check.sh https://seecript.example.com
# 可选传一段本地 mp3 做完整 ASR 轮询：
bash scripts/health-check.sh https://seecript.example.com /tmp/sample.mp3
```

服务器基础设施约定见 [`docs/INFRA.md`](docs/INFRA.md)。

---

## 6. 常见问题

| 现象 | 排查 |
|---|---|
| `run.ps1` 报「未找到 python」 | 装 Python 3.10+，把 `python` 或 `py` 加进 PATH |
| 端口被占用 | `.\stop.ps1` 后换端口重启；`netstat -ano \| findstr :8090` 看占用 |
| 复制按钮不工作 | 用 `file://` 直接打开 HTML 时 clipboard API 受限，必须通过 `run.*` 起的 HTTP 服务访问 |
| DeepSeek 报「连续两次未返回合法 JSON」 | completion 被 `max_tokens` 截断；在 `.env` 调高对应 `LLM_*_MAX_TOKENS`（上限 8192），或缩短输入 |
| `transcript` 超长报 422 | 后端 `TRANSCRIPT_MAX_CHARS = 50000`；分段提交 |
| 502 / 「AI 服务暂时不可用」 | provider key 无效、余额耗尽或上游限频；查 `logs/uvicorn.err.log` |
| 视频生成 422 prompt 超长 | 智谱 prompt 硬上限 500 字符（`T2V_MAX_PROMPT_CHARS`） |
| 视频生成 502 HTTP_429 | 智谱并发上限（V0=5、V1=10、V2=15、V3=20），等当前任务结束 |
| pytest 找不到 `app` 模块 | `cd server` 后再跑，或 `python -m pytest server/tests` |

---

## 7. 关联文档

- [`docs/INFRA.md`](docs/INFRA.md) — 生产服务器基础设施约定
- [`docs/AI-DESIGN.md`](docs/AI-DESIGN.md) — 6 个 LLM 干预点的 prompt / schema / 三层兜底详解（T2V 工程详解见 `services/t2v_client.py` 顶部 docstring）
- [`server/.env.example`](server/.env.example) — 所有配置项含义说明
- [`CLAUDE.md`](CLAUDE.md) — Claude Code 会话备忘
