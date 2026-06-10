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

### 1.3 11 个 Agent 清单（`server/app/services/agent/`）

| # | Agent | 触发时机 | LLM 调用 | 输出 |
|---|---|---|---|---|
| 1 | `decompose_agent` | `POST /api/decompose` 后台任务 | ① 多模态打标（每镜头缩略图） ② `complete_json` 给段落结构（按 video_type 三选一 prompt） ③ 节奏曲线规则计算 | `SampleManifest`（shots / sections / rhythm / packaging / emotion） |
| 2 | `plan_agent` | `POST /api/plan/build` | `complete_json` 给 `AdaptedSection[]`（把样例 sections 重写到用户主题） | `Plan`（main_track / packaging_track / bgm / settings / emotion_curve） |
| 3 | `gap_agent` | `POST /api/gap/detect` + `POST /api/gap/fill` | ① 槽位匹配纯算法 ② action=copy 用 `complete_json` 给文案 ③ action=aigc 走 T2V/T2I | `Gap[]` / `FillResult` |
| 4 | `packaging_agent` | `POST /api/packaging/recommend` | `complete_json` 一次性给 6 种转场 + 封面（标题/副标题/调色板/布局） | `PackagingRecommendationV2` |
| 5 | `compose_edit_agent` | `POST /api/edit/compose`（⌘K 浮层） | `complete_with_tools`（step2/step3 分流，20+ tools） | `ComposeEditResponse`（diff[] + 新 plan） |
| 6 | `emotion_agent`（stage-28） | 拆解阶段 + Plan 阶段 + `POST /plan/{id}/recompute-emotion` | `complete_json` 给 `anchors[] + peaks[] + valleys[]` | `EmotionCurve`（60 点平滑曲线） |
| 7 | `clarify_agent` | `GET /api/clarify/round` | `complete_json` 引导式问答（≤3 轮） | `ClarifyRound`（question + options） |
| 8 | `aigc_prompt_agent` | `POST /api/gap/aigc-prompt` 等 4 个 | `complete_json` 把 brief + 段角色 → Seedance 视频 prompt | `AigcPromptResponse` |
| 9 | `copy_outline_agent` | `POST /api/gap/copy-outline` | `complete_json` 给段文案改写大纲 | `CopyOutlineResponse` |
| 10 | `narration_agent` | `POST /api/plan/{id}/regenerate-narrations` | `complete_json` 全段口播重写 | `RegenerateNarrationsResponse` |
| 11 | `shot_matcher`（非 LLM） | 内部模块 | 镜头主体匹配 + 高光评分 | matching score |

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
