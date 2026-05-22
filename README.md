# Seecript · 创作者智能副驾

> 让一个人，干出一个 MCN 团队的产出。
> 前端 + FastAPI 后端一体；4 个 AI 端点同进程服务静态前端。
> 默认 `LLM_PROVIDER=mock`，**不配 API Key 也能跑全流程**；填入 DeepSeek Key 后即用真模型。

---

## 1. 这是什么

Seecript 把"人设定位 → 内容生产 → 长尾分发 → 互动运营"四大 MCN 编导能力封装成端到端 AI 工作流。**爆款拆解作为「内容生产」的核心实现手段嵌入工作流**，不再单独作为对外卖点。

### 1.1 用户旅程

1. **首页（产品说明）** `index.html` — 介绍价值主张、痛点、四步工作流、三大对外能力，强 CTA 引导进入工作台。
2. **工作台** `workspace.html` — 推荐工作流卡片（人设 → 拆解 → 标题车间 → 评论分拣），加「我的人设」「我的拆解项目」两个本地历史看板（localStorage）。
3. **五个功能页**：
   - `feature-2.html` 人设生成（**第一步**，每次创作的起点）
   - `feature-1.html` 爆款拆解 + 脚本引擎
   - `feature-3.html` 标题车间（**仅抖音**，去除多平台切换）
   - `feature-4.html` 评论分拣
   - `feature-5.html` **分镜素材生成（智谱清影 CogVideoX-3，默认；导出后在剪映等软件剪辑成片）**

### 1.2 仓库结构

```
seecript/
├── index.html                         # 首页（产品说明 / 落地页）
├── workspace.html                     # 工作台（含 localStorage 历史看板）
├── feature-1.html ~ feature-5.html    # 5 个功能页（feature-5 = 智谱分镜短视频素材）
├── styles.css / app-screens.css       # 全站样式（CSS 变量 = 单点换皮）
├── api.js                             # 前端 API 客户端（fetch / loading / toast）
├── interactions.js                    # 4 个表单的提交-渲染逻辑（feature-1/2/3/4）
├── t2v.js                             # feature-5 专属：轮询状态机 / prompt 自动带入
├── seecript-history.js                     # localStorage 历史 + 工作台看板渲染 + updatePersona API
├── seecript-persona-editor.js              # v0.10 起：全局 modal 人设手动编辑器（SRP）
├── asr-uploader.js                    # ffmpeg.wasm 抽轨 + 调用后端 ASR
├── run.ps1 / run.sh                   # 启动 uvicorn FastAPI（含 venv 自举 + pip 安装）
├── stop.ps1 / stop.sh                 # 优雅停止
├── server/                            # 后端代码
│   ├── app/
│   │   ├── main.py                    # FastAPI 入口（路由 + 中间件 + 静态挂载）
│   │   ├── config.py                  # Pydantic Settings（读取 .env）
│   │   ├── schemas.py                 # 所有请求/响应 Pydantic 模型
│   │   ├── routers/                   # 7 个业务端点 + ASR + T2V
│   │   │   ├── persona.py             # POST /api/persona/generate
│   │   │   ├── skeleton.py            # POST /api/skeleton/extract
│   │   │   ├── qa.py                  # POST /api/qa/next         (引导式问答 ≤3 轮)
│   │   │   ├── script.py              # POST /api/script/generate (基于骨架+答案出原创脚本)
│   │   │   ├── seo.py                 # POST /api/seo/titles      （platform 锁定 douyin）
│   │   │   ├── comments.py            # POST /api/comments/classify
│   │   │   ├── asr.py                 # POST /api/asr/transcribe  (火山豆包 Flash)
│   │   │   └── t2v.py                 # POST /api/t2v/submit + GET /api/t2v/query/{id}
│   │   │                              #   v0.9 新增 · 智谱清影 CogVideoX（默认 cogvideox-3）
│   │   └── services/
│   │       ├── llm_client.py          # LLMClient 抽象 + Mock + DeepSeek 实现
│   │       ├── asr_client.py          # ASRClient 抽象 + Mock + 火山豆包 Flash 实现
│   │       ├── t2v_client.py          # T2VClient 抽象 + Mock + 智谱 CogVideoX 实现
│   │       └── prompts/               # 6 个 LLM 模块的 system prompt 模板
│   ├── tests/                         # pytest 单元 + 集成测试（54 通过 · 含 17 个 T2V 新增）
│   ├── requirements.txt               # 生产依赖
│   └── .env.example                   # 复制为 .env 后填入 DeepSeek + 豆包 + 智谱 Key
├── deploy/
│   ├── seecript-server.service       # systemd 单元（占位符 → sed 替换）
│   └── nginx.conf.example             # nginx 站点配置（占位符 → sed 替换）
├── scripts/
│   ├── deploy.sh                      # 备份 → git pull → 重启 → 健康检查 → 失败回滚
│   ├── install-on-medi-server.sh      # 一键在已有服务器上落地
│   └── push-to-github.ps1 / .cmd      # 安全推送（含 secret 扫描 + cwd 守卫）
├── docs/PRD.md                        # 产品需求文档（历史档，结构基本沿用）
└── README.md                          # 本文件
```

### 1.3 推荐工作流

```
①人设生成 (feature-2)        ② 爆款拆解 (feature-1)        ③标题车间 (feature-3)        ④评论分拣 (feature-4)
   ↓ 生成 3 个差异化方案       ↓ ASR 抽轨 + DeepSeek 拆骨架   ↓ 抖音算法标题/简介/标签       ↓ 高/中/低分拣 + 三种语气回复
   存入 localStorage          存入 localStorage              （结果不入库，按需复制）       （结果不入库，按需复制）
```

工作台首页的「我的人设」「我的拆解项目」两块看板会自动展示这两类历史，纯前端 localStorage，仅当前浏览器可见，最多保留 30 条；不需要后端表，也无需登录。

---

## 2. 本地运行（30 秒）

### 2.1 Windows（推荐）

环境：**Python 3.10+** 已在 PATH（`python` 或 `py`）。

```powershell
cd d:\nocode\seecript
.\run.ps1
```

首次运行会自动：① 创建 `server/venv` ② `pip install -r server/requirements.txt`（约 1 分钟）③ 从 `.env.example` 拷一份 `server/.env`（默认 `LLM_PROVIDER=mock`）④ 启动 uvicorn。

- 访问：<http://127.0.0.1:8090/>
- API：<http://127.0.0.1:8090/api/health>
- 自动文档：<http://127.0.0.1:8090/docs>
- 切换端口：`$env:PORT=8091; .\run.ps1`
- 跳过 pip 安装（依赖未变更时更快）：`$env:SKIP_INSTALL=1; .\run.ps1`
- 停止：`.\stop.ps1`
- 日志：`./logs/uvicorn.log`、`./logs/uvicorn.err.log`
- 进程信息：`.server.pid`（脚本维护，请勿手改）

### 2.2 Linux / macOS / WSL

```bash
cd /path/to/seecript
chmod +x run.sh stop.sh
./run.sh
# 切换端口：PORT=8091 ./run.sh
# 跳过 pip：  SKIP_INSTALL=1 ./run.sh
./stop.sh
```

### 2.3 切换到真 DeepSeek

```bash
# 编辑 server/.env
LLM_PROVIDER=deepseek
DEEPSEEK_API_KEY=sk-xxxxxxxxxxxx
```

然后 `.\stop.ps1; $env:SKIP_INSTALL=1; .\run.ps1`（或 `./stop.sh; SKIP_INSTALL=1 ./run.sh`）即可。

若任意 DeepSeek 步骤报错「模型连续两次未返回合法 JSON」，且后端日志里的 snippet 像**半截 JSON**，多半是 **completion 被 `max_tokens` 截断**。可在 `server/.env` 按需提高对应上限（均为最大 8192）：`LLM_SKELETON_MAX_TOKENS`、`LLM_SCRIPT_MAX_TOKENS`（脚本含 `full_text` 最长）、`LLM_COMMENTS_MAX_TOKENS`、`LLM_PERSONA_MAX_TOKENS`、`LLM_SEO_MAX_TOKENS`、`LLM_QA_MAX_TOKENS`；或缩短输入台词/评论再试。

爆款拆解等接口的 **`transcript`（粘贴台词）** 与后端 `schemas.TRANSCRIPT_MAX_CHARS` 对齐（默认 **50000** 字）；超出请删减或分段后再提交。

> **注意**：DeepSeek API 是**真实付费**的。每次点击「生成 / 拆解 / 分拣」都会扣 token。
> v0.1 没做配额限制；如需自我保护可临时切回 `LLM_PROVIDER=mock`。

### 2.4 ASR：纯前端 ffmpeg.wasm + 火山豆包大模型录音文件识别**极速版**（v0.4 起）

**架构**

```
浏览器                                Seecript 后端                          火山引擎
─────                                ──────────────                          ────────
file input  ─→ ffmpeg.wasm ─→ mp3 ─→ POST /api/asr/transcribe (multipart)
                                  ─→ base64 编码 ─→ POST /recognize/flash ─→
                                                              ←─ 200 / X-Api-Status-Code 20000000
                                                              ←─ result.text  (1-5s)
                                     transcript ←──────────────
```

**对比标准版（已弃用）的关键差异**

| 维度 | 标准版（旧） | 极速版（现在） |
|---|---|---|
| 端点 | `/submit` + `/query` 轮询 | `/recognize/flash` 一次请求 |
| 资源 ID | `volc.bigasr.auc` | `volc.bigasr.auc_turbo` |
| 音频上传 | 必须公网 https URL | base64 inline 直传 |
| 本地真测 | 必须 ngrok | **直接能跑通** |
| 服务器写盘 | 必需（暴露给火山下载） | 不需要 |
| 延迟 P95 | 30-180s | **2-5s** |

**本地真实测试豆包**（极速版直接调通，不再需要任何隧道）：

```powershell
# 1) 编辑 server/.env：
#      ASR_PROVIDER=doubao
#      DOUBAO_API_KEY=<your-volc-uuid-key>
#      DOUBAO_RESOURCE_ID=volc.bigasr.auc_turbo
# 2) 启动：
.\stop.ps1; $env:SKIP_INSTALL=1; .\run.ps1
# 3) 浏览器打开 http://127.0.0.1:8090/feature-1.html，上传视频
#    或者直接 curl 烟测：
curl.exe -F "file=@your-audio.mp3;type=audio/mpeg" http://127.0.0.1:8090/api/asr/transcribe
# 期望返回 {"transcript":"...","provider":"doubao","elapsed_ms":2832}
```

**首次浏览器 ASR 流程**

1. 选一个视频（mp4/mov，**建议约 1 分钟**，更长亦可但解析更慢、更易触达体积上限）
2. 浏览器自动下载 ffmpeg-core wasm（约 30 MB，只下一次）
3. ffmpeg.wasm 抽出 16kHz 单声道 mp3（约 1 分钟量级通常数百 KB 级）
4. 上传到后端 → 后端 base64 编码 → 一次请求豆包极速版 → 2-5 秒返回文本
5. 文本自动填入下方 textarea，"用 AI 拆解骨架"按钮高亮闪烁

**为什么需要 COOP/COEP 头？**

ffmpeg.wasm 0.12 用 SharedArrayBuffer 跨线程传数据，浏览器要求页面处于 `crossOriginIsolated` 状态。后端 `app/main.py` 中间件给每个 HTML 页面响应自动加：

- `Cross-Origin-Opener-Policy: same-origin`
- `Cross-Origin-Embedder-Policy: credentialless`

`credentialless` 模式允许加载第三方 CDN（jsdelivr / Google Fonts），唯一限制是这些请求不带 cookies——刚好我们也不需要。

**豆包 ASR 价格参考**（2026.05，极速版与标准版价格相近）

- 极速版：约 ¥0.0008 / 秒 = ¥0.05 / 分钟 = ¥3 / 小时
- 1 分钟视频 ≈ ¥0.05 / 次（示意）
- 100 用户 × 每天 5 次 × 1 分钟 ≈ ¥25 / 天（示意），需要在前端做配额（v0.5 待办）

### 2.5 T2V：智谱清影文生视频（默认 **CogVideoX-3**，v0.9 起第 7 个 AI 干预点）

**架构**

```
浏览器 feature-5.html              Seecript 后端                      智谱开放平台
─────                              ──────────────                      ──────────
prompt 输入 ─→ POST /api/t2v/submit ─→ T2VClient.submit() ─→ POST /paas/v4/videos/generations
                                      ↓ < 2s 返回 task_id              (异步任务，30s-3min)
            ←── { task_id, pending } ─
每 5s 轮询 ─→ GET /api/t2v/query/{id} ─→ T2VClient.query() ─→ GET /paas/v4/async-result/{id}
                                                              ↓ task_status: SUCCESS
            ←── { status: succeeded, video_url, cover_image_url }
下载 mp4 / 重新生成 / 跳标题车间
```

**默认开箱即用（mock 模式）**：未配置 `ZHIPU_API_KEY` 时自动降级到 `MockT2VClient`——8 秒后返回示例视频 URL，前端轮询 UI 完整跑通，离线演示零依赖。

从 `feature-1` 第 4 步进入 `feature-5` 时：若有结构化脚本，页面会列出 **Hook / 各分镜 / CTA** 供**单选**；提交时带 `shot_preview_mode: true`，服务端拼接固定**分镜演示系统提示词**并请求 **cogvideox-3 约 10 秒**成片（预期画面预览）。无分镜数据时可在文本框自由填写提示词；智谱单次 prompt **500 字**硬上限不变。另可选请求体字段 `duration_seconds`（5 或 10，仅 v3）在非演示模式下覆盖 `.env` 默认时长。

**接入真实智谱 API**：

```powershell
# 1) 去 https://open.bigmodel.cn/usercenter/proj-mgmt/apikeys 创建 API Key
# 2) 编辑 server/.env（默认已与开放平台主推对齐）：
#      T2V_PROVIDER=zhipu
#      ZHIPU_API_KEY=<你的 zhipu-api-key>
#      ZHIPU_VIDEO_MODEL=cogvideox-3          # 默认；可改为 cogvideox-2 压低成本
#      ZHIPU_VIDEO_FPS=30                     # 仅 cogvideox-3：30 或 60
#      ZHIPU_VIDEO_DURATION=5                 # 仅 cogvideox-3：5 或 10（秒）
# 3) 重启服务：
.\stop.ps1; $env:SKIP_INSTALL=1; .\run.ps1
# 4) 浏览器打开 http://127.0.0.1:8090/feature-5.html，输入 prompt → 等待返回视频
```

**计费 / 配额** （2026.05，以智谱开放平台公示为准）

- **CogVideoX-3（默认）**：官网体验中心与文档主推；约 **¥1/次**，时长 **5s 或 10s**（由 `ZHIPU_VIDEO_DURATION` 决定），支持 **fps 30/60**。
- **CogVideoX-2（可选）**：约 **¥0.5/次**，输出固定约 **6 秒**；在 `.env` 设 `ZHIPU_VIDEO_MODEL=cogvideox-2` 即可切换——注意分辨率枚举与 v3 略有差异（见 `schemas.T2VSize` / feature-5 下拉说明）。
- **失败任务不扣费**——智谱按成功任务结算（与官方说明一致）。
- **并发限制**：V0 用户 5 任务并发，高等级账户更高；Seecript 单租户场景一般用不到上限。

**架构亮点**：T2VClient 完全对齐现有 LLMClient/ASRClient 抽象；`ZhipuT2VClient` 在模型名为 `cogvideox-3` 时自动附加 `fps`/`duration`，避免对 `cogvideox-2` 误传导致 400。详细论证见 `docs/PRD.md §3.7`。

---

## 3. 验收 checklist

启动后请逐项确认：

- [ ] `http://127.0.0.1:8090/` 正常打开 `工作台`
- [ ] `http://127.0.0.1:8090/api/health` 返回 `{"status":"healthy",...}`
- [ ] **mock 模式**下 4 个端点全部返回 200：
  ```bash
  curl http://127.0.0.1:8090/api/health
  curl -X POST http://127.0.0.1:8090/api/persona/generate \
       -H "Content-Type: application/json" \
       -d '{"background":"PM 8 years","interests":"home","resources":"6h/week"}'
  ```
- [ ] `feature-2`：填表单 → 点「生成 3 个人设方案」→ 看到右下角 toast +  3 张人设卡刷新
- [ ] `feature-1`：在台词输入框粘贴文本 → 点「用 AI 拆解骨架」→ 骨架区刷新
- [ ] `feature-3`：切换平台 tab → 点「生成发布元数据」→ 标题/简介/标签整体刷新
- [ ] `feature-4`：粘贴评论 → 点「开始分拣」→ 高/中/低三栏重新渲染
- [ ] 拔网线后再点提交 → 应该看到红色 toast「网络异常：无法连接到后端…」
- [ ] 浏览器控制台无未捕获错误（字体 404 不算）

跑后端测试：

```bash
cd server
.\venv\Scripts\python.exe -m pip install -r requirements-dev.txt --quiet  # Win
# source venv/bin/activate; pip install -r requirements-dev.txt           # Unix
.\venv\Scripts\python.exe -m pytest -v                                    # Win
# python -m pytest -v                                                     # Unix
```

预期：**54 passed**。

---

## 4. 架构与设计原则

### 4.1 SOLID 实现位置

| 原则 | 体现 |
|---|---|
| **S**RP | 每个 router 只管一个端点；prompts/ 一文件一模块；schemas.py 只放 I/O 契约 |
| **O**CP | 加新 LLM 提供商只需在 `services/llm_client.py` 写新子类 + 注册到 `_PROVIDERS` 字典 |
| **L**SP | `MockLLMClient` 与 `DeepSeekLLMClient` 完全可替换；测试默认走 mock |
| **I**SP | `LLMClient` 接口仅 2 个方法（`complete` / `complete_json`），不强加用户用不到的能力 |
| **D**IP | 业务代码只 `from .services.llm_client import get_llm_client`；不直接 new 具体实现 |

### 4.2 防御性编程

- LLM 返回非 JSON 时自动重试一次，并在 system prompt 加严格约束
- 所有 `httpx` 调用包裹超时 + 网络错误捕获 + 状态码白名单
- 前端 `SeecriptApi` 把 422 / 502 / 500 映射成中文提示
- Pydantic Settings 在 `.env` 缺 key 时优雅回退到 mock，并打 warning 日志

### 4.3 可观测

- 每个 HTTP 请求生成 12 位 `trace_id`，回写 `X-Trace-Id` header
- 每次 LLM 调用打印 `provider / 耗时 / prompt token / completion token`
- 启停脚本统一通过 `.server.pid` + `logs/` 管理

### 4.4 命名约定

- CSS class 全部 `.seecript-*` 前缀
- 主题色集中在 `styles.css` 顶部 `:root`，**单点换皮**
- 后端模块按业务名小写：`persona / skeleton / seo / comments`

---

## 5. 部署到生产

> **推荐路径** = 5.0 一键脚本 + 5.2 验收  
> **手动路径** = 5.1（兜底，跟慢病项目逐条对齐）

### 5.0 一键部署到「慢病用药小管家」服务器（推荐）

> 基础设施事实：详见 `docs/INFRA.md`（已实测确认 2026-05-04）  
> 主域名 `zlhu.asia` → 阿里云轻量香港 → IP `47.239.58.145` → Ubuntu + nginx 1.18

#### 前提（你需要先做完这 3 件事）

| # | 你需要做 | 怎么做 |
|---|---|---|
| ① | **加 DNS A 记录** | 登录 [https://dns.console.aliyun.com/](https://dns.console.aliyun.com/) → 找 `zlhu.asia` → 添加记录：<br>**类型** A &nbsp;&nbsp; **主机记录** `seecript` &nbsp;&nbsp; **记录值** `47.239.58.145` &nbsp;&nbsp; **TTL** `600`<br>5 分钟后验证：`Resolve-DnsName seecript.zlhu.asia` 应返回 `47.239.58.145` |
| ② | **火山豆包资源开通** | 登录 [火山引擎控制台](https://console.volcengine.com/speech/app) → 找「**录音文件识别 - 大模型极速版**」（资源 ID `volc.bigasr.auc_turbo`，**注意不是标准版！**）→ 点「开通服务」（按量付费，新用户有免费额度）。没开通就直接用 Key 调用会返回 `45000001` (参数无效)。 |
| ③ | **代码上服务器** | 把整个 `seecript/` 目录推到一个 Git 仓库（私有也行），后面脚本会 `git clone`。也可以本地 `tar czf - . \| ssh server "cd /opt && tar xzf -"` 然后跳过 git。 |

#### 跑一键脚本

ssh 上你的服务器（你已经能 ssh 上去管慢病项目）：

```bash
# 推荐：先备份慢病的 nginx 配置（按规则 B）
DATE=$(date +%F)
sudo cp -r /etc/nginx /etc/nginx.${DATE}.bak

# 选项 A：从 Git 拉取（推荐）
sudo REPO_URL=git@github.com:you/seecript.git \
     DOMAIN=seecript.zlhu.asia \
     bash /tmp/install-on-medi-server.sh

# 选项 B：rsync 上来后直接跑
sudo DOMAIN=seecript.zlhu.asia \
     bash /opt/seecript/scripts/install-on-medi-server.sh
```

脚本会**交互式**问你（环境变量传过的就跳过）：

1. Seecript 域名（默认值已设为 `seecript.zlhu.asia`，回车即接受）
2. Git 仓库 URL（已在 `/opt/seecript` 可留空）
3. DeepSeek API Key（粘贴 sk- 开头的 Key）
4. 火山豆包 API Key（粘贴 UUID 形式的 Key）

跑完后状态：
- ✅ 后端 systemd `seecript-server` 已起，监听 `127.0.0.1:5001`
- ✅ nginx 站点已装（HTTP only）
- ✅ `/opt/seecript/server/.env` 已写，`chmod 600`，资源 ID 已设为 `volc.bigasr.auc_turbo`
- ⚠️ HTTPS 还没配（**ffmpeg.wasm 在浏览器需要 HTTPS** 才能拿到 SharedArrayBuffer；豆包极速版本身**不要求** HTTPS）
- ⚠️ 主域名 `zlhu.asia` 完全不受影响（`server_name` 隔离）

#### 剩下 1 步手动做（脚本结尾会再提示一遍）

```bash
# 申请 Let's Encrypt 证书（与主域名独立证书）
sudo certbot --nginx -d seecript.zlhu.asia
# 选 2「Redirect HTTP→HTTPS」
```

> 极速版不再需要 PUBLIC_BASE_URL，certbot 跑完就直接能用。

#### 出问题怎么回滚（不影响慢病主项目）

```bash
sudo systemctl stop seecript-server && sudo systemctl disable seecript-server
sudo rm -f /etc/systemd/system/seecript-server.service
sudo rm -f /etc/nginx/sites-enabled/seecript.conf
sudo nginx -t && sudo systemctl reload nginx
curl -I https://zlhu.asia    # 主项目应立即恢复
```

### 5.2 端到端验收

```bash
# 4 LLM 端点 + ASR 可达性
bash /opt/seecript/scripts/health-check.sh https://seecript.zlhu.asia

# 真测一次豆包 ASR（需要本地有一段 30 秒以内的 mp3/m4a 文件）
bash /opt/seecript/scripts/health-check.sh https://seecript.zlhu.asia /tmp/sample.mp3
```

预期输出：

```text
[ OK ] health
[ OK ] persona (http=200, ...B)
[ OK ] skeleton (http=200, ...B)
[ OK ] seo (http=200, ...B)
[ OK ] comments (http=200, ...B)
[ OK ] asr full round-trip (doubao, "elapsed_ms":58320)
       transcript preview: "大家好，今天给大家分享一个..."
========== ALL CHECKS PASSED ==========
```

浏览器打开 `https://seecript.zlhu.asia/feature-1.html` → 选一段视频 → 应看到：
1. 「正在加载 ffmpeg…」→「抽轨中…」→「上传中…」→「识别中…」  
2. 文本框被自动填上识别结果  
3. 「用 AI 拆解骨架」按钮闪一下吸引注意（脉冲动画）

### 5.1 部署到生产（手动版，兜底）

> 完整步骤见上层 `../DEPLOYMENT.md` 第 2-5 章；本节只列 Seecript 特化点。

```bash
# 1. 在生产服务器上准备目录与用户
sudo useradd -m -s /bin/bash seecript
sudo mkdir -p /opt/seecript && sudo chown seecript:seecript /opt/seecript
su - seecript
cd /opt
git clone <你的仓库> seecript
cd seecript

# 2. 后端 venv + 依赖
cd server
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
deactivate
cd ..

# 3. 配 .env
cp server/.env.example server/.env
chmod 600 server/.env
# 编辑：LLM_PROVIDER=deepseek + DEEPSEEK_API_KEY + PORT=5001

# 4. 装 systemd 单元（注意占位符替换）
sudo cp deploy/seecript-server.service /etc/systemd/system/seecript-server.service
sudo sed -i 's|__PROJECT_DIR__|/opt/seecript|g; s|__RUN_USER__|seecript|g' \
    /etc/systemd/system/seecript-server.service
sudo mkdir -p /opt/seecript/var/logs && sudo chown -R seecript:seecript /opt/seecript/var
sudo systemctl daemon-reload
sudo systemctl enable --now seecript-server
curl http://127.0.0.1:5001/api/health  # 应返回 healthy

# 5. 装 nginx 站点（注意占位符替换）
sudo cp deploy/nginx.conf.example /etc/nginx/sites-available/seecript.conf
sudo sed -i \
    -e 's|__DOMAIN__|seecript.zlhu.asia|g' \
    -e 's|__FRONTEND_DIR__|/opt/seecript|g' \
    -e 's|__BACKEND_PORT__|5001|g' \
    /etc/nginx/sites-available/seecript.conf
sudo ln -s /etc/nginx/sites-available/seecript.conf /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx

# 6. HTTPS
sudo certbot --nginx -d seecript.zlhu.asia

# 7. 后续升级（备份 + git pull + 重启 + 健康检查 + 失败回滚）
bash scripts/deploy.sh
```

### 5.1 与慢病项目共存

| 项 | 慢病用药小管家 | Seecript |
|---|---|---|
| 内部端口 | 5000 | **5001** |
| systemd 单元 | `medi-server` | `seecript-server` |
| 域名 | `zlhu.asia` | `seecript.zlhu.asia` |
| 项目路径 | `/opt/medi` | `/opt/seecript` |
| 运行用户 | `medi` | `seecript` |

两者可在同一台 2C4G 香港服务器上同时跑，互不干扰。

---

## 6. 换皮（5 分钟切色系）

只需改 `styles.css` 顶部 `:root` 几个变量即可全站换皮。预设：

- **暖橙活力风**：`--primary: #FF6B35; --accent: #FFD23F; --bg-deep: #FFF6E5`
- **科技蓝**：`--primary: #2563eb; --accent: #06b6d4; --bg-deep: #eef4ff`
- **暗黑模式**：把 `--bg-soft / --bg-deep / --surface / --ink / --ink-muted` 一起反转

---

## 7. 常见问题

| 现象 | 排查 |
|---|---|
| `run.ps1` 报错"未找到 python" | 装 Python 3.10+，把 `python` 或 `py` 加进 PATH |
| `pip install` 卡在装 numpy/torch | Seecript 后端**不依赖 ML 库**，应该秒装；卡住请检查网络/换源 |
| 端口被占用 | `.\stop.ps1` 后 `$env:PORT=8091` 重启；或 `netstat -ano | findstr :8090` 找占用 |
| 点「生成」无反应 | 看浏览器 Network 面板：是否调到 `/api/...`？后端日志 `logs/uvicorn.err.log` 有报错？ |
| 502 / "AI 服务暂时不可用" | DeepSeek Key 无效、余额耗尽、或 `LLM_PROVIDER=deepseek` 但 Key 没填 |
| 复制按钮不工作 | 用 `file://` 直接打开 HTML 时 clipboard API 受限。请通过 `run.ps1` 的本地 HTTP 启动 |
| pytest 找不到 app 模块 | `cd server` 后再跑，或检查 `tests/conftest.py` 已自动加 sys.path |

---

## 8. 后续路线

| 阶段 | 状态 | 目标 |
|---|---|---|
| v0.1 | ✅ 已交付 | 4 模块端到端 + DeepSeek + mock 双模式 + 单台部署对齐慢病项目 |
| v0.2-v0.4 | ✅ 已并入 | ASR 真接入：纯前端 ffmpeg.wasm + 火山豆包 |
| v0.5 | ✅ 已交付 | 迁移到豆包极速版，删除磁盘临时文件 + 公网 URL 依赖 |
| **v0.6（当前）** | ✅ 已交付 | 首页改产品说明 / 工作台拆出 / localStorage 历史看板 / 标题车间锁定抖音 / 全站去 Demo 表述 |
| v0.7 | ⏳ 待办 | 流式 SSE typewriter 效果 + slowapi IP 限频 |
| v0.8 | ⏳ 待办 | 模块二案例向量库（Qdrant 或 pgvector）+ RAG |
| v0.9 | ⏳ 待办 | 用户系统、每日配额、付费节点 |

---

## 9. 关联文档

- `docs/PRD.md` — 产品需求文档 **v3.3**（含 PRD 迭代记录表 + §2.6 分镜素材 + §3.7 文生视频策略）。重生成 docx 见 §11.11
- `docs/AI-DESIGN.md` — 6 个文本类 AI 干预点的 prompt / schema / 三层兜底详解（T2V 干预点的工程详解直接见 `services/t2v_client.py` 顶部 docstring）
- `docs/screenshots/` — PRD 截图集中目录（`s-01.png` ~ `s-27.png`）。新增截图按下一序号命名即可
- `docs/screenshots/mapping.json` — 抽图脚本生成的"图 ↔ 章节"映射，供 PRD 作者对照位置
- `server/.env.example` — 后端环境变量模板，含 LLM / ASR / T2V 三大模块全部配置项注释
- `../DEPLOYMENT.md` — 慢病项目部署手册，Seecript 流程与之 95% 一致

---

## 10. 改进点（每次迭代后回填）

> 按工作流规则 F：任务完成后回顾"过程暴露的问题/改进点"，将其更新到此处。

### 2026-05-03 第一次交付（v0.0 静态 demo）
- ✅ 13 个文件，零 npm，可直接 cp 部署
- ✅ 端口与慢病项目错开（8090 vs 8080）

### 2026-05-03 第二次交付（v0.1 前后端一体）
- ✅ FastAPI 后端：4 路由 + LLM/ASR 抽象层 + 26 个 pytest 测试全过
- ✅ DeepSeek 适配器走 OpenAI-兼容 `/chat/completions`，mock 模式按 schema 字段名指纹路由
- ✅ 前端接入：`api.js` 统一 fetch，`interactions.js` 4 个表单 → 4 个端点，loading + toast 全覆盖
- ✅ 部署文件：`deploy/seecript-server.service` + `deploy/nginx.conf.example` + `scripts/deploy.sh`，与慢病项目占位符约定一致
- ⚠️ **PowerShell 5.1 编码坑**：`Get-Content -Raw` 默认按系统 ANSI 解码 UTF-8 文件 → 中文标点损坏。解决：批量改 HTML 时用 `[System.IO.File]::ReadAllBytes` + `[System.Text.Encoding]::UTF8.GetString`，或干脆用 IDE 的 Write/StrReplace 工具，避免 `Get-Content`/`Set-Content` 流。

### 2026-05-04 第三次交付（ASR：ffmpeg.wasm + 火山豆包 2.0）
- ✅ 新增 `services/asr_client.py::DoubaoBigmodelASRClient`：submit/query 异步轮询，X-Api-Status-Code 全状态码映射成中文提示
- ✅ 新增 `routers/asr.py::POST /api/asr/transcribe`：multipart/form-data 上传，落盘到 `var/asr-tmp/<uuid>.<ext>`，自动清理 10 分钟前的孤儿文件
- ✅ FastAPI middleware 给 HTML 页面加 COOP/COEP credentialless（满足 ffmpeg.wasm 的 SharedArrayBuffer 要求，不影响 CDN 字体）
- ✅ 新增 `asr-uploader.js`（ES module）：ffmpeg.wasm 0.12 抽 16kHz 单声道 mp3 + 进度回调 + 自动填 textarea
- ✅ 35 个 pytest 全过（原 26 + ASR client/endpoint 9）
- ⚠️ **本地真测豆包必须 ngrok**：火山异步任务模式需要公网音频 URL，`PUBLIC_BASE_URL=""` 时自动降级 mock（避免误以为成功）
- ⏳ **待用户验收**：① 浏览器打开 feature-1，上传一段 30 秒视频，看 ffmpeg.wasm 抽轨进度 ② 配 ngrok + DOUBAO 真调一次

### 2026-05-04 第四次交付（生产部署 artifacts 对齐慢病服务器）
- ✅ 新增 `scripts/install-on-medi-server.sh`：一键安装到现有慢病服务器（root 跑），自动建用户/拉代码/装依赖/写 .env/装 systemd/装 nginx/健康检查；交互式问域名 + 2 个 Key；幂等可重跑
- ✅ 新增 `scripts/health-check.sh`：生产端到端验收脚本，4 LLM 端点 + 1 ASR 端点；ASR 可选传一个真实 mp3 文件做完整轮询
- ✅ 通过 dig + curl + ipapi 主动探测，把 `zlhu.asia` 的真实基础设施事实写进 `docs/INFRA.md`（阿里云香港、IP 47.239.58.145、Ubuntu + nginx 1.18，已配 HTTPS 强制跳转）
- ⚠️ **当时设计基于「标准版异步」**：nginx 含 `/asr-tmp/` location、systemd `ReadWritePaths` 含 `var/asr-tmp`、env 含 `PUBLIC_BASE_URL` — 全部在 v0.6 中删除

### 2026-05-04 第五次交付（迁移到豆包**极速版**，部署大幅简化）
- ✅ 重写 `services/asr_client.py::DoubaoBigmodelASRClient`：标准版 submit/query 轮询 → 极速版 `/recognize/flash` 一次请求；资源 ID `volc.bigasr.auc` → `volc.bigasr.auc_turbo`；音频 base64 inline，废弃公网 URL 路径
- ✅ 重写 `routers/asr.py`：移除磁盘临时文件 + 移除 `_cleanup_stale_files`；ASRError 按 upstream code 映射 422/502
- ✅ `ASRClient` 抽象基类：主入口 `transcribe_bytes(audio_bytes)`；保留 `transcribe_url` 默认实现以保持向后兼容
- ✅ `config.py`：删 `doubao_submit_url` / `doubao_query_url` / `asr_poll_*` / `public_base_url` / `asr_tmp_dir` / `asr_tmp_max_age_seconds` 6 个字段，新增 `doubao_recognize_url` + `asr_timeout_seconds`
- ✅ `main.py`：移除 `/asr-tmp/` 静态挂载；COOP/COEP 中间件保留（ffmpeg.wasm 仍需 SharedArrayBuffer）
- ✅ `deploy/nginx.conf.example`：删 `/asr-tmp/` location；proxy 超时 200s → 90s
- ✅ `deploy/seecript-server.service`：gunicorn timeout 240s → 120s；`ReadWritePaths` 只保留 `server/logs/`
- ✅ `scripts/install-on-medi-server.sh`：移除 PUBLIC_BASE_URL 引导步骤；自动清理旧 .env 中的 6 个 legacy 字段；只剩 1 步手动 (certbot)
- ✅ **本地端到端真测豆包跑通**：SAPI 合成 565KB wav → POST → 2.83 秒返回 transcript（极速版承诺的 P95<5s 兑现）
- ✅ 35 个 pytest 全过（mock 路径 100% 覆盖）
- ⏳ **待用户做的 5 件事**：① 阿里云加 DNS A 记录 ② 火山开通**极速版**资源（资源 ID `volc.bigasr.auc_turbo`） ③ rsync 代码上服务器 ④ root 跑 install ⑤ certbot
- ⏳ **v0.6 计划**：① OSS 直传支持 100MB 上限的更长视频 ② 流式 SSE typewriter ③ slowapi IP 限频 ④ Sentry 错误监控

### 2026-05-06 第十一次交付（v0.10 死按钮普查 + 人设手动编辑器）
- ✅ **前端按钮完整普查**：扫描 7 个 HTML 页面共 84 个按钮（index/workspace/feature-1..5），逐一对照 `interactions.js` / `seecript-history.js` / `t2v.js` / `asr-uploader.js` 的绑定逻辑，输出诊断表
  - 结论：**没有真正的死按钮**——所有 button 都至少有 1 处事件监听（直接 `data-seecript-action` / 文本匹配 / `bindCopyButtons` 全局扫描三种模式）
  - 唯一可优化项：feature-3 「换一版」按钮文案模糊（实际触发 runGenerate 重跑全部）→ 改为「重新生成」+ 加 `title` 提示，`bindSeoOutputActions` 双匹配兼容缓存中的旧 HTML
- ✅ **新增 `seecript-persona-editor.js`**：基于全局 modal 单例的可复用编辑器，遵循 SOLID
  - **SRP**：只管「展示 / 校验 / 提交」表单；不持久化（onSave 回调注入）
  - **DIP**：feature-2 / workspace 两处通过同一个 `SeecriptPersonaEditor.open(persona, options)` 调用
  - **OCP**：扩展字段只改 `FIELD_DEFS`，不动生命周期
  - **字段**：方案名（必填，30 字软上限）/ 差异化逻辑 / 为何值得做 / 起号建议 / 变现预判 / 推荐星级（5 颗星点选）/ 对标账号（多 token 输入，逗号顿号空格分号都行）
  - **交互**：实时字符计数 `0/40` + 越界变红、ESC / 背景点击 / × 都能关闭、保存中按钮锁、回调返回 false 时不关 modal 保留输入
- ✅ **`seecript-history.js` 加 3 个 API**：
  - `getPersona(recordId, idx)` → 单方案查询（编辑器初始化时拿完整字段）
  - `updatePersona(recordId, idx, updates)` → **白名单字段合并**（拒绝改 `id` / `createdAt`）+ score 自动 1-5 clamp + reference_accounts 类型兜底
  - `bindPersonaEditButtons()` → 给 detail 区的 `[data-action="edit-persona"]` 按钮挂事件（幂等，dataset 锁防重）
  - **彩蛋**：当被编辑的方案恰好是 `SeecriptActivePersona` 当前选中那个时，自动同步 `sessionStorage["seecript.activePersona"]` 快照——避免改了名字但 feature-1 第 0 步面板还显示旧名字
- ✅ **feature-2.html 人设结果卡** 在「采用此方案 → 进入爆款拆解」旁边加「✎ 编辑」按钮；编辑保存后单卡内存数组同步 + 重新渲染
- ✅ **workspace.html「我的人设方案」看板** 展开 detail 后每个方案右上角加「✎ 编辑」按钮；保存后整版看板 `renderBoards()` 重渲染
- ✅ **`app-screens.css` 补 ~120 行样式**：`.btn.xs` 紧凑尺寸、编辑器表单字段、5 星点选交互、字符计数器越界态、modal 复用 `.seecript-modal__*` 骨架
- ✅ **零回归**：54/54 pytest 仍全过；额外用 Node + jsdom-stub 跑了一段最小集成测试覆盖 `SeecriptHistory.updatePersona`：savePersonas → getPersona → updatePersona → score clamp（999→5、-2→1）→ 未知 record id 返回 null → id/createdAt 防覆盖 → sessionStorage 同步刷新，全部 OK
- ⏳ **后续打磨候选**：① 编辑器引入「撤销最近一次修改」（保留 1 步历史）② 给方案"复制为新方案"动作（从 1 个生 N 个变体）③ workspace 看板增删改后通过自定义事件广播，避免依赖 `renderBoards` 全局重渲染

### 2026-05-06 第十次交付（v0.9 文生视频接入 · 第 7 个 AI 干预点）
- ✅ **新增 `services/t2v_client.py`**：`T2VClient` 抽象基类 + `ZhipuT2VClient`（智谱 REST；默认 cogvideox-3 自动附加 fps/duration）+ `MockT2VClient`（8 秒后自动 SUCCESS 的内存任务存储）；与 LLMClient/ASRClient 完全对齐 SOLID 风格
- ✅ **新增 `routers/t2v.py`**：`POST /api/t2v/submit` + `GET /api/t2v/query/{task_id}`；prompt 防御性双层校验（Pydantic schema + 路由层）；T2VError 按 upstream code 映射 400/404/422/502，永不返回 500
- ✅ **新增 `feature-5.html` + `t2v.js`**：4 阶段状态机（input/loading/result/error）；prompt 自动从 sessionStorage 带入脚本 `scenes[].visual` 字段；轮询逻辑含 5s 间隔、3 次连续失败放弃、8 分钟硬超时、单飞控制
- ✅ **`feature-1.html` 第 4 步加 CTA**「→ 分镜素材生成」；脚本未生成时按钮 disabled，生成成功后 enable + sessionStorage 写入脚本对象（含 `full_text` 供文生视频默认提示词）
- ✅ **`config.py` 加 5 个 T2V 字段**：`t2v_provider` / `zhipu_api_key` / `zhipu_video_model` / `t2v_max_prompt_chars` / `t2v_mock_duration_seconds`；`/api/health` 同步暴露 `t2v_provider`
- ✅ **新建 `server/.env.example`**：作为标准配置模板提交进库（不忽略），把 LLM/ASR/T2V 三大模块的环境变量集中说明
- ✅ **17 个新单测全过 + 零回归**（54 = 37 老 + 17 新）：mock 任务时间渐进、单例保留、factory 降级、Zhipu 构造校验、生命周期端到端、超长 prompt 422、未知任务 404
- ✅ **本地 mock 联调通过**：submit 0.4s 返回 → 立即查询 pending → 9 秒后查询 succeeded + video_url + cover_image_url；trace_id 链路完整可观测
- ✅ **PRD 升级到 v3.0**：§3.1 表格扩展为 7 行、§3.2 改为"7/7 都是 AI"、新增 §3.7 文生视频接入策略（5 路径评估 + 6 家供应商对比 + 工程落地图）
- ⚠️ **关键设计选择**：默认 **`cogvideox-3`**（与智谱开放平台主推一致）；服务端对 v3 自动传 `fps`/`duration`。若需低价 6 秒方案，在 `.env` 设 `ZHIPU_VIDEO_MODEL=cogvideox-2`（勿向 v2 请求体写入 fps/duration）。
- ⏳ **待用户做**：① 去 [智谱开放平台](https://open.bigmodel.cn/usercenter/proj-mgmt/apikeys) 创建 API Key（注册即送 18M Token + 0.5 元/条视频）② 在 server/.env 设 `T2V_PROVIDER=zhipu` + `ZHIPU_API_KEY=<your-key>` ③ 重启后用 feature-5 真测一条视频
- ✅ **同日增补 · 分镜素材表述与拆解台词上限**：`feature-5` 统一为「分镜素材生成」并引导剪映 / Premiere / 达芬奇剪辑成片；后端 `TRANSCRIPT_MAX_CHARS=50000` 与前端校验对齐；爆款拆解上传建议改为「约 1 分钟」量级素材。
- ⏳ **v1.0 计划**：① 演进路径 A → 路径 D（叠加 TTS 配音 + 字幕合成出完整 AI 解说视频）② 用户级视频额度配额 ③ 任务失败原因结构化分类（区分内容审核 / 余额不足 / 临时错误）

### 2026-05-05 第九次交付（PRD v2.0 + 文档构建工作流自动化）
- ✅ **PRD.md 升级到 v2.0**，新增 6 大块内容：
  - §1.1 目标用户加 22-35 岁年龄段 + 行为特征（每天刷视频 ≥ 90 分钟、月均付费 1-2 次）
  - §1.2 三大痛点采用 **A+C+F 佐证**（行业数据 + 平台规则 + 案例叙事），引用克劳锐 / 新榜 / 抖音创作者大会 / 星子文化白皮书等权威数据
  - §2.0 加产品概述一句话
  - §3.2 / §3.3 加"去掉 AI 这个产品还能成立吗"反问 + 4 项 AI 生成能力强大之处
  - §3.4 加"模型 / API / 平台清单 + AI 工作流程图"集中段
  - §八 商业化（目标市场 1000 万 KOC + 4 阶段盈利模式 + 5 项竞争优势 + 4 类风险对冲）
  - §九 落地可行性（技术架构图 + 已落地 / 规划开发节奏 + 三阶段资源评估）
- ✅ **新增 `scripts/extract_docx_images.py`**：从 docx 抽图到 `docs/screenshots/s-NN.png`，输出 mapping.json 标注每张图所在章节
- ✅ **升级 `scripts/build_prd_docx.py`**：支持 markdown 标准 `![alt](path)` 占位语法，自动嵌入图片 + alt 转灰色斜体图注；图缺失降级为红字占位不阻塞构建
- ✅ **截图集中管理**：27 张截图全部归档 `docs/screenshots/`，PRD.md 通过相对路径引用——以后改 PRD 不用再手工往 docx 里粘图
- ✅ **PRD 迭代记录表**：每次 PRD 重大改版都在表头登记一行（v 号 / 日期 / 对应产品版本 / 关键变更）

### 2026-05-04 第八次交付（人设直连拆解 + brief 创作要求）
- ✅ **人设页生成即可一键采用**：feature-2 的每张人设方案卡底部新增「采用此方案 → 进入爆款拆解」按钮；点击后 `SeecriptActivePersona.setSelected(record, idx)` 把 `{recordId, personaIdx, name, differentiation, rationale, score, inputs}` 写进 `sessionStorage["seecript.activePersona"]`，再跳转 `feature-1.html`，第 0 步面板自动渲染选定方案——告别"生成完还得跳两次才能开始拆解"的体验断点
- ✅ **第 3 步加 brief 创作要求表单**：在引导式问答启动前增加一组 `<chip>`（视频时长 15s/30s/60s/90s/3min · 节奏 紧凑/标准/慢节奏 · 风格 中性/幽默/严肃/夸张/温情）+ 200 字自由补充 textarea；用户点「开始问答」时表单立即锁定（防中途篡改），整轮 QA + 脚本都用同一份 brief
- ✅ **brief 字段贯穿后端两个端点**：`schemas.QARequest.brief` / `schemas.ScriptRequest.brief` 均为 `Optional[str]` (≤1000 字)；`routers/qa.py` 与 `routers/script.py` 在 user message 中加入「【用户自填的创作要求（必须遵守）】」块；`prompts/qa.py` 与 `prompts/script.py` 强制 LLM 把时长/节奏/风格/自由补充作为硬约束落地（避免出现"问答按 30s 选了选项，最终脚本写成 1200 字"的不一致）
- ✅ **OpenAPI schema 自验通过**：`brief` 在两个 request schema 中均以 `anyOf [string max 1000 | null]` 注册；超长 brief 边界返回 422；满轮次仍由 router 强制 `done=true`（0ms，不调 LLM）—— brief 不影响轮次硬收敛
- ✅ **mock fingerprint 兼容**：mock client 仅依赖 system prompt 中的 `rationale` / `hook_narration` 关键词分诊，brief 字段引入后 mock 模式行为零变化
- ⏳ **v0.9 计划**：① brief 选择落入 saveScript 历史项目，工作台展开后能看到当时的创作约束 ② 标题车间也接入 brief（让 SEO 标题贴合创作风格）

### 2026-05-04 第七次交付（feature-1 真正闭环：QA + 原创脚本）
- ✅ **第 1 步输入做成选择题**：上传视频/音频 vs 粘贴台词文本 用 tab 切换，消除"两个并列输入框到底填哪个"的歧义；ASR 完成后自动切到文本 tab 并填入识别结果，引导用户点「用 AI 拆解骨架」
- ✅ **第 2 步加空状态**：未拆解前显示「等待拆解」说明而非硬编码 demo 卡片，避免误导
- ✅ **第 3 步真做引导式问答**：新增 `POST /api/qa/next` 端点（DeepSeek 实现）；prompt 限制为 3 轮单选题（Hook / Body 切入 / CTA），路由层在 answers 长度 ≥ 3 时强制 `done=true`（router 拦截、不调 LLM、0ms 收敛）；前端用状态机驱动 IDLE→RUNNING(1..3)→DONE，progress 进度条 + 已答历史摘要 + 选项点过即冻结防重复
- ✅ **第 4 步真出原创脚本**：新增 `POST /api/script/generate`，基于骨架 + 3 个答案 + 人设生成 hook_narration + scenes[] + cta_narration + full_text；前端用 .seecript-skeleton 卡片复用样式渲染，**复制纯文本**按钮调 `navigator.clipboard.writeText()` 并提示字符数
- ✅ **不开放自由输入（v0.x 决策）**：早期方案曾保留「让我自己输入…」自由文本框，但内测发现 LLM 把自由文本回填到下一轮 prompt 时容易出现"重复确认"循环、对话发散；v0.x 优先保收敛与产物质量，全部用 LLM 生成的可朗读选项，用户单选即可
- ✅ **mock fingerprint 扩到 6 个**：`hook_narration` → script、`rationale` → qa；保证 `LLM_PROVIDER=mock` 时新接口仍有合规 sample 返回
- ✅ **生产 ffmpeg.wasm 修复**：`@ffmpeg/ffmpeg` + `@ffmpeg/util` 改为本地 `/vendor/ffmpeg/` 同源加载（满足 Worker 同源约束）；nginx COOP/COEP 头在 `location /` 与 `/assets/` 内重复声明（修复 add_header 子块覆盖父块的经典坑）
- ⏳ **v0.7 计划**：① 第 3 步加"重新出题"按钮 ② 已生成的脚本写入工作台历史 ③ 一键导出脚本到剪贴板 + 钉钉/飞书 webhook

### 2026-05-04 第六次交付（产品形态调整 + 运维卡片）
- ✅ **首页 = 产品说明**：旧 `landing.html` 升格为 `index.html`，原工作台迁到 `workspace.html`；删除"爆款拆解"作为独立卖点的卡片（保留为工作台流程内的实现手段）；hero + 底部紫色 CTA 双重「进入工作台」
- ✅ **工作台历史看板**：新增 `seecript-history.js` 模块（SRP），把"我的人设方案"和"我的拆解项目"以 localStorage 持久化（30 条上限）；KPI 卡也读 LS 实时算
- ✅ **标题车间锁定抖音**：前端删除 4 个平台 tab；`SEORequest.platform` 收紧为 `Literal["douyin"]`；prompt 重写为单平台抖音规则（钩子前置/标签密度/emoji 控制）；新增 2 个反向测试，37 通过
- ✅ **导航顺序统一**：人设生成放在爆款拆解前；feature-1 工作流条加 Step 0 = 人设生成
- ✅ **全站去 Demo 表述**：5 HTML + interactions.js 清干净 v0.x / Demo / 静态高保真 / 演示模式 等所有"未上线"暗示
- ✅ **push 脚本加防护**：`scripts/push-to-github.ps1` 加项目根目录守卫；`.cmd` 包装层加 `pushd "%~dp0.."` 自动切目录（修复了之前从父目录跑误伤无关 git 仓库的真实事故）
- ✅ **README 加第 11 章**：日常自助运维 cheat sheet，覆盖开发→push→部署→重启→看日志→排错→换 Key 全链路

---

## 11. 日常自助运维 Cheat Sheet（你独自跑全流程）

> 这一章是为了让你**完全脱离我**也能维护这个项目而写的。每个场景都给可复制粘贴的命令，按顺序抄就行。

### 11.1 开发流程：从改代码到上线的完整一圈

```
1. 改代码（VS Code / Cursor 任意编辑器）
       ↓
2. 本地起 uvicorn 自测              [.\run.ps1]
       ↓
3. 跑测试，确认没破东西              [server\venv\Scripts\python.exe -m pytest server/tests -q]
       ↓
4. 提交到 git                       [git add . ; git commit -m "..."]
       ↓
5. push 到 GitHub                   [.\scripts\push-to-github.cmd <repo-url> ...]
       ↓
6. SSH 上服务器跑 deploy.sh         [/opt/seecript/scripts/deploy.sh]
       ↓
7. 跑生产健康检查                    [/opt/seecript/scripts/health-check.sh]
       ↓
完工 ✓
```

### 11.2 常用命令一页打印（最重要）

| 我想干什么 | 命令（在 `D:\nocode\seecript\` 下跑） |
|---|---|
| **本地起服务** | `.\run.ps1`（首次会装依赖；后续加 `$env:SKIP_INSTALL=1` 加速） |
| **本地停服务** | `.\stop.ps1` |
| **本地跑全套测试** | `.\server\venv\Scripts\python.exe -m pytest server/tests -q` |
| **看本地日志** | `Get-Content logs\uvicorn.log -Tail 50 -Wait` |
| **提交并推到 GitHub** | `git add .` → `git commit -m "your message"` → `git push` |
| **从 GitHub 拉最新代码** | `git pull` |
| **SSH 上服务器** | `ssh root@47.239.58.145`（你自己的 SSH key） |
| **服务器一键升级** | （在服务器上）`sudo /opt/seecript/scripts/deploy.sh` |
| **服务器看实时日志** | `sudo journalctl -u seecript-server -f` |
| **服务器重启服务** | `sudo systemctl restart seecript-server` |
| **服务器看 nginx 日志** | `sudo tail -f /var/log/nginx/access.log` |
| **生产健康检查** | （在服务器上）`bash /opt/seecript/scripts/health-check.sh https://seecript.zlhu.asia` |

### 11.3 场景一：我改了代码想发布

```powershell
# ⚠️ 必须在项目根目录跑
cd D:\nocode\seecript

# 1. 本地自测
.\run.ps1
# 浏览器打开 http://127.0.0.1:8090 / 检查
.\stop.ps1

# 2. 跑测试
.\server\venv\Scripts\python.exe -m pytest server/tests -q

# 3. 提交
git status                    # 看改了哪些文件
git diff                      # 看具体改了什么
git add .                     # 添加全部改动
git commit -m "feat: 简短描述这次改了什么"

# 4. 推到 GitHub（首次设过身份后，以后直接 git push 即可）
git push

# 5. 部署到生产
ssh root@47.239.58.145
# 服务器上：
sudo /opt/seecript/scripts/deploy.sh
# 这个脚本会自动：备份当前版本 → git pull → pip install → 重启 → 健康检查 → 失败自动回滚
exit
```

### 11.4 场景二：服务挂了，5 分钟应急

按这个顺序排查：

```bash
ssh root@47.239.58.145

# A. 服务进程在不在？
sudo systemctl status seecript-server
# 如果 inactive/failed → 直接重启：
sudo systemctl restart seecript-server

# B. 看错误日志（最新 100 行）
sudo journalctl -u seecript-server -n 100 --no-pager

# C. nginx 通不通？
sudo systemctl status nginx
sudo tail -50 /var/log/nginx/error.log

# D. 端到端健康检查
curl -fsSL https://seecript.zlhu.asia/api/health
# 应该返回 {"status":"healthy",...}

# E. 全量 e2e 检查（耗时 ~30 秒，会真调一次每个 AI）
bash /opt/seecript/scripts/health-check.sh https://seecript.zlhu.asia
```

如果上面都没解决，**回滚**（deploy.sh 会备份每次发布）：

```bash
# 看历史备份
ls -la /opt/seecript.backups/
# 选最新一个稳定版本
sudo /opt/seecript/scripts/deploy.sh --rollback /opt/seecript.backups/<时间戳>
```

### 11.5 场景三：换 / 撤销 API Key

> **每隔 90 天换一次 Key 是好习惯**。Key 一旦不小心进过 git history，必须立刻撤销。

**DeepSeek**：

```bash
# 1. 在 https://platform.deepseek.com/api_keys 点旧 key 旁的 Disable / Delete
# 2. 在同页面新建一个 Key，复制（只能复制一次）
# 3. 服务器上更新 .env
ssh root@47.239.58.145
sudo nano /opt/seecript/server/.env
# 找到 DEEPSEEK_API_KEY=sk-xxx 一行，改成新 Key
# Ctrl+O 保存，Ctrl+X 退出
# 4. 重启服务（systemd 会重新读 .env）
sudo systemctl restart seecript-server
# 5. 验证
curl -fsSL https://seecript.zlhu.asia/api/health
```

**火山豆包**：流程一样，控制台在 [https://console.volcengine.com/speech/app](https://console.volcengine.com/speech/app)，环境变量名是 `DOUBAO_API_KEY`。

### 11.6 场景四：怎么开新功能分支

```powershell
cd D:\nocode\seecript

# 1. 从 main 拉一个新分支
git checkout -b feat/my-new-feature

# 2. 改代码、提交
git add .
git commit -m "feat: 新增 XXX"

# 3. 推到 GitHub（第一次推某个新分支需要 -u）
git push -u origin feat/my-new-feature

# 4. 在 GitHub 网页发起 Pull Request 合到 main
# 5. 合并后回到 main 拉最新
git checkout main
git pull
# 6. 删掉本地的旧分支（远端的可以在 PR 合并时勾选自动删）
git branch -d feat/my-new-feature
```

### 11.7 场景五：突然想撤回上一次 commit

```powershell
# 我刚 commit 了但还没 push —— 撤回保留改动
git reset --soft HEAD~1

# 我刚 commit 了但还没 push —— 撤回并丢掉改动（小心！）
git reset --hard HEAD~1

# 我已经 push 了，需要"反向"再 commit 一次撤销
git revert HEAD
git push
```

### 11.8 场景六：本地 .env 配置忘了

`.env` **不在 git 里**（被 `.gitignore` 排除）。如果丢了：

```powershell
cd D:\nocode\seecript
Copy-Item server\.env.example server\.env
notepad server\.env
# 填入 DEEPSEEK_API_KEY 和 DOUBAO_API_KEY
```

服务器上的 `.env` 在 `/opt/seecript/server/.env`（root 可读写，`seecript` 用户只读）。

### 11.9 不可破坏的红线（这几条踩了会出大事）

| ❌ 不要做 | ✅ 应该做 |
|---|---|
| 在 `D:\nocode\` 父目录跑 git 命令 | 永远 `cd D:\nocode\seecript` 再跑 |
| 把 API Key 写到任何 `*.md` 或 `*.html` 里 | 只放在 `server/.env`（已被 gitignore） |
| 在生产服务器手动改 `/opt/seecript/server/app/*.py` | 永远在本地改 → push → `deploy.sh`，让生产服务器 git pull |
| `git push --force` 到 main 分支 | 永远只 `git push`；要回退用 `git revert` |
| 直接 `kill -9` 服务进程 | 用 `systemctl restart seecript-server` |
| 删 `.git/` 目录 | 真的要重新开局，先做 `git clone` 一份当备份再说 |

### 11.10 报错关键字 → 怎么处理

| 看到这个 | 通常原因 | 处理 |
|---|---|---|
| `LLM 调用失败：HTTP 401` | DeepSeek Key 错或被封 | 去 platform.deepseek.com 重置 |
| `LLM 调用失败：HTTP 429` | 余额不足 / 触发限频 | 充值 / 等几分钟 |
| `ASR 失败：upstream 401` | 火山 Key 错 | 去火山控制台核对 |
| `T2V_NO_KEY` / 视频生成 400 | 设了 `T2V_PROVIDER=zhipu` 但没配 `ZHIPU_API_KEY` | 编辑 `server/.env` 填 key 后 `.\stop.ps1; $env:SKIP_INSTALL=1; .\run.ps1` |
| 视频生成 422 ：`String should have at most 500 characters` | prompt 超过智谱官方 512 字节限制 | 精简描述（建议结构：主体 + 环境 + 镜头 + 氛围）|
| 视频生成 422 ：`prompt 违反内容审核` | 含人物 / 品牌 / 政治 / 暴力等敏感关键词 | 改为具象画面描述，避开实体 |
| 视频生成 502 ：`HTTP_429` | 智谱并发上限（V0=5、V1=10、V2=15、V3=20）| 等当前任务结束再提交，或升级账户等级 |
| 视频生成 ：8 分钟超时 | 智谱当前在排队 | 不扣费，稍后重试；高峰时段可手动用 `task_id` 查询 |
| `502 Bad Gateway`（nginx） | 后端 systemd 服务挂了 | `systemctl restart seecript-server` |
| `gunicorn timeout` | 视频太长 / DeepSeek 卡 | 检查 `journalctl -u seecript-server` 看上游耗时 |
| pytest 找不到 `app` 模块 | cwd 不对 | `cd server` 后再跑，或用 `python -m pytest server/tests` |
| `git push` 弹浏览器登录 | 凭证过期 | 用浏览器登 GitHub 重新授权即可 |
| GitHub 拒绝 push 显示 secret detected | 不小心把 key 写进了文件 | 删 key、`git commit --amend`、再 push |

### 11.11 PRD 文档维护工作流（v0.9 起 docx 完全自动化）

PRD 的源是 `docs/PRD.md`，docx 是产物——**永远不要手改 PRD.docx，改了下次构建会被覆盖**。

#### 日常迭代：改 PRD 内容

```powershell
# 1. 改 docs/PRD.md（在表头追加一行迭代记录）
# 2. 重新生成 docx
$env:PYTHONIOENCODING="utf-8"
python scripts\build_prd_docx.py
# 输出：OK -> D:\nocode\seecript\docs\PRD.docx  (8966.1 KB)
```

构建脚本会自动把 md 里的 `![alt](docs/screenshots/s-NN.png)` 转成内嵌图 + 灰色图注，宽度统一 6 英寸。

#### 新增截图：把图扔进 `docs/screenshots/` 即可

1. 截图按现有序号往后排，命名 `s-28.png` / `s-29.png`...
2. 在 `docs/PRD.md` 的目标章节插入 `![图注文字](docs/screenshots/s-28.png)`
3. 重跑 `python scripts\build_prd_docx.py`

#### 紧急回收别人改过的 docx（极少用到）

如果用户/同事把图片或文字直接塞进 docx 里没回 md，可以用抽图脚本把图救出来：

```powershell
$env:PYTHONIOENCODING="utf-8"
python scripts\extract_docx_images.py
# 27 张图导出到 docs/screenshots/，mapping.json 标记每张图所在章节
```

然后比对 mapping 与 md，把缺位的图占位补回 md，重跑构建即可。

#### 红线

- ❌ 直接编辑 `docs/PRD.docx` —— 会被构建覆盖
- ❌ 把图直接 base64 嵌进 md —— Word 会把它当文字
- ❌ 删 `docs/screenshots/mapping.json` —— 这是抽图工作流的索引
