"""视频素材预处理 · Stage 20。

输入：刚上传完的视频 Material（material_id + 本地文件路径 + project_id）。
输出：MaterialShot 列表写回 material_store（同时落盘 + 内存）。

为什么要预处理：
- step-1 上传一段 60s 长视频，step-2 的 _pick 只能"取前 N 秒"——很容易把开场静止画面塞进 climax 段。
- 预处理后，_pick 可以按 section.role 智能选片（见 Batch 4 的 _pick_shot_for_section）。

链路：
  ffprobe duration → PySceneDetect 切片 → 每片中间帧抽缩略图 → 批量多模态 LLM caption + role 推荐
  → 写回 Material.shots / preprocess_status='ready'

设计要点：
- 失败一律落到 status='failed'，shots=[]，不阻塞用户用素材（fallback 到 truncate 行为）。
- 单镜头时长 < 0.8s 的合并到相邻；> 8s 的不切（VLM 帧抽中间一帧足够代表）。
- 上限 12 个 shot：超过把最短相邻合并到 12 个；防止 LLM payload 爆炸。
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path

from ...config import get_settings
from ...schemas import MaterialShot, SectionRole, all_role_names
from ..llm_client import LLMError, get_llm_client
from ..video import ffmpeg as ffmpeg_svc
from ..video import scene_detect
from .store import material_store

log = logging.getLogger("seecript.materials.preprocess")

# 单段最短/最长阈值；过短合并、过长不切
_MIN_SHOT_SECONDS = 0.8
_MAX_SHOTS = 12

_ALLOWED_ROLES = tuple(all_role_names())

_SHOT_SYSTEM = (
    "你是短视频镜头打标 Agent。看一帧画面，返回 JSON：\n"
    "{\"caption\": string（一句话画面描述，≤25 字），"
    "\"action_density\": number（0.0-1.0；1=快切/全屏运动，0=完全静态），"
    "\"recommended_role\": string（必须从 allowed_roles 里选一个）}。\n"
    "字段名固定；不要漏。"
)


def _shots_dir(project_id: str, material_id: str) -> Path:
    """缩略图按 material_id 子目录隔离：var/uploads/<sid>/shots/<material_id>/。"""
    settings = get_settings()
    root = settings.log_dir.parent / "var" / "uploads" / project_id / "shots" / material_id
    root.mkdir(parents=True, exist_ok=True)
    return root


def _compact_shots(raw: list[scene_detect.DetectedShot]) -> list[scene_detect.DetectedShot]:
    """合并过短镜头 / 截到 _MAX_SHOTS。

    1) 过短 ( < _MIN_SHOT_SECONDS )：与较短邻居合并
    2) 总数 > _MAX_SHOTS：循环合并最短两个相邻
    """
    items = list(raw)
    if not items:
        return items
    # 1) 处理过短
    changed = True
    while changed and len(items) > 1:
        changed = False
        for i, sh in enumerate(items):
            if sh.duration >= _MIN_SHOT_SECONDS:
                continue
            if i == 0:
                # 合并到下一个
                nxt = items[i + 1]
                items[i + 1] = scene_detect.DetectedShot(
                    index=nxt.index, start=sh.start, end=nxt.end, duration=nxt.end - sh.start,
                )
                items.pop(i)
            elif i == len(items) - 1:
                prv = items[i - 1]
                items[i - 1] = scene_detect.DetectedShot(
                    index=prv.index, start=prv.start, end=sh.end, duration=sh.end - prv.start,
                )
                items.pop(i)
            else:
                prv = items[i - 1]
                nxt = items[i + 1]
                # 合到较短邻居
                if prv.duration <= nxt.duration:
                    items[i - 1] = scene_detect.DetectedShot(
                        index=prv.index, start=prv.start, end=sh.end, duration=sh.end - prv.start,
                    )
                else:
                    items[i + 1] = scene_detect.DetectedShot(
                        index=nxt.index, start=sh.start, end=nxt.end, duration=nxt.end - sh.start,
                    )
                items.pop(i)
            changed = True
            break
    # 2) 截到 _MAX_SHOTS
    while len(items) > _MAX_SHOTS:
        # 找相邻最短两段合并
        best_i = 0
        best_sum = float("inf")
        for i in range(len(items) - 1):
            s = items[i].duration + items[i + 1].duration
            if s < best_sum:
                best_sum = s
                best_i = i
        a, b = items[best_i], items[best_i + 1]
        items[best_i] = scene_detect.DetectedShot(
            index=a.index, start=a.start, end=b.end, duration=b.end - a.start,
        )
        items.pop(best_i + 1)
    # reindex
    return [
        scene_detect.DetectedShot(index=i, start=s.start, end=s.end, duration=s.duration)
        for i, s in enumerate(items)
    ]


async def _caption_shot(
    image_path: Path,
) -> tuple[str, float, SectionRole]:
    """单帧 → LLM 多模态描述 + action_density + role。失败回落 placeholder。"""
    user_text = (
        f"allowed_roles={list(_ALLOWED_ROLES)}\n"
        "请按 system 中的 schema 返回 JSON。"
    )
    try:
        client = get_llm_client()
        text = await client.complete_multimodal(_SHOT_SYSTEM, user_text, [image_path])
    except LLMError as exc:
        log.warning("[preprocess] LLM caption failed (%s) → placeholder", exc)
        return ("[auto] 待打标", 0.5, "development")
    except Exception as exc:  # noqa: BLE001
        log.warning("[preprocess] LLM caption unexpected: %s → placeholder", exc)
        return ("[auto] 待打标", 0.5, "development")

    try:
        data = json.loads(text) if text.strip().startswith(("{", "[")) else None
    except json.JSONDecodeError:
        data = None
    if not isinstance(data, dict):
        return ("[auto] 待打标", 0.5, "development")

    caption = str(data.get("caption") or "")[:80].strip() or "[auto] 待打标"
    try:
        action_density = float(data.get("action_density") or 0.5)
    except (TypeError, ValueError):
        action_density = 0.5
    action_density = max(0.0, min(1.0, action_density))

    raw_role = data.get("recommended_role")
    role: SectionRole = "development"
    if isinstance(raw_role, str):
        cleaned = re.sub(r"^(step|item)\s*0*(\d+)$", r"\1_\2", raw_role.strip())
        if cleaned in _ALLOWED_ROLES or re.match(r"^(step|item)_\d+$", cleaned):
            role = cleaned  # type: ignore[assignment]
    return caption, action_density, role


async def preprocess_video_material(
    project_id: str,
    material_id: str,
    video_path: Path,
) -> None:
    """后台任务：切片 + 缩略图 + VLM caption；结果写回 material_store。

    任意一步失败都把 status='failed'，shots=[]——_pick 会自动 fallback 到旧的 truncate 行为。
    """
    log.info("[preprocess] start project=%s material=%s path=%s", project_id, material_id, video_path)
    material_store.update(project_id, material_id, preprocess_status="running")

    if not video_path.exists():
        material_store.update(
            project_id, material_id,
            preprocess_status="failed",
            preprocess_error="文件不存在",
        )
        return

    # 1) 时长 probe → 同时回填 duration_seconds
    duration: float | None = None
    try:
        info = await asyncio.to_thread(ffmpeg_svc.probe, video_path)
        duration = float(info.duration_seconds or 0.0)
        if duration > 0.0:
            material_store.update(project_id, material_id, duration_seconds=duration)
    except (ffmpeg_svc.FFmpegError, FileNotFoundError) as exc:
        log.warning("[preprocess] probe failed material=%s: %s", material_id, exc)
        material_store.update(
            project_id, material_id,
            preprocess_status="failed",
            preprocess_error=f"probe 失败: {exc}",
        )
        return

    if duration is None or duration < _MIN_SHOT_SECONDS:
        material_store.update(
            project_id, material_id,
            preprocess_status="failed",
            preprocess_error="视频时长过短",
        )
        return

    # 2) 切片
    try:
        raw_shots = await asyncio.to_thread(scene_detect.detect_shots, str(video_path))
    except Exception as exc:  # noqa: BLE001
        log.warning("[preprocess] scene_detect failed material=%s: %s", material_id, exc)
        material_store.update(
            project_id, material_id,
            preprocess_status="failed",
            preprocess_error=f"切片失败: {exc}",
        )
        return

    if not raw_shots:
        material_store.update(
            project_id, material_id,
            preprocess_status="failed",
            preprocess_error="未检测出镜头",
        )
        return

    shots_compact = _compact_shots(raw_shots)
    log.info("[preprocess] %s shots %d → %d (after compact)",
             material_id, len(raw_shots), len(shots_compact))

    # 3) 每片中间帧抽缩略图
    shots_dir = _shots_dir(project_id, material_id)
    thumb_paths: list[Path | None] = []
    if not ffmpeg_svc.ffmpeg_available():
        log.warning("[preprocess] ffmpeg unavailable; skip thumbnails")
        thumb_paths = [None] * len(shots_compact)
    else:
        for sh in shots_compact:
            mid = (sh.start + sh.end) / 2.0
            dst = shots_dir / f"shot-{sh.index:02d}.jpg"
            try:
                await asyncio.to_thread(ffmpeg_svc.extract_frame, video_path, mid, dst)
                thumb_paths.append(dst)
            except ffmpeg_svc.FFmpegError as exc:
                log.warning("[preprocess] frame %d failed: %s", sh.index, exc)
                thumb_paths.append(None)

    # 4) VLM caption（并发，但限制为 4 路避免火山速率）
    sem = asyncio.Semaphore(4)

    async def _one(idx: int, tp: Path | None) -> tuple[str, float, SectionRole]:
        if tp is None:
            return ("[auto] 待打标", 0.5, "development")
        async with sem:
            return await _caption_shot(tp)

    captions = await asyncio.gather(*(_one(i, tp) for i, tp in enumerate(thumb_paths)))

    # 5) 组装 MaterialShot 落库
    shots: list[MaterialShot] = []
    for sh, tp, (cap, density, role) in zip(shots_compact, thumb_paths, captions):
        url = (
            f"/uploads/{project_id}/shots/{material_id}/{tp.name}"
            if tp is not None else None
        )
        shots.append(MaterialShot(
            index=sh.index,
            start=round(sh.start, 3),
            end=round(sh.end, 3),
            duration=round(sh.duration, 3),
            thumbnail_url=url,
            caption=cap,
            action_density=density,
            recommended_role=role,
        ))

    material_store.update(
        project_id, material_id,
        preprocess_status="ready",
        shots=[s.model_dump() for s in shots],
    )
    log.info("[preprocess] done material=%s shots=%d", material_id, len(shots))


def dispatch(project_id: str, material_id: str, video_path: Path) -> None:
    """开异步 task 跑预处理；调用方不需要 await。"""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        log.error("[preprocess] no running loop; cannot dispatch %s", material_id)
        return
    loop.create_task(preprocess_video_material(project_id, material_id, video_path))
