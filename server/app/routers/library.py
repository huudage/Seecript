"""Module 1 — 素材库。

`GET  /api/library`                  返回样例卡片列表（默认 system + user 合并）
`GET  /api/library?source=system`    只返回内置爆款样例
`GET  /api/library?source=user`      只返回用户上传到样例库的样例（MVP 占位空数组）
`GET  /api/sample/{id}/manifest`     返回单个样例的完整预解析 manifest

阶段 1：纯静态 mock，3 个写死的样例（marketing / editing / motion_graph 各一）。
阶段 4 接到 server/samples/ 实际预解析 JSON。
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from ..schemas import (
    LibraryItem,
    LibrarySource,
    PackagingProfile,
    RhythmCurve,
    SampleManifest,
    Section,
    Shot,
    VideoType,
    kinds_for_video_type,
)

router = APIRouter()


# 三个内置系统样例：营销 / 剪辑 / Motion Graph。
# duration_seconds 与 shot_count 必须和 server/samples/<id>/ 里的 video.mp4 + shot-NN.jpg 真实对应。
_SYSTEM_LIBRARY: list[LibraryItem] = [
    LibraryItem(
        id="sample-marketing-01",
        title="营销样例｜痛点开场 + 产品演示 + 行动引导",
        video_type="marketing",
        scene="营销",
        duration_seconds=18.4,
        shot_count=8,
        cover_url="/samples/sample-marketing-01/cover.jpg",
        source="system",
    ),
    LibraryItem(
        id="sample-vlog-01",
        title="剪辑样例｜Vlog 节奏 · 氛围铺垫到高潮收尾",
        video_type="editing",
        scene="剪辑",
        duration_seconds=118.2,
        shot_count=22,
        cover_url="/samples/sample-vlog-01/cover.jpg",
        source="system",
    ),
    LibraryItem(
        id="sample-motion-01",
        title="Motion Graph 样例｜标题入场 + 信息铺陈 + 爆点落版",
        video_type="motion_graph",
        scene="Motion Graph",
        duration_seconds=31.2,
        shot_count=12,
        cover_url="/samples/sample-motion-01/cover.jpg",
        source="system",
    ),
]


# 用户样例库：当前 MVP 不持久化，留空。下一期可接入 user_library_store + 上传转录流程。
_USER_LIBRARY: list[LibraryItem] = []


# 旧代码（gap.py 等）仍引用 _LIBRARY，给个聚合别名保持兼容。
_LIBRARY = _SYSTEM_LIBRARY + _USER_LIBRARY


# 3-段 / 4-段视频的占位 summary——按 video_type 写一组。
_STUB_SECTION_SUMMARIES: dict[VideoType, list[str]] = {
    "marketing": ["痛点提问 + 大字幕", "产品演示 + 对比", "点赞收藏"],
    "editing": ["环境/氛围铺垫", "情绪/动作高潮", "余韵收尾"],
    "motion_graph": ["标题/Logo 入场", "信息铺陈动画", "视觉爆点", "落版收尾"],
}


def _stub_sections(item: LibraryItem) -> list[Section]:
    """按 video_type 切段——3 类型用 15/70/15，motion_graph 4 段等分。"""
    kinds = kinds_for_video_type(item.video_type)
    summaries = _STUB_SECTION_SUMMARIES[item.video_type]
    n_seg = len(kinds)
    total = item.duration_seconds
    if n_seg == 3:
        boundaries = [0.0, total * 0.15, total * 0.85, total]
    else:
        step = total / n_seg
        boundaries = [step * i for i in range(n_seg)] + [total]

    sections: list[Section] = []
    for i, kind in enumerate(kinds):
        start = boundaries[i]
        end = boundaries[i + 1]
        # 把 shot_indices 按 start <= shot_start < end 的比例分摊
        first = int(item.shot_count * (start / total))
        last = int(item.shot_count * (end / total))
        if i == n_seg - 1:
            last = item.shot_count  # 最后一段兜底到所有镜头
        shot_idx = list(range(first, max(first + 1, last)))
        sections.append(Section(
            kind=kind, start=start, end=end,
            summary=summaries[i] if i < len(summaries) else f"{kind} 段",
            shot_indices=shot_idx,
        ))
    return sections


def _stub_manifest(sample_id: str, item: LibraryItem) -> SampleManifest:
    """阶段 1 占位 manifest——给前端把 5 个 page 跑通。"""
    shots = [
        Shot(
            index=i,
            start=i * (item.duration_seconds / item.shot_count),
            end=(i + 1) * (item.duration_seconds / item.shot_count),
            duration=item.duration_seconds / item.shot_count,
            thumbnail_url=f"/samples/{sample_id}/shot-{i:02d}.jpg",
            transcript=f"[mock] 镜头 {i + 1} 口播片段。" if item.video_type != "motion_graph" else None,
            tags=["近景", "口播"] if i % 3 == 0 else ["特写", "产品"],
        )
        for i in range(item.shot_count)
    ]
    rhythm = RhythmCurve(
        times=[s.start for s in shots],
        cut_density=[1.0 if i % 2 == 0 else 0.6 for i in range(item.shot_count)],
        bgm_energy=[round((i % 5) / 5.0, 2) for i in range(item.shot_count)],
        tempo_bpm=120.0,
    )
    sections = _stub_sections(item)
    packaging = PackagingProfile(
        subtitle_style="大字加描边" if item.video_type != "motion_graph" else "无字幕",
        has_title_bar=item.video_type == "marketing",
        transition_types=["cut", "fade"] if item.video_type != "motion_graph" else ["cut", "wipe", "scale"],
        cover_style="纯色大字" if item.video_type == "marketing" else (
            "合成画面 + 大字标题" if item.video_type == "motion_graph" else "实拍画面 + 标题条"
        ),
        sticker_density=0.6 if item.video_type == "motion_graph" else 0.2,
    )
    return SampleManifest(
        sample_id=sample_id,
        title=item.title,
        video_type=item.video_type,
        duration_seconds=item.duration_seconds,
        video_url=f"/samples/{sample_id}/video.mp4",
        has_voice=item.video_type != "motion_graph",
        shots=shots,
        rhythm=rhythm,
        sections=sections,
        packaging=packaging,
    )


@router.get("/library", response_model=list[LibraryItem])
async def list_library(
    source: Optional[LibrarySource] = Query(
        default=None,
        description="可选过滤：system=只返回内置样例；user=只返回用户上传到样例库的样例；不传=合并返回。",
    ),
) -> list[LibraryItem]:
    if source == "system":
        return _SYSTEM_LIBRARY
    if source == "user":
        return _USER_LIBRARY
    return _SYSTEM_LIBRARY + _USER_LIBRARY


@router.get("/sample/{sample_id}/manifest", response_model=SampleManifest)
async def get_sample_manifest(sample_id: str) -> SampleManifest:
    for item in _SYSTEM_LIBRARY + _USER_LIBRARY:
        if item.id == sample_id:
            return _stub_manifest(sample_id, item)
    raise HTTPException(status_code=404, detail=f"sample not found: {sample_id}")
