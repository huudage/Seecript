"""Module 3 — 新内容上传 + 多模态 LLM 打标 + MaterialStore 落地。

`POST /api/material/upload`  multipart，落地到 `server/var/uploads/<session_id>/`，
                              做多模态 LLM 打标（tags + recommended_section），
                              结果存进 MaterialStore 供 /gap/detect 反查。

- video：ffmpeg 抽首帧（t=0.5s）→ 给 LLM 看图打标 → 缩略图同时挂到 thumbnail_url
- image：原图直接喂 LLM
- audio：跳过 LLM，给一组 placeholder 标
- 任何 LLM 失败：fallback 到 mock 标，不阻断上传
- 并发：files > 1 时用 asyncio.gather 并行打标

OOP 重构（stage-21）：单次上传请求的状态（project_id / target_dir / video_type /
base_order）封装在 `MaterialUploadService`；router 函数只负责参数校验与拼装响应。
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import uuid
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from ..config import get_settings
from ..schemas import (
    Material,
    MaterialCloneFromSystemRequest,
    MaterialCloneFromSystemResponse,
    MaterialUploadResponse,
    SectionRole,
    VideoType,
    all_role_names,
)
from ..services.llm_client import LLMError, get_llm_client
from ..services.materials import material_store
from ..services.materials.preprocess import dispatch as dispatch_preprocess
from ..services.video.ffmpeg import FFmpegError, extract_frame, ffmpeg_available

log = logging.getLogger("seecript.material")
router = APIRouter()

#: 系统素材库的特殊 project_id。运维通过 `POST /material/upload?project_id=__system__`
#: 往里塞共享素材；任何项目都能 list / clone-from-system。
SYSTEM_PROJECT_ID = "__system__"

# Stage-16：允许 5 模式下任何静态 role 名（step_N/item_N 走正则兜底）。
# 上传时不知道用户最终用哪个 pattern，所以只过滤"明显非法"的字符串。
_ALLOWED_ROLES: tuple[str, ...] = tuple(all_role_names())

_MATERIAL_TAG_SYSTEM = (
    "你是短视频素材打标 Agent。看一帧画面，返回 JSON：\n"
    "{\"tags\": [string]（3-5 个，物体/场景/构图/风格关键词），"
    "\"recommended_section\": string（必须从 allowed_sections 里选一个 role；"
    "若 allowed_sections 含动态后缀如 step_*/item_*，可输出 step_1/item_2 这种带序号形式；"
    "若不确定使用第一个 target_role 作为默认值），"
    "\"highlight_score\": number（0.0-1.0；0.8+ 强冲击/可做开场或峰值，"
    "0.5-0.8 标准镜头适合中段，<0.5 仅 B-roll），"
    "\"highlight_reason\": string（一句话理由：构图/动作/情绪/光线，≤20 字）}。\n"
    "字段名 frame_tags / material_tag 是 mock 路由用，不要漏。"
)


def _placeholder_tags(media_type: str) -> tuple[list[str], SectionRole, float, str]:
    """LLM 不可用 / audio / 调用失败 时的兜底标。返回 (tags, role, highlight_score, highlight_reason).

    role 默认 development（主体段）。
    """
    if media_type == "audio":
        return ["[auto] 音频素材", "[auto] 口播/BGM 候选"], "development", 0.3, "[auto] 音频无画面评分"
    return ["[auto] 待打标", "[auto] 通用素材"], "development", 0.5, "[auto] LLM 不可用，给中位分"


class MaterialUploadService:
    """单次 upload 请求的处理器。

    一批 files 共享 project_id / video_type / target_dir / base_order，
    所以这些状态作为实例属性，避免在每个内部函数里反复透传。

    流水线：校验 → 落盘 → 缩略图 → LLM 打标 → Material → 写 store → 调度视频预处理。
    """

    ALLOWED_VIDEO = {"video/mp4", "video/quicktime", "video/webm"}
    ALLOWED_IMAGE = {"image/jpeg", "image/png", "image/webp"}
    ALLOWED_AUDIO = {"audio/mpeg", "audio/wav", "audio/x-wav"}
    MAX_BYTES = 50 * 1024 * 1024  # 单文件 50MB 硬上限

    def __init__(self, project_id: str, video_type: VideoType) -> None:
        self.project_id = project_id
        self.video_type = video_type
        self.target_dir = self._uploads_root() / project_id
        self.target_dir.mkdir(parents=True, exist_ok=True)
        # 已有素材数 → 新批次的 sort_order 起点
        self._base_order = len(material_store.list(project_id))

    # --- 路径辅助 ---
    @staticmethod
    def _uploads_root() -> Path:
        settings = get_settings()
        root = settings.log_dir.parent / "var" / "uploads"
        root.mkdir(parents=True, exist_ok=True)
        return root

    # --- 类型识别 ---
    @classmethod
    def detect_media_type(cls, content_type: str | None) -> str | None:
        if not content_type:
            return None
        if content_type in cls.ALLOWED_VIDEO:
            return "video"
        if content_type in cls.ALLOWED_IMAGE:
            return "image"
        if content_type in cls.ALLOWED_AUDIO:
            return "audio"
        return None

    # --- LLM 打标 ---
    async def _tag_with_llm(
        self,
        image_path: Path,
        media_type: str,
    ) -> tuple[list[str], SectionRole, float, str]:
        """单帧 → LLM 多模态打标。失败回落 placeholder。"""
        user_text = (
            f"video_type={self.video_type}\n"
            f"allowed_sections={list(_ALLOWED_ROLES)}\n"
            f"media_type={media_type}\n"
            "请按 system 中的 schema 返回 JSON，highlight_score 必须给一个 0.0-1.0 的数。"
        )
        try:
            client = get_llm_client()
            text = await client.complete_multimodal(
                _MATERIAL_TAG_SYSTEM, user_text, [image_path],
            )
        except LLMError as exc:
            log.warning("[material] LLM tagging failed (%s) → placeholder", exc)
            return _placeholder_tags(media_type)
        except Exception as exc:  # noqa: BLE001
            log.warning("[material] LLM tagging unexpected error: %s → placeholder", exc)
            return _placeholder_tags(media_type)

        try:
            data = json.loads(text) if text.strip().startswith(("{", "[")) else None
        except json.JSONDecodeError:
            data = None
        if not isinstance(data, dict):
            return _placeholder_tags(media_type)

        # 兼容两种形态：{tags, recommended_section, ...} 或 mock 的 {frame_tags: [{...}]}
        raw_tags: list = []
        raw_section: Optional[str] = None
        raw_score: Any = None
        raw_reason: Optional[str] = None
        if isinstance(data.get("tags"), list):
            raw_tags = data["tags"]
            raw_section = data.get("recommended_section")
            raw_score = data.get("highlight_score")
            raw_reason = data.get("highlight_reason")
        elif isinstance(data.get("frame_tags"), list) and data["frame_tags"]:
            first = data["frame_tags"][0]
            if isinstance(first, dict):
                raw_tags = first.get("tags") or []
                raw_section = first.get("recommended_section")
                raw_score = first.get("highlight_score")
                raw_reason = first.get("highlight_reason")

        tags = [str(t)[:30] for t in raw_tags if t][:5]
        if not tags:
            return _placeholder_tags(media_type)

        # role 校验：必须是 17 个静态 role 之一或 step_N/item_N 形式；否则回落 development
        role: SectionRole = "development"
        if isinstance(raw_section, str):
            cleaned = re.sub(r"^(step|item)\s*0*(\d+)$", r"\1_\2", raw_section.strip())
            if cleaned in _ALLOWED_ROLES or re.match(r"^(step|item)_\d+$", cleaned):
                role = cleaned  # type: ignore[assignment]

        try:
            score = float(raw_score) if raw_score is not None else 0.5
        except (TypeError, ValueError):
            score = 0.5
        score = max(0.0, min(1.0, score))
        reason = str(raw_reason)[:60] if isinstance(raw_reason, str) and raw_reason.strip() else "LLM 未给理由"
        return tags, role, score, reason

    # --- 单文件流水线 ---
    async def _save_file(self, file: UploadFile, media_type: str) -> tuple[str, str, Path]:
        """读 bytes → 落盘 → 返回 (material_id, safe_name, dest)."""
        data = await file.read()
        if len(data) > self.MAX_BYTES:
            raise HTTPException(status_code=413, detail=f"{file.filename} exceeds 50MB")
        safe_name = Path(file.filename or "unnamed").name
        material_id = uuid.uuid4().hex[:12]
        dest = self.target_dir / f"{material_id}_{safe_name}"
        dest.write_bytes(data)
        log.info("[material] session=%s saved %s (%d bytes, %s)",
                 self.project_id, dest.name, len(data), media_type)
        return material_id, safe_name, dest

    async def _make_thumbnail(
        self, dest: Path, material_id: str, media_type: str,
    ) -> tuple[Optional[Path], Optional[str]]:
        """image 用自身；video 抽首帧；audio 无缩略图。返回 (path, url)."""
        if media_type == "image":
            return dest, f"/uploads/{self.project_id}/{dest.name}"
        if media_type == "video":
            if not ffmpeg_available():
                log.info("[material] ffmpeg unavailable; skip video thumbnail for %s", dest.name)
                return None, None
            thumb = self.target_dir / f"{material_id}_thumb.jpg"
            try:
                await asyncio.to_thread(extract_frame, dest, 0.5, thumb)
                return thumb, f"/uploads/{self.project_id}/{thumb.name}"
            except FFmpegError as exc:
                log.warning("[material] extract_frame failed for %s: %s", dest.name, exc)
                return None, None
        return None, None

    async def _build_one(self, file: UploadFile, idx: int) -> Material:
        """单文件全流程：校验 → 落盘 → 缩略图 → 打标 → Material。"""
        media_type = self.detect_media_type(file.content_type)
        if media_type is None:
            raise HTTPException(
                status_code=415, detail=f"unsupported content-type: {file.content_type}",
            )
        material_id, safe_name, dest = await self._save_file(file, media_type)
        thumbnail_path, thumbnail_url = await self._make_thumbnail(dest, material_id, media_type)

        if thumbnail_path is not None and media_type in ("image", "video"):
            tags, role, score, reason = await self._tag_with_llm(thumbnail_path, media_type)
        else:
            tags, role, score, reason = _placeholder_tags(media_type)

        return Material(
            material_id=material_id,
            filename=safe_name,
            media_type=media_type,  # type: ignore[arg-type]
            duration_seconds=None,
            thumbnail_url=thumbnail_url,
            file_url=f"/uploads/{self.project_id}/{dest.name}",
            tags=tags,
            recommended_section=role,
            highlight_score=score,
            highlight_reason=reason,
            sort_order=self._base_order + idx,
            preprocess_status="pending" if media_type == "video" else "skipped",
        )

    # --- 调度视频预处理 ---
    def _dispatch_video_preprocess(self, materials: list[Material]) -> None:
        for m in materials:
            if m.media_type != "video":
                continue
            local = self.target_dir / f"{m.material_id}_{m.filename}"
            try:
                dispatch_preprocess(self.project_id, m.material_id, local)
            except Exception as exc:  # noqa: BLE001
                log.warning("[material] dispatch preprocess failed material=%s: %s",
                            m.material_id, exc)

    # --- 入口 ---
    async def upload_all(self, files: list[UploadFile]) -> list[Material]:
        """并发处理一批 file，写 store，调度视频预处理。"""
        tasks = [self._build_one(f, idx) for idx, f in enumerate(files)]
        materials = await asyncio.gather(*tasks)
        material_store.put(self.project_id, list(materials))
        self._dispatch_video_preprocess(list(materials))
        return list(materials)


@router.post("/material/upload", response_model=MaterialUploadResponse)
async def upload_material(
    files: list[UploadFile] = File(...),
    project_id: str | None = Form(default=None),
    session_id: str | None = Form(default=None),  # 兼容老前端：等价 project_id
    video_type: VideoType = Form(default="marketing"),
) -> MaterialUploadResponse:
    if not files:
        raise HTTPException(status_code=400, detail="no files")
    # v2 起 session_id == project_id；老前端只传 session_id 时仍可工作，
    # 但不再 mint 随机 sid——必须有显式 project_id / session_id，避免跨项目串货。
    sid = (project_id or session_id or "").strip()
    if not sid:
        raise HTTPException(status_code=400, detail="project_id 必填（session_id 作为兼容别名亦可）")

    service = MaterialUploadService(sid, video_type)
    materials = await service.upload_all(files)
    return MaterialUploadResponse(session_id=sid, materials=materials)


@router.get("/material/{material_id}/preprocess", response_model=Material)
async def get_material_preprocess(material_id: str, project_id: str) -> Material:
    """前端轮询视频预处理进度（preprocess_status / shots）。

    project_id 必填——MaterialStore 按 project 分区，跨 project 不允许查询。
    返回 200 + 完整 Material；status 字段是 pending / running / ready / failed / skipped。
    """
    sid = project_id.strip()
    if not sid:
        raise HTTPException(status_code=400, detail="project_id 必填")
    m = material_store.get(sid, material_id)
    if m is None:
        raise HTTPException(status_code=404, detail=f"material {material_id} not found in project {sid}")
    return m


@router.get("/material", response_model=list[Material])
async def list_materials(project_id: str) -> list[Material]:
    """列出某 project 已上传的素材。

    刷新页面后前端用它回灌素材库（in-memory MaterialStore，进程重启清空——本期接受）。
    project_id 在 store 内即 session_id 别名（详见 upload_material）。
    """
    sid = project_id.strip()
    if not sid:
        raise HTTPException(status_code=400, detail="project_id 必填")
    return material_store.list(sid)


def _uploads_root() -> Path:
    """复用 MaterialUploadService 的 uploads 根目录路由。"""
    settings = get_settings()
    root = settings.log_dir.parent / "var" / "uploads"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _clone_one_file(src_url: str, src_root: Path, dst_dir: Path, new_id: str, suffix: str) -> Optional[str]:
    """src_url 形如 /uploads/__system__/<id>_<file.mp4>；按文件名找回源文件再复制到目标项目。

    返回新文件的 file_url（同 /uploads/<dst_project>/<new_id>_xxx）；找不到源时返 None。
    """
    if not src_url:
        return None
    name = Path(src_url).name
    src_path = src_root / name
    if not src_path.exists():
        log.warning("[material/clone] missing source file: %s", src_path)
        return None
    dst_name = f"{new_id}_{suffix}" if suffix else f"{new_id}_{name}"
    dst_path = dst_dir / dst_name
    try:
        shutil.copy2(src_path, dst_path)
    except Exception as exc:  # noqa: BLE001
        log.warning("[material/clone] copy %s → %s failed: %s", src_path, dst_path, exc)
        return None
    return f"/uploads/{dst_dir.name}/{dst_path.name}"


@router.post("/material/clone-from-system", response_model=MaterialCloneFromSystemResponse)
async def clone_from_system(req: MaterialCloneFromSystemRequest) -> MaterialCloneFromSystemResponse:
    """从系统素材库克隆若干素材到目标项目。

    流程：
    1. 校验目标 project_id，源 project_id 固定 __system__
    2. 对每个 source_material_id：material_store.get → 复制原文件 + 缩略图到目标 uploads
    3. 铸新 material_id，复制 tags / recommended_section / shots / preprocess_status 等元数据
    4. 一并 put 到目标 store；视频类型已 ready 的不再触发 preprocess（已经预处理过）
    """
    target_project = req.project_id.strip()
    if not target_project or target_project == SYSTEM_PROJECT_ID:
        raise HTTPException(status_code=400, detail="目标 project_id 非法（不能为空或 __system__）")
    if not req.source_material_ids:
        raise HTTPException(status_code=400, detail="source_material_ids 必填且非空")

    uploads_root = _uploads_root()
    src_dir = uploads_root / SYSTEM_PROJECT_ID
    dst_dir = uploads_root / target_project
    dst_dir.mkdir(parents=True, exist_ok=True)

    base_order = len(material_store.list(target_project))
    created: list[Material] = []
    skipped: list[str] = []

    for offset, src_id in enumerate(req.source_material_ids):
        src = material_store.get(SYSTEM_PROJECT_ID, src_id)
        if src is None:
            skipped.append(src_id)
            continue
        new_id = uuid.uuid4().hex[:12]
        new_file_url = _clone_one_file(
            src.file_url or "", src_dir, dst_dir, new_id, src.filename,
        )
        if not new_file_url:
            skipped.append(src_id)
            continue
        # 缩略图（视频和 image 都可能有；image 的 thumbnail_url == file_url，不重复复制）
        new_thumb_url: Optional[str] = None
        if src.thumbnail_url:
            if src.thumbnail_url == src.file_url:
                new_thumb_url = new_file_url
            else:
                # 视频缩略图通常以 _thumb.jpg 结尾；直接按文件名复制
                thumb_name = Path(src.thumbnail_url).name
                thumb_src = src_dir / thumb_name
                if thumb_src.exists():
                    thumb_dst = dst_dir / f"{new_id}_thumb.jpg"
                    try:
                        shutil.copy2(thumb_src, thumb_dst)
                        new_thumb_url = f"/uploads/{target_project}/{thumb_dst.name}"
                    except Exception as exc:  # noqa: BLE001
                        log.warning("[material/clone] thumb copy failed: %s", exc)

        cloned = src.model_copy(update={
            "material_id": new_id,
            "file_url": new_file_url,
            "thumbnail_url": new_thumb_url,
            "sort_order": base_order + offset,
        })
        created.append(cloned)

    if created:
        material_store.put(target_project, created)
        log.info("[material/clone] %s ← __system__ cloned=%d skipped=%d",
                 target_project, len(created), len(skipped))

    return MaterialCloneFromSystemResponse(
        project_id=target_project,
        materials=created,
        skipped=skipped,
    )
