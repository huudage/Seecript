"""用户素材库（Asset Library）服务层。

承担两类用途：
- BGM 资产：渲染阶段供 ffmpeg mix_bgm 使用
- 参考素材（图/视频抽帧）：多模态 LLM 风格/调性/结构参考

与 materials/store.py 的 MaterialStore 严格分离——Material 是 session 隔离的"本次原料"，
Asset 是用户长期资产，跨 session/plan 复用。
"""
from __future__ import annotations

from .store import AssetStoreError, asset_store
from .reference import resolve_reference_image_urls

__all__ = ["asset_store", "AssetStoreError", "resolve_reference_image_urls"]
