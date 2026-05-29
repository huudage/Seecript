"""Module 1 — 素材库。

`GET  /api/library`                  返回样例卡片列表（默认 system + user 合并）
`GET  /api/library?source=system`    只返回内置爆款样例
`GET  /api/library?source=user`      只返回用户上传到样例库的样例（MVP 占位空数组）
`GET  /api/sample/{id}/manifest`     返回单个样例的完整预解析 manifest

样例 manifest 优先从 `server/samples/<id>/manifest.json` 读取（由 scripts/precompute_samples.py
离线跑 decompose_agent 真模型拆解后写入）；命中盘上预算结果即返回，没有再回落到等分 stub。
LibraryItem.shot_count/duration_seconds 也会从真 manifest 修正。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import ValidationError

from ..schemas import (
    LibraryItem,
    LibrarySource,
    PackagingProfile,
    RhythmCurve,
    SampleManifest,
    Section,
    SectionRole,
    Shot,
    VideoType,
)

log = logging.getLogger("seecript.library")
router = APIRouter()


# server/samples 目录——precompute_samples 把 manifest.json 写在这里
_SAMPLES_ROOT = Path(__file__).resolve().parents[2] / "samples"


def _load_real_manifest(sample_id: str) -> Optional[SampleManifest]:
    """尝试加载预计算好的真 manifest.json。失败/不存在返回 None。

    查找顺序：
    1. `server/samples/<sample_id>/manifest.json`（内置样例，precompute_samples.py 写）
    2. `server/var/uploads/decompose/<sample_id>/manifest.json`（用户上传样例，decompose_agent 写）
    """
    candidates = [_SAMPLES_ROOT / sample_id / "manifest.json"]
    try:
        from ..config import get_settings
        user_root = get_settings().log_dir.parent / "var" / "uploads" / "decompose"
        candidates.append(user_root / sample_id / "manifest.json")
    except Exception:  # noqa: BLE001
        pass
    for p in candidates:
        if not p.is_file():
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            return SampleManifest.model_validate(data)
        except (json.JSONDecodeError, ValidationError, OSError) as exc:
            log.warning("[library] %s 解析失败，跳过：%s", p, exc)
    return None



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


def _scan_user_library() -> list[LibraryItem]:
    """扫描 var/uploads/decompose/<sample_id>/ 下所有带 video.mp4 的目录，
    读 meta.json 还原 LibraryItem。让 /library?source=user 能列出"我的上传样例"。

    没 meta.json（旧上传）也兜住：title 用 sample_id，video_type 默认 marketing。
    duration / shot_count 走预拆解 manifest（_load_real_manifest），没拆过就给 0。
    """
    from ..config import get_settings
    root = get_settings().log_dir.parent / "var" / "uploads" / "decompose"
    if not root.is_dir():
        return []
    items: list[LibraryItem] = []
    for child in root.iterdir():
        if not child.is_dir():
            continue
        if not (child / "video.mp4").is_file():
            continue
        sample_id = child.name
        # 默认值（meta 缺失时兜底）
        title = sample_id
        video_type: VideoType = "marketing"
        scene = "我的上传"
        meta_path = child / "meta.json"
        if meta_path.is_file():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                title = str(meta.get("title") or sample_id)[:80]
                vt = meta.get("video_type")
                if vt in ("marketing", "editing", "motion_graph"):
                    video_type = vt  # type: ignore[assignment]
            except (json.JSONDecodeError, OSError) as exc:
                log.warning("[library] user meta %s parse failed: %s", meta_path, exc)
        # 时长 / shot_count：拆过有 manifest 才填，否则 0
        mf = _load_real_manifest(sample_id)
        duration = mf.duration_seconds if mf else 0.0
        shot_count = len(mf.shots) if mf else 0
        # 封面：拆解时如果生成了 cover.jpg 走它，没有就用 video.mp4 第一帧（前端 <video poster>）
        cover_url = f"/uploads/decompose/{sample_id}/cover.jpg"
        if not (child / "cover.jpg").is_file():
            cover_url = f"/uploads/decompose/{sample_id}/video.mp4"
        items.append(LibraryItem(
            id=sample_id,
            title=title,
            video_type=video_type,
            scene=scene,
            duration_seconds=duration,
            shot_count=shot_count,
            cover_url=cover_url,
            source="user",
        ))
    # 按 sample_id 倒序（user-<hex> 是随机的，但近期上传的也凑合排前面没意义；
    # meta.json 里有 uploaded_at 时按它倒序更友好）
    def _uploaded_at(it: LibraryItem) -> float:
        p = root / it.id / "meta.json"
        if p.is_file():
            try:
                return float(json.loads(p.read_text(encoding="utf-8")).get("uploaded_at", 0.0))
            except Exception:  # noqa: BLE001
                return 0.0
        return 0.0
    items.sort(key=_uploaded_at, reverse=True)
    return items


# 旧代码（gap.py 等）仍引用 _LIBRARY，给个聚合别名保持兼容。
_LIBRARY = _SYSTEM_LIBRARY + _USER_LIBRARY


# 内置样例的 stub manifest 默认 4 段：opening / development / climax / closing
# 每个 video_type 给一组 (role, theme, summary) 三元组——summary 用于占位 UI。
# 真 manifest（precompute_samples 跑出来的）会覆盖这套 stub。
_STUB_STRUCTURE: dict[VideoType, list[tuple[SectionRole, str, str]]] = {
    "marketing": [
        ("opening", "钩子开场", "痛点提问 + 大字幕"),
        ("development", "产品演示", "卖点展开 + 对比"),
        ("climax", "卖点高潮", "强构图特写"),
        ("closing", "行动引导", "点赞收藏"),
    ],
    "editing": [
        ("opening", "氛围铺垫", "环境/氛围铺垫"),
        ("development", "节奏铺陈", "情绪/动作展开"),
        ("climax", "情绪高潮", "情绪/动作顶点"),
        ("closing", "余韵收尾", "慢镜或长镜"),
    ],
    "motion_graph": [
        ("opening", "标题入场", "标题/Logo 入场"),
        ("development", "信息铺陈", "图表/字段动画"),
        ("climax", "视觉爆点", "快剪/形变"),
        ("closing", "落版收尾", "品牌定格"),
    ],
}


def _stub_sections(item: LibraryItem) -> list[Section]:
    """4 段 opening/development/climax/closing 等比例切，时间占比 15/50/20/15。"""
    structure = _STUB_STRUCTURE.get(item.video_type, _STUB_STRUCTURE["marketing"])
    n_seg = len(structure)
    total = item.duration_seconds
    # 时间占比：开场 15% · 主体 50% · 高潮 20% · 收尾 15%
    ratios = [0.15, 0.50, 0.20, 0.15]
    if n_seg != 4 or len(ratios) != n_seg:
        # 退化到等分
        ratios = [1.0 / n_seg] * n_seg
    boundaries = [0.0]
    for r in ratios:
        boundaries.append(boundaries[-1] + total * r)
    boundaries[-1] = total

    sections: list[Section] = []
    for i, (role, theme, summary) in enumerate(structure):
        start = boundaries[i]
        end = boundaries[i + 1]
        first = int(item.shot_count * (start / total))
        last = int(item.shot_count * (end / total))
        if i == n_seg - 1:
            last = item.shot_count
        shot_idx = list(range(first, max(first + 1, last)))
        sections.append(Section(
            role=role,
            theme=theme,
            start=start,
            end=end,
            summary=summary,
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
    """列出样例卡片。system 样例若有预拆解 manifest.json，shot_count/duration 用真数据修正。"""

    def _augment(items: list[LibraryItem]) -> list[LibraryItem]:
        out: list[LibraryItem] = []
        for it in items:
            mf = _load_real_manifest(it.id)
            if mf is None:
                out.append(it)
            else:
                out.append(it.model_copy(update={
                    "shot_count": len(mf.shots),
                    "duration_seconds": mf.duration_seconds,
                }))
        return out

    if source == "system":
        return _augment(_SYSTEM_LIBRARY)
    if source == "user":
        return _scan_user_library()
    return _augment(_SYSTEM_LIBRARY) + _scan_user_library()


@router.get("/sample/{sample_id}/manifest", response_model=SampleManifest)
async def get_sample_manifest(sample_id: str) -> SampleManifest:
    for item in _SYSTEM_LIBRARY + _USER_LIBRARY:
        if item.id == sample_id:
            real = _load_real_manifest(sample_id)
            if real is not None:
                log.info("[library] %s 使用预拆解 manifest（%d shots）", sample_id, len(real.shots))
                return real
            log.warning("[library] %s 无预拆解 manifest.json，回落等分 stub", sample_id)
            return _stub_manifest(sample_id, item)
    # 用户上传样例：先扫盘确认 sample 真实存在，再取预拆解 manifest
    user_items = _scan_user_library()
    for item in user_items:
        if item.id == sample_id:
            real = _load_real_manifest(sample_id)
            if real is not None:
                return real
            raise HTTPException(
                status_code=409,
                detail=f"sample {sample_id} 尚未拆解，请先在「样例拆解」页跑一次 decompose",
            )
    raise HTTPException(status_code=404, detail=f"sample not found: {sample_id}")
