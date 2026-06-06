# Seecript

> 视频拆解与重组平台：样例视频拆解 → 结构抽取 → 素材缺口补全 → 视频重组。

## 仓库结构

```
seecript/
├── web/                                   # React 19 + Vite + TS + Tailwind v4 + Zustand
│   └── src/
│       ├── pages/                         # Home / Library / Decompose / Compose / Knowledge
│       ├── components/
│       │   ├── compose/                   # FillAigcPanel · FillCopyPanel · MaterialGrid · FourTrackBoard …
│       │   ├── preview/                   # PlanComposition (Remotion in-browser) · 5 AnimatedImage 渲染层
│       │   ├── library/ · edit/           # 资产库列表 · 自然语言编辑面板
│       │   ├── timeline/ · home/          # 主轨道 / 包装轨 · 首页
│       │   └── nav/ · layout/             # 顶部导航 · 框架
│       ├── hooks/                         # useMaterialPreprocessPolling …
│       ├── stores/                        # session · plan · projects · edit (zustand)
│       ├── api/                           # client.ts (fetch 封装) + sse.ts (EventSource)
│       └── types/schemas.ts               # 与后端 schemas.py 镜像
│
├── remotion/                              # 包装轨独立 Node 项目（透明 WebM）+ AnimatedImage composition
│
├── server/
│   ├── app/
│   │   ├── main.py                        # FastAPI + CORS + 错误中间件 + 静态挂载
│   │   ├── config.py                      # 配置 / Provider 开关 / 资源根目录
│   │   ├── schemas.py                     # 全模块 Pydantic v2 契约（Plan / Scene / Material / Gap …）
│   │   ├── routers/                       # 路由层 —— 每个文件对应一个业务模块
│   │   │   ├── library · decompose · material · gap · plan · render · edit
│   │   │   ├── asset · packaging · clarify · knowledge · project · step · voice · asr
│   │   │   └── _deps.py                   # 通用依赖
│   │   └── services/
│   │       ├── llm_client.py              # LLMClient 抽象 + Mock + DeepSeek + Doubao Ark（含 multimodal）
│   │       ├── asr_client.py              # ASR 抽象 + VAD 门控
│   │       ├── t2v_client.py              # Seedance T2V（submit / poll）
│   │       ├── seedream_client.py         # Seedream T2I 多镜头 storyboard
│   │       ├── video/                     # ffmpeg / scene_detect / aspect / bgm_analysis / voice_detect / ocr
│   │       ├── agent/                     # decompose · plan · gap · packaging · clarify · copy_outline · aigc_prompt · compose_edit
│   │       ├── render/                    # pipeline.py（多步流水线）+ seedance_chain.py（首尾帧拼接）
│   │       ├── materials/                 # MaterialStore · GapStore · VideoPreprocessor（PySceneDetect + VLM caption）
│   │       ├── assets/ · library/         # 资产库 · 样例库
│   │       ├── plans/ · projects/         # PlanStore · PlanSnapshotStore · ProjectStore
│   │       ├── jobs/                      # JobStore + asyncio.Queue + SSE
│   │       ├── prompts/ · profile/        # 提示词集中 · 用户偏好
│   │       └── tts/                       # TTS 抽象 + provider
│   ├── tests/                             # pytest 用例（mock 路径）
│   ├── samples/                           # 内置样例视频 + 预解析 manifest
│   └── var/
│       ├── projects/<project_id>/         # materials/index.json · gaps/<plan_id>.json
│       ├── uploads/<project_id>/          # 用户上传文件 + shots/<material_id>/ 缩略图
│       └── aigc_videos/                   # AIGC 视频本地落盘（解决 TOS 跨域）
│
├── docs/                                  # ARCHITECTURE / DEMO / INFRA / PRD
├── deploy/ · scripts/                     # 部署脚本与服务器 systemd unit
└── run.* · stop.*                         # 本地启动 / 停止
```
