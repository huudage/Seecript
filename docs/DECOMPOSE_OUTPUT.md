# 样例拆解能力（Decompose Output）

> 视频拆解 pipeline 的完整产出说明。配套源码：`server/app/services/agent/decompose_agent.py`、`server/app/services/agent/emotion_agent.py`、schemas 见 `server/app/schemas.py`（`SampleManifest` / `Section` / `Shot` / `RhythmCurve` / `VideoUnderstanding` / `SampleAnalysis` / `BGMAnalysis` / `PackagingProfile` / `EmotionCurve`）。

我们把视频的结构分析做成了 **6 种结构模式 × 17 种段落角色** 的两层体系，再叠加镜头粒度、节奏曲线、整片画像等 8 个独立维度，最终产出 **10 类拆解产物**，供 Plan 阶段批量消费。

---

## 1. 顶层定性：6 种结构模式

结构模式由 LLM 在「先理解再切段」阶段对整片打的一个标签，决定了下游使用哪套段落角色集合：

| 模式 | 含义 | 角色集合 |
|---|---|---|
| **dramatic** | 戏剧四段式：起→承→转→合 | opening / development / climax / closing |
| **stepwise** | 线性步骤式：教程 / 操作流程 | intro / step_N / recap |
| **listicle** | 并列盘点式：榜单 / N 个理由 | hook / item_N / closer |
| **atmospheric** | 氛围推进式：Vlog / 纪录片 | establish / flow / peak / resolve |
| **info_dense** | 信息密集快切式：信息可视化 / 新闻摘要 | title_card / info_block / payoff |
| **vlog** | 日常无高潮型：日常碎片 | intro_scene / daily_N / wrap_up |

> `vlog` 是为没有强情绪峰值的视频准备的，避免 LLM 硬塞 climax。

---

## 2. 底层标注：17 种段落角色

每段叙事都会被打上一个 `role` 标签。同一个 role 在新 plan 里走相同的节奏锚定 / 字幕时长 / 转场密度——这是结构迁移的最小语义单元。

### dramatic 戏剧弧（4 角色）
| role | 含义 |
|---|---|
| `opening` | 开场段：建立情境 / 抛钩子 |
| `development` | 发展段：铺陈信息、推进叙事 |
| `climax` | 高潮段：情绪 / 信息密度峰值 |
| `closing` | 收尾段：余韵 / 行动引导 |

### stepwise 步骤式（2 静态 + 1 动态家族）
| role | 含义 |
|---|---|
| `intro` | 步骤介绍前的引入（是什么 / 为什么做） |
| `step_N` | 第 N 步（动态序号，N=1..8） |
| `recap` | 总结回顾 |

### listicle 盘点式（2 静态 + 1 动态家族）
| role | 含义 |
|---|---|
| `hook` | 开头钩子（"今天讲 5 个 ___"） |
| `item_N` | 第 N 个条目（动态序号） |
| `closer` | 结尾收束（"以上就是…"） |

### atmospheric 氛围推进（4 角色）
| role | 含义 |
|---|---|
| `establish` | 立题氛围铺陈 |
| `flow` | 中段流淌（多个空镜 / 长镜衔接） |
| `peak` | 情绪高点（不一定是叙事高潮，可能是画面的最强一镜） |
| `resolve` | 情绪释放收尾 |

### info_dense 信息密集（3 角色）
| role | 含义 |
|---|---|
| `title_card` | 标题卡（大字开头） |
| `info_block` | 信息块（一段一组数据 / 要点） |
| `payoff` | 落版收尾（结论 / CTA） |

### vlog 日常无高潮（2 静态 + 1 动态家族）
| role | 含义 |
|---|---|
| `intro_scene` | 开场场景（如"早上起床"） |
| `daily_N` | 第 N 个日常片段（动态序号） |
| `wrap_up` | 一天 / 一段时间的收尾 |

> **总计 17 个静态角色名**（opening / development / climax / closing / intro / recap / hook / closer / establish / flow / peak / resolve / title_card / info_block / payoff / intro_scene / wrap_up）+ **3 个动态序号家族**（step_N / item_N / daily_N）。
>
> 这套体系由 `STRUCTURAL_PATTERNS` 字典约束（schemas.py:95），LLM 输出时按所选 `structural_pattern` 走对应角色集合，越界由路由层校验拒绝。

---

## 3. 拆解管线的 10 类产物

每个样例跑一次完整 pipeline 后，落到 `SampleManifest` 给前端 / Plan 阶段消费。

### ① 元信息（轻量层）
`sample_id` / `title` / `video_type`（marketing / editing / motion_graph）/ `duration_seconds` / `video_url` / `has_voice`（librosa VAD 探测，纯 BGM 视频跳过 ASR 与逐句字幕）

### ② 镜头切片 `shots: list[Shot]`
PySceneDetect 切镜 + 多模态 LLM 增强。每镜含：

- `index` / `start` / `end` / `duration` 时间窗
- `thumbnail_url` 抽帧缩略
- `subject` 画面主体（具象名词，如「青铜器残片特写」「主播正脸」「展厅长廊」；禁比喻 / 上位词 / 营销修饰，下游 AIGC prompt 会原样使用）
- `visual_summary` 画面描述（≤60 字）
- `tags` VLM 帧打标（封面风格 / 转场 / 字幕样式）
- `script` 本镜口播 / 字幕脚本（有 voice 时清洗自 ASR；无 voice 时 LLM 看画面写代字幕）
- `transcript` 原始 ASR 片段
- `merged_from` 语义合并保留（N 镜合 1 时记原 indices，len>1 表示「N 镜合 1」）
- `targets` 目标分布（仅作 plan_agent 节奏参考——具体视觉物体不会被原样迁移到目标主题）

### ③ 整片语义画像 `understanding: VideoUnderstanding`
拆解 pipeline 的关键转折——**先理解再切段**：LLM 看完整片输出整片画像，再用这份画像驱动切段。这样「艺术展宣传片」不会被强切成 hook/body/cta。

- `archetype` 视频原型（『艺术展宣传』『带货种草』『城市 Vlog』）
- `narrative_summary` 一段话讲清整支视频在说什么（≤200 字）
- `structural_pattern` 选定的 6 种结构模式之一
- `tempo` 整体节奏（slow / medium / fast / peak / deceleration，可选）
- `estimated_segments` LLM 估计切几段（2-8，listicle 上限到 8）
- `tone` 基调描述（『冷静克制』『高燃热血』『诙谐自嘲』）

### ④ 段落结构 `sections: list[Section]`
LLM 在 understanding 之后切的段，每段含：

- `role` 17 种角色之一（抽象骨架）
- `theme` 中文小标签（≤10 字，如『展品揭幕』『痛点钩子』；反映「这一段真实在讲什么」，比 role 信息量大）
- `start` / `end` / `summary` / `shot_indices` 段内覆盖的镜头

### ⑤ 节奏曲线 `rhythm: RhythmCurve`
多条曲线复合：

- `times[]` 采样时间点
- `bgm_energy[]` librosa RMS 能量曲线（归一 0..1）
- `mood_curve[]` 规则版情绪走势（按段落 role 平滑，作为 fallback）
- `emotion: EmotionCurve` LLM 多信号情绪曲线（见 ⑥）
- `bgm_fit_score` BGM 与情绪曲线的相关度 0..1（接近 1 说明 BGM 节奏与视频结构同步）
- `bgm_fit_note` 一句话说明 BGM 是否服务于视频结构（命中 / 错位 / 平稳 / 过度起伏）

> `cut_density` / `tempo_bpm` 是 R1 改版前的字段，已弃用，新数据写空，前端不再读。

### ⑥ LLM 多信号情绪曲线 `EmotionCurve`（stage-28）
由 `emotion_agent.score_emotion` 综合 8 类信号打分：

- 段落 `role` / 段时长占比 / 段内镜头数与平均镜头时长（节奏密度的 proxy）
- 抽样镜头（每段首 / 中 / 末 2-3 镜）的 `subject` / `tags` / 口播原文（≤80 字）
- BGM 曲风（`title_guess` + `mood_tags`）与高潮节点（`climaxes[]`）
- librosa 能量摘要（mean / max / std / peak_t）
- 整片 `tone`
- `SampleAnalysis.highlights` 全片高光
- 用户意图 `PlanIntent`（仅 Plan 阶段；含 `brief` / `video_goal` / `migration_preference`，`amp_emotion` 时 prompt 显式要求 anchor 平均抬高 15-25% / peaks 抬高 20-30%）

LLM 只输出：
- **每段一条 `EmotionAnchor`**（`section_idx` + `intensity` 0..1 + `reason` ≤80 字）
- **≤2 个 `peaks`**（时刻点 + intensity + reason）
- **≤2 个 `valleys`**（时刻点 + intensity + reason）
- 一段话 `summary`

规则层做：
1. 每段中点取 anchor.intensity
2. 段间线性插值
3. peaks / valleys 在 ±2.0s 窗口做凸包凸起 / 凹陷
4. 60 个等距时间点采样
5. 滑动平均（window = max(3, n_points/12)）平滑掉拐点

最终输出 `EmotionCurve.points`（60 点）+ `anchors` + `peaks` + `valleys` + `summary` + `signals_used`（实际启用的信号列表）+ `backend`（`"llm"` 或 `"rule_fallback"`）+ `computed_at`。

LLM 不可达 / JSON 不合法 / 超时 → 回落 `_rule_fallback_curve`（仅看段落 role + 平滑），`backend="rule_fallback"`，`signals_used=["role"]`。

### ⑦ 包装风格画像 `packaging: PackagingProfile`
- `subtitle_style` 主导字幕样式名（『大字加描边』）
- `has_title_bar` 是否使用标题条（bool）
- `transition_types[]` 转场类型分布
- `cover_style` 封面风格
- `sticker_density` 贴纸 / icon 出现密度（0..1）

Plan 阶段 `packaging_agent` 据此推荐字幕 / 标题条 / 贴纸 / 转场 / 封面 5 维候选。

### ⑧ ASR 逐句时间戳 `utterances: list[Utterance]`
每句一条 `{text, start, end}`，时间单位均为秒。

- 模块 5 字幕烧录直接读这个列表
- 模块 2 decompose 用它做「按 shot 时间窗映射 transcript」，替代旧版按字符比例切分（会把英文单词从中间截断）
- 纯 BGM 视频为空列表

### ⑨ 全片复盘 `analysis: SampleAnalysis`（stage-23）
LLM 在 segment 之后跑一次综合评估：

- `highlights[]` ≤6 条高光
  - `aspect` ∈ `hook` / `narrative` / `visual` / `audio` / `rhythm` / `copy` / `cta`
  - `text` ≤40 字描述
  - `shot_indices[]` 关联镜头（前端高亮对应行）
- `improvements[]` ≤6 条改进建议
  - `aspect` 含 `structure`
  - `text` ≤40 字描述这个不足
  - `suggestion` 具体怎么改（≤60 字）
  - `shot_indices[]` 关联镜头
- `overall_score` 0-100 主观打分
- `one_line_verdict` 一句话总评（≤30 字，可作前端大字标题）

`plan_agent` / `copy_outline_agent` 把它当作迁移引导：**保留亮点，规避改进项**。

### ⑩ 音频多模态画像 `audio_understanding: BGMAnalysis`
decompose 跑完后异步算：抽样例视频音轨到 `samples/{sid}/audio.mp3`，送 doubao-seed multimodal `input_audio`，输出：

- `title_guess` 曲风猜测 + `mood_tags` 情绪标签
- `energy_shape` 能量形态（`build_up` / `wave` / `flat` / etc）+ `energy_shape_reason`
- `climaxes[]` 高潮节点（`at_seconds` + `kind` ∈ `climax` / `drop` / `release` / `build_start` / `break` + `label` + `fit_with_video`）
- `calm_segments[]` 平稳段（含 `start` / `end` / `note`）
- `theme_fit_score` 与视频题材的契合度
- `theme_fit_reason` 一句话说明
- `overall_advice` 一句话总建议
- `backend` 标识来源（`"doubao_seed"` / `"mock"` / 其他）

失败 / 未配 ARK / 老缓存 → None，前端兜底显示 librosa BPM + 单点 peak。

### 额外锚点：`climax_position`
单一秒数，前端节奏图叠 ReferenceLine。

- 优先取 `role=climax` 段中点
- 无 climax 段时回落 BGM 能量峰值

---

## 4. 产物链路与下游消费

```
镜头(shots)
  ↓
整片画像(understanding)        ← 先理解再切段，决定 structural_pattern
  ↓
段落(sections)                 ← 17 种 role + 中文 theme
  ├── 节奏与情绪(rhythm + emotion)
  ├── 包装画像(packaging)        → packaging_agent 5 维候选
  ├── 字幕时间戳(utterances)     → 字幕烧录 + 编辑器精对齐
  ├── 全片复盘(analysis)         → plan_agent 保亮点 / 规避弱点
  └── 音频画像(audio_understanding) → bgm_agent 选曲 + 高潮对齐
```

10 类产物在 Plan 阶段被以下 agent 各自消费：

- **`plan_agent`** —— 读 sections / understanding / analysis / emotion，生成 `AdaptedSection[]` 与新主题的段落骨架
- **`packaging_agent`** —— 读 packaging，推荐字幕 / 标题条 / 贴纸 / 转场 / 封面 5 维候选
- **`bgm_agent`** —— 读 audio_understanding 的 climaxes，与 emotion peaks 对齐做 BGM 高潮落点
- **`copy_outline_agent`** —— 读 highlights / improvements，写新主题的口播大纲时保留亮点结构、规避改进项

这就是「**爆款结构迁移引擎**」从拆解到重组的语义桥梁：**样例的结构骨架被抽象成 17 个 role + 多维画像，新主题在 Plan 阶段按这套骨架填肉，结构守恒、内容焕新**。

---

## 5. 失败兜底与兼容

| 场景 | 处理 |
|---|---|
| LLM 视频画像失败 | 拆解中断，向用户报错（拆解的关键转折，无法降级） |
| LLM 段落切分失败 | 按 video_type 走老的硬模板兜底（marketing=hook/body/cta 等） |
| LLM 情绪曲线失败 / 超时 | 回落 `_rule_fallback_curve`（仅 role + 平滑），`backend="rule_fallback"` |
| LLM 全片复盘失败 | `analysis=None`，plan_agent 不做高光保留 / 改进规避 |
| 多模态音频画像失败 / 未配 ARK | `audio_understanding=None`，前端兜底显示 librosa BPM + 单点 peak |
| 纯 BGM 视频（has_voice=False） | 跳过 ASR，`utterances=[]`，shot.script 由 LLM 看画面写代字幕 |
| 老缓存 manifest 字段缺失 | Pydantic before-validator 做 legacy 字段映射（`kind→role` / `suggested_segments→estimated_segments`） |
