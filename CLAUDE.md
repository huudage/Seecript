# Seecript · Claude 会话备忘

## 项目身份

- 创作者智能副驾，由 KOCopilot 全量改名 fork 而来（HEAD: ddea395，2026-05-22）。
- 技术栈：FastAPI（Python 3.10+） + 原生 JS / CSS；前端静态文件由后端同进程挂载在 `/`。
- 默认 `LLM_PROVIDER=mock` / `ASR_PROVIDER=mock` / `T2V_PROVIDER=mock`，不配 Key 也能跑全流程。
- 启动：Windows `./run.ps1`（推荐，用 `py -3`）；Bash 系 `./run.sh`（注意：`run.sh` 调 `python`，若 PATH 上只有 `py`/`python3-shim` 会失败）。
- 测试：`server/` 下 `pytest`，预期 54 passed。

## 仓库布局速查

```
seecript-history.js, seecript-persona-editor.js   ← 改名后；曾叫 koc-*
deploy/seecript-server.service                    ← systemd 单元（曾叫 kocopilot-server）
server/app/{main,config,schemas}.py               ← FastAPI 入口、Pydantic Settings、I/O 契约
server/app/routers/                               ← 8 个端点：persona/skeleton/qa/script/seo/comments/asr/t2v
server/app/services/{llm,asr,t2v}_client.py       ← 3 个抽象客户端 + Mock/真实双实现
mattpocock-skills-zh-CN/                          ← 见下「Agent skills」
```

CSS 类前缀已统一为 `.seecript-*`；JS 全局对象命名 `SeecriptApi` / `SeecriptHistory` / `SeecriptQAFlow` 等；保留行业术语 `KOC`（key opinion consumer）原样。

## Agent skills（mattpocock-skills-zh-CN）

仓库 `./mattpocock-skills-zh-CN/skills/**/SKILL.md` 是 [mattpocock/skills](https://github.com/mattpocock/skills) 的中文本地化工程化工作流集合，**已通过 `skills add -g` 复制到 `~/.claude/skills/`**，Claude Code 启动后会自动注册为 slash command 与可用 skill。

| 何时该用 | skill | 触发关键词 |
|---|---|---|
| 写新功能 / 修 bug，要走测试先行 | `tdd` | "用 TDD"、"red-green-refactor"、"先写测试" |
| 棘手 bug、性能回退、复现不稳定 | `diagnose` | "诊断"、"debug"、"为什么挂了"、"性能掉了" |
| 升一层视角看代码 / 不熟悉的模块 | `zoom-out` | "整体看一下"、"这块怎么搭的" |
| 把当前讨论沉淀为 PRD | `to-prd` | "出个 PRD"、"写需求文档" |
| 把 PRD/方案拆成可领取 issue | `to-issues` | "拆成 issue"、"列工单" |
| issue 分流 / 准备给 AFK agent | `triage` | "三态分流"、"哪些先做" |
| 拿 CONTEXT.md/ADR 压测方案 | `grill-with-docs` | "压测一下这方案"、"对照文档评审" |
| 围绕方案做 Q&A 直至共识 | `grill-me` | "盘问我"、"grill me" |
| 找 codebase 重构机会 | `improve-codebase-architecture` | "重构机会"、"哪里耦合太深" |
| 一次性原型（命令行 / 多 UI 变体） | `prototype` | "搭个原型"、"先试几个版本" |
| 把当前会话压成交接文档 | `handoff` | "做个交接"、"压缩上下文" |
| 创建新 skill | `write-a-skill` | "写个 skill"、"封装成 skill" |
| 给 Claude Code 加 git 保险栓 | `git-guardrails-claude-code` | "拦 git push"、"防止 reset --hard" |
| **首次使用以上 skill 前必跑** | `setup-matt-pocock-skills` | 初始化 AGENTS.md 中的 `## Agent skills` 块、issue tracker 约定、`docs/agents/` |

不建议在 Seecript 用的（栈不匹配）：
- `migrate-to-shoehorn`（TS 专用）
- `scaffold-exercises`（Total TypeScript 课程脚手架）
- `setup-pre-commit`（基于 Husky/npm，本仓 Python 为主）

### 调用链路两条

1. **正规链路**：skill 装到 `~/.claude/skills/` 后，Claude Code 启动期把 SKILL.md frontmatter 的 `description` 注入 system-reminder；用户话语经语义匹配命中 `description` → 模型产出 `Skill(skill="tdd", args="…")` → 工具加载完整 SKILL.md 注入对话。
2. **本文件兜底**：上表把 description 翻译成中文触发关键词，假如 skill 注册表没生效（系统刚装完未重启、或被禁用），任何 Claude 会话读到本 CLAUDE.md 仍能用 `Read ./mattpocock-skills-zh-CN/skills/<name>/SKILL.md` 手动执行。

### 维护

- 升级：`npx skills@latest update -g -y`
- 卸载某个：`npx skills@latest remove -g -s <name> -a claude-code -y`
- 这套 skill 的中文版同步规则见 `./mattpocock-skills-zh-CN/.skills/translate-skill/SKILL.md`。
