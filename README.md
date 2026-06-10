# Seecript

> 视频拆解与重组平台：样例视频拆解 → 结构抽取 → 素材缺口补全 → 视频重组 → 自然语言编辑。

公网 demo：<https://seecript.zlhu.asia>

## 核心能力

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

## 仓库结构

```
seecript/
├── web/                                 React 19 + Vite + TS + Tailwind v4 + Zustand
│   └── src/{pages,components,stores,api,types}
├── remotion/                            包装轨独立 Node 项目（透明 WebM）+ AnimatedImage
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
├── docs/                                ARCHITECTURE · AI-DESIGN · PRD · DEMO · CATALOG_FRAME
└── run.{ps1,sh} / stop.{ps1,sh}         本地启动 / 停止
```

## 技术栈

| 层 | 选型 |
|---|---|
| 后端 | FastAPI + Pydantic v2 + Python 3.10+ |
| 前端 | React 19 + Vite + TypeScript + Tailwind v4 + Zustand |
| 视频包装 | Remotion |
| LLM | Doubao Seed-2.0-lite（多模态，OpenAI 兼容） |
| ASR | 豆包 bigasr_auc_turbo + librosa VAD 门控 |
| T2V | doubao-seedance-2-0-fast-260128 |
| T2I | doubao-seedream |
| 镜头分割 | PySceneDetect |
| 音频分析 | librosa（RMS energy + onset + tempo） |
| 视频处理 | FFmpeg subprocess |

## 本地部署

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
curl -LO https://seecript.zlhu.asia/release/seecript-source.tgz
# 可选：校验完整性
curl -sL https://seecript.zlhu.asia/release/seecript-source.tgz.sha256 | sha256sum -c -
tar xzf seecript-source.tgz
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

打开 `server/.env`，至少把下面三组其中之一填上真值：

| 用途 | 关键变量 | 申请入口 |
|---|---|---|
| LLM（段落结构 / 文案 / 多模态打标） | `LLM_PROVIDER=doubao_ark` + `ARK_API_KEY` + `ARK_LLM_MODEL` | <https://console.volcengine.com/ark> |
| T2V（AIGC 缺口生成 + 长视频首尾帧扩展，可选） | `T2V_PROVIDER=doubao_ark` + `ARK_T2V_API_KEY`（可复用上面的 Key） | 同上 |
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

- [AGENTS.md](AGENTS.md) — 给代码 agent 的项目简报（硬约束 / 目录 / 部署）
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — 整体架构 / 数据流 / 工具协议 / 安全边界
- [docs/AI-DESIGN.md](docs/AI-DESIGN.md) — AI 干预点详解 + 三层兜底
- [docs/PRD.md](docs/PRD.md) — 产品需求文档
- [docs/DEMO.md](docs/DEMO.md) — 5 分钟演示走查
