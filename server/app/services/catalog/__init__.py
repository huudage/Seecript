"""HyperFrames catalog 元数据加载器。

数据源：heygen-com/hyperframes 仓的 registry/blocks 与 registry/components
快照（Apache-2.0）。我们只引用名字 + 描述 + 预览资源 URL，不复制 HTML 实体；
当前阶段把 catalog 当成「LLM 可挑选的标签字典」用——packaging_agent 在
推荐 transition / caption / cover 时引用 catalog name，前端在浏览器 picker
里通过 preview_video 直链 HeyGen 静态站点展示缩略。

为什么不本地化资源：
- preview_video/poster 都是 https://static.heygen.ai/... 上的稳定 URL；
- 复制 HTML 模板等于把 HyperFrames 的 GSAP+CSS 渲染管线拉进 Seecript，
  当前阶段只做 schema/agent 衔接，不改渲染端；后续 phase 再决定是否落地。
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Iterable, Literal, Optional

CatalogCategory = Literal[
    "transition", "caption", "vfx", "overlay",
    "data-viz", "cover", "code-snippet", "other",
]


@lru_cache(maxsize=1)
def load_catalog() -> dict:
    """读取打包随仓库的 catalog.json 快照。"""
    here = Path(__file__).parent / "data" / "catalog.json"
    return json.loads(here.read_text(encoding="utf-8"))


def list_items(
    *,
    category: Optional[CatalogCategory] = None,
    tags: Optional[Iterable[str]] = None,
    limit: Optional[int] = None,
) -> list[dict]:
    """按分类/标签过滤 catalog 条目。"""
    items = load_catalog().get("items", [])
    if category:
        items = [i for i in items if i.get("category") == category]
    if tags:
        wanted = {t.lower() for t in tags}
        items = [i for i in items if wanted.intersection(t.lower() for t in (i.get("tags") or []))]
    if limit:
        items = items[:limit]
    return items


def find_by_name(name: str) -> Optional[dict]:
    for item in load_catalog().get("items", []):
        if item.get("name") == name:
            return item
    return None


def names_for_prompt(category: CatalogCategory, *, max_n: int = 12) -> list[dict]:
    """给 LLM system prompt 用的精简列表：name + title + description。

    控制 token 量：仅取头 max_n 个。
    """
    out = []
    for item in list_items(category=category, limit=max_n):
        out.append({
            "name": item["name"],
            "title": item.get("title"),
            "description": item.get("description"),
        })
    return out
