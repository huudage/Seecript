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
import time
import uuid
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile
from pydantic import BaseModel

from ..config import get_settings
from ..schemas import (
    LibraryItem,
    LibrarySource,
    ManifestSaveRequest,
    PackagingProfile,
    ReferenceListItem,
    RhythmCurve,
    SampleManifest,
    SampleVersionInfo,
    Section,
    SectionRole,
    Shot,
    VideoType,
)
from ..services.library import manifest_store
from ..services.video import ffmpeg as ffmpeg_util

log = logging.getLogger("seecript.library")
router = APIRouter()


# server/samples 目录——precompute_samples 把 manifest.json 写在这里
_SAMPLES_ROOT = Path(__file__).resolve().parents[2] / "samples"

# === 上传到「系统样例库」的校验阈值 ===
# 沿用 decompose.upload 的同一套约束:mp4/mov/webm,单文件 200MB,时长 3 分钟 + 20s 余量
# (容器封装层可能比真实流多几秒)。和 decompose 那边保持一致,避免两条上传链路语义漂移。
_SYSTEM_UPLOAD_ALLOWED = {"video/mp4", "video/quicktime", "video/webm"}
_SYSTEM_UPLOAD_MAX_BYTES = 200 * 1024 * 1024
_SYSTEM_UPLOAD_MAX_DURATION_SECONDS = 200.0


def _load_real_manifest(sample_id: str) -> Optional[SampleManifest]:
    """已发布的 sample manifest——代理到 manifest_store.load_active。

    保留这个名字是为 plan.py / gap.py / 本文件其它点的旧调用兼容。
    v3 语义上 = "当前 active 版本"，由 manifest.active 指针决定。
    """
    return manifest_store.load_active(sample_id)


def _attach_status(item: LibraryItem) -> LibraryItem:
    """填 manifest_status / version_count / active_slot——Library 列表统一走这一道。"""
    cnt = manifest_store.version_count(item.id)
    return item.model_copy(update={
        "manifest_status": "ready" if cnt > 0 else "none",
        "version_count": cnt,
        "active_slot": manifest_store.get_active_slot(item.id),
    })


def _build_version_list(sample_id: str) -> list[SampleVersionInfo]:
    """list_versions（按 mtime 升序）→ SampleVersionInfo，加 v1/v2 标签。

    最旧 = v1，最新 = v2。标签由位置决定，slot_id 才是稳定 id。
    """
    raw = manifest_store.list_versions(sample_id)
    return [
        SampleVersionInfo(
            slot_id=v.slot_id,
            label=f"v{i + 1}",
            updated_at=v.updated_at,
            is_active=v.is_active,
        )
        for i, v in enumerate(raw)
    ]



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

# 内置 3 条爆款样例的 ID 黑名单——_scan_system_library_extras 用它跳过这些目录,
# 避免把硬编码 LibraryItem 和扫盘出来的副本重复列出。
_BUILTIN_SYSTEM_IDS: set[str] = {it.id for it in _SYSTEM_LIBRARY}


# 用户样例库：当前 MVP 不持久化，留空。下一期可接入 user_library_store + 上传转录流程。
_USER_LIBRARY: list[LibraryItem] = []


_SCENE_LABEL_BY_TYPE: dict[VideoType, str] = {
    "marketing": "营销",
    "editing": "剪辑",
    "motion_graph": "Motion Graph",
}


def _scan_system_library_extras() -> list[LibraryItem]:
    """扫 server/samples/ 下不在 _BUILTIN_SYSTEM_IDS 里的目录,
    把用户通过 /api/library/system/upload 上传的样例也列入「系统样例库」tab。

    判定标准:目录含 video.mp4。meta.json 用于还原 title / video_type / uploaded_at,
    缺失时退化到目录名 + marketing 默认值。duration / shot_count 走预拆解 manifest,
    没拆过给 0(前端列表会显示 "0s · 0 镜头",提示用户先拆解)。
    """
    if not _SAMPLES_ROOT.is_dir():
        return []
    items: list[LibraryItem] = []
    for child in _SAMPLES_ROOT.iterdir():
        if not child.is_dir():
            continue
        sample_id = child.name
        if sample_id in _BUILTIN_SYSTEM_IDS:
            continue
        if not (child / "video.mp4").is_file():
            continue
        title = sample_id
        video_type: VideoType = "marketing"
        meta_path = child / "meta.json"
        if meta_path.is_file():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                title = str(meta.get("title") or sample_id)[:80]
                vt = meta.get("video_type")
                if vt in ("marketing", "editing", "motion_graph"):
                    video_type = vt  # type: ignore[assignment]
            except (json.JSONDecodeError, OSError) as exc:
                log.warning("[library] system extra meta %s parse failed: %s", meta_path, exc)
        mf = _load_real_manifest(sample_id)
        duration = mf.duration_seconds if mf else 0.0
        shot_count = len(mf.shots) if mf else 0
        cover_url = f"/samples/{sample_id}/cover.jpg"
        if not (child / "cover.jpg").is_file():
            cover_url = f"/samples/{sample_id}/video.mp4"
        items.append(LibraryItem(
            id=sample_id,
            title=title,
            video_type=video_type,
            scene=_SCENE_LABEL_BY_TYPE.get(video_type, "系统上传"),
            duration_seconds=duration,
            shot_count=shot_count,
            cover_url=cover_url,
            source="system",
        ))

    def _uploaded_at(it: LibraryItem) -> float:
        p = _SAMPLES_ROOT / it.id / "meta.json"
        if p.is_file():
            try:
                return float(json.loads(p.read_text(encoding="utf-8")).get("uploaded_at", 0.0))
            except Exception:  # noqa: BLE001
                return 0.0
        return 0.0
    items.sort(key=_uploaded_at, reverse=True)
    return items


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
        cut_density=[],
        bgm_energy=[round((i % 5) / 5.0, 2) for i in range(item.shot_count)],
        tempo_bpm=None,
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
                base = it
            else:
                base = it.model_copy(update={
                    "shot_count": len(mf.shots),
                    "duration_seconds": mf.duration_seconds,
                })
            out.append(_attach_status(base))
        return out

    if source == "system":
        return _augment(_SYSTEM_LIBRARY) + [_attach_status(it) for it in _scan_system_library_extras()]
    if source == "user":
        return [_attach_status(it) for it in _scan_user_library()]
    return (
        _augment(_SYSTEM_LIBRARY)
        + [_attach_status(it) for it in _scan_system_library_extras()]
        + [_attach_status(it) for it in _scan_user_library()]
    )


@router.get("/sample/{sample_id}/manifest", response_model=SampleManifest)
async def get_sample_manifest(
    sample_id: str,
    slot: Optional[str] = Query(
        default=None,
        description="指定版本槽 slot_id；不传则取 active 槽。",
    ),
) -> SampleManifest:
    """取样例的 manifest。

    - slot 不传 → 当前 active 槽（Compose / Library 默认行为）
    - slot=<slot_id> → 取指定槽（Decompose 页对比 v1/v2 时按 slot 显式拉）
    - 没有任何版本槽且不是内置 stub fallback → 409，让前端跳 Decompose 页拆解
    """
    if manifest_store.locate_sample_dir(sample_id) is None:
        # 兜底：内置 _SYSTEM_LIBRARY 即使没 sample dir 也回 stub manifest，保证旧前端跑通
        for item in _SYSTEM_LIBRARY:
            if item.id == sample_id:
                log.warning("[library] %s 无样例目录，返回 stub manifest", sample_id)
                return _stub_manifest(sample_id, item)
        raise HTTPException(status_code=404, detail=f"sample not found: {sample_id}")

    if slot is not None:
        mf = manifest_store.load_version(sample_id, slot)
        if mf is None:
            raise HTTPException(status_code=404, detail=f"slot {slot} 不存在")
        return mf

    active = manifest_store.load_active(sample_id)
    if active is not None:
        return active

    # 没任何版本——内置 3 条退化到 stub 让旧 Compose 跑通；其它的拒绝
    for item in _SYSTEM_LIBRARY:
        if item.id == sample_id:
            log.warning("[library] %s 无 active manifest，回落等分 stub", sample_id)
            return _stub_manifest(sample_id, item)
    raise HTTPException(
        status_code=409,
        detail=f"sample {sample_id} 尚未拆解，请先在「视频拆解」页跑一次 decompose",
    )


# ---------------------------------------------------------------------------
# Manifest 版本槽 CRUD
# ---------------------------------------------------------------------------
class ManifestStatusResponse(BaseModel):
    """`GET /api/sample/{id}/manifest/status` —— Compose 入口判断要不要弹"未拆解"提示用。

    versions 列表给 Decompose 页画版本 tabs；version_count == 0 时为空数组。
    """

    sample_id: str
    version_count: int
    max_versions: int
    active_slot: Optional[str]
    versions: list[SampleVersionInfo]


class VersionMutationResponse(BaseModel):
    """update / activate / delete / regenerate 共用返回体——前端按此刷新版本 tabs。"""

    sample_id: str
    version_count: int
    active_slot: Optional[str]
    versions: list[SampleVersionInfo]


def _mutation_response(sample_id: str) -> VersionMutationResponse:
    return VersionMutationResponse(
        sample_id=sample_id,
        version_count=manifest_store.version_count(sample_id),
        active_slot=manifest_store.get_active_slot(sample_id),
        versions=_build_version_list(sample_id),
    )


@router.get("/sample/{sample_id}/manifest/status", response_model=ManifestStatusResponse)
async def get_manifest_status(sample_id: str) -> ManifestStatusResponse:
    """轻量探针——Compose 入口批量调，确认每个 sample 是否已拆解 + 列出版本槽。"""
    if manifest_store.locate_sample_dir(sample_id) is None:
        # 内置 _SYSTEM_LIBRARY 即使没目录也能列出（视作未拆）
        if not any(it.id == sample_id for it in _SYSTEM_LIBRARY):
            raise HTTPException(status_code=404, detail=f"sample not found: {sample_id}")
    return ManifestStatusResponse(
        sample_id=sample_id,
        version_count=manifest_store.version_count(sample_id),
        max_versions=manifest_store.MAX_VERSIONS,
        active_slot=manifest_store.get_active_slot(sample_id),
        versions=_build_version_list(sample_id),
    )


@router.get("/sample/{sample_id}/versions", response_model=list[SampleVersionInfo])
async def list_sample_versions(sample_id: str) -> list[SampleVersionInfo]:
    """列出某样例的所有版本槽（≤ MAX_VERSIONS=2，按 mtime 升序）。"""
    if manifest_store.locate_sample_dir(sample_id) is None:
        if not any(it.id == sample_id for it in _SYSTEM_LIBRARY):
            raise HTTPException(status_code=404, detail=f"sample not found: {sample_id}")
    return _build_version_list(sample_id)


@router.put("/sample/{sample_id}/manifest", response_model=VersionMutationResponse)
async def put_sample_manifest(
    sample_id: str,
    manifest: SampleManifest,
    slot: Optional[str] = Query(
        default=None,
        description="目标 slot_id；不传则写入 active 槽。**就地编辑**——不开新版本。",
    ),
) -> VersionMutationResponse:
    """整段替换某槽的内容——用户在 Decompose 页编辑后点"保存"调这个。

    不开新版本（重新拆解才会开新槽），不做语义校验（用户明确要求允许任意修改）。
    Pydantic 只做格式校验。slot 不存在抛 404。
    """
    if manifest_store.locate_sample_dir(sample_id) is None:
        raise HTTPException(status_code=404, detail=f"sample not found: {sample_id}")
    target_slot = slot or manifest_store.get_active_slot(sample_id)
    if target_slot is None:
        raise HTTPException(
            status_code=409,
            detail=f"sample {sample_id} 没有任何版本槽，请先跑一次拆解",
        )
    if manifest.sample_id and manifest.sample_id != sample_id:
        raise HTTPException(
            status_code=400,
            detail=f"manifest.sample_id={manifest.sample_id} 与 URL {sample_id} 不一致",
        )
    manifest = manifest.model_copy(update={"sample_id": sample_id})
    try:
        manifest_store.update_version(sample_id, target_slot, manifest)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"写槽失败: {exc}") from exc
    return _mutation_response(sample_id)


@router.post("/sample/{sample_id}/versions/{slot_id}/activate", response_model=VersionMutationResponse)
async def activate_sample_version(sample_id: str, slot_id: str) -> VersionMutationResponse:
    """切换 active 指针到指定槽。slot 不存在抛 404。"""
    if manifest_store.locate_sample_dir(sample_id) is None:
        raise HTTPException(status_code=404, detail=f"sample not found: {sample_id}")
    try:
        manifest_store.activate(sample_id, slot_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _mutation_response(sample_id)


@router.delete("/sample/{sample_id}/versions/{slot_id}", response_model=VersionMutationResponse)
async def delete_sample_version(sample_id: str, slot_id: str) -> VersionMutationResponse:
    """删除一个版本槽。被删的若是 active,自动跳到剩下那个。

    - slot 不存在 → 404
    - 删完没版本了:active 自动清空,前端走"未拆解"分支
    """
    if manifest_store.locate_sample_dir(sample_id) is None:
        raise HTTPException(status_code=404, detail=f"sample not found: {sample_id}")
    if not manifest_store.delete_version(sample_id, slot_id):
        raise HTTPException(status_code=404, detail=f"slot {slot_id} 不存在")
    return _mutation_response(sample_id)


# ---------------------------------------------------------------------------
# stage-15: 全局结构知识库
# ---------------------------------------------------------------------------
# Compose 顶部 ReferencePicker 通过 GET /references 拍平所有 (sample, slot) 让用户
# 按 slot 粒度选 1-2 个版本作为结构参考(可同一 sample 双槽)。POST /manifest/save
# 是 Decompose 页「保存到资产库」的入口:用户跑完拆解先拿到草稿(SSE done.manifest
# 在前端 zustand),确认后调它落到版本槽。

def _library_item_lookup() -> dict[str, LibraryItem]:
    """汇总 system 内置 + system extras + user 上传所有样例,按 id 索引。

    给 /references 用 —— 拍平 (sample, slot) 时需要 title/video_type/scene/cover 等
    sample 级元数据,直接复用 LibraryItem 现成构造逻辑。
    """
    items: dict[str, LibraryItem] = {}
    for it in _SYSTEM_LIBRARY:
        items[it.id] = it
    for it in _scan_system_library_extras():
        items[it.id] = it
    for it in _scan_user_library():
        items[it.id] = it
    return items


@router.get("/references", response_model=list[ReferenceListItem])
async def list_references() -> list[ReferenceListItem]:
    """全局结构知识库:拍平所有样例的所有版本槽。

    返回顺序:按 sample 在 library 列表中的顺序;同 sample 内按 v1→v2(updated_at 升序)。
    Compose 顶部 ReferencePicker 直接渲染这个列表让用户多选 1-2 个版本。
    """
    items = _library_item_lookup()
    out: list[ReferenceListItem] = []
    # 用 _LIBRARY-like 顺序:先系统内置 → 系统 extras → 用户上传(_library_item_lookup
    # 的 dict 顺序就是这个,Python 3.7+ dict 保插入序)
    for sample_id, item in items.items():
        versions = _build_version_list(sample_id)
        for v in versions:
            mf = manifest_store.load_version(sample_id, v.slot_id)
            # mf 理论不会 None(list_versions 拿出来的槽都有文件),保险起见用 item 兜底
            duration = mf.duration_seconds if mf else item.duration_seconds
            shot_count = len(mf.shots) if mf else item.shot_count
            out.append(ReferenceListItem(
                sample_id=sample_id,
                sample_title=item.title,
                slot_id=v.slot_id,
                label=v.label,
                video_type=item.video_type,
                scene=item.scene,
                duration_seconds=duration,
                shot_count=shot_count,
                cover_url=item.cover_url,
                source=item.source,
                updated_at=v.updated_at,
                is_active=v.is_active,
            ))
    return out


@router.post("/sample/{sample_id}/manifest/save", response_model=VersionMutationResponse)
async def save_sample_manifest(
    sample_id: str,
    req: ManifestSaveRequest,
) -> VersionMutationResponse:
    """把前端草稿落到资产库的版本槽。

    Decompose 页用户跑完拆解 → SSE done 把 manifest 推到前端 zustand → 用户点
    「保存到资产库」时把整段 manifest 通过这个端点写进版本槽。

    槽容量逻辑(复用 manifest_store.create_version 内置规则):
    - 槽未满 + 无 replace_slot → 新建槽并 activate
    - 槽未满 + 传了 replace_slot → 422(防止误覆盖空位)
    - 槽满 + 无 replace_slot → 409 slots_full,前端弹覆盖对话框让用户挑
    - 槽满 + replace_slot → 覆盖该槽,保留另一个不动
    """
    if manifest_store.locate_sample_dir(sample_id) is None:
        raise HTTPException(status_code=404, detail=f"sample not found: {sample_id}")

    # manifest.sample_id 兜底校验(前端可能漏填或填错)
    if req.manifest.sample_id and req.manifest.sample_id != sample_id:
        raise HTTPException(
            status_code=400,
            detail=f"manifest.sample_id={req.manifest.sample_id} 与 URL {sample_id} 不一致",
        )
    manifest = req.manifest.model_copy(update={"sample_id": sample_id})

    cur_count = manifest_store.version_count(sample_id)
    # 预校验:槽未满但传了 replace_slot → 422(让 manifest_store 内部抛 ValueError 也行,
    # 但 422 比 500 友好)
    if req.replace_slot is not None and cur_count < manifest_store.MAX_VERSIONS:
        raise HTTPException(
            status_code=422,
            detail=f"slot 还有空位({cur_count}/{manifest_store.MAX_VERSIONS}),不应传 replace_slot",
        )
    # 预校验:槽满 + 无 replace_slot → 409 复用 stage-14 slots_full 协议体,
    # 前端 Decompose 页 SaveOverwriteDialog 解析它列出 v1/v2 让用户挑
    if req.replace_slot is None and cur_count >= manifest_store.MAX_VERSIONS:
        existing = manifest_store.list_versions(sample_id)
        raise HTTPException(
            status_code=409,
            detail={
                "error": "slots_full",
                "message": f"sample {sample_id} 已有 {cur_count} 个版本,请先选一个覆盖",
                "max_versions": manifest_store.MAX_VERSIONS,
                "versions": [
                    {
                        "slot_id": v.slot_id,
                        "updated_at": v.updated_at,
                        "is_active": v.is_active,
                    }
                    for v in existing
                ],
            },
        )

    try:
        manifest_store.create_version(
            sample_id,
            manifest,
            replace_slot=req.replace_slot,
            activate=True,
        )
    except manifest_store.SlotsFullError as exc:
        # 理论已被上面预校验拦下,但 manifest_store 内部也防御性抛了,这里兜底转 409
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except (FileNotFoundError, OSError) as exc:
        raise HTTPException(status_code=500, detail=f"写槽失败: {exc}") from exc

    return _mutation_response(sample_id)


# ---------------------------------------------------------------------------
# 上传到「系统样例库」: POST /api/library/system/upload
# ---------------------------------------------------------------------------
# 与 /api/decompose/upload 的区别:
#   - 物理路径落到 server/samples/<sys-hex>/ 而不是 var/uploads/decompose/<user-hex>/
#   - sample_id 前缀 sys- (vs user-),避免和 _BUILTIN_SYSTEM_IDS / user 上传冲突
#   - 在「系统样例库」tab 列出,所有用户共享(本期单租户,等价于"管理员上传一段公共样例")
#   - 校验阈值复用一套(200MB/3min),避免两条上传链路语义漂移
class LibrarySystemUploadResponse(BaseModel):
    """`POST /api/library/system/upload` 返回——前端拿到 sample_id 后再走 /api/decompose 触发拆解。"""

    sample_id: str
    title: str
    video_type: VideoType
    filename: str
    size_bytes: int
    video_url: str


@router.post("/library/system/upload", response_model=LibrarySystemUploadResponse)
async def upload_to_system_library(
    file: UploadFile = File(...),
    video_type: VideoType = Form(default="marketing"),
    title: Optional[str] = Form(default=None),
) -> LibrarySystemUploadResponse:
    """把一段视频上传到「系统样例库」,落到 server/samples/<sys-hex>/video.mp4。

    - 仅接受 video/mp4 | video/quicktime | video/webm
    - 单文件硬上限 200MB,时长上限 3 分钟(+ 20s 余量)
    - sample_id 形如 sys-<hex>,与内置 demo (sample-marketing-01 等) 不冲突
    - meta.json 记录 title/video_type/uploaded_at,供 _scan_system_library_extras 还原列表
    - 上传后调用方需走 /api/decompose 触发实际拆解,manifest 生成完才能在「样例拆解」页用
    """
    if file.content_type not in _SYSTEM_UPLOAD_ALLOWED:
        raise HTTPException(
            status_code=415,
            detail=f"unsupported content-type: {file.content_type}（支持 mp4/mov/webm）",
        )

    data = await file.read()
    if len(data) > _SYSTEM_UPLOAD_MAX_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"{file.filename} 超过 {_SYSTEM_UPLOAD_MAX_BYTES // (1024 * 1024)}MB 上限",
        )

    sample_id = f"sys-{uuid.uuid4().hex[:10]}"
    target_dir = _SAMPLES_ROOT / sample_id
    # parents=True:确保 server/samples 根本不存在时也能起来(开发机第一次跑没 samples 目录)
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = target_dir / "video.mp4"
    target_path.write_bytes(data)

    # 时长校验:ffprobe 拿真实秒数(容器头里 metadata 不一定准)。
    # ffprobe 不可用时(开发机没装 ffmpeg)放过——后续真链路会再撞同样的问题,
    # 比起在上传环节卡死开发者,让流水线自己降级更友好。
    try:
        probe_info = ffmpeg_util.probe(target_path)
        duration = probe_info.duration_seconds
    except (ffmpeg_util.FFmpegError, FileNotFoundError, OSError) as exc:
        log.warning("[library.system.upload] ffprobe failed for %s: %s; 跳过时长校验", target_path, exc)
        duration = None

    if duration is not None and duration > _SYSTEM_UPLOAD_MAX_DURATION_SECONDS:
        try:
            target_path.unlink(missing_ok=True)
            target_dir.rmdir()
        except OSError:
            pass
        raise HTTPException(
            status_code=413,
            detail=(
                f"视频时长 {duration:.1f}s 超过 3 分钟上限"
                f"(最长 {_SYSTEM_UPLOAD_MAX_DURATION_SECONDS:.0f}s)"
            ),
        )

    safe_title = (title or Path(file.filename or "video.mp4").stem)[:80]
    meta = {
        "sample_id": sample_id,
        "title": safe_title,
        "video_type": video_type,
        "filename": Path(file.filename or "video.mp4").name,
        "size_bytes": len(data),
        "uploaded_at": time.time(),
        "uploaded_via": "library.system.upload",
    }
    (target_dir / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    log.info(
        "[library.system.upload] sample=%s type=%s saved %s (%d bytes)",
        sample_id,
        video_type,
        target_path,
        len(data),
    )
    return LibrarySystemUploadResponse(
        sample_id=sample_id,
        title=safe_title,
        video_type=video_type,
        filename=Path(file.filename or "video.mp4").name,
        size_bytes=len(data),
        video_url=f"/samples/{sample_id}/video.mp4",
    )
