"""MaterialStore + GapStore：内存 dict + JSON 落盘（按 project_id 分区）。

存储结构：
  var/projects/<project_id>/materials/index.json   # session_id == project_id
  var/projects/<project_id>/gaps/<plan_id>.json    # GapStore；project_id 从 Gap.project_id 取
  var/projects/__legacy/...                        # 无 project_id 的旧数据

设计：
- 兼容老前端：session_id 现在等价于 project_id；MaterialStore 仍以"session_id"为 key
- GapStore：plan_id → [Gap]，每个 plan_id 一个 json 文件；落盘时按 gap.project_id 分目录
  （同一 plan 的所有 gap 必然来自同一 project，取第一个即可）
"""
from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Optional

from ...config import get_settings
from ...schemas import Gap, Material

log = logging.getLogger("seecript.materials")

_LEGACY_OWNER = "__legacy"


def _var_root() -> Path:
    settings = get_settings()
    return settings.log_dir.parent / "var"


def _projects_root() -> Path:
    root = _var_root() / "projects"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _materials_index(session_id: str) -> Path:
    owner = session_id or _LEGACY_OWNER
    d = _projects_root() / owner / "materials"
    d.mkdir(parents=True, exist_ok=True)
    return d / "index.json"


def _gaps_dir(project_id: Optional[str]) -> Path:
    owner = project_id or _LEGACY_OWNER
    d = _projects_root() / owner / "gaps"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _atomic_write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


class MaterialStore:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._by_session: dict[str, list[Material]] = {}
        self._load()

    def _load(self) -> None:
        root = _projects_root()
        if not root.exists():
            return
        loaded = 0
        for owner_dir in root.iterdir():
            idx = owner_dir / "materials" / "index.json"
            if not idx.exists():
                continue
            try:
                raw = json.loads(idx.read_text(encoding="utf-8"))
                items = [Material.model_validate(m) for m in raw]
                self._by_session[owner_dir.name] = items
                loaded += len(items)
            except Exception as exc:  # noqa: BLE001
                log.warning("[materials] skip broken index %s: %s", idx, exc)
        log.info("[materials] loaded %d material(s) from disk", loaded)

    def _persist(self, session_id: str) -> None:
        items = self._by_session.get(session_id, [])
        try:
            _atomic_write_json(_materials_index(session_id), [m.model_dump() for m in items])
        except Exception as exc:  # noqa: BLE001
            log.error("[materials] persist %s failed: %s", session_id, exc)

    def put(self, session_id: str, materials: list[Material]) -> None:
        """追加（不覆盖）—— upload 端点支持分批传，新批次接在原 list 末尾。"""
        with self._lock:
            existing = self._by_session.setdefault(session_id, [])
            existing.extend(materials)
            total = len(existing)
            self._persist(session_id)
        log.info("[materials] session=%s appended=%d total=%d",
                 session_id, len(materials), total)

    def list(self, session_id: str) -> list[Material]:
        with self._lock:
            return list(self._by_session.get(session_id, []))

    def remove(self, session_id: str, material_id: str) -> bool:
        with self._lock:
            items = self._by_session.get(session_id)
            if not items:
                return False
            before = len(items)
            kept = [m for m in items if m.material_id != material_id]
            self._by_session[session_id] = kept
            removed = len(kept) < before
            if removed:
                self._persist(session_id)
            return removed

    def get(self, session_id: str, material_id: str) -> Optional[Material]:
        with self._lock:
            for m in self._by_session.get(session_id, []):
                if m.material_id == material_id:
                    return m
            return None

    def update(self, session_id: str, material_id: str, **fields) -> Optional[Material]:
        """原地更新一条素材的指定字段（部分字段补丁式合并），并落盘。

        用于视频预处理：preprocess 后写回 preprocess_status / shots / duration_seconds 等。
        未知字段忽略；未命中 material_id 时返回 None。
        """
        with self._lock:
            items = self._by_session.get(session_id) or []
            for i, m in enumerate(items):
                if m.material_id != material_id:
                    continue
                data = m.model_dump()
                data.update({k: v for k, v in fields.items() if v is not None})
                updated = Material.model_validate(data)
                items[i] = updated
                self._persist(session_id)
                return updated
            return None


class GapStore:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._by_plan: dict[str, list[Gap]] = {}
        self._by_gap_id: dict[str, Gap] = {}
        self._load()

    def _load(self) -> None:
        root = _projects_root()
        if not root.exists():
            return
        loaded_plans = 0
        for owner_dir in root.iterdir():
            gaps_dir = owner_dir / "gaps"
            if not gaps_dir.exists():
                continue
            for f in gaps_dir.glob("*.json"):
                try:
                    raw = json.loads(f.read_text(encoding="utf-8"))
                    gaps = [Gap.model_validate(g) for g in raw]
                    plan_id = f.stem
                    self._by_plan[plan_id] = gaps
                    for g in gaps:
                        self._by_gap_id[g.gap_id] = g
                    loaded_plans += 1
                except Exception as exc:  # noqa: BLE001
                    log.warning("[gaps] skip broken file %s: %s", f, exc)
        log.info("[gaps] loaded %d plan-bucket(s) from disk", loaded_plans)

    def _persist(self, plan_id: str, project_id: Optional[str]) -> None:
        gaps = self._by_plan.get(plan_id, [])
        try:
            path = _gaps_dir(project_id) / f"{plan_id}.json"
            _atomic_write_json(path, [g.model_dump() for g in gaps])
        except Exception as exc:  # noqa: BLE001
            log.error("[gaps] persist plan=%s failed: %s", plan_id, exc)

    def put(self, plan_id: str, gaps: list[Gap]) -> None:
        # 覆盖该 plan 的旧 gap，但全局 gap_id → Gap 字典保持累加
        # （rerank/copy/aigc 链路里 detect 重发是常态，fill 还能查到上一次的）
        with self._lock:
            self._by_plan[plan_id] = list(gaps)
            for g in gaps:
                self._by_gap_id[g.gap_id] = g
            # 取第一个 gap 的 project_id 作为本 plan 的归属
            project_id = gaps[0].project_id if gaps else None
            self._persist(plan_id, project_id)
        log.info("[gaps] stored plan_id=%s gaps=%d", plan_id, len(gaps))

    def list_by_plan(self, plan_id: str) -> list[Gap]:
        with self._lock:
            return list(self._by_plan.get(plan_id, []))

    def get(self, gap_id: str) -> Optional[Gap]:
        with self._lock:
            return self._by_gap_id.get(gap_id)


material_store = MaterialStore()
gap_store = GapStore()
