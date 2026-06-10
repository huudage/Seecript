# Seecript · 整体 AI 架构 / 工具协议 / 安全边界

> 本文是写给工程师 + 评审人看的系统级技术文档。覆盖：① 整体 AI 架构（哪几个干预点、各点用什么模型、调用链怎么走）；② 工具协议（FastAPI 路由 + Pydantic 契约 + SSE 事件 + LLM tool calling）；③ 安全边界（key 管控、上传隔离、prompt 注入、降级策略、tool calling 校验、跨域、可观测）。
>
> 与 `docs/ARCHITECTURE.md` 的区别：那份是高层项目结构索引；本文做**技术深度**——逐个 agent 拆 prompt 设计、逐个端点列契约、逐条边界给措施。
>
> 对应代码：post stage-52（commit `b9d8f0e`），后端 84 个路由 / 11 个 agent / 6 个外部模型客户端。

---

## 第一部分 · 整体 AI 架构

### 1.1 设计要旨

Seecript 不是"调一次 LLM 出结果"的应用，而是**链式 agent + 多模态融合**的视频处理流水线。设计上有三个核心选择：

1. **AI 干预点收敛到 3 类**——画面理解全走多模态 LLM（不再有独立 VLM client）；缺口画面直接 T2V 生短片（不再先 T2I 再 image-to-video）；ASR 走豆包但加 librosa VAD 门控（纯 BGM 视频跳过省钱）
2. **Agent 分工细颗粒**——11 个 agent 各管一段，prompt 单一职责，互相不抢功能。一个 LLM 调用解决一个具体子问题（段落结构、帧打标、缺口文案、情绪打分、转场推荐、tool 路由）
3. **三层兜底范式**——Prompt 软约束 + Router 硬约束 + Schema 终判，叠加保证确定性收敛

### 1.2 三类 AI 干预点（合并前是 6 个）

| 干预点 | 模型 | 调用方式 | 落点 agent / router |
|---|---|---|---|
| **多模态 LLM**（段落结构 / 帧打标 / 缺口文案 / 包装推荐 / NL 编辑 tool call / 情绪打分 / 澄清问 / AIGC prompt 改写 / 改稿对话） | Doubao Seed-2.0-lite，OpenAI 兼容 | `LLMClient.complete` / `complete_json` / `complete_with_tools` / `complete_multimodal` | `decompose_agent` / `plan_agent` / `gap_agent` / `packaging_agent` / `compose_edit_agent` / `emotion_agent` / `clarify_agent` / `aigc_prompt_agent` / `copy_outline_agent` / `narration_agent` |
| **ASR 口播转写**（VAD 门控） | 豆包 bigasr_auc_turbo（异步 submit + query） | `ASRClient.transcribe` | `decompose_agent`（先用 librosa VAD 判定 has_voice，纯 BGM 直接跳过） |
| **AIGC 视觉**（缺口生图 / 生视频 / 长视频首尾帧串接） | doubao-seedance-2-0-fast-260128（T2V）+ doubao-seedream（T2I） | `T2VClient.submit/query` + `SeedreamClient.generate` | `gap_agent`（aigc 分支 = T2V 5-8s 短片）+ `services/render/seedance_chain.py`（首尾帧扩长视频） |

### 1.3 11 个 Agent 详表（`server/app/services/agent/`）

按"上游产物 → agent → 下游产物"维度组织。每个 agent 一独立块，便于查 prompt 设计与失败兜底。

---

**① `decompose_agent` — 样例拆解主管道**
- **触发**：`POST /api/decompose` 后台任务（用户拆解新样例）
- **职责**：从原始 mp4 抽出全套结构化元数据
- **LLM 调用**（3 次）：
  - 多模态帧打标——每个 shot 的关键帧 + `_VLM_TAG_PROMPT`，输出 `tags[] + script + subject`
  - 段落结构——按 `video_type ∈ {marketing, editing, motion_graph}` 三选一 prompt（`_SECTION_PROMPT_MARKETING` / `_EDITING` / `_MOTION_GRAPH`），输出 `sections[]` 含 `kind + theme + summary`
  - 整片画像——`_UNDERSTANDING_PROMPT`，输出 `archetype + narrative_summary + tone + structural_pattern`
- **非 LLM 步骤**：PySceneDetect 切镜头 + librosa RMS/onset/tempo + librosa VAD 门控 ASR + ffmpeg shot-NN.jpg 关键帧
- **输出**：`SampleManifest`（shots / sections / rhythm / packaging / understanding / analysis / emotion）
- **失败兜底**：单步失败 SSE `error`，不入库；ASR 不可用 → `has_voice=false` 走纯画面分析

---

**② `plan_agent` — 结构改编**
- **触发**：`POST /api/plan/build`（用户选定样例 + 上传素材后）
- **职责**：把样例的 `sections[]` **改写**到用户主题，输出主轨分镜骨架
- **LLM 调用**：`complete_json` 给 `AdaptedSection[]`（含每段 `theme / narration / subjects / shots[]`）；prompt 强约束"保留段角色和镜头数，只换文案与主体锚点"
- **输出**：`Plan`（main_track Scene[] + packaging_track 占位 + bgm + settings + emotion_curve）
- **失败兜底**：`_fallback_adaptation` 镜像复制样例 sections（规则降级，非 mock）

---

**③ `gap_agent` — 缺口检测与三路补全**
- **触发**：`POST /api/gap/detect`（检测） + `POST /api/gap/fill`（执行）
- **职责**：把用户素材 → 9 个 SectionKind 槽位匹配，缺的走三路补
- **算法**：检测纯算法（关键词 + 标签 + duration）；补全分三路
  - `action=rerank` — 纯算法重排现有素材
  - `action=copy` — 委托 `copy_outline_agent` 写文案 + 字卡渲染
  - `action=aigc` — 委托 `aigc_prompt_agent` 写 prompt + `T2VClient.submit`
- **输出**：`Gap[]` / `FillResult`（含新 Scene + match_quality 三档）

---

**④ `packaging_agent` — 包装轨一次性推荐**
- **触发**：`POST /api/packaging/recommend`（plan build 时自动 + step3 手动）
- **职责**：一次给齐转场风格 + 封面 + 字幕样式
- **LLM 调用**：`complete_json` 输出 `PackagingRecommendationV2`——6 种转场方案 + 封面（标题/副标题/调色板/布局）+ 字幕样式 + 标题条
- **输出**：写回 `plan.packaging_track[]`；Remotion 用此渲透明 WebM

---

**⑤ `compose_edit_agent` — Compose 态 ⌘K NL 编辑**
- **触发**：`POST /api/edit/compose`（step2/step3 浮层）
- **职责**：把自然语言指令 → 多 tool_calls → `ComposeEditDiff[]`
- **LLM 调用**：`complete_with_tools` + step2/step3 分流（20+ tools，详见 §2.4）
- **对话三态约束**（`_QA_RULES`）：A) 编辑 tool_calls / B) 讲解 1-3 句中文 / C) 追问 1 句反问
- **关键纪律**：禁止把单一指令拆成多个 diff（脑补）；段落识别必须映射到当前 plan 实际 sections
- **输出**：`ComposeEditResponse(diff[] + 新 plan + plan_id)`

---

**⑥ `emotion_agent`（stage-28）— LLM 多信号情绪打分**
- **触发**：拆解阶段（`decompose_agent` 调用）+ Plan 阶段（`plan_agent` 调用）+ `POST /plan/{id}/recompute-emotion`（手动重算）
- **职责**：综合段落 + BGM + 整片画像 + 用户意图打分，输出曲线
- **LLM 调用**：`complete_json` 给 `anchors[]`（每段一锚） + `peaks[]`（≤2） + `valleys[]`（≤2） + `summary`；规则层做 60 点插值 + 滑动平均
- **输出**：`EmotionCurve`（`backend: "llm" | "rule_fallback"` + `signals_used: ["role","bgm","cut","script","climax","intent"]`）
- **失败兜底**：LLM 挂 → `_role_mood_value + _smooth` 规则版（`backend="rule_fallback"`）

---

**⑦ `clarify_agent` — 意图澄清**
- **触发**：`GET /api/clarify/round`（项目创建后强制 1 轮）
- **职责**：引导式问答收敛用户的 video_goal / target_subjects / 调性偏好
- **LLM 调用**：`complete_json` 给 `ClarifyRound(question + options[])`，最多 3 轮
- **输出**：finalize 时落 `Project.brief + clarify_outline`（**finalize 不再调 LLM**，stage-30 改纯结构化收尾）

---

**⑧ `aigc_prompt_agent` — AIGC 视频 prompt 改写**
- **触发**：`POST /api/gap/aigc-prompt`（4 个变体路由：标准 / image-spec / seedream / tail-frame）
- **职责**：把段角色 + brief + subjects → Seedance T2V 可吃的 prompt（含镜头语言：运镜 / 主体 / 时长 / 风格）
- **LLM 调用**：`complete_json` 给 `AigcPromptResponse(prompt + negative + size + duration)`
- **关键纪律**：注入 `shot_matcher` 给的 ShotPlan，每个 shot 独立 prompt（stage-49 改成单图独立运镜）

---

**⑨ `copy_outline_agent` — 段文案改写大纲**
- **触发**：`POST /api/gap/copy-outline`（gap action=copy 分支调用 + step2/3 手动文案重写）
- **职责**：按段角色重写口播 + 字卡文案
- **LLM 调用**：`complete_json` 给 `CopyOutlineResponse(narration + text_card_lines + subjects)`
- **失败兜底**：`_fallback_outline` 按角色给模板文案（规则降级）

---

**⑩ `narration_agent` — 全段口播重写**
- **触发**：`POST /api/plan/{id}/regenerate-narrations`（用户改主题或语气后批量刷新）
- **职责**：全 plan 所有 scene 的 narration 一次性重写
- **LLM 调用**：`complete_json` 输出 `{scene_id → new_narration}` 字典
- **关键纪律**：保留每 scene duration（stage-44/49 加"严禁凑时长"约束）

---

**⑪ `shot_matcher`（**非 LLM**）— 主体锚点匹配**
- **触发**：内部模块，被 `gap_agent` / `plan_agent` / `aigc_prompt_agent` 调用
- **职责**：把样例镜头主体 ↔ 用户素材主体做相似度评分；输出 `match_quality ∈ {good, fair, poor}`
- **算法**：embedding 余弦 + 角色匹配 + duration 权重（纯算法）
- **输出**：`ShotMatch[]`（一个样例镜头一个 match，含分数 + 推荐换源候选）

### 1.4 主链路数据流

```
浏览器                                         后端                                              模型
─────────                                      ─────────                                         ────────
1. 选/上传样例
   GET /api/library                         → library_router 列举 sys-* + user-*
   POST /api/decompose/upload (新视频)      → ffprobe 时长校验 + cover.jpg 抽帧 + meta.json
   POST /api/decompose                      → BackgroundTask + JobStore + SSE
                                                ├─ scene_detect (PySceneDetect)
                                                ├─ audio_analysis (librosa RMS + tempo)
                                                ├─ voice_detect (librosa VAD)               ────► 有口播？
                                                ├─ asr_transcribe (条件)                   ─────────► ASR 豆包 bigasr 2.0 (audio.url 公网必达)
                                                ├─ frame_extract (ffmpeg shot-NN.jpg 关键帧)
                                                ├─ vlm_tag (multimodal LLM 帧打标)         ─────────► Doubao Seed-2.0-lite (multimodal)
                                                ├─ llm_section (按 video_type 三选一 prompt) ────────► Doubao Seed-2.0-lite (json)
                                                ├─ rhythm + emotion_score (并行)            ────────► emotion_agent: anchors+peaks+valleys
                                                └─ done → SampleManifest 入 manifest_store
   GET /api/decompose/stream                ← SSE event: progress/done/error

2. 上传素材 + 缺口检测
   POST /api/material/upload                → multipart + ffprobe + multimodal LLM 自动打标
   POST /api/gap/detect                     → 槽位匹配（9 个 SectionKind）

3. 缺口三路补全
   POST /api/gap/fill action=rerank         → 纯算法重排
                       action=copy          → copy_outline_agent (LLM)
                       action=aigc          → aigc_prompt_agent (LLM 写 prompt)
                                              + T2VClient.submit ─────────────────────────► Seedance 2.0 fast
                                              + 轮询 query  →  /aigc-videos/<id>.mp4

4. 方案构建
   POST /api/plan/build                     → plan_agent 改写 sections + emotion_agent 重算
                                            + packaging_agent 一次性给转场/封面
   PATCH /plan/{id}/scene/{sid}/...         → 局部改单个 scene/shot
   POST /plan/{id}/recompute-emotion        → emotion_agent 重新打分

5. 视频渲染（6 步流水线）
   POST /api/render/submit                  → BackgroundTask
   GET  /api/render/stream                  ← SSE
                                                ├─ prepare      8%
                                                ├─ ffmpeg_concat 28% (主轨拼接)
                                                ├─ seedance     48% (T2V 长视频首尾帧串接)
                                                ├─ remotion     70% (包装轨透明 WebM + AI 生图动效)
                                                ├─ overlay      88% (主轨 + 包装 + BGM 混流)
                                                └─ finalize     99% → final.mp4 + cover.jpg

6. 自然语言编辑（双入口）
   POST /api/edit/apply (Render 态)         → 三轨分流 (main/packaging/voice) → tool calls
   POST /api/edit/compose (Compose 态 ⌘K)   → step2/step3 分流 → tool calls / 讲解 / 追问
                                              → ComposeEditDiff[] → plan_store.replace() → 新 plan_id
```

### 1.5 三层兜底范式

每个 LLM 调用都按这个范式设防：

```
Layer 1 · Prompt（软约束）           system message 写约束
   ↓ LLM 通常会遵守，但不保证
Layer 2 · Router（硬约束）           routers/*.py 路由函数无条件拦截
   ↓ HTTPException 直接 4xx/5xx
Layer 3 · Schema（终判约束）         Pydantic v2 模型校验
   ↓ 畸形即 422
```

通用兜底（所有 LLM 调用共享，集中在 `services/llm_client.py`）：

| 机制 | 实现 | 触发条件 |
|---|---|---|
| JSON 解析自愈 | `_extract_json` 剥离 ```` ```json ```` 代码栅栏；切首个 `{` 到末位 `}` | LLM 在 JSON 前后加废话 |
| JSON 失败重试 1 次 | `complete_json` 第二次拼"严格要求：必须返回合法 JSON。不要使用 markdown 代码块" | 第一次解析失败 |
| Provider 自动降级 | `get_llm_client` 检测 `LLM_PROVIDER=deepseek` 但 `DEEPSEEK_API_KEY=空` → 落 `MockLLMClient`，记 warn | 仅开发环境；**生产路径关闭此降级，缺 key 直接 5xx** |
| HTTP 状态码透传 | DeepSeek/Doubao 5xx → `LLMError(upstream_status=5xx)` → 路由 502；4xx auth/quota → 502 + 详情 | 上游异常 |
| 结构化日志 | `[trace_id] module ok | provider | elapsed_ms | tokens | size_metric` | 每次调用 |
| 单测 mock | `MockLLMClient` 按 system prompt 关键字猜分支返回结构化假数据 | 仅测试 |

**保留的规则降级**：
- `_fallback_outline`（gap_agent / copy_outline_agent）—— LLM 写文案失败时按段角色给模板文案
- `_fallback_adaptation`（plan_agent）—— LLM 改写 sections 失败时镜像复制
- `_role_mood_value` + `_smooth`（emotion_agent）—— LLM 情绪打分失败时按段角色基线 + 滑动平均

这些**是规则降级，不是 mock 数据**——区别在于不返回假英文 / 假占位文本，而是用确定性算法兜底，能在生产真链路里安全使用。

### 1.6 客户端抽象层（`server/app/services/`）

所有外部模型走统一 client 抽象基类，便于切 provider + 单测注入。

#### `LLMClient`（`llm_client.py`）

```python
class LLMClient(ABC):
    async def complete(system, user, *, temperature, max_tokens) -> str
    async def complete_json(system, user, *, schema_hint, repair_retry=True) -> dict
    async def complete_with_tools(system, user, tools, *, tool_choice, temperature) -> ToolCallResponse
    async def complete_multimodal(system, user_text, images: list[ImageRef], *, ...) -> str
```

**实现栈**：
- `MockLLMClient` —— 单测专用，按关键字匹配
- `_OpenAICompatLLMClient` —— OpenAI 兼容协议基类（chat completions / tools）
- `DoubaoArkLLMClient` —— 豆包方舟 endpoint，多模态走同一接口
- `DeepSeekLLMClient` —— DeepSeek API（v0.x 历史 provider，仍支持）

**多模态 ImageRef 协议**：传图给 LLM 走 OpenAI 兼容的 `content: [{type:"text"}, {type:"image_url", image_url:{url}}]`。本地图片要先 resolve 成 `https://...` 公网 URL 或 base64 data URL；缺图自动回落 16×16 灰色 PNG（避免 LLM 直接拒绝）。

#### `ASRClient`（`asr_client.py`）

`transcribe(audio_url, lang) -> ASRResponse`。豆包 bigasr 2.0 强制要求 `audio.url` 公网可达——**生产 server `.env` 必须配 `PUBLIC_AUDIO_BASE_URL=https://seecript.zlhu.asia`**，否则 ASR 失败。

VAD 门控放在调用方（`decompose_agent`）：先用 librosa 算 300-3400Hz 频带能量阈值 0.35 判 `has_voice`，false 直接跳过 ASR 节省配额。

#### `T2VClient`（`t2v_client.py`）

```python
async def submit(prompt, *, first_frame=None, last_frame=None, duration_seconds, size, watermark, generate_audio) -> T2VTask
async def query(task_id) -> T2VStatus
```

异步 submit + 轮询模式。`gap_agent.aigc` 分支提交后返回 `task_id`，前端用 `POST /api/gap/aigc-refresh` 轮询直到 `done`，本地落盘到 `var/aigc_videos/<id>.mp4`（解决 TOS 临时 URL 跨域问题）。

#### `SeedreamClient`（`seedream_client.py`）

多镜头 storyboard 模式：一次给 N 个 shot prompt，返回 N 张图。中间有 `_ImageRef` resolver 把临时 CDN 转持久 URL，配合 `POST /api/asset/save-from-url` 入资产库。

#### `TTSClient`（`tts/`）

豆包 TTS。`POST /api/voice/synthesize` 走单 scene 合成；`synthesize-all` 批量。生成结果挂 `/voiceovers/<plan_id>/<scene_id>.wav`。**严禁凑时长**——TTS wav 长度小于 scene duration 时不补无声段，由渲染层用 BGM 填充。

---

## 第二部分 · 工具协议

### 2.1 路由清单（按业务域，84 个端点）

| 域 | 路由前缀 | 关键端点 |
|---|---|---|
| **健康检查** | `/api/health` | GET：5 个 provider 状态 |
| **样例库** | `/api/library` `/api/sample/*` | GET 列表 / GET manifest / POST manifest/save / GET versions / POST versions/{slot}/activate / DELETE versions/{slot} / POST library/system/upload |
| **拆解** | `/api/decompose` | POST 提交 / POST upload 用户视频 / GET stream(SSE) |
| **素材** | `/api/material` | POST upload / GET preprocess / GET 列表 / POST clone-from-system |
| **资产库** | `/api/asset` | POST upload / POST save-from-url / GET library / PATCH / DELETE / POST touch |
| **缺口** | `/api/gap` | POST detect / POST fill / POST aigc-refresh / POST aigc-prompt / POST aigc-image-spec / POST copy-outline / POST aigc-seedream / POST aigc-tail-frame |
| **方案** | `/api/plan` | POST build / GET / GET 列表 / PATCH bgm / DELETE bgm / POST recompute-emotion / PATCH settings / POST regenerate-narrations / PATCH scene/{sid} / PATCH transition / PATCH shot-subject / PATCH shot-fields / POST swap-source / POST snapshot / GET snapshots / POST restore / DELETE snapshot |
| **包装** | `/api/packaging` | POST recommend / POST apply / POST items/draft / POST items/place / DELETE items/{plan}/{item} / POST recommend-for-scene |
| **渲染** | `/api/render` | POST submit / GET stream(SSE) |
| **NL 编辑** | `/api/edit` | POST apply（Render 态三轨）/ POST compose（Compose 态 step2/3）/ POST compose/dismiss |
| **对话** | `/api/conversation` | GET / POST append / DELETE / GET stream(SSE) |
| **澄清** | `/api/clarify` | GET round / POST finalize |
| **配音** | `/api/voice` | POST synthesize / POST synthesize-all / DELETE |
| **项目** | `/api/project` | POST 创建 / GET 列表 / GET 单个 / PATCH / DELETE |
| **步骤** | `/api/project/{pid}/step/{step}` | POST commit / GET 单步 / GET 列表 |
| **知识** | `/api/profile` | GET / PATCH settings / PATCH projects/{pid}/enabled / GET projects/{pid} |
| **catalog** | `/api/catalog/blocks` | GET 列表 / GET {name} |
| **ASR 直调** | `/api/asr/transcribe` | 调试用 |

完整 OpenAPI：`http://127.0.0.1:8090/docs`

### 2.2 Pydantic v2 核心契约（`server/app/schemas.py`）

**SampleManifest（拆解产物）**：
```python
class SampleManifest(BaseModel):
    sample_id: str
    video_type: Literal["marketing", "editing", "motion_graph"]
    duration_seconds: float
    has_voice: bool                                  # librosa VAD 判定
    shots: list[Shot]                                # PySceneDetect + 多模态 LLM 帧标签
    rhythm: RhythmCurve                              # 镜头切换频次 + BGM 能量 + emotion 子字段
    sections: list[Section]                          # 9 个 SectionKind 之一
    packaging: PackagingProfile                      # 字幕样式 / 标题条 / 转场统计
    understanding: VideoUnderstanding | None         # 整片 archetype + tone + structural_pattern
    analysis: SampleAnalysis | None                  # highlights[] + improvements[]
```

**Plan（重组产物）**：
```python
class Plan(BaseModel):
    plan_id: str                                     # 每次编辑生成新 id
    sample_id: str
    main_track: list[Scene]                          # source ∈ {sample, user_material, aigc_t2v, aigc_image, text_card}
    packaging_track: list[PackagingItem]             # subtitle / title_bar / sticker / transition / cover
    bgm: BGMConfig                                   # url + volume + offset + analysis
    settings: ComposeSettings                        # platform / aspect / migration_preference / subtitle_enabled / tts_voice / packaging_preset / frame_design_preset
    emotion_curve: EmotionCurve | None               # 60 点平滑曲线 + anchors + peaks + valleys + summary
    duration_seconds: float
    variant: Literal["A", "B"]
```

**Scene（主轨分镜）**：
```python
class Scene(BaseModel):
    scene_id: str
    section_id: str                                  # 归属段
    start: float
    duration: float
    source: Literal["sample", "user_material", "aigc_t2v", "aigc_image", "text_card"]
    source_ref: str                                  # 资源 id 或 URL
    narration: str                                   # 字幕 / 口播文本
    subject: str                                     # 主体锚点（≤40 字）
    tags: list[str]
    transition: SceneTransition | None               # style + duration
    animation_spec: AnimationSpec | None             # remotion / ffmpeg engine + 动效参数
    aigc_image_url: str | None                       # source=aigc_image 时
    voiceover_url: str | None                        # TTS 合成后回写
    text_card: TextCardSpec | None                   # 字卡 source 时
```

**EmotionCurve（stage-28）**：
```python
class EmotionCurve(BaseModel):
    points: list[EmotionPoint]                       # 60 点 (t, intensity)
    anchors: list[EmotionAnchor]                     # 段落 anchor (section_idx, intensity, reason)
    peaks: list[EmotionPeak]                         # ≤2 个峰
    valleys: list[EmotionPeak]                       # ≤2 个谷
    summary: str
    backend: Literal["llm", "rule_fallback"]
    signals_used: list[str]                          # ["role","bgm","cut","script","climax","intent",...]
```

完整契约见 `server/app/schemas.py`（约 2200 行）。前端镜像在 `web/src/types/schemas.ts`。

### 2.3 SSE 事件协议

四条 SSE 端点共用同一种事件协议：

| 端点 | 用途 |
|---|---|
| `GET /api/decompose/stream?job_id=` | 拆解进度 |
| `GET /api/render/stream?job_id=` | 渲染进度 |
| `GET /api/conversation/stream?project_id=` | 对话流式输出 |
| `GET /api/material/{id}/preprocess` (轮询) | 素材预处理状态（非真 SSE，前端 setInterval） |

**事件格式**：
```
event: progress
data: {"step": "scene_detect", "percent": 10, "payload": {"note": "PySceneDetect 切镜头"}}

event: done
data: {"job_id": "...", "payload": {...终态结果...}}

event: error
data: {"detail": "..."}
```

**step 列表**：
- decompose：`scene_detect → audio_analysis → voice_detect → asr_transcribe → frame_extract → vlm_tag → llm_section → rhythm → emotion → done`
- render：`prepare → ffmpeg_concat → seedance → remotion → overlay → finalize → done`

**实现**：`services/jobs/JobStore` 用 `asyncio.Queue` 推事件；`routers/*/stream` 用 `StreamingResponse(event_gen(), media_type="text/event-stream")`；前端 `web/src/api/sse.ts` 用 `EventSource` 订阅，断线自动重连。

### 2.4 LLM Tool Calling 协议（双入口分流）

**Render 态**（`POST /api/edit/apply`，`routers/edit.py`）按 `track ∈ {main, packaging, voice}` 分流：

| track | 工具集 |
|---|---|
| `main` | `edit_scene_duration` / `replace_scene_material` / `set_scene_transition`（3 个） |
| `packaging` | `update_packaging_text` / `update_bgm_volume`（2 个） |
| `voice` | `edit_scene_narration`（1 个） |

每个 track 一个独立 system prompt 写"本次只能修改 X 轨"。

**Compose 态**（`POST /api/edit/compose`，`services/agent/compose_edit_agent.py`）按 `step ∈ {step2, step3}` 分流：

| step | 工具数 | 关键差异 |
|---|---|---|
| `step2` | 19 | 内容轨全开 + 渲染/包装/全局全开；**禁 aigc_image / aigc_t2v** 改（要去 AIGC 面板） |
| `step3` | 11 | 禁内容轨；其余全开（含 aigc_image 的 regenerate_fill） |

工具清单（step2 子集，step3 是其子集 + aigc_image）：
```
update_section_narration / update_section_duration / delete_section / reorder_sections
update_shot_visual / update_shot_subject / update_shot_narration / update_shot_duration
update_text_card_spec / update_packaging_text / update_packaging_item_time
update_scene_transition / regenerate_narrations_all
update_bgm_offset / update_bgm_volume
update_compose_setting (target_platform | aspect_ratio | target_duration_seconds | migration_preference | subtitle_enabled | voiceover_enabled | tts_voice | frame_design_preset | packaging_preset)
regenerate_fill (action ∈ rerank | copy | aigc_image[step3 only])
regenerate_all_fills
```

**对话三态约束**（`_QA_RULES`）：
- A) 编辑：用户**明确**说改什么+改成什么 → tool_calls
- B) 讲解：基于上下文 1-3 句中文回答，不编造
- C) 追问：意图模糊 → 1 句反问
- 禁止把单一指令拆成多个 diff（脑补）；禁止讲解里夹 tool_calls

**段落识别**（强制约束）：用户说"第 1 段 / 开头段 / 高潮段 / 最后一段" → LLM 必须按当前 plan 实际 section 列表映射，禁止造 sec-id。

**结果落盘**：每个 mutator 返回 `ComposeEditDiff(op, target_id, before, after, summary)`；apply=False 仅算 diff（dry-run），apply=True 才 `plan_store.replace()` 写新 plan_id 入 PlanSnapshotStore（撤销栈用）。

### 2.5 静态资源挂载（`main.py`）

| URL 前缀 | 物理目录 | 用途 |
|---|---|---|
| `/samples/<id>/...` | `server/samples/<id>/` | 内置样例（sys-*）原视频 + cover.jpg + shot-NN.jpg 关键帧 + meta.json |
| `/uploads/decompose/<id>/...` | `server/var/uploads/decompose/<id>/` | 用户上传待拆解视频（user-*） |
| `/uploads/<project_id>/...` | `server/var/uploads/<project_id>/` | 项目素材（multipart 上传 + shots/{material_id} 缩略图） |
| `/outputs/<job_id>/...` | `server/var/outputs/<job_id>/` | 渲染产物（final.mp4 + cover.jpg + 中间片段） |
| `/assets/<project_id>/<kind>/...` | `server/var/assets/<project_id>/<kind>/` | 资产库（reference_image / reference_video / bgm / sticker） |
| `/voiceovers/<plan_id>/<scene_id>.wav` | `server/var/voiceovers/` | TTS 合成产物 |
| `/aigc-videos/<id>.mp4` | `server/var/aigc_videos/` | Seedance T2V 落地（解决 TOS 跨域） |
| `/aigc-images/<id>.{png,jpg,webp}` | `server/var/aigc_images/` | Seedream 落地 |

**坑**：CSS `background-image: url()` 不渲 mp4——卡片封面**必须**抽 jpg。stage-51 的 user-* 封面修复就是这个。

### 2.6 Job / Plan / Project 持久化

- `JobStore`（`services/jobs/`）：内存 dict + asyncio.Queue per job，**进程内 / 不跨重启**——重启服务前确认无飞行中 job
- `PlanStore`（`services/plans/`）：JSON 文件落盘 `var/projects/<pid>/plans/<plan_id>.json`，`replace()` 永远写新文件
- `PlanSnapshotStore`：每次 NL 编辑前快照旧 plan，最多保留 N 个，撤销栈用
- `ManifestStore`（`services/library/`）：每个 sample 最多 `MAX_VERSIONS=3` 个版本槽，激活态 + 时间戳；满了要前端选 replace_slot
- `ProjectStore`（`services/projects/`）：项目元数据 + step 进度（library / decompose / compose / render）+ 对话历史

---

## 第三部分 · 安全边界

### 3.1 API Key 管控

| 维度 | 措施 |
|---|---|
| 存储位置 | 所有 key 走 `server/.env`；`chmod 600`；`.env` 已加入 `.gitignore` |
| 前端持有 | **永不**——前端调 `/api/*`，后端单点持 key |
| 提交防护 | 每次 commit 前 `git diff` 自查 `ark-[a-f0-9]+` / `sk-[A-Za-z0-9]+` 等指纹 |
| Mock 模式 | 仅开发环境保留：缺 key 自动落 `MockLLMClient` 记 warn；**生产路径关闭此降级**（缺 key 直接 5xx） |
| Provider 切换 | 通过 `LLM_PROVIDER` / `T2V_PROVIDER` / `T2I_PROVIDER` / `ASR_PROVIDER` / `TTS_PROVIDER` 环境变量切，无需改代码 |

### 3.2 用户上传隔离

| 维度 | 措施 |
|---|---|
| 落地路径 | `server/var/uploads/<project_id>/`（项目素材）/ `server/var/uploads/decompose/<sample_id>/`（用户拆解视频）—— **session/project 级隔离** |
| MIME 白名单 | 视频：mp4 / quicktime / webm；图片：jpg / png / webp；音频：mp3 / wav |
| 大小上限 | 单文件 50MB（material）/ 200MB（decompose 整片，因为拆解通常吃整段） |
| 时长上限 | 用户拆解视频 ≤200s（3 分钟 + 20s 余量），ffprobe 真实读取，超时即删 + 413 |
| 可入库白名单 | `/asset/save-from-url` 仅接受 `reference_image` / `reference_video`，**禁 bgm**（防滥用抓取音乐） |
| 删除清盘 | 用户主动删除时同步删盘（材料）；版本满删旧槽时前端 confirm 弹窗 |
| 路径穿越 | sample_id 强制 `^(sys|user)-[a-z0-9]+$`，路径拼接前用 `Path.is_file` 校验 |

### 3.3 Prompt 注入防护

| 维度 | 措施 |
|---|---|
| 输入侧长度 | brief ≤500 字 / edit instruction ≤1000 字 / shot.script ≤80 字（注入 LLM 时） |
| 结构化打包 | 用户文本永远嵌入 `【字段名】内容` 包裹；从不直接拼到 system prompt |
| 输出侧 schema 校验 | 强制 JSON schema + Pydantic v2 严校验；畸形 422 |
| 多模态图片校验 | `_resolve_local_image` 检查存在性 + MIME；不存在回落 16×16 灰 PNG（**已知坑**：会引发 LLM 幻觉"灰色背景"，stage-50 在 frame_tag prompt 里加"禁止凭空写 '纯色背景 / 占位帧'"约束） |
| 工具调用参数校验 | `complete_with_tools` 收到的 `tool_calls[].arguments` 必过 mutator 的 Pydantic 严校验，未知 tool name / 字段缺失直接拒绝 |

### 3.4 内容审核

| 维度 | 措施 |
|---|---|
| 上游模型 | 调豆包 Seedance / Seedream 时透传 `trace_id`，厂商侧做内容审核（NSFW / 违规） |
| 服务侧拦 | brief / edit instruction 过明显违规关键词列表，命中拒绝 + 422 |
| 用户上传 | MIME 白名单挡可执行文件，但**不内容审核图像 / 视频本身**——这是工程模板侧的限制 |

### 3.5 模型降级策略

任何 provider 失败**绝不静默**：

| 场景 | 处理 |
|---|---|
| LLM JSON 失败 | 重试 1 次拼更严格 system；二次失败 → `LLMError` → 502 |
| LLM 5xx | `LLMError(upstream_status=5xx)` → 502 + 详情透传 |
| LLM 超时 | `LLM_TIMEOUT_SECONDS=60`（默认）→ `LLMError` → 502 |
| ASR 不可用 | SSE payload `note: "ASR 不可用，按画面+节奏分析"`；`has_voice=false` 后续走纯画面分析路径 |
| T2V 失败 | 缺口 fill 返回 `error: ...`，前端展示"AIGC 失败，请改 prompt 重试" |
| ffmpeg 不可用 | 渲染流水线**已停用 mock fallback**——直接 5xx；开发环境必装 ffmpeg |
| Remotion 不可用 | 包装轨**仅**包装轨用 Remotion；不可用时 ffmpeg drawtext 兜底字幕；AI 生图动效降级为 ffmpeg 静帧 image_to_video |

**已删除的兜底（不要加回来）**：`_stub_manifest` / `_stub_sections` / render placeholder mp4 / 缺 key 的 mock fallback（生产路径）。

### 3.6 Tool Calling 安全

| 维度 | 措施 |
|---|---|
| 工具白名单 | `_TOOLS_STEP2` / `_TOOLS_STEP3` / 三轨 tools 在代码里硬编码，LLM 返回未知 tool name 直接拒绝 |
| 参数校验 | 每个 mutator 入口 Pydantic v2 严校验；越界值（duration < 2 / volume > 1.5）钳制到合法区间 |
| 段落识别 | LLM 报的 section_id 必须在当前 plan 的实际段列表内，否则拒绝（防造 sec-5 但只有 4 段） |
| 不可变 Plan | 改 Plan 走 `plan_store.replace()` 而非原地变更；新 `plan_id` 入快照栈，便于撤销 |
| dry-run | apply=False 时只算 diff 不落盘，前端 confirm 后才 apply=True |
| 锁定轨道 | Render 页传 `lockedTracks=['main']`，前端禁用 + 后端 409 双保险（防绕前端直调 API） |

### 3.7 跨域 / 跨源隔离

| 维度 | 措施 |
|---|---|
| CORS | `CORSMiddleware` 允许 dev origin（5173）+ 生产域（seecript.zlhu.asia）；prod 禁 `allow_origins=["*"]` |
| COOP / COEP | 已加 `Cross-Origin-Opener-Policy: same-origin` + `Cross-Origin-Embedder-Policy: require-corp`（为浏览器端 ffmpeg.wasm/Remotion 预留） |
| Static files CORS | `/uploads`、`/assets`、`/aigc-*` 静态挂载允许跨源 GET（视频源给 `<video>` 标签） |
| TOS 跨域 | Seedance / Seedream 临时 CDN 不开 CORS → 全部下载落地到 `/aigc-*/` 后再返给前端 URL |

### 3.8 任务隔离 / 资源回收

| 维度 | 措施 |
|---|---|
| Job 目录 | 每个 render job 独立 `var/outputs/<job_id>/`；中间产物（concat / packaging.webm / overlay 临时）都在该目录 |
| 失败回收 | render 流水线任何 step 失败 → 不删 job 目录（便于排查），但 SSE 推 `error` + 前端拒绝预览 |
| Trace ID | 每条请求带 `X-Trace-Id`（middleware 生成 / 透传）；agent 内部 step 推 SSE 时也带 |
| SSE 鉴权 | `job_id` 不存在直接 404；不区分用户（项目内 trust）——production 多用户场景需要再加 project_id 鉴权 |
| AIGC 缓存命中 | `var/aigc_cache/img-<sha1>.png` 等按 URL hash 缓存，重复 fill 命中复用 |

### 3.9 可观测性

| 维度 | 措施 |
|---|---|
| 请求日志 | `[trace_id] METHOD PATH -> STATUS (Nms)` middleware 输出到 `var/logs/server.log` |
| Agent 日志 | `[trace_id] module ok | provider | elapsed_ms | tokens | size_metric` 每次 LLM 调用一行 |
| SSE 进度 | 每个 step 推 `progress` 事件，前端按 step 显示当前阶段，便于复盘哪一步降级 |
| 健康检查 | `GET /api/health` 返回 5 个 provider 状态（mock / live / error） + git commit |
| 失败堆栈 | 路由层 `try / except` 兜住 → 502 + `detail` 字段透传 root cause（不暴露 traceback 到前端，仅日志） |

### 3.10 部署边界

生产服务器 `root@47.239.58.145:5002`：

| 维度 | 措施 |
|---|---|
| systemd unit | `seecript-server.service`，user `seecript`，port `5002`，nginx 反代到 `https://seecript.zlhu.asia` |
| 证书 | Let's Encrypt（certbot --nginx），与主域 `zlhu.asia` 独立 |
| 部署方式 | repo 是 private → **不能 git pull**，必须本地 tar 打包 → scp → `chown seecript:seecript` → `systemctl restart` |
| tar 排除 | `.env` / `web/node_modules` / `server/venv` / `__pycache__` / `.git` / `var` / `server/var` / `server/samples/*/video.mp4` / `logs` / `.scratch` |
| tar 包含 | `web/dist`（前端构建产物） |
| 服务器 .env | 必须配 `SEEDREAM_PROVIDER=doubao_ark`、`PUBLIC_AUDIO_BASE_URL=https://seecript.zlhu.asia`（豆包 ASR 2.0 audio.url 公网必达） |

---

## 第四部分 · 项目历史蒸馏

> 从 100 个 commit（`280d41d` 初始 fork → `4b18206` 当前）里蒸馏出来的演进路径。看这一段就能理解"为什么是现在这个架构"——很多看起来奇怪的设计都是被某个具体问题逼出来的。
>
> 项目身份从一开始就有一次大转向：commit `280d41d` 是 fork 自 **KOCopilot**（创作者副驾形态），`c0a01fd` 起改名 Seecript 并把所有文案改为"视频拆解与重组助手"——这是为什么早期还有 `stage-0` 清理废弃代码的 chore commit。

### 4.1 七个阶段（按职能演进切分）

| 阶段 | commit 范围 | 主题 | 关键产出 |
|---|---|---|---|
| **Ⅰ. 骨架奠基** | `stage-0` → `stage-5` | 重写 schemas + 路由骨架 + 4 个 AI client 抽象 + 端到端 6 用例 | FastAPI + React 19 + Doubao Ark 客户端 + 拆解/缺口 agent 雏形 + Remotion 包装轨 + Seedance 首尾帧串接 |
| **Ⅱ. Compose 工作坊成形** | `stage-6` → `stage-14` | 三栏 Compose + 项目化隔离 + 四轨工作台 + NL 编辑三轨分流 + 拆解结果落资产库 | 系统/用户样例库拆分 + 项目 store + xfade 转场内化 + 包装推荐 5 维偏好 + 双版本槽 |
| **Ⅲ. 素材/文案/设计 agent 化** | `stage-19` → `stage-22` | AIGC 视频本地落盘 + 720p 锁死 + VideoPreprocessor OOP + HyperFrames catalog | AIGC 跨域终结（落 `/aigc-videos/`）+ frame.md 设计系统 + 比例平台解耦 |
| **Ⅳ. 分镜成为最小单元** | `stage-23` → `stage-25` | ShotPlan schema 引入 + 分镜级 NL 工具集 + 结构-only 迁移 | `Scene.parent_section_id` + `shot_matcher` 服务 + 分镜数硬约束 1-3 推荐/5 上限 |
| **Ⅴ. Subject 锚点 + 情绪曲线** | `stage-26` → `stage-28` | subject 锚点串到 Seedream + frame 烧入 ffmpeg 滤镜 + LLM 多信号情绪曲线 | match_quality 三档 + 单镜换源弹窗 + EmotionCurve 60 点 + 段独立工作台 |
| **Ⅵ. 段独立工作台 + 弹窗化 UX** | `stage-29` → `stage-39` | ⌘K 对话持久化 + clarify 五件套 + step2/3 段独立 keepalive + Seedream 5.0 | 双路 subject 识别 + 真并行补全 + 段块编辑弹窗 + section_id 主键 |
| **Ⅶ. 生产硬化 + UX 修** | `stage-40` → `stage-52` | 缺 key 一律硬失败 + Seedream 超时拉到 180s + 拆解抽 shot-NN.jpg + 段卡排版重做 | 多镜头 AIGC 独立运镜 + TTS 严禁凑时长 + 用户上传样例 cover.jpg + 修生产 ASR audio.url |

### 4.2 改写架构的几个关键决策（"为什么是现在这样"）

每条都有具体的 commit 来源——这些是架构上的转折点，不是普通迭代。

---

**决策 1 · AI 干预点从 6 个收口到 3 个**（`stage-5+`，`b96605a`）

最初设计是六层独立 AI 调用：VLM（画面理解）/ ASR / LLM 段落 / LLM 文案 / T2I / T2V。stage-5+ 上 Seedance 2.0 后发现 Doubao 多模态 LLM 可以**直接吃帧**做画面理解，VLM 客户端整个拆掉；T2V 也吃 image-to-video 模式，T2I 不再是独立环节而是 Seedream 多镜头 storyboard。

收口后剩下 3 类：**多模态 LLM**（覆盖 9 个 agent）/ **ASR**（豆包 bigasr） / **AIGC 视觉**（Seedance + Seedream）。

为什么这事很重要：6→3 让 client 抽象从 4 个降到 3 个，prompt 管理从 6 套降到 1 套 LLMClient + 1 套 ASRClient + 1 套 T2VClient。

---

**决策 2 · 生产路径关闭 Mock 兜底**（`stage-prod`，`7936f31`）

早期开发为了能脱离 API key 跑通流程，加了大量 mock fallback：`_stub_manifest` / `_stub_sections` / render placeholder mp4。结果**生产部署后发现**用户看到的是"假英文" / "灰色占位帧"——LLM 失败时静默吃掉而不暴露。

`stage-prod` 一刀切：缺 API key 直接 500，不落 mock。保留三个**规则降级**（`_fallback_outline` / `_fallback_adaptation` / `_role_mood_value`）——区别在于这些用确定性算法兜底，不返回假数据。

`_stub_manifest` / `_stub_sections` / render placeholder **已删，文档明令不要加回来**。

---

**决策 3 · 分镜（Shot）成为最小单元，而不是段（Section）**（`stage-23`/`24`，`6dbaeb9` → `9bc49f1`）

stage-22 之前段是 Plan 的最小单元；用户改"调整第三段"就是整段重写。stage-23 引入"分镜"概念：每段有 1-3 个分镜（硬约束 1-3，上限 5），每个分镜对应一个 Scene。这让 NL 编辑可以下钻到镜头级（"把第二段的第二镜换成 B-roll"），也让 `shot_matcher` 能给镜头级匹配评分。

代价：所有 schema 改一遍（`Scene.parent_section_id` 加字段）、`plan_agent` prompt 重写、`compose_edit_agent` tool 集翻倍。

---

**决策 4 · ⌘K NL 编辑双入口分流**（`stage-11` → `stage-24/PR-D` → `stage-29`）

- `stage-11`：渲染态 NL 编辑诞生，分三轨 main/packaging/voice，每轨独立 system prompt（防 LLM 跨轨乱改）
- `stage-24/PR-D`：Compose 态 ⌘K 浮层加入，step2/step3 分流（step2 全开但禁 aigc / step3 禁内容轨）
- `stage-29`：对话持久化（项目级 + 最近 200 条），跨会话延续

为什么不合并：渲染态用户面对的是"已经渲完的视频"，能改的只有时长/转场/字幕这类"重渲快"的；Compose 态还在编排阶段，content 也能改。两个 system prompt 不能合写，否则越界。

---

**决策 5 · Plan 不可变更新 + 撤销栈**（`stage-26`+）

第一版 NL 编辑直接原地改 Plan。结果用户改坏了想撤销没办法（要重跑 build）。改为 `plan_store.replace()` 永远写新 plan_id，旧 plan 保留在 `PlanSnapshotStore`。前端 `stores/edit.ts` 维护 undo/redo 栈。

代价：磁盘占用——但 Plan JSON 通常 < 50KB，可接受。

---

**决策 6 · AIGC 视频/图必须本地落盘**（`stage-19`，`c0bf88f`）

豆包 Seedance/Seedream 返回的 CDN URL 是临时签名，且 CDN **不开 CORS** —— `<video>` 标签直接拉 mp4 浏览器会拒绝。改成生成后立即下载落到 `var/aigc_videos/<id>.mp4` 和 `var/aigc_images/`，返给前端的 URL 都是 `/aigc-videos/<id>`。

附带好处：缓存命中可避免重复扣费。

---

**决策 7 · ASR 必须公网可达 audio.url**（`stage-50`，`2dd7ccf` 附带修复）

豆包 ASR 2.0 强制要求 `audio.url` 公网可达——开发环境本地起服务时用 `http://127.0.0.1` 当然不行。生产 `.env` 必须有 `PUBLIC_AUDIO_BASE_URL=https://seecript.zlhu.asia`，否则 ASR 路由 502。这是个非显然的部署约束，写进了 `AGENTS.md` 硬规矩。

---

**决策 8 · 包装轨用 Remotion + 透明 WebM**（`stage-3`，`0d06d7f`）

最初想全用 ffmpeg drawtext 渲字幕/标题条。问题：复杂动效（粒子/标题入场/转场遮罩）ffmpeg 写起来是地狱。改为：**包装轨独立**用 Remotion（独立 Node 项目 `remotion/`）渲透明 WebM，ffmpeg 在最后一步 overlay 到主轨上。

代价：多一个 Node 项目 + Remotion 不可用时降级 ffmpeg drawtext。收益：动效任意 React + CSS 写。

---

**决策 9 · LLM 情绪打分用"段 anchor + peaks/valleys + 规则插值"**（`stage-28`，`f1bf018`）

最直接的设计是让 LLM 直接输出 60 个时间点的 intensity。试过后发现：① token 浪费（输出 60 浮点） ② 抖动大（LLM 写浮点不稳） ③ 没法对齐 BGM climax 时刻。

改为：LLM 只输出每段一个 anchor（intensity + reason）+ ≤2 个 peak 时刻 + ≤2 个 valley 时刻。规则层做线性插值 + 凸包 bump + 滑动平均 → 60 点平滑曲线。LLM 出错时 fallback `_role_mood_value`。

### 4.3 仍在生效的早期约束（"虽然像 legacy 但有原因"）

| 约束 | 来源 commit | 为什么还在 |
|---|---|---|
| `MAX_VERSIONS=3` 版本槽 | `stage-14` `fc63de4` | 用户编辑实验需要 A/B 对比；多于 3 个 UX 难做选择 |
| 分镜数硬约束 1-3 推荐 / 5 上限 | `stage-23` `66e8fab` | LLM 给 6+ 镜头时拼 prompt 容易超 4k token + 用户看不过来 |
| Seedream timeout 180s + 3 次重试 | `stage-41` `0054255` | 高并发下豆包 50% 概率 ReadTimeout，1 次重试不够 |
| 用户拆解视频 ≤200s | `stage-1` 一直没改 | 实测拆解 3 分钟视频 LLM 调用 5+ 分钟，更长用户体验不可接受 |
| AIGC 缺口 5-8s 短片 | `stage-3` `0d06d7f` | Seedance 单次调用上限；超过要走"首尾帧串接"组合 |
| 包装轨只用 Remotion 不接管内容轨 | `stage-3` `0d06d7f` | 内容轨 ffmpeg 性能 > Remotion；只在透明动效层用 Remotion |

### 4.4 已删除的设计（"教训"）

| 删了什么 | 来源 commit | 教训 |
|---|---|---|
| `_stub_manifest` / `_stub_sections` | `stage-prod` `7936f31` | 生产 mock 兜底导致用户看到假数据；规则降级才允许 |
| Render placeholder mp4 | `stage-prod` `7936f31` | ffmpeg 不可用时写黑屏 mp4，掩盖真问题 |
| 一键 AI 生图批量入口 | `stage-26` `3cab185` | UX 上跟 fill-all 重复，移除 |
| step3 自动包装推荐 | `stage-26` `f101d87` | 用户期望进 step3 看到已编排好的轨，结果自动推荐覆盖了，改成手动按钮 |
| 包装轨「✨ 推荐生成 ▾」按钮 | `stage-45.1` `ae4ff0a` | UX 残余，删完整 |
| 旧智谱 T2V provider | `stage-5+` `572c36a` | Seedance 2.0 后没人用智谱 T2V，删配置 |

### 4.5 Stage 命名规则

- `stage-N` — 主迭代节点（如 `stage-23` 是分镜引入）
- `stage-N/PR-X` — 同一主题下的子 PR（如 `stage-24/PR-A` → `PR-D`）
- `fix(stage-N)` — 该 stage 的修补
- 跨 stage 的横切（如 `fix(clarify)` / `fix(library)`）— 不绑 stage 号，按模块名
- `PR-E` → `PR-N` 不带 stage（stage-25 时期的 PR 系列）

---

## 第五部分 · AI 节点全清单

> §1.3 列的是 11 个 **agent**（代码模块层面）；本节列的是 **AI 决策节点**（业务功能层面）。一个 agent 内部可能含多个独立 LLM 调用，每个对应一个"业务上有名字的 AI 能力"。比如 `decompose_agent` 内部跑 5 次 LLM、1 次 ASR、2 次 BGM 音频理解；`packaging_agent` 一次 LLM 输出 6 类子方案（转场/封面/字幕/标题条/贴纸/调色板）—— 这些都是用户感知层面的独立 AI 节点。
>
> 全项目共 **28 个 AI 决策节点**：22 个 LLM 节点 + 1 个 ASR + 2 个 AIGC 视觉 + 1 个 TTS + 2 个规则降级（rule_fallback 也算节点，因为承担同等责任）。

### 5.1 拆解链路节点（9 个）

| # | 节点 | 模型 / 方式 | 输入 | 输出 | 代码 |
|---|---|---|---|---|---|
| D1 | **镜头切分** | PySceneDetect | mp4 | DetectedShot[] | `services/video/scene_detect.py` |
| D2 | **音频能量曲线** | librosa RMS + onset + tempo | mp4 音轨 | rms[] + tempo + onset[] | `services/video/bgm_analysis.py` |
| D3 | **VAD 口播判定** | librosa 300-3400Hz 频带能量阈值 0.35 | mp4 音轨 | `has_voice: bool` | `services/video/voice_detect.py` |
| D4 | **ASR 口播转写** | 豆包 bigasr_auc_turbo（异步 submit + query） | audio.url（公网） | 段落 + 时间戳 | `services/asr_client.py` |
| D5 | **多模态帧打标** | Doubao Seed-2.0-lite multimodal | 每镜头关键帧 + `_FRAME_TAG_SYSTEM` | `tags[] + script + subject` 每镜头 | `agent/decompose_agent.py:255` |
| D6 | **镜头角色识别** | Doubao multimodal | 关键帧 + `_SHOT_ROLE_SYSTEM` | `role ∈ SectionRole` 每镜头 | `agent/decompose_agent.py:231` |
| D7 | **整片画像** | Doubao multimodal | 抽样关键帧 + `_UNDERSTAND_SYSTEM` | archetype + narrative_summary + structural_pattern + tone | `agent/decompose_agent.py:205` |
| D8 | **视觉脚本反推** | Doubao multimodal | 关键帧 + `_VISUAL_SCRIPT_SYSTEM` | 字幕原文摘录（纯画面有字幕但 ASR 没拾取时） | `agent/decompose_agent.py:851` |
| D9 | **样例亮点 / 改进分析** | Doubao LLM | shots + sections + understanding + `_VIDEO_ANALYSIS_SYSTEM` | `highlights[] + improvements[]`（aspect + text + shot_indices） | `agent/decompose_agent.py:970` |

### 5.2 音乐链路节点（2 个）

| # | 节点 | 模型 / 方式 | 输入 | 输出 | 代码 |
|---|---|---|---|---|---|
| M1 | **样例音轨节奏画像** | Doubao 多模态音频 | 样例 mp4 音轨 url | `mood_tags + energy_shape + climaxes[] + calm_segments[] + title_guess` | `services/video/bgm_analysis.py:430` |
| M2 | **Plan 绑定 BGM 分析** | Doubao 多模态音频 | 用户绑定的 BGM url + brief + video_goal | 同上 + 匹配建议 | `services/video/bgm_analysis.py:141` |

### 5.3 素材链路节点（1 个）

| # | 节点 | 模型 / 方式 | 输入 | 输出 | 代码 |
|---|---|---|---|---|---|
| U1 | **素材自动打标** | Doubao multimodal | 用户上传素材的中间帧 + `_MATERIAL_TAG_SYSTEM` | `tags[] + subjects[] + role + highlight_score + highlight_reason` | `routers/material.py:147` + `services/materials/preprocess.py:131`（视频按镜头切片各跑一次） |

### 5.4 意图澄清节点（1 个 + 1 历史降级）

| # | 节点 | 模型 / 方式 | 输入 | 输出 | 代码 |
|---|---|---|---|---|---|
| C1 | **clarify 多轮问答**（流式） | Doubao LLM `stream_complete` | brief + history | `ClarifyRound(question + options)` | `agent/clarify_agent.py:344` |
| C0 | **clarify finalize**（**已退役 LLM，改纯结构化**） | 规则收尾 | 问答 history | `clarify_outline` 五件套 | stage-30 之后 finalize 不再调 LLM |

`brief_subjects` 反推可拍物体（`fix(clarify)` `8c621e2`）—— LLM 不再字面抽 brief 词，而是反推用户实际能拍到的物体，注入 `clarify_outline.content`。

### 5.5 Plan 构建节点（3 个）

| # | 节点 | 模型 / 方式 | 输入 | 输出 | 代码 |
|---|---|---|---|---|---|
| P1 | **结构改编**（核心） | Doubao multimodal（带 ref_images） / 文本（无 ref） | 样例 sections + brief + video_goal + ref_images + `_ADAPT_SYSTEM` | `AdaptedSection[]`（含每段 theme/narration/subjects/shots[]） | `agent/plan_agent.py:413` |
| P2 | **主体锚点匹配**（**非 LLM**） | embedding 余弦 + 角色匹配 + duration 权重 | 样例 ShotPlan + 用户 materials | `ShotMatch[]`（match_quality 三档 + 候选） | `agent/shot_matcher.py` |
| P3 | **subject_anchors 反解注入**（stage-35） | 规则提取 | 样例 sections 的 subjects[] | 注入到 plan_agent prompt 作为硬约束 | `agent/plan_agent.py` |

### 5.6 缺口补全节点（5 个）

| # | 节点 | 模型 / 方式 | 输入 | 输出 | 代码 |
|---|---|---|---|---|---|
| G1 | **缺口检测**（**非 LLM**） | 槽位匹配纯算法 | Plan + materials + 9 个 SectionKind | `Gap[]`（缺哪段哪镜） | `agent/gap_agent.py` |
| G2 | **段文案改写**（gap action=copy） | Doubao LLM | 段角色 + brief + `_COPY_SYSTEM` | `narration + text_card_lines + subjects` | `agent/gap_agent.py:624` / `agent/copy_outline_agent.py:249` |
| G3 | **AIGC 视频 prompt 改写** | Doubao LLM | 段角色 + brief + ShotPlan + `_PROMPT_SYSTEM` | Seedance 可吃的 prompt（含运镜/主体/时长/风格 + negative） | `agent/aigc_prompt_agent.py:154` |
| G4 | **AIGC image-spec 改写** | Doubao LLM | 同上 + `_IMAGE_SPEC_SYSTEM` | Seedream 多镜头 storyboard prompt | `agent/aigc_prompt_agent.py:369` |
| G5 | **段落重排**（gap action=rerank，**非 LLM**） | 规则算法 | materials + match_quality | 重排后的 main_track | `agent/gap_agent.py` |

### 5.7 AIGC 视觉节点（4 个）

| # | 节点 | 模型 / 方式 | 输入 | 输出 | 代码 |
|---|---|---|---|---|---|
| V1 | **Seedance T2V**（短片 5-8s） | doubao-seedance-2-0-fast-260128 | prompt + size + duration | mp4 落地到 `var/aigc_videos/` | `services/t2v_client.py` + `agent/gap_agent.py:785` |
| V2 | **Seedance 首尾帧串接长视频** | T2V image-to-video | 上一段尾帧 + 下一段首帧 + prompt | 长拼接 mp4 | `services/render/seedance_chain.py` |
| V3 | **Seedream T2I**（多镜头 storyboard） | doubao-seedream | 多 shot prompt + ratio | N 张图落地到 `var/aigc_images/` | `services/seedream_client.py` + `agent/gap_agent.py:1408` |
| V4 | **AIGC tail-frame 补帧** | Doubao LLM 写 prompt + Seedream 出图 | 已有相邻 scene + 段角色 | 单图（连接帧） | `routers/gap.py POST /aigc-tail-frame` |

### 5.8 包装推荐节点（3 个 LLM × 6 类输出 = 18 个子决策）

包装链路是 AI 决策最密集的部分。3 次 LLM 调用，每次输出多个子方案：

| # | 节点 | 模型 / 方式 | 输入 | 输出（一次性给齐 6 类） | 代码 |
|---|---|---|---|---|---|
| K1 | **包装推荐 v1**（早期版本） | Doubao LLM | Plan + PackagingPreferences | 转场 + 封面 + 字幕 + 标题条（旧 schema） | `agent/packaging_agent.py:414` |
| K2 | **包装推荐 v2**（当前主用） | Doubao LLM | Plan + prefs + 5 维偏好 + frame_design + catalog_hint | `transitions[6种] + cover[标题+副标题+调色板+布局] + subtitle_style + title_bar + sticker` | `agent/packaging_agent.py:1026` |
| K3 | **场景级包装推荐**（stage-27） | Doubao LLM | 单个 Scene + 上下文 + prefs | 该 scene 的 sticker / title_bar 候选 | `agent/packaging_agent.py:1318` |

K2 输出展开就是 6 类**独立 AI 子决策**——LLM 一次性给齐避免多次往返：

| 子决策 | 候选数 | 落点字段 |
|---|---|---|
| 转场风格推荐 | 6 种（淡入/切换/粒子/光斑/位移/反相） | `PackagingItem.kind=transition` |
| 封面方案 | 1 套 | `PackagingItem.kind=cover`（标题/副标题/调色板/布局） |
| 字幕样式 | 1-3 候选 | `SubtitleStyleCandidate` |
| 标题条样式 | 1-3 候选 | `TitleBarCandidate`（位置/字号/调色板） |
| 贴纸推荐 | 0-N | `StickerCandidate` |
| 调色板 | 1 套 | 内嵌在 cover / title_bar 内 |

### 5.9 视频包装与渲染节点（3 个）

| # | 节点 | 模型 / 方式 | 输入 | 输出 | 代码 |
|---|---|---|---|---|---|
| R1 | **frame.md 烧入 ffmpeg 滤镜**（stage-27） | 规则模板 → ffmpeg 滤镜串 | frame_design_preset + Scene[] | ffmpeg filter_complex 命令 | `services/render/pipeline.py` |
| R2 | **AI 生图动效**（ken-burns / keyframe_morph） | 规则推导（基于 subject 位置） | aigc_image + subject 锚点 | Remotion AnimatedImage 参数 | `remotion/src/AnimatedImage.tsx` |
| R3 | **TTS 配音合成** | 豆包 TTS | narration + voice_id | wav 落地到 `/voiceovers/` | `services/tts/` + `POST /api/voice/synthesize` |

R3 关键纪律：**严禁凑时长**（stage-44/49 修复）——TTS wav 长度小于 scene duration 时不补无声段；同时段口播禁止复述凑时长。

### 5.10 自然语言编辑节点（4 个）

| # | 节点 | 模型 / 方式 | 输入 | 输出 | 代码 |
|---|---|---|---|---|---|
| E1 | **Render 态 NL 编辑** | Doubao LLM `complete_with_tools` | 用户指令 + 三轨 system prompt（main/packaging/voice 分流）+ track tools 子集 | `tool_calls[]` → Plan 局部 mutator | `routers/edit.py:342` |
| E2 | **Compose 态 ⌘K NL 编辑** | Doubao LLM `complete_with_tools` | 用户指令 + step2/step3 分流 system + 19/11 tools | `ComposeEditDiff[]` | `agent/compose_edit_agent.py:1918` |
| E3 | **对话三态判定**（编辑/讲解/追问） | LLM tool_choice 隐式分流 | 同 E2 + `_QA_RULES` | A/B/C 三态之一 | 同 E2 |
| E4 | **段落识别强约束** | LLM 内置 + Router 校验 | 用户说"第 X 段" + 当前 plan sections | section_id 必须在实际列表内，否则路由 422 | `services/agent/compose_edit_agent.py` |

`POST /api/edit/compose` 内部一次调用同时完成 E2 + E3 + E4——三个都是同一次 LLM 调用的不同 facets。`complete_json` 还会另调一次做 dry-run diff 预览（`agent/compose_edit_agent.py:1489`）。

### 5.11 情绪与节奏节点（3 个）

| # | 节点 | 模型 / 方式 | 输入 | 输出 | 代码 |
|---|---|---|---|---|---|
| F1 | **节奏曲线**（**非 LLM**） | 规则计算 | 镜头切换时间序列 + BGM 能量 | `RhythmCurve(cut_density + bgm_energy + tempo)` | `agent/decompose_agent.py` |
| F2 | **LLM 情绪打分**（stage-28） | Doubao LLM | sections + shots + bgm_analysis + understanding + intent + `_EMOTION_SYSTEM` | `anchors[] + peaks[] + valleys[] + summary` | `agent/emotion_agent.py:576` |
| F3 | **情绪曲线规则降级** | `_role_mood_value + _smooth` | sections | 60 点曲线（`backend="rule_fallback"`） | `agent/emotion_agent.py` |

F2 输出后规则层做线性插值 + 凸包 bump + 滑动平均 → 60 个采样点。

### 5.12 知识沉淀节点（2 个）

| # | 节点 | 模型 / 方式 | 输入 | 输出 | 代码 |
|---|---|---|---|---|---|
| KB1 | **项目知识库蒸馏** | Doubao LLM `complete_json` | 该项目全部 trace（最近 N 条） | `ProjectKB`（4 类 scope：structure/source/narration/pacing） | `services/profile/distill.py:141` |
| KB2 | **全段口播重写** | Doubao LLM | 全 plan scenes + 新主题 + 新语气 | `{scene_id → new_narration}` | `agent/narration_agent.py:115` |

KB1 是 Hermes 风格规则提炼，把多轮编辑产生的用户偏好沉淀成下次复用的 PromptKB（用户级 + 项目级）。

### 5.13 节点统计与分布

| 链路 | 节点数 | LLM | ASR/TTS | AIGC | 算法 |
|---|---:|---:|---:|---:|---:|
| 拆解 | 9 | 5 | 1 | 0 | 3 |
| 音乐 | 2 | 2 | 0 | 0 | 0 |
| 素材 | 1 | 1 | 0 | 0 | 0 |
| 澄清 | 1 | 1 | 0 | 0 | 0 |
| Plan | 3 | 1 | 0 | 0 | 2 |
| 缺口 | 5 | 3 | 0 | 0 | 2 |
| AIGC 视觉 | 4 | 0 | 0 | 4 | 0 |
| 包装 | 3 | 3 | 0 | 0 | 0 |
| 渲染包装 | 3 | 0 | 1 | 0 | 2 |
| NL 编辑 | 4 | 4 | 0 | 0 | 0 |
| 情绪节奏 | 3 | 1 | 0 | 0 | 2 |
| 知识沉淀 | 2 | 2 | 0 | 0 | 0 |
| **合计** | **40** | **23** | **2** | **4** | **11** |

> 上表 40 含同链路独立节点（如缺口 5 个 / 拆解 9 个）。其中 §5.8 K2 一次 LLM 调用展开是 6 类子决策——若按"用户感知的独立 AI 能力"计数则总数约 45+。

### 5.14 节点失败级联表

每个节点失败影响哪些下游：

| 节点失败 | 直接影响 | 兜底 |
|---|---|---|
| D4 ASR | 没字幕；`has_voice=false` 走纯画面分析 | SSE `note: "ASR 不可用"`，pipeline 继续 |
| D5 帧打标 | 镜头无 tags/subject | 单镜头 placeholder（stage-50 防"灰色背景"幻觉） |
| D7 整片画像 | sections 缺 understanding 信号 | sections 仍能产出，缺 archetype |
| D9 样例分析 | 拆解卡片无亮点/改进 chip | 卡片仍渲染，缺 chip |
| M1 / M2 音轨分析 | BGM 卡片无 mood_tags / climaxes | 节奏曲线退化为纯能量 |
| P1 plan_agent | 整个 Plan 失败 | `_fallback_adaptation` 镜像复制样例（规则降级） |
| G2 copy | 段文案空 | `_fallback_outline` 角色模板（规则降级） |
| G3/G4 prompt 改写 | AIGC 用 brief 直接当 prompt | 用户体验差但能跑 |
| V1 T2V | 缺口 fill 返回 `error` | 前端展示"AIGC 失败，请改 prompt" |
| V3 Seedream | 同上 | 同上 |
| K2 包装推荐 | 无转场/封面候选 | `_rule_based_v2_candidates` 规则版兜底 |
| R3 TTS | scene 无 voiceover | scene 仍渲染，缺配音；BGM 填充 |
| E1/E2 NL 编辑 | 用户指令无效 | 502 + "请重试" |
| F2 情绪打分 | EmotionCurve.backend="rule_fallback" | F3 规则版 |

**节点失败永远不静默降级到 mock**——要么硬失败 5xx，要么走显式规则降级（rule_fallback / `_fallback_*`）。

---

## 附录 A · 关键代码索引

| 关注点 | 文件 |
|---|---|
| FastAPI 入口 + middleware + 静态挂载 | `server/app/main.py` |
| 全模块 Pydantic 契约 | `server/app/schemas.py` |
| LLM 客户端抽象 | `server/app/services/llm_client.py` |
| ASR 抽象 | `server/app/services/asr_client.py` |
| T2V 抽象 | `server/app/services/t2v_client.py` |
| 拆解流水线 | `server/app/services/agent/decompose_agent.py` |
| 方案构建 | `server/app/services/agent/plan_agent.py` |
| 缺口检测 / 补全 | `server/app/services/agent/gap_agent.py` |
| 包装推荐 | `server/app/services/agent/packaging_agent.py` |
| 情绪打分 | `server/app/services/agent/emotion_agent.py` |
| Compose ⌘K NL 编辑 | `server/app/services/agent/compose_edit_agent.py` |
| 渲染流水线 | `server/app/services/render/pipeline.py` |
| Remotion 渲染器调用 | `server/app/services/render/remotion_renderer.py` |
| 长视频首尾帧串接 | `server/app/services/render/seedance_chain.py` |
| 三轨 NL 编辑 | `server/app/routers/edit.py` |
| Plan 局部更新 | `server/app/routers/plan.py` |
| Job 调度 + SSE | `server/app/services/jobs/` |
| 前端类型镜像 | `web/src/types/schemas.ts` |
| Remotion compositions | `remotion/src/{Cover,Subtitles,TitleBar,StickerOverlay,Transition,AnimatedImage,PackagingTrack}.tsx` |

---

## 附录 B · 已知限制与未来工作

| 限制 | 现状 | 改进路径 |
|---|---|---|
| 单段情绪强化无对应工具 | 只有全局 `migration_preference=amp_emotion` | 新增 `amp_section_emotion(section_id, intensity_delta)` 复合 tool（重写口播 + 抬该段 BGM 音量 + 切转场） |
| 字幕"减少"无细粒度 | 只有 `subtitle_enabled` 全开关 | 给 `update_packaging_text` 加 `action: "delete"` |
| `migration_preference` 改了不立即生效 | 是悬空 flag，要 regenerate_fill 才落地 | mutator 改完自动级联触发 `regenerate_all_fills(action="rerank")` |
| AIGC 缓存命中率低 | 按 URL hash 缓存，prompt 改一字就 miss | 改按 `(prompt, section_role, video_type)` 三元组哈希 |
| 多用户隔离 | SSE / job 不区分用户 | 加 project_id 鉴权 + JWT |
| 长视频拼接 token 飙升 | 50+ shots 时 `_segment_with_roles` 拼 prompt 超 4k token | 滑窗 + 局部段落分析合并 |

---

> 本文档随代码同步更新。最后修订：post stage-52（commit `b9d8f0e`）。
