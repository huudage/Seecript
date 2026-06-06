"""ProjectStore：JSON 文件持久化 + 内存索引 + 启动扫盘。

存储结构：
  var/projects/<project_id>/
    project.json        # Project 模型唯一可信源
    plans/<plan_id>.json     # Step 3 落 PlanStore
    gaps/<plan_id>.json      # Step 3 落 GapStore
    materials/index.json     # Step 3 落 MaterialStore

设计：
- 单进程 + RLock 即可；多 worker 上 gunicorn 时必须先换 SQLite/Postgres
- 任何 mutate → 先改内存、再原子写盘（先写 .tmp 再 os.replace 覆盖）
- delete 级联清理 var/uploads/<id>/、var/assets/<id>/

# 生产部署路线图
JSON 文件实现适合本地 / 小规模 demo。生产部署按下面替换：
1. 新建 store_pg.py：实现同样 5 个方法签名（create / get / update / list / delete），
   背后 SQLAlchemy + asyncpg；Project Pydantic .model_dump() 直接入 JSONB 列
2. var/uploads/ 与 var/assets/ 切 S3：boto3 上传后把 file_url 改成 CDN 拼接
3. var/outputs/ 切 S3；/outputs 挂载改 CDN 直链或 nginx 反代
4. main.py 启动按 STORAGE_BACKEND=json|postgres env 选 store 实现
路由层和前端契约零改动。
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from ...config import get_settings
from ...schemas import Project, ProjectStatus, ReferenceVersion

log = logging.getLogger("seecript.projects")


class ProjectStoreError(Exception):
    """项目存储异常基类。"""


class ProjectNotFoundError(ProjectStoreError):
    """指定 project_id 不存在。"""


def _var_root() -> Path:
    """server/var 根目录，与 AssetStore 用同样的锚点（log_dir 的父目录）。"""
    settings = get_settings()
    return settings.log_dir.parent / "var"


def _projects_root() -> Path:
    root = _var_root() / "projects"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _project_dir(project_id: str) -> Path:
    return _projects_root() / project_id


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """原子写：先写 .tmp 再 rename，避免崩溃留半截 JSON。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _new_project_id() -> str:
    return uuid.uuid4().hex[:12]


class ProjectStore:
    """线程安全的 Project 注册表。

    - in-memory `_by_id` 是热路径
    - project.json 是冷备份；启动时扫盘重建内存
    - 所有 mutate 必须持锁
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._by_id: dict[str, Project] = {}
        self._load()

    # ---------- 持久化 ----------
    def _project_json_path(self, project_id: str) -> Path:
        return _project_dir(project_id) / "project.json"

    def _persist(self, project: Project) -> None:
        _atomic_write_json(self._project_json_path(project.project_id), project.model_dump())

    def _load(self) -> None:
        """启动扫描 var/projects/*/project.json 重建索引。"""
        root = _projects_root()
        loaded = 0
        for child in root.iterdir() if root.exists() else []:
            if not child.is_dir():
                continue
            # 跳过 __legacy 等保留目录的占位（仍允许被显式 get）
            manifest = child / "project.json"
            if not manifest.exists():
                continue
            try:
                raw = json.loads(manifest.read_text(encoding="utf-8"))
                project = self._project_from_dict(raw)
                self._by_id[project.project_id] = project
                loaded += 1
            except Exception as exc:  # noqa: BLE001
                log.warning("[projects] skip broken manifest %s: %s", manifest, exc)
        log.info("[projects] loaded %d project(s) from %s", loaded, root)

    @staticmethod
    def _project_from_dict(raw: dict) -> Project:
        """stage-15 兼容层：把旧版 `sample_ids: list[str]` 或更早的 `sample_id: str` 升级为
        `reference_versions`。

        旧项目落盘时还没有版本槽概念，sample_ids 隐式指向 active 槽。本方法在启动加载时
        懒迁移：按当前 active slot 回填，原字段移到 `_legacy_sample_ids` 保留备份。
        找不到 active slot（样例被删 / 未拆解）→ 该 sid 跳过，结果可能为空（项目会被 schema 拒绝）。
        """
        if "reference_versions" in raw and raw.get("reference_versions"):
            return Project.model_validate(raw)
        legacy_ids: list[str] = []
        sids = raw.get("sample_ids")
        if isinstance(sids, list) and sids:
            legacy_ids = [s for s in sids if isinstance(s, str)]
        else:
            sid_one = raw.get("sample_id")
            if isinstance(sid_one, str) and sid_one:
                legacy_ids = [sid_one]
        if legacy_ids:
            from ..library import manifest_store
            refs: list[dict] = []
            for sid in legacy_ids:
                active = manifest_store.get_active_slot(sid)
                if active:
                    refs.append({"sample_id": sid, "slot_id": active})
            raw["reference_versions"] = refs
            raw["_legacy_sample_ids"] = legacy_ids
            raw.pop("sample_ids", None)
            raw.pop("sample_id", None)
        return Project.model_validate(raw)

    # ---------- CRUD ----------
    def create(
        self,
        name: str,
        reference_versions: list[ReferenceVersion] | None = None,
        *,
        sample_ids: list[str] | None = None,
        video_type: Optional[str] = None,
    ) -> Project:
        """新建项目。

        - 主入口：传 `reference_versions=[ReferenceVersion(sample_id, slot_id), ...]`，0-2 个均可
          （为空表示「未选样例」态——前端在 Decompose 页选定后回写）
        - 兼容入口：传 `sample_ids=[sid, ...]`，按当时 active slot 反查填进 reference_versions
          （供老测试和迁移期代码使用）。两个都传时以 reference_versions 为准。
        - `video_type`：建项目时即定的视频种类，前端按它过滤样例。
        """
        if not reference_versions and sample_ids:
            from ..library import manifest_store
            resolved: list[ReferenceVersion] = []
            for sid in sample_ids:
                active = manifest_store.get_active_slot(sid)
                if active:
                    resolved.append(ReferenceVersion(sample_id=sid, slot_id=active))
                else:
                    # 还没拆解 → 用占位 slot_id,后续测试 / 调用方按需重写
                    resolved.append(ReferenceVersion(sample_id=sid, slot_id="legacy"))
            reference_versions = resolved
        # reference_versions 允许为空（建项目可只选种类）
        reference_versions = list(reference_versions or [])
        with self._lock:
            now = time.time()
            project = Project(
                project_id=_new_project_id(),
                name=name,
                video_type=video_type,
                reference_versions=reference_versions,
                created_at=now,
                updated_at=now,
            )
            self._by_id[project.project_id] = project
            self._persist(project)
            log.info(
                "[projects] created %s name=%r video_type=%s refs=%s",
                project.project_id, name, video_type,
                [(rv.sample_id, rv.slot_id) for rv in reference_versions],
            )
            return project

    def get(self, project_id: str) -> Optional[Project]:
        with self._lock:
            return self._by_id.get(project_id)

    def require(self, project_id: str) -> Project:
        proj = self.get(project_id)
        if proj is None:
            raise ProjectNotFoundError(project_id)
        return proj

    def list(self) -> list[Project]:
        """按 updated_at 倒序，给首页项目网格用。"""
        with self._lock:
            items = list(self._by_id.values())
        items.sort(key=lambda p: p.updated_at, reverse=True)
        return items

    def update(self, project_id: str, **fields: Any) -> Project:
        """部分字段更新。None 值视为 '不动'。"""
        with self._lock:
            project = self.require(project_id)
            data = project.model_dump()
            changed = False
            for key, val in fields.items():
                if val is None:
                    continue
                if key not in data:
                    raise ProjectStoreError(f"未知字段 {key}")
                if data[key] != val:
                    data[key] = val
                    changed = True
            if changed:
                data["updated_at"] = time.time()
                project = Project.model_validate(data)
                self._by_id[project_id] = project
                self._persist(project)
                log.info("[projects] updated %s fields=%s", project_id, sorted(fields.keys()))
            return project

    def touch(self, project_id: str) -> None:
        """仅刷新 updated_at（首页排序用）。不持久化失败也不抛。"""
        try:
            with self._lock:
                project = self._by_id.get(project_id)
                if project is None:
                    return
                project = project.model_copy(update={"updated_at": time.time()})
                self._by_id[project_id] = project
                self._persist(project)
        except Exception as exc:  # noqa: BLE001
            log.warning("[projects] touch %s failed: %s", project_id, exc)

    def delete(self, project_id: str) -> None:
        """级联删除：var/projects/<id>/、var/uploads/<id>/、var/assets/<id>/，
        以及 asset_store / material_store / plan_store / gap_store 的内存状态。

        三个目录中任何一个不存在都视为正常；都成功才算 delete 完成。
        """
        with self._lock:
            self._by_id.pop(project_id, None)
        # 晚 import 避免循环依赖（assets/materials/plans 反过来都可能引用 projects）
        try:
            from ..assets import asset_store
            asset_store._states.pop(project_id, None)
            asset_store._owner_by_asset = {
                aid: owner for aid, owner in asset_store._owner_by_asset.items()
                if owner != project_id
            }
        except Exception as exc:  # noqa: BLE001
            log.warning("[projects] cascade asset_store evict %s failed: %s", project_id, exc)
        try:
            from ..materials.store import material_store, gap_store
            material_store._by_session.pop(project_id, None)
            # gap_store 是按 plan_id 索引的；evict 所有 project_id 匹配的 plan bucket
            evicted_plans = [
                plan_id for plan_id, gaps in gap_store._by_plan.items()
                if gaps and gaps[0].project_id == project_id
            ]
            for plan_id in evicted_plans:
                gaps = gap_store._by_plan.pop(plan_id, [])
                for g in gaps:
                    gap_store._by_gap_id.pop(g.gap_id, None)
        except Exception as exc:  # noqa: BLE001
            log.warning("[projects] cascade material/gap evict %s failed: %s", project_id, exc)
        try:
            from ..plans.store import plan_store
            evicted_plans = [
                pid for pid, plan in plan_store._plans.items()
                if plan.project_id == project_id
            ]
            for pid in evicted_plans:
                plan_store._plans.pop(pid, None)
        except Exception as exc:  # noqa: BLE001
            log.warning("[projects] cascade plan_store evict %s failed: %s", project_id, exc)
        var = _var_root()
        for sub in ("projects", "uploads", "assets"):
            target = var / sub / project_id
            if target.exists():
                try:
                    shutil.rmtree(target)
                    log.info("[projects] removed %s", target)
                except Exception as exc:  # noqa: BLE001
                    log.error("[projects] failed to remove %s: %s", target, exc)
                    raise

    # ---------- 状态推进辅助 ----------
    def mark_planned(self, project_id: str, plan_id: str) -> None:
        """Plan 构建完后调用：last_plan_id 与 status 一起写进去。"""
        try:
            self.update(project_id, last_plan_id=plan_id, status="planned")
        except ProjectStoreError as exc:
            log.warning("[projects] mark_planned %s 失败: %s", project_id, exc)

    def mark_rendered(self, project_id: str, render_job_id: str) -> None:
        try:
            self.update(project_id, last_render_job_id=render_job_id, status="rendered")
        except ProjectStoreError as exc:
            log.warning("[projects] mark_rendered %s 失败: %s", project_id, exc)


project_store = ProjectStore()
