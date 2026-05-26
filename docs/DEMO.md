# Seecript · 演示走查手册

> 给评委 / 现场演示用的「5 分钟跑通 7 模块」剧本。
> 默认 mock 模式不依赖任何 API Key；接真模型只需在 `server/.env` 切 `_PROVIDER`。

---

## 0 · 启动

两个进程，分别开两个终端：

```powershell
# 终端 A — 后端 FastAPI
.\run.ps1
# 默认 :8090；首次跑会自举 venv + 装 server/requirements.txt
```

```powershell
# 终端 B — 前端 Vite
cd web
pnpm install        # 或 npm i
pnpm dev            # http://127.0.0.1:5173
```

健康检查：<http://127.0.0.1:8090/api/health> → 5 个 `*_provider=mock`。

> **mock 模式下所有 7 个模块都能跑完整流程**，视频流水线落地的是占位 mp4，足够走演示。
> 接真模型只需把 `server/.env` 里 `LLM_PROVIDER=doubao_ark` 等开关切开，对应的 `*_API_KEY` 填上即可。

---

## 1 · 5 分钟串讲

| 步骤 | 路由 | 演示重点 |
|---|---|---|
| ① 选样例 | `/library` | 三个内置样例：营销 / 剪辑 / Motion Graph。点一张卡进入。 |
| ② 拆解 | `/decompose` | SSE 进度条：抽帧 → BGM 能量 → ASR → VLM 标签 → LLM 段落。停在 manifest 概览：节奏曲线 + Hook/Body/CTA 段落条 + 包装画像。 |
| ③ 上传素材 + 缺口补全 | `/compose` | 拖几个 mp4/jpg 进来 → 点「开始构建」→ 缺口列表带状态徽章。每个缺口三种动作：结构重排 / 文案补全 / AIGC 生成。 |
| ④ 迁移可视化 | `/migrate` | React Flow 双列：左样例段落，右新方案 scene。线颜色：绿命中 / 黄勉强 / 红虚线缺口。 |
| ⑤ 渲染 / 编辑（一页） | `/render` | A/B 变体切换 → 提交渲染 → 6 步进度条：prepare/concat/seedance/remotion/overlay/finalize → 视频预览 + 分步耗时表。下方主轨/包装轨双时间线。 |
| ⑥ 画面包装 | 同上 | 包装轨横向时间线，按 kind 着色（subtitle/title_bar/sticker/transition/cover）。 |
| ⑦ 自然语言编辑 | 同上 | 底部 textarea：「把开场改得更口语化」→ 应用 → 新 Plan 入栈，撤销/重做按钮立即可用。 |

---

## 2 · 联调验收脚本

```powershell
cd server
python -m pytest tests/test_e2e_pipeline.py -v
```

覆盖：
- `test_library_and_manifest` — 模块 1
- `test_material_upload_and_plan_build` — 模块 3 + 5（含 gap detect/fill）
- `test_render_submit_and_stream` — 模块 5/6 SSE 终态
- `test_edit_apply_creates_new_plan` — 模块 7（新 plan_id + plan_store 持久化）
- `test_edit_apply_rejects_unknown_plan` / `test_render_rejects_unknown_plan` — 错误路径

期望：6 passed。所有上游走 mock，不消耗任何外部配额。

---

## 3 · 现场常见问题预案

| 现象 | 排查 |
|---|---|
| 渲染卡在 28% | FFmpeg 未装 → 流水线自动 mock fallback，最终也会 done。装 ffmpeg 可看真拼接。 |
| Seedance / Seedream 不生效 | mock 模式下两者输出占位；真模式查 `T2V_PROVIDER` / `T2I_PROVIDER` + 对应 API_KEY。 |
| 编辑点了没反应 | 看右上历史计数；模型没识别到工具调用时，会 fallback 把指令 prepend 进第一个 scene 的 narration（也算入栈）。 |
| 包装轨为空 | mock plan 包装 item 数量取决于样例 packaging profile；marketing 样例最多。 |

---

## 4 · 关联文档

- [`docs/ARCHITECTURE.md`](ARCHITECTURE.md) — 7 模块完整架构（赛题交付物，权威）
- [`server/.env.example`](../server/.env.example) — 所有 provider 切换开关
- [`README.md`](../README.md) — 仓库总览 + 运行 / 部署
