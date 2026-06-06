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

OOP 重构（stage-20 收尾）：
- 5 步流水线封装到 `VideoPreprocessor` 类，状态（duration / shots_compact /
  thumbnail_paths / captions）作为实例属性，每步是一个私有方法。
- 模块顶层只剩 `dispatch(project_id, material_id, video_path)` 兼容入口，
  内部实例化 VideoPreprocessor 并 fire-and-forget。
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


async def _caption_shot(image_path: Path) -> tuple[str, float, SectionRole]:
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


class VideoPreprocessor:
    """单条视频素材的预处理流水线，5 个 stage 串成一个对象的状态机。

    用法（与旧 preprocess_video_material 等价）：
        await VideoPreprocessor(project_id, material_id, video_path).run()

    设计：
    - 任一步失败立刻把 status='failed' 写回 store 并 return；上层不抛异常
    - 状态属性（duration / shots_compact / thumbnail_paths / captions）按 stage 顺序填充
    - _shots_dir 按 material_id 子目录隔离，避免不同 material 的 shot-XX.jpg 互盖
    """

    SEMAPHORE_LIMIT = 4  # 并发 VLM caption 上限——避免火山速率

    def __init__(self, project_id: str, material_id: str, video_path: Path) -> None:
        self.project_id = project_id
        self.material_id = material_id
        self.video_path = video_path

        self.duration: float | None = None
        self.shots_compact: list[scene_detect.DetectedShot] = []
        self.thumbnail_paths: list[Path | None] = []
        self.captions: list[tuple[str, float, SectionRole]] = []

    # --- 路径辅助 ---
    @property
    def shots_dir(self) -> Path:
        """缩略图按 material_id 子目录隔离：var/uploads/<sid>/shots/<material_id>/."""
        settings = get_settings()
        root = (
            settings.log_dir.parent / "var" / "uploads" / self.project_id
            / "shots" / self.material_id
        )
        root.mkdir(parents=True, exist_ok=True)
        return root

    # --- 状态写回辅助 ---
    def _mark_running(self) -> None:
        material_store.update(self.project_id, self.material_id, preprocess_status="running")

    def _mark_failed(self, reason: str) -> None:
        material_store.update(
            self.project_id, self.material_id,
            preprocess_status="failed",
            preprocess_error=reason,
        )

    # --- 5 个 stage ---
    async def _stage_probe(self) -> bool:
        """ffprobe 时长；同时回填 duration_seconds。失败 → status=failed。"""
        if not self.video_path.exists():
            self._mark_failed("文件不存在")
            return False
        try:
            info = await asyncio.to_thread(ffmpeg_svc.probe, self.video_path)
            duration = float(info.duration_seconds or 0.0)
            if duration > 0.0:
                material_store.update(
                    self.project_id, self.material_id, duration_seconds=duration,
                )
        except (ffmpeg_svc.FFmpegError, FileNotFoundError) as exc:
            log.warning("[preprocess] probe failed material=%s: %s", self.material_id, exc)
            self._mark_failed(f"probe 失败: {exc}")
            return False

        if duration < _MIN_SHOT_SECONDS:
            self._mark_failed("视频时长过短")
            return False
        self.duration = duration
        return True

    async def _stage_detect_shots(self) -> bool:
        """PySceneDetect 切片 + compact。失败/无镜头 → status=failed。"""
        try:
            raw_shots = await asyncio.to_thread(
                scene_detect.detect_shots, str(self.video_path),
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("[preprocess] scene_detect failed material=%s: %s",
                        self.material_id, exc)
            self._mark_failed(f"切片失败: {exc}")
            return False

        if not raw_shots:
            self._mark_failed("未检测出镜头")
            return False

        self.shots_compact = _compact_shots(raw_shots)
        log.info("[preprocess] %s shots %d → %d (after compact)",
                 self.material_id, len(raw_shots), len(self.shots_compact))
        return True

    async def _stage_extract_thumbnails(self) -> None:
        """每片中间帧抽缩略图。ffmpeg 不可用时全部 None；single shot 失败保留 None。"""
        if not ffmpeg_svc.ffmpeg_available():
            log.warning("[preprocess] ffmpeg unavailable; skip thumbnails")
            self.thumbnail_paths = [None] * len(self.shots_compact)
            return
        out: list[Path | None] = []
        for sh in self.shots_compact:
            mid = (sh.start + sh.end) / 2.0
            dst = self.shots_dir / f"shot-{sh.index:02d}.jpg"
            try:
                await asyncio.to_thread(ffmpeg_svc.extract_frame, self.video_path, mid, dst)
                out.append(dst)
            except ffmpeg_svc.FFmpegError as exc:
                log.warning("[preprocess] frame %d failed: %s", sh.index, exc)
                out.append(None)
        self.thumbnail_paths = out

    async def _stage_caption(self) -> None:
        """VLM 并发打标，限速 SEMAPHORE_LIMIT 路。"""
        sem = asyncio.Semaphore(self.SEMAPHORE_LIMIT)

        async def _one(tp: Path | None) -> tuple[str, float, SectionRole]:
            if tp is None:
                return ("[auto] 待打标", 0.5, "development")
            async with sem:
                return await _caption_shot(tp)

        self.captions = await asyncio.gather(
            *(_one(tp) for tp in self.thumbnail_paths)
        )

    def _stage_persist(self) -> None:
        """组装 MaterialShot 写回 store；status=ready。"""
        shots: list[MaterialShot] = []
        for sh, tp, (cap, density, role) in zip(
            self.shots_compact, self.thumbnail_paths, self.captions,
        ):
            url = (
                f"/uploads/{self.project_id}/shots/{self.material_id}/{tp.name}"
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
            self.project_id, self.material_id,
            preprocess_status="ready",
            shots=[s.model_dump() for s in shots],
        )
        log.info("[preprocess] done material=%s shots=%d", self.material_id, len(shots))

    # --- orchestration ---
    async def run(self) -> None:
        """执行 5 个 stage；任一前置 stage 失败立即返回，已经在内部写过 failed 状态。"""
        log.info("[preprocess] start project=%s material=%s path=%s",
                 self.project_id, self.material_id, self.video_path)
        self._mark_running()

        if not await self._stage_probe():
            return
        if not await self._stage_detect_shots():
            return
        await self._stage_extract_thumbnails()
        await self._stage_caption()
        self._stage_persist()


def dispatch(project_id: str, material_id: str, video_path: Path) -> None:
    """开异步 task 跑预处理；调用方不需要 await。"""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        log.error("[preprocess] no running loop; cannot dispatch %s", material_id)
        return
    pre = VideoPreprocessor(project_id, material_id, video_path)
    loop.create_task(pre.run())
