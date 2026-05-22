# Seecript · AI 干预点工程设计文档

> **文档定位**：本文是写给工程师 / 产品技术负责人看的，覆盖系统中**全部 7 个 AI 干预点**的：
> ① 触发时机 ② 调用逻辑 ③ Prompt 关键设计点 ④ 三层兜底策略 ⑤ 失败路径
>
> **与 PRD 的区别**：PRD（`docs/PRD.md`）讲产品形态与用户体验；本文讲实现细节与可控性证据。
>
> 版本对应：基于 v0.8 落地代码。

---

## 0 · 全局架构：三层兜底范式

Seecript **每一个** LLM 调用都遵循同一个三层兜底范式：

```
Layer 1 · Prompt（软约束）         ┐
   ↓ LLM 通常会遵守，但不保证       │
Layer 2 · Router（硬约束）         │ → 三层叠加 = 确定性收敛
   ↓ 后端代码无条件拦截            │
Layer 3 · Schema（终判约束）       ┘
   ↓ Pydantic v2 严校验，畸形即 422
```

| 层 | 实现 | 何时生效 | 失败处理 |
|---|---|---|---|
| **Layer 1 · Prompt** | `services/prompts/*.py` 的 system message | 调用前注入 | LLM 偶尔漂移 → Layer 2/3 兜底 |
| **Layer 2 · Router** | `routers/*.py` 路由函数内 | 业务逻辑层 | 直接 `HTTPException` 返回 4xx/5xx，前端 toast |
| **Layer 3 · Schema** | `schemas.py` 的 Pydantic 模型 + `complete_json` 内的 JSON 解析 | LLM 响应到达后 | 解析失败 / 字段缺失 → 502 |

**通用兜底（所有 LLM 调用共享）**：

1. **JSON 解析自愈**（`llm_client.py::_extract_json`）：自动剥离 ` ```json ` 代码栅栏 + 把首个 `{` 到最后一个 `}` 之间切片，应对 LLM 在 JSON 前后加废话的常见毛病
2. **JSON 失败重试一次**（`complete_json`）：第一次解析失败 → 拼接更严格 system prompt（"严格要求：必须返回合法 JSON。不要使用 markdown 代码块"）再试一次
3. **Provider 自动降级**（`get_llm_client`）：`LLM_PROVIDER=deepseek` 但 `DEEPSEEK_API_KEY=空` → 自动落 `MockLLMClient`，记 warn 不抛异常，保证开发环境永远能跑
4. **HTTP 状态码透传**：DeepSeek 5xx → `LLMError(upstream_status=5xx)` → 路由层 502；DeepSeek 4xx（auth/quota）→ 502 + 详情；超时 → `LLM_TIMEOUT` → 502
5. **结构化日志**：每次调用都打 `[trace_id] module ok | provider | elapsed_ms | tokens | size_metric`，便于线上排查

---

## 1 · 人设生成（Module 2）

| 字段 | 值 |
|---|---|
| **触发时机** | `feature-2.html` 用户填三段背景后点「生成 3 个人设方案」 |
| **后端入口** | `POST /api/persona/generate` → `routers/persona.py::generate_persona` |
| **Prompt 文件** | `services/prompts/persona.py::PERSONA_SYSTEM_PROMPT` |
| **请求 schema** | `PersonaRequest{background, interests, resources}`，三段各 1-500 字 |
| **响应 schema** | `PersonaResponse{personas[1..5], model_used, elapsed_ms}` |

### 1.1 调用逻辑

```python
user_msg = (
    "【职业背景】" + req.background +
    "【兴趣 / 可拍内容】" + req.interests +
    "【可用资源】" + req.resources
)
data = await client.complete_json(PERSONA_SYSTEM_PROMPT, user_msg)
plans = [PersonaPlan(**p) for p in data["personas"]]
```

简单一次性调用，无多轮、无中间 router 干预。

### 1.2 Prompt 关键设计点

| 设计目标 | 具体约束 |
|---|---|
| **强制结构化** | 输出契约写死 7 个字段，禁用 markdown 代码块、禁前后加废话 |
| **保证差异化** | "三个方案必须有明显差异化，避免高度同质化"——避免 LLM 输出 3 个同质方案 |
| **保证至少一个高分方案** | "score 是 1-5 的整数，第一个方案不低于 4"——避免 LLM 全部输出 3 分方案推卸责任 |
| **去除合规噪音** | "不要写任何风险提示、免责声明或合规说明"——避免 LLM 输出"以上仅供参考"等无用文本 |
| **提示真实化** | reference_accounts "仅作示意，不必真实存在"——避免 LLM 编造看起来很真实的账号让用户去搜索 |

### 1.3 兜底策略（三层）

| 层 | 约束 |
|---|---|
| **Prompt** | 同上 5 条 |
| **Router** | `if not isinstance(raw_personas, list) or not raw_personas: 502` 兜住 LLM 没返回 personas 字段或返回空数组 |
| **Schema** | `PersonaPlan` 严校验：`score ∈ [1,5]`、字段非空；`PersonaResponse.personas` 长度 1-5；任何字段缺失 → `Exception` 被路由捕获 → 502 |

### 1.4 失败路径

| 失败 | 现象 | 处理 |
|---|---|---|
| LLM 网络/超时 | `LLMError(LLM_TIMEOUT/LLM_NETWORK)` | 502 + 中文消息 |
| LLM 4xx（API key 错） | `LLMError(LLM_HTTP_401)` | 502 + 上游片段 |
| LLM 返回非 JSON | `_extract_json` 抛 ValueError → `complete_json` 重试一次 | 重试也失败 → 502 |
| LLM 返回 JSON 但缺 personas | 路由层 `if not raw_personas: 502` | 502 |
| LLM 返回 personas 但字段不全 | `PersonaPlan(**p)` 抛 `ValidationError` | 502 + Pydantic 错误详情 |
| 前端 toast | "生成失败 · {message}" + loading 收回 | 用户可以再点一次 |

---

## 2 · 爆款拆解（Module 1）

| 字段 | 值 |
|---|---|
| **触发时机** | `feature-1.html` 第 1 步用户点「用 AI 拆解骨架」 |
| **后端入口** | `POST /api/skeleton/extract` → `routers/skeleton.py::extract_skeleton` |
| **Prompt 文件** | `services/prompts/skeleton.py::SKELETON_SYSTEM_PROMPT` |
| **请求 schema** | `SkeletonRequest{transcript(20-10000 字), persona_hint(可选)}` |
| **响应 schema** | `SkeletonResponse{hook, body[3..6], cta, transferable_template, model_used, elapsed_ms}` |

### 2.1 调用逻辑

```python
persona_block = "\n【当前人设上下文】" + req.persona_hint if req.persona_hint else ""
user_msg = "【视频台词】\n" + req.transcript + persona_block
data = await client.complete_json(SKELETON_SYSTEM_PROMPT, user_msg)
hook = HookSection(**data["hook"])
body = [NarrativeBeat(**b) for b in data["body"]]
cta = CTASection(**data["cta"])
template = str(data["transferable_template"])
```

### 2.2 Prompt 关键设计点

| 设计目标 | 具体约束 |
|---|---|
| **Hook 6 策略枚举** | 痛点前置 / 反常识陈述 / 悬念提问 / 视觉冲击 / 身份认同 / 数字罗列 / 其他——避免 LLM 自创策略名 |
| **CTA 5 策略枚举** | 点赞收藏 / 评论区留言 / 关注追更 / 引导私域 / 其他 |
| **节奏可读化** | body 段必须含 timestamp + emotion_arc，让用户能可视化"情绪曲线" |
| **可迁移模板** | "去除原内容、保留结构的可复用模板，使用 [占位符] 表示需要填空的部分"——产物可以被用户套用到自己的选题 |
| **Body 3-6 段** | "至少 3 段、最多 6 段"——少于 3 段太单薄，多于 6 段视频时长不合理 |
| **人设贴合** | "如果用户提供了 persona_hint，transferable_template 中的占位符应贴合该人设" |
| **拒绝照抄** | "不要重复原视频内容；transferable_template 是抽象后的模板，不能直接照抄" |

### 2.3 兜底策略

| 层 | 约束 |
|---|---|
| **Prompt** | 同上 7 条 |
| **Router** | 简单 `if not isinstance(data, dict): 502`；没有第二轮逻辑（拆解是一次性任务） |
| **Schema** | `HookSection.strategy` / `CTASection.strategy` 用 `Literal[...]` 严限定枚举；任何 LLM 自创策略 → Pydantic 拒绝 → 502 |

### 2.4 失败路径

同 §1.4，外加：
- **Hook strategy 不在枚举内** → Pydantic ValidationError → 502
- **body 数组为空 / 不是数组** → list comprehension 失败 → 502

---

## 3 · 引导式问答（Module 5，v0.8 含 brief）

> **本模块是 7 个干预点中复杂度最高的**：多轮调用 + 强收敛 + brief 透传 + router 硬拦截。

| 字段 | 值 |
|---|---|
| **触发时机** | `feature-1.html` 第 3 步 brief 表单填好后点「用上面要求开始 3 轮单选问答 →」 |
| **后端入口** | `POST /api/qa/next` → `routers/qa.py::qa_next` |
| **Prompt 文件** | `services/prompts/qa.py::QA_SYSTEM_PROMPT` |
| **请求 schema** | `QARequest{skeleton, transcript, persona_hint, brief, answers[0..3]}` |
| **响应 schema** | `QAResponse{round, done, question, rationale, options[0..4], model_used, elapsed_ms}` |
| **常量** | `MAX_QA_ROUNDS = 3`（schema 与 router 共用） |

### 3.1 调用逻辑（多轮 + 强收敛）

```python
# routers/qa.py
async def qa_next(req: QARequest):
    answered = len(req.answers)

    # ★★★ Layer 2 · Router 硬收敛：满 3 轮直接 done=true，不调 LLM ★★★
    if answered >= MAX_QA_ROUNDS:
        return QAResponse(round=3, done=True, options=[],
                          model_used="router", elapsed_ms=0)

    next_round = answered + 1
    user_msg = _build_user_message(req, next_round)
    data = await client.complete_json(QA_SYSTEM_PROMPT, user_msg)

    options = [QAOption(**o) for o in data.get("options", [])]
    if len(options) < 2:
        raise HTTPException(502, "LLM 返回选项数过少")
    options = options[:4]    # ★ Router 截断：超过 4 强制截
    return QAResponse(round=next_round, done=False, ..., options=options)
```

`_build_user_message` 注入 5 块上下文（顺序固定）：

```
【对标视频骨架】
{skeleton_json}
【当前人设】               ← persona_hint（可空）
【用户自填的创作要求（必须遵守）】   ← brief（v0.8 新增，可空）
【原视频台词（供参考）】     ← transcript（可空）
【已回答的轮次】
  - 第 1 题：xxx
    用户选了：xxx
  ...
【当前应出第 N 题】
  本轮主题：{ROUND_TOPICS[N]}     ← Hook / Body 切入 / CTA 三选一
  请严格按系统提示词的 JSON 格式返回，round 字段必须等于 N。
```

### 3.2 Prompt 关键设计点

| 设计目标 | 具体约束 |
|---|---|
| **3 轮主题硬绑定** | round=1 → Hook 角度；round=2 → Body 切入；round=3 → CTA 风格 |
| **不开放自由输入** | "100% 是 AI 出 3-4 个具体可朗读选项 → 用户单选"——v0.x 早期保留过自由文本，实测对话发散 |
| **选项必须可朗读** | "每一项都必须是『可朗读、立刻可拍』的具体角度，禁止『你可以怎样』的开放式套话" |
| **选项必须有差异** | "选项之间必须有可识别的差异（角度差异 / 情绪差异 / 受众差异），禁止『同义不同字』的伪选项" |
| **选项不能照抄原台词** | "options 中每个 label 都不能照抄原视频台词；要做"换人设、换场景、换说法"" |
| **brief 强落地（v0.8）** | "如果用户消息中带有【用户自填的创作要求（必须遵守）】，所有 options 与 question 都必须把这套要求当成强约束" —— 时长决定语言密度、节奏决定句式、风格决定用词、自由补充至少 2 项落地 |
| **done 永远为 false** | "done=true 由后端 router 在轮次 ≥ 3 时直接拦截，prompt 不需要判断"——把责任明确切到 router |

### 3.3 兜底策略（三层最完整的一个模块）

| 层 | 约束 |
|---|---|
| **Prompt** | 同上 7 条 |
| **Router · 硬收敛** | `if answered >= 3: return done=True` 不调 LLM、0ms |
| **Router · 选项数下限** | `if len(options) < 2: 502`——单选题至少要 2 个选项 |
| **Router · 选项数上限** | `options = options[:4]` 静默截断超出的——避免 LLM 出 8 个选项 |
| **Schema · 字段约束** | `MAX_QA_ROUNDS=3` 限制 round；`brief ≤ 1000 字`；`QAOption.label` 1-200 字；`options ≤ 4`；`answers ≤ MAX_QA_ROUNDS` |

### 3.4 状态机（前端视角）

```
IDLE → BRIEF_FORM → ROUND_1 → ROUND_2 → ROUND_3 → DONE → GENERATING_SCRIPT → FINAL
                    ↑          ↑          ↑          ↑
                    └─ POST /api/qa/next  ─┘          └─ POST /api/script/generate
```

详见 PRD §4.2。

### 3.5 失败路径

| 失败 | 处理 |
|---|---|
| LLM 网络/超时 | 502；前端在当前题位置显示"出题失败：请重试"，点击可重新调本轮 |
| LLM 返回选项 < 2 | 502 + "无法构成单选题"；前端 toast |
| LLM 返回选项 > 4 | 静默截断到 4，不报错，不让用户多看 4 个 |
| LLM 漂移到第 4、5 轮 | **不可能发生**——router 在 answered ≥ 3 时根本不调 LLM |
| brief 超 1000 字 | Pydantic 422，前端 textarea `maxlength=200` 已经做了第一道防线 |
| 用户中途刷新 | 前端 `SeecriptQAFlow.state` 重置；brief 表单恢复默认；骨架还在但需要重答（拆解便宜） |

---

## 4 · 原创脚本（Module 6，v0.8 含 brief）

| 字段 | 值 |
|---|---|
| **触发时机** | QA `done=true` 时由前端 `SeecriptQAFlow.generateScript()` 自动触发 |
| **后端入口** | `POST /api/script/generate` → `routers/script.py::generate_script` |
| **Prompt 文件** | `services/prompts/script.py::SCRIPT_SYSTEM_PROMPT` |
| **请求 schema** | `ScriptRequest{skeleton, answers, persona_hint, transcript, brief}` |
| **响应 schema** | `ScriptResponse{hook_narration, scenes[2..8], cta_narration, full_text, model_used, elapsed_ms}` |

### 4.1 调用逻辑

```python
user_msg = (
    "【对标视频骨架】" + skeleton_json +
    "【当前人设】" + persona_hint +
    "【用户自填的创作要求（必须遵守）】" + brief +    # v0.8 新增
    "【原视频台词（仅供识别「不能照抄」的反面教材）】" + transcript +
    "【用户在 3 轮单选题里给出的关键决策】" +
    "  - 第 1 题：xxx\n    用户选了：xxx\n  ..."
)
data = await client.complete_json(SCRIPT_SYSTEM_PROMPT, user_msg)
scenes = [ScriptScene(**s) for s in data["scenes"]]
hook_narration = str(data["hook_narration"])
cta_narration = str(data["cta_narration"])
full_text = str(data.get("full_text") or "")

# ★ Router 兜底：如果 LLM 忘记拼 full_text，路由层自己拼一份
if not full_text:
    full_text = _synthesize_full_text(hook_narration, scenes, cta_narration)
```

### 4.2 Prompt 关键设计点

| 设计目标 | 具体约束 |
|---|---|
| **结构对齐** | `scenes` 数组长度必须等于骨架 body 长度，且 `scenes[i].timestamp == body[i].timestamp`——保持节奏对齐 |
| **答案直接落地** | 第 1 题 → `hook_narration` 核心钩子 / 第 2 题 → `scenes[len/2]` 中段 / 第 3 题 → `cta_narration` 行动呼吁 |
| **不允许照抄** | "不允许照抄对标视频原台词（即用户消息中的『原视频台词』里出现的句子）"——这是和骨架拆解的本质区别 |
| **风格贴合人设** | "全文风格必须贴合人设（人设 hint 提到的语气、口头禅、目标受众）。如果没提供人设，写成中性叙述风格" |
| **Hook 钩子化** | "前 1 秒抛冲突或反差，禁止『大家好今天来聊…』这种开场" |
| **CTA 可执行** | "明确告诉用户做什么动作（评论 / 关注 / 收藏 / 搜索）" |
| **full_text 可阅读** | 必须含 `【Hook · 0:00-0:03】` `【title · timestamp】` `【CTA · 收尾】` 段落分隔符 |
| **brief 强落地（v0.8）** | 时长 → 按 ~3.5 字/秒朗读速度估算总字数；节奏 → 句式长短/停顿/悬念分布；风格 → 用词修辞；自由补充 → 必须在 hook 或 scenes 里直接命中 |

### 4.3 兜底策略

| 层 | 约束 |
|---|---|
| **Prompt** | 同上 8 条 |
| **Router · full_text 自动合成** | LLM 没拼 full_text → `_synthesize_full_text(hook, scenes, cta)` 用骨架格式自己拼一份，保证「复制纯文本」按钮永远有内容 |
| **Schema** | `ScriptResponse.scenes` 必须 2-8 段；`hook_narration` / `cta_narration` 各 ≤ 500 字；`brief ≤ 1000 字` |

### 4.4 失败路径

| 失败 | 处理 |
|---|---|
| LLM 漏字段 | `data["hook_narration"]` KeyError → 502 |
| LLM 给的 scenes 与骨架时长不对齐 | **prompt 软约束**，未做 router 强校验（v0.x 取舍：太严会频繁 502，影响 UX） |
| `full_text` 缺失 | Router 自动合成（不报错） |
| 脚本入库失败 | localStorage 配额满 → 静默丢弃（`seecript-history.js` 的 `writeArray` catch 异常） |
| 整体失败 | 前端 toast "脚本生成失败：请重试"；第 4 步不入库（保历史干净）；用户可点开始按钮重新走问答（v0.8 简化为重新填 brief） |

---

## 5 · 标题车间（Module 3）

| 字段 | 值 |
|---|---|
| **触发时机** | `feature-3.html` 用户点「生成发布元数据」 |
| **后端入口** | `POST /api/seo/titles` → `routers/seo.py::generate_titles` |
| **Prompt 文件** | `services/prompts/seo.py::SEO_SYSTEM_PROMPT` |
| **请求 schema** | `SEORequest{script(20-10000 字), platform=Literal["douyin"], persona_hint}` |
| **响应 schema** | `SEOResponse{titles[3..8], description(≤200 字), tags{...}, platform, model_used, elapsed_ms}` |

### 5.1 调用逻辑

```python
persona_block = "\n【当前人设上下文】" + req.persona_hint if req.persona_hint else ""
user_msg = "【脚本/口播稿】\n" + req.script + persona_block
data = await client.complete_json(SEO_SYSTEM_PROMPT, user_msg)
titles = [TitleCandidate(**t) for t in data["titles"]]
description = str(data["description"])
tags = TagCluster(**data["tags"])
```

### 5.2 Prompt 关键设计点

| 设计目标 | 具体约束 |
|---|---|
| **5 类标题枚举** | 反常识 / 数字 / 身份 / 痛点 / 悬念 / 其他 |
| **保证多样性** | "必须返回 5 个标题候选，覆盖至少 4 种不同类型"——避免全是同一种类型 |
| **字数硬限** | "标题不得超过 30 字" |
| **法规风险** | "广告法敏感词（极致词、最/第一等）禁用" |
| **平台算法适配** | 标题前 6-10 字必须出现强情绪/反差/数字；简介 ≤ 3 句、句末诱导互动；标签按"泛流量 + 长尾 + 话题挑战"三段 |
| **标签数量约束** | broad_traffic 3 个 / long_tail 3-5 个 / challenge_topics 1-2 个 |
| **平台锁定** | `platform: Literal["douyin"]`——v0.x 故意只做单平台，避免 prompt 稀释 |

### 5.3 兜底策略

| 层 | 约束 |
|---|---|
| **Prompt** | 同上 7 条 |
| **Router** | 简单 `if not isinstance(data, dict): 502`；无重试 |
| **Schema** | `titles` 长度 3-8；`TitleCandidate.type` 用 `Literal[...]` 严枚举；`description ≤ 200 字`；`platform` 锁死 douyin |

### 5.4 失败路径

| 失败 | 处理 |
|---|---|
| LLM 标题超 30 字 | **prompt 软约束**，未做 router 强校验（用户能在前端看到字数自己删） |
| LLM 出现广告法敏感词 | **prompt 软约束**，未做后端关键词过滤（v0.x 取舍：黑名单维护成本太高） |
| 标题数 < 3 | Pydantic 422 → 502 |
| `换一版` 重跑 | 前端直接再调一次，覆盖当前输出 |

---

## 6 · 评论分拣（Module 4）

| 字段 | 值 |
|---|---|
| **触发时机** | `feature-4.html` 用户粘评论后点「开始分拣」 |
| **后端入口** | `POST /api/comments/classify` → `routers/comments.py::classify_comments` |
| **Prompt 文件** | `services/prompts/comments.py::COMMENTS_SYSTEM_PROMPT` |
| **请求 schema** | `CommentsRequest{raw_text(10-20000 字), persona_hint}` |
| **响应 schema** | `CommentsResponse{high_value[], medium_value[], low_value_count, model_used, elapsed_ms}` |

### 6.1 调用逻辑

```python
persona_block = "\n【当前人设上下文】" + req.persona_hint if req.persona_hint else ""
user_msg = "【原始评论】\n" + req.raw_text + persona_block
data = await client.complete_json(COMMENTS_SYSTEM_PROMPT, user_msg)
high = [ClassifiedComment(**c) for c in data.get("high_value", [])]
med = [ClassifiedComment(**c) for c in data.get("medium_value", [])]
low_count = int(data.get("low_value_count", 0))
```

### 6.2 Prompt 关键设计点

| 设计目标 | 具体约束 |
|---|---|
| **三级分级** | high_value / medium_value / low_value_count（仅计数，不展示文本） |
| **高价值上限** | "high_value 上限 5 条，medium_value 上限 5 条" |
| **三种回复语气** | 每条 high_value 必须给 3 种语气（专业解读 / 幽默调侃 / 共情安抚），各 ≤ 80 字 |
| **拒绝套话** | "回复草稿不能空话套话，必须基于原评论的具体内容" |
| **敏感场识别** | "『敏感场』指含负面情绪 / 投诉 / 争议大的评论，需重点提示" |
| **人设贴合** | "如果用户提供了 persona_hint，回复语气应贴合该人设" |
| **5 类分类枚举** | 干货提问 / 争议探讨 / 高互动潜力 / 下期选题 / 敏感场 / 中价值 / 灌水 |

### 6.3 兜底策略

| 层 | 约束 |
|---|---|
| **Prompt** | 同上 7 条 + "low_value 仅返回数量，不返回内容" |
| **Router** | `data.get("high_value", [])` 默认空数组，避免 KeyError |
| **Schema** | `ClassifiedComment.classification` 用 `Literal[...]` 严枚举；`ReplyDraft.tone` 用 `Literal[...]`；`ReplyDraft.text ≤ 300 字` |

### 6.4 失败路径

| 失败 | 处理 |
|---|---|
| LLM 完全没识别出高价值 | `high_value=[]`、`medium_value=[]`、`low_value_count=N`——前端正常渲染"已识别灌水 N 条" |
| LLM 给敏感场写了不合适的回复 | **prompt 软约束 + 用户终判**——前端展示 3 种回复让用户选，用户可以不发 |
| 评论文本超长（>20000 字） | Pydantic 422，前端 toast |

---

## 7 · ASR · 音频转写（Module 1 前置）

> **唯一一个不调 LLM 的 AI 干预点**——用火山豆包大模型录音文件极速版（不是 OpenAI Whisper）。

| 字段 | 值 |
|---|---|
| **触发时机** | `feature-1.html` 第 1 步用户上传视频/音频后，浏览器内 `ffmpeg.wasm` 抽完 wav 后自动调 |
| **后端入口** | `POST /api/asr/transcribe` (multipart/form-data) → `routers/asr.py::transcribe` |
| **Provider 文件** | `services/asr_client.py::DoubaoBigmodelASRClient` |
| **资源 ID** | `volc.bigasr.auc_turbo`（极速版）|
| **响应 schema** | `ASRResponse{transcript, duration_seconds, provider, elapsed_ms}` |

### 7.1 调用逻辑

```python
# 1. 文件类型 / 大小校验
ALLOWED_EXTENSIONS = {".mp3", ".m4a", ".wav", ".aac", ".ogg", ".opus"}
MAX_UPLOAD_BYTES = 25 * 1024 * 1024  # base64 编码后会 +33% → 实际 HTTP body 约 33MB

# 2. 直接调豆包极速版（一次 HTTP，1-5 秒返回）
headers = {"X-Api-Key": ..., "X-Api-Resource-Id": "volc.bigasr.auc_turbo",
           "X-Api-Request-Id": uuid4(), "X-Api-Sequence": "-1"}
body = {"user": {"uid": api_key},
        "audio": {"data": base64(audio_bytes)},
        "request": {"model_name": "bigmodel",
                    "enable_itn": True, "enable_punc": True}}

# 3. 解析多种响应 schema（兼容 legacy）
text = data["result"]["text"] or data["data"]["result"]["text"]
```

### 7.2 关键设计点

| 设计目标 | 实现 |
|---|---|
| **0 服务器 CPU** | 抽轨在浏览器内做（`ffmpeg.wasm`），后端只负责把 base64 直传给豆包 |
| **极速版 vs 标准版** | 极速版 = 1 次 HTTP P95 < 5s；标准版需要 submit/query 轮询，且要求音频文件可公网访问 → 太麻烦 |
| **ITN + 标点** | `enable_itn=True`（一百二十 → 120）+ `enable_punc=True`，提升可读性 |
| **trace 追溯** | 用 `X-Api-Request-Id`（UUID）+ 响应头 `X-Tt-Logid` 双 ID，方便和火山客服对单 |

### 7.3 兜底策略

| 层 | 约束 |
|---|---|
| **前端** | `feature-1.html` 限制 `accept="video/*,audio/*"`；提示文件 ≤ 200 MB（路径 A 抽轨后通常远小于） |
| **路由层 · 文件类型** | `Path(filename).suffix in ALLOWED_EXTENSIONS` → 415 |
| **路由层 · MIME** | 不是 `audio/*` 也不是 `application/octet-stream` → 415 |
| **路由层 · 大小** | > 25MB → 413 + 友好中文提示 |
| **路由层 · 空文件** | 长度为 0 → 400 |
| **服务层 · API 状态码映射** | 上游 4xxxxxxx（参数错/格式不对）→ 422；上游 5xxxxxxx → 502 |
| **服务层 · 友好错误** | 把上游错误码翻译成中文（如 `45000151 → "音频格式不正确（仅支持 mp3 / wav / ogg / opus）"`） |
| **服务层 · 多种响应解析** | 同时支持极速版 (`result.text`) 与 legacy 标准版 (`data.result.text`) |
| **降级** | `ASR_PROVIDER=doubao` 但 key 空 → 自动落 `MockASRClient` 返回固定示例台词 |

### 7.4 失败路径

| 失败 | 上游码 | 后端处理 | 前端 |
|---|---|---|---|
| 静音音频 | `20000003` | 502 + "音频静音或无人声，无法识别" | toast + 自动切到「粘贴台词」tab，引导手输 |
| 格式不对 | `45000151` | 422 + "仅支持 mp3 / wav / ogg / opus" | toast + 切 tab |
| API key 错 | `45000001` | 502 + "请求参数无效（请检查...）" | toast |
| 火山服务繁忙 | `55000031` | 502 + "请稍后重试" | toast + 提示重试 |
| HTTP 超时 | timeout 60s | 502 + "豆包请求超时" | toast |
| 响应缺 X-Api-Status-Code | - | 502 + "豆包响应缺少 X-Api-Status-Code 头" | toast |

---

## 8 · 兜底策略汇总速查表

把全部 7 个 AI 干预点的兜底分成 4 类，按"用户感受到的最终行为"看：

| 兜底类型 | 触发场景 | 用户感受 | 涉及模块 |
|---|---|---|---|
| **静默自愈** | LLM 返回带 ```json``` 代码栅栏 / JSON 前后有废话 | 无感，正常拿到结果 | 全部 6 个 LLM 模块 |
| **后台重试** | LLM 第一次返回非 JSON | 无感，多等 5-30 秒 | 全部 6 个 LLM 模块（`complete_json` 内置） |
| **路由器自动合成** | LLM 漏 `full_text` 字段 | 无感，「复制纯文本」按钮仍能用 | 仅脚本生成 |
| **路由器静默截断** | LLM 出 8 个 QA 选项 | 无感，看到 4 个 | 仅引导式问答 |
| **路由器硬收敛** | answers ≥ 3 时直接 done=true | 无感，0ms 进入第 4 步 | 仅引导式问答 |
| **schema 严判 422/502** | LLM 字段缺失 / 枚举漂移 / 字数超限 | 看到 toast "返回字段不符合 schema" | 全部 6 个 LLM 模块 |
| **provider 自动降级** | API key 空 / `LLM_PROVIDER=mock` | 无感，看到 mock 数据 | 全部模块（LLM + ASR） |
| **状态码翻译** | 上游服务返回错误码 | 看到中文错误描述（不是英文 stack trace） | ASR + LLM |
| **前端兜底** | LLM 调用 5xx | toast "请重试"，loading 收回 | 全部模块 |

---

## 9 · 设计哲学（写给后续维护者）

### 9.1 为什么要"三层叠加"

LLM 永远不可信。任何"只靠 prompt 约束就能保证产物质量"的设计在生产里都会翻车。三层兜底中：

- **Prompt 决定大多数情况下的 70-90% 行为**（成本最低，最快迭代）
- **Router 决定确定性（如收敛轮次）和工程边界（如截断）**（不依赖 LLM 听话）
- **Schema 是最后一道闸门**（LLM 给什么进来都先过 Pydantic）

写新模块时**必须**问自己："如果 LLM 完全不听话，我的产物会是什么？"——如果答案是"用户拿到一个崩坏的页面"，则 Layer 2/3 还不够。

### 9.2 为什么 brief 是"软约束"而不是"硬约束"

时长/节奏/风格是创作维度，没法用 schema 强校验（你没法 Pydantic 一段叙事文本是不是"幽默")。所以 brief 走的是 **prompt 注入 + 用户终判** 的路径：

- prompt 在 system 里强制"必须遵守"
- 用户在前端看到产物后自己决定"这是不是我要的"，不行就重跑

这与"问答轮次"截然不同——后者是工程约束（必须收敛），所以走 router 硬拦截。

### 9.3 为什么 mock 模式是必须的

3 个原因：
1. **CI/CD 不能依赖外部服务** —— `pytest` 跑测试不该花 LLM 钱
2. **前端开发不该被 LLM 调用速度卡住** —— mock 模式 0.4s 就返回，DeepSeek 实调 30+ 秒
3. **客户演示场地无外网** —— `LLM_PROVIDER=mock + ASR_PROVIDER=mock` 全离线跑通

mock 通过 system prompt 中**独占字段名**做指纹路由（如 `personas` / `transferable_template` / `hook_narration`），新增模块时只要保证 schema 字段名独占，mock 自动适配，无需改 mock client。

---

## 10 · 文件索引

| 类型 | 文件 |
|---|---|
| **Pydantic schemas** | `server/app/schemas.py` |
| **System prompts** | `server/app/services/prompts/{persona,skeleton,qa,script,seo,comments}.py` |
| **路由层** | `server/app/routers/{persona,skeleton,qa,script,seo,comments,asr}.py` |
| **LLM 客户端** | `server/app/services/llm_client.py` |
| **ASR 客户端** | `server/app/services/asr_client.py` |
| **配置中心** | `server/app/config.py`（环境变量、provider 切换） |
| **前端 LLM 调用** | `interactions.js`（SeecriptQAFlow / bindSkeletonForm / bindPersonaForm / bindSeoForm / bindCommentsForm） |
| **前端 ASR 调用** | `asr-uploader.js`（ffmpeg.wasm 抽轨 + multipart 上传） |

---

> **更新规则**：每次给某个 AI 干预点改代码（无论是 prompt / router / schema），都要在本文对应章节同步更新；不允许出现"代码改了文档没改"的状态。本文是 Seecript AI 行为的**唯一权威说明**。
