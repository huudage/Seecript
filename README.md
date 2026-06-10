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

## 本地运行

需要：Python 3.10+ / Node.js 18+ / ffmpeg+ffprobe 在 PATH。

```bash
# 后端：自举 venv + 装 requirements + 起 :8090
./run.ps1               # Windows
./run.sh                # macOS / Linux

# 前端：起 :5173
cd web
pnpm install
pnpm dev
```

健康检查：<http://127.0.0.1:8090/api/health>

API key 配置在 `server/.env`（参考 `server/.env.example`）。**缺 key 不会降级 mock，会直接 5xx**——生产路径要求真链路。

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
