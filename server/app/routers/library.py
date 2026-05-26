"""Module 1 — 素材库。

`GET  /api/library`                  返回 3 个内置样例的卡片信息
`GET  /api/sample/{id}/manifest`     返回单个样例的完整预解析 manifest

阶段 1：纯静态 mock，3 个写死的样例（marketing / editing / motion_graph 各一）。
阶段 4 接到 server/samples/ 实际预解析 JSON。
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ..schemas import (
    LibraryItem,
    PackagingProfile,
    RhythmCurve,
    SampleManifest,
    Section,
    Shot,
    VideoType,
    kinds_for_video_type,
)

router = APIRouter()


# 三个内置样例：营销 / 剪辑 / Motion Graph。所有 url 走 /samples 静态映射或占位。
_LIBRARY: list[LibraryItem] = [
    LibraryItem(
        id="sample-marketing-01",
        title="护肤新品种草｜30 秒大字幕痛点开场",
        video_type="marketing",
        scene="营销",
        duration_seconds=30.5,
        shot_count=12,
        cover_url="/samples/sample-marketing-01/cover.jpg",
    ),
    LibraryItem(
        id="sample-vlog-01",
        title="一日咖啡店探店｜剪辑感节奏 vlog",
        video_type="editing",
        scene="剪辑",
        duration_seconds=48.0,
        shot_count=22,
        cover_url="/samples/sample-vlog-01/cover.jpg",
    ),
    LibraryItem(
        id="sample-motion-01",
        title="新功能上线动效宣传｜Motion Graph",
        video_type="motion_graph",
        scene="Motion Graph",
        duration_seconds=18.2,
        shot_count=9,
        cover_url="/samples/sample-motion-01/cover.jpg",
    ),
]


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
async def list_library() -> list[LibraryItem]:
    return _LIBRARY


@router.get("/sample/{sample_id}/manifest", response_model=SampleManifest)
async def get_sample_manifest(sample_id: str) -> SampleManifest:
    for item in _LIBRARY:
        if item.id == sample_id:
            return _stub_manifest(sample_id, item)
    raise HTTPException(status_code=404, detail=f"sample not found: {sample_id}")
