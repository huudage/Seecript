"""Module 1 — 素材库。

`GET  /api/library`                  返回 3 个内置样例的卡片信息
`GET  /api/sample/{id}/manifest`     返回单个样例的完整预解析 manifest

阶段 1：纯静态 mock，3 个写死的样例。阶段 4 接到 server/samples/ 实际预解析 JSON。
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
)

router = APIRouter()


# 三个内置样例：营销 / 剪辑 / Motion Graph。所有 url 走 /samples 静态映射或占位。
_LIBRARY: list[LibraryItem] = [
    LibraryItem(
        id="sample-marketing-01",
        title="护肤新品种草｜30 秒大字幕痛点开场",
        scene="营销",
        duration_seconds=30.5,
        shot_count=12,
        cover_url="/samples/sample-marketing-01/cover.jpg",
    ),
    LibraryItem(
        id="sample-vlog-01",
        title="一日咖啡店探店｜剪辑感节奏 vlog",
        scene="剪辑",
        duration_seconds=48.0,
        shot_count=22,
        cover_url="/samples/sample-vlog-01/cover.jpg",
    ),
    LibraryItem(
        id="sample-motion-01",
        title="新功能上线动效宣传｜Motion Graph",
        scene="Motion Graph",
        duration_seconds=18.2,
        shot_count=9,
        cover_url="/samples/sample-motion-01/cover.jpg",
    ),
]


def _stub_manifest(sample_id: str, item: LibraryItem) -> SampleManifest:
    """阶段 1 占位 manifest——给前端把 5 个 page 跑通。"""
    shots = [
        Shot(
            index=i,
            start=i * (item.duration_seconds / item.shot_count),
            end=(i + 1) * (item.duration_seconds / item.shot_count),
            duration=item.duration_seconds / item.shot_count,
            thumbnail_url=f"/samples/{sample_id}/shot-{i:02d}.jpg",
            transcript=f"[mock] 镜头 {i + 1} 口播片段。",
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
    sections = [
        Section(kind="hook", start=0.0, end=item.duration_seconds * 0.15, summary="痛点提问 + 大字幕",
                shot_indices=[i for i in range(item.shot_count) if i < item.shot_count * 0.15]),
        Section(kind="body", start=item.duration_seconds * 0.15, end=item.duration_seconds * 0.85,
                summary="产品演示 + 对比", shot_indices=list(range(int(item.shot_count * 0.15),
                                                                int(item.shot_count * 0.85)))),
        Section(kind="cta", start=item.duration_seconds * 0.85, end=item.duration_seconds, summary="点赞收藏",
                shot_indices=[i for i in range(item.shot_count) if i >= item.shot_count * 0.85]),
    ]
    packaging = PackagingProfile(
        subtitle_style="大字加描边",
        has_title_bar=item.scene == "营销",
        transition_types=["cut", "fade"] if item.scene != "Motion Graph" else ["cut", "wipe", "scale"],
        cover_style="纯色大字" if item.scene == "营销" else "实拍画面 + 标题条",
        sticker_density=0.6 if item.scene == "Motion Graph" else 0.2,
    )
    return SampleManifest(
        sample_id=sample_id,
        title=item.title,
        duration_seconds=item.duration_seconds,
        video_url=f"/samples/{sample_id}/video.mp4",
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
