"""GET /api/catalog/* —— HyperFrames catalog 元数据查询。

返回的是 services/catalog/data/catalog.json 的 snapshot。前端 PackagingPanel
/ FrameDesignPicker / CatalogPicker 用它列出可选 transition / caption / cover
block 缩略图。

不带认证；纯读；snapshot 随仓库 ship。
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from ..services.catalog import load_catalog, list_items, find_by_name, CatalogCategory

router = APIRouter(prefix="/api/catalog", tags=["catalog"])


class CatalogItem(BaseModel):
    name: str
    title: Optional[str] = None
    description: str = ""
    tags: list[str] = Field(default_factory=list)
    kind: str  # block | component
    category: str  # transition | caption | vfx | overlay | data-viz | cover | code-snippet | other
    duration: Optional[float] = None
    preview_video: Optional[str] = None
    preview_poster: Optional[str] = None


class CatalogListResponse(BaseModel):
    source: str
    version: str
    license: str = "Apache-2.0"
    items: list[CatalogItem]
    total: int


@router.get("/blocks", response_model=CatalogListResponse)
def list_catalog(
    category: Optional[str] = Query(None, description="过滤分类，例如 transition / caption / cover / vfx / overlay / data-viz"),
    tag: Optional[str] = Query(None, description="过滤 tag（单值，OR 匹配）"),
    limit: Optional[int] = Query(None, ge=1, le=200),
) -> CatalogListResponse:
    catalog = load_catalog()
    items = list_items(
        category=category,  # type: ignore[arg-type]
        tags=[tag] if tag else None,
        limit=limit,
    )
    return CatalogListResponse(
        source=catalog.get("source", "heygen-com/hyperframes"),
        version=catalog.get("version", "snapshot"),
        license=catalog.get("license", "Apache-2.0"),
        items=[CatalogItem(**i) for i in items],
        total=len(items),
    )


@router.get("/blocks/{name}", response_model=CatalogItem)
def get_catalog_item(name: str) -> CatalogItem:
    item = find_by_name(name)
    if item is None:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail=f"catalog item '{name}' not found")
    return CatalogItem(**item)
