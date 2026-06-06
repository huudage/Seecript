# Catalog & Frame 设计系统

本文档说明 Seecript 中 HyperFrames catalog blocks 与 frame.md 设计系统两块新引入的能力。

---

## 1. 缘起

短视频内容拆段后视觉风格容易段段割裂：开场封面用一种字体、转场又是另一种节奏、AIGC 段画风又跑了。我们引入两层抽象解决：

- **catalog block**：把 HyperFrames 的 88 blocks + 23 components 元数据收进来当成 LLM 可挑的"风格 hint"——packaging_agent 给每个 transition / cover 候选挂一个 catalog 名（如 `whip-pan` / `flash-through-white`），不替换 ffmpeg xfade 实现，仅作为风格基准让后续 prompt 收敛。
- **frame.md 设计系统**：每个 plan 携带一份 `FrameDesignSystem`（preset + palette + motion_density + grain/vignette + notes），三个生成 agent（packaging / copy_outline / aigc_prompt）都从中读 token，确保 4 段视觉统一。

灵感来自 HeyGen 开源的 [HyperFrames](https://github.com/heygen-com/hyperframes)（HTML→MP4 视频框架）的 frame.md 概念。

---

## 2. 数据流

### 2.1 catalog 元数据快照

```
hyperframes/registry/blocks/<name>/data.json
hyperframes/registry/components/<name>/data.json
                                    ↓ 一次性同步
server/app/services/catalog/data/catalog.json    （111 条，离线静态）
                                    ↓
server/app/services/catalog/__init__.py          （load + 分类过滤）
                                    ↓
GET /api/catalog/blocks?category=transition      → 前端 CatalogPicker
                                    ↓
                                packaging_agent system prompt
```

`catalog.json` 是从 HyperFrames 仓库静态抓的快照（Apache 2.0），字段：
```json
{
  "source": "https://github.com/heygen-com/hyperframes",
  "version": "<commit-sha>",
  "license": "Apache-2.0",
  "items": [
    {
      "name": "whip-pan",
      "title": "Whip Pan",
      "description": "...",
      "tags": ["motion", "energy"],
      "kind": "block",
      "category": "transition",
      "duration": 0.6,
      "preview_video": "https://static.heygen.ai/.../whip-pan.mp4",
      "preview_poster": "https://static.heygen.ai/.../whip-pan.png"
    }
  ]
}
```

类目映射规则在 `services/catalog/__init__.py:_CATEGORY_BUCKETS`：tags 中含 `transition` → `transition`，含 `caption|subtitle` → `caption`，等等。

### 2.2 frame.md 在 schema 里的位置

```python
# server/app/schemas.py
class FrameDesignSystem(BaseModel):
    preset: FrameDesignPreset = "custom"  # 11 个 HyperFrames 模板风格名
    palette: list[str]                    # ≤ 6 HEX
    background_color: Optional[str]
    typography_display / body / mono: Optional[str]
    motion_density: MotionDensity = "balanced"  # minimal / balanced / kinetic
    grain_overlay: bool = False
    vignette: bool = False
    notes: str = ""

class ComposeSettings(BaseModel):
    ...
    frame_design: FrameDesignSystem    # 必有，默认 preset=custom
```

非破坏性默认：旧 plan 没这个字段时反序列化成 preset="custom" + 空字段，agent 端拿到的是空风格 hint。

---

## 3. Agent 端如何使用

三个 agent 都在自己的 system prompt / user content 里嵌入 frame_design 摘要 + catalog 候选名清单：

| Agent | 读 frame_design | 读 catalog |
|---|---|---|
| `packaging_agent` | system prompt 顶部插 `_frame_design_block(frame)` 教 LLM"按这个色板挑封面背景" | system prompt 列出本类目可选的 12 个 catalog name，LLM 给每个 transition / cover 候选回 `catalog_block` 字段（验证用 `find_by_name` 兜底） |
| `copy_outline_agent` | user_lines 加一条 frame summary，让 LLM 知道是冷蓝商务还是高燃黄黑 | 不读（文案与 catalog 无关） |
| `aigc_prompt_agent` | T2V prompt + image spec 都加 frame summary（含 grain/vignette toggles） | 不读（catalog 与 Seedance 无关） |

`packaging_agent.py` 关键代码段：
```python
def _frame_design_block(frame: Optional[FrameDesignSystem]) -> str:
    if not frame: return ""
    ...

def _catalog_hint_block() -> str:
    return f"可选 transition catalog: {catalog_names_for_prompt('transition', max_n=12)}"

def _build_system_prompt(prefs, frame=None):
    return (
        _SYSTEM_BASE
        + _frame_design_block(frame)
        + _catalog_hint_block()
        + _SCHEMA_HINT
    )

def _coerce_transition(raw):
    ...
    name = raw.get("catalog_block")
    if name and not catalog_find_by_name(name):
        name = None  # LLM 编的 name 直接丢
    return TransitionSuggestion(catalog_block=name, ...)
```

---

## 4. 前端组件

### 4.1 FrameDesignPicker

位置：`web/src/components/compose/FrameDesignPicker.tsx`

集成在 `ComposeSettingsPanel`（Compose 页高级设置面板，关键词区下方）。UI：
- 11 个 preset chip（Custom / Biennale Yellow / BlockFrame / Blue Pro / …）
- 折叠的"细调"区：palette HEX 输入 + 色块预览、motion_density 3 选 1、grain/vignette 开关、notes textarea

值通过 `value.frame_design` + `onChange({frame_design: {...}})` 双向绑定到 `ComposeSettings`。

### 4.2 CatalogPicker

位置：`web/src/components/compose/CatalogPicker.tsx`

集成在 `PackagingPanel` 的 transition options + cover candidates 上："+ 选风格"按钮 → 弹出 80vh 模态：
- 顶部 tag chips（按 tags 二次过滤）
- 网格缩略图：`<img src=preview_poster>` 默认显示，hover 切到 `<video src=preview_video>` 自动播放
- 点击 → 写回 `catalog_block`，关闭弹窗

预览资源直链 `static.heygen.ai`，无需自己代理；catalog ~110 条不缓存，每次打开重新拉。

---

## 5. 升级 catalog 快照

HyperFrames 上游有更新时：
```bash
# 1. 拉最新仓库
cd ~/hyperframes && git pull

# 2. 跑同步脚本（首次手动写过、未来需要再补）
python server/scripts/sync_catalog.py
# → 重新生成 server/app/services/catalog/data/catalog.json
#    更新 source.version 为最新 commit sha

# 3. server reload
systemctl restart seecript-server
```

⚠️ **不要在前端硬编码 catalog name**——所有 LLM 输出的 name 都过 `catalog_find_by_name` 校验，前端的 picker 也通过 `/api/catalog/blocks` 拿到列表，两端都对齐到这一份快照。

---

## 6. 风险与边缘

| 场景 | 行为 |
|---|---|
| 老 plan 没有 `frame_design` 字段 | Pydantic 反序列化成默认 `FrameDesignSystem(preset="custom")`，agent 端 `_frame_design_block` 检测后只在有 palette/notes 时才注入 prompt |
| LLM 编的 catalog_block name 不存在 | `_coerce_transition` / `_coerce_cover_candidate` 调 `catalog_find_by_name` 兜底，找不到直接置 null，不阻塞 |
| HeyGen CDN preview 视频偶尔 404 | 前端 `<img>` 用 lazy load + 浏览器自带 alt fallback；用户没看到缩略图也能凭 name + tags 选 |
| catalog.json 未同步 / 文件损坏 | `load_catalog()` 抛异常被 `/api/catalog/blocks` 接住转 500，前端 picker 显示错误提示，不影响 packaging 主流程（system prompt 那段会变空字符串） |

---

## 7. 后续工作（暂不做）

- **catalog 缩略图同源代理**：当前直链 HeyGen，国内访问可能慢；可以选择性预下载到 `var/catalog_previews/` 走 `/catalog-previews/...`。
- **frame.md 文件导入**：让用户上传一份完整 frame.md（HyperFrames 格式），后端解析填充 `FrameDesignSystem`。
- **packaging_agent 多版本 frame**：当前一个 plan 一个 frame；未来可以支持开场用 Bold Poster、收尾用 Capsule。
