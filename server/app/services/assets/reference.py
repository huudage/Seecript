"""把 reference_asset_ids 解析成可喂给多模态 LLM 的图像 URL 列表。

设计点：
- BGM 类型直接跳过（不参与视觉参考）
- reference_image：file_url 自身作为 1 张图
- reference_video：用元数据里的 frame_urls（后台抽好的 8 张）；缺则回落 thumbnail_url
- 总图数硬上限 max_total（默认 12）防 token 爆炸；多视频按 round-robin 均匀采样
- 找不到的 asset_id 静默忽略，不阻塞主流程
"""
from __future__ import annotations

import logging
from typing import Iterable

from .store import asset_store

log = logging.getLogger("seecript.assets.reference")


def resolve_reference_image_urls(
    asset_ids: Iterable[str],
    *,
    max_total: int = 12,
) -> list[str]:
    """asset_ids → 图像 URL 列表（/assets/... 形式）。

    返回顺序：先 reference_image，再 reference_video 抽帧；同类型内按 asset_ids 输入顺序。
    """
    if not asset_ids:
        return []

    image_urls: list[str] = []
    video_frame_pools: list[list[str]] = []

    for aid in asset_ids:
        a = asset_store.get(aid)
        if a is None:
            log.warning("[reference] asset_id=%s 不存在，跳过", aid)
            continue
        if a.status != "ready":
            log.warning("[reference] asset_id=%s status=%s 未就绪，跳过", aid, a.status)
            continue
        if a.kind == "bgm":
            continue
        if a.kind == "reference_image":
            image_urls.append(a.file_url)
            continue
        if a.kind == "reference_video":
            frames = a.metadata.get("frame_urls") or []
            if isinstance(frames, list) and frames:
                video_frame_pools.append([str(f) for f in frames])
            else:
                thumb = a.metadata.get("thumbnail_url")
                if isinstance(thumb, str) and thumb:
                    image_urls.append(thumb)

    # 视频帧 round-robin 取样，让多个参考视频都有露出
    flattened_video: list[str] = []
    if video_frame_pools:
        max_len = max(len(p) for p in video_frame_pools)
        for i in range(max_len):
            for pool in video_frame_pools:
                if i < len(pool):
                    flattened_video.append(pool[i])

    combined = image_urls + flattened_video
    if len(combined) <= max_total:
        return combined

    # 超出上限：图优先（最多占 max_total 一半），剩余给视频帧均匀采样
    image_budget = min(len(image_urls), max(1, max_total // 2))
    video_budget = max_total - image_budget
    if video_budget <= 0:
        return image_urls[:max_total]
    # 视频帧按等间隔降采样
    if len(flattened_video) <= video_budget:
        video_pick = flattened_video
    else:
        step = len(flattened_video) / video_budget
        video_pick = [flattened_video[int(i * step)] for i in range(video_budget)]
    return image_urls[:image_budget] + video_pick
