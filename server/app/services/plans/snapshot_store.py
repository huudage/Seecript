"""PlanSnapshotStore：plan 的命名快照（用户主动保存的版本点）。

存储结构：
  var/projects/<project_id>/plan_snapshots/<plan_id>/<snapshot_id>.json
  var/projects/__legacy/plan_snapshots/<plan_id>/<snapshot_id>.json   # 无 project_id 的旧 plan

设计：
- 与 PlanStore 解耦：plan_store 是"最新一版"，本 store 是"历史版本点"，由用户按需触发。
- 每条快照含完整 Plan JSON 副本——磁盘开销以 plan_id 为粒度按需收敛。
- user_id 字段预留：当前无账号系统填 None；后续接入账号后按 user_id 鉴权可见性。
- in-memory 不缓存全量；list 时扫盘对应目录即可（命名快照本身低频，不是热路径）。
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

from ...config import get_settings
from ...schemas import Plan

log = logging.getLogger("seecript.plans.snapshot")

_LEGACY_OWNER = "__legacy"


def _var_root() -> Path:
    settings = get_settings()
    return settings.log_dir.parent / "var"


def _projects_root() -> Path:
    root = _var_root() / "projects"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _snapshot_dir(project_id: Optional[str], plan_id: str) -> Path:
    owner = project_id or _LEGACY_OWNER
    d = _projects_root() / owner / "plan_snapshots" / plan_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


class PlanSnapshotStore:
    def __init__(self) -> None:
        self._lock = threading.RLock()

    def create(
        self,
        plan: Plan,
        *,
        name: str,
        user_id: Optional[str] = None,
    ) -> dict:
        """保存一条命名快照，返回 metadata（不含 plan 体）。"""
        snapshot_id = f"snap-{uuid.uuid4().hex[:10]}"
        ts = time.time()
        record = {
            "snapshot_id": snapshot_id,
            "name": name.strip() or f"未命名 {time.strftime('%H:%M', time.localtime(ts))}",
            "plan_id": plan.plan_id,
            "project_id": plan.project_id,
            "user_id": user_id,
            "ts": ts,
            "plan": plan.model_dump(),
        }
        with self._lock:
            d = _snapshot_dir(plan.project_id, plan.plan_id)
            try:
                _atomic_write_json(d / f"{snapshot_id}.json", record)
            except Exception as exc:  # noqa: BLE001
                log.error("[snapshot] persist failed plan=%s snap=%s: %s",
                          plan.plan_id, snapshot_id, exc)
                raise
        log.info("[snapshot] created plan=%s snap=%s name=%s", plan.plan_id, snapshot_id, name)
        return self._meta(record)

    def list(
        self,
        plan_id: str,
        *,
        project_id: Optional[str],
        user_id: Optional[str] = None,
    ) -> list[dict]:
        """按 plan_id 列出所有快照 metadata，按 ts 倒序（新→旧）。

        当前 user_id 为 None 时不过滤；接入账号后需按 user_id 收紧。
        """
        d = _snapshot_dir(project_id, plan_id)
        items: list[dict] = []
        with self._lock:
            for f in d.glob("*.json"):
                try:
                    raw = json.loads(f.read_text(encoding="utf-8"))
                    if user_id is not None and raw.get("user_id") and raw.get("user_id") != user_id:
                        continue
                    items.append(self._meta(raw))
                except Exception as exc:  # noqa: BLE001
                    log.warning("[snapshot] skip broken %s: %s", f, exc)
        items.sort(key=lambda x: x.get("ts", 0), reverse=True)
        return items

    def get(
        self,
        plan_id: str,
        snapshot_id: str,
        *,
        project_id: Optional[str],
    ) -> Optional[dict]:
        """读单条完整快照（含 plan 体）；找不到返回 None。"""
        f = _snapshot_dir(project_id, plan_id) / f"{snapshot_id}.json"
        if not f.exists():
            return None
        with self._lock:
            try:
                return json.loads(f.read_text(encoding="utf-8"))
            except Exception as exc:  # noqa: BLE001
                log.warning("[snapshot] read %s failed: %s", f, exc)
                return None

    def delete(
        self,
        plan_id: str,
        snapshot_id: str,
        *,
        project_id: Optional[str],
    ) -> bool:
        f = _snapshot_dir(project_id, plan_id) / f"{snapshot_id}.json"
        if not f.exists():
            return False
        with self._lock:
            try:
                f.unlink()
                log.info("[snapshot] deleted plan=%s snap=%s", plan_id, snapshot_id)
                return True
            except Exception as exc:  # noqa: BLE001
                log.warning("[snapshot] delete %s failed: %s", f, exc)
                return False

    @staticmethod
    def _meta(record: dict) -> dict:
        return {
            "snapshot_id": record.get("snapshot_id"),
            "name": record.get("name"),
            "plan_id": record.get("plan_id"),
            "project_id": record.get("project_id"),
            "user_id": record.get("user_id"),
            "ts": record.get("ts"),
        }


plan_snapshot_store = PlanSnapshotStore()
