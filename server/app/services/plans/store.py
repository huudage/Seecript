"""PlanStore：内存 dict + JSON 落盘（按 project_id 分区）。

存储结构：
  var/projects/<project_id>/plans/<plan_id>.json
  var/projects/__legacy/plans/<plan_id>.json   # 无 project_id 的旧 plan

设计：
- in-memory `_plans` 是热路径；扫盘只在启动时做
- put/replace → 写盘 + 内存更新；get → miss 时不再回扫（启动已经全量加载）
- plan 不允许跨 project_id 移动；若同 plan_id 复出且 project_id 变了，记 warning 仍写盘
"""
from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Optional

from ...config import get_settings
from ...schemas import Plan

log = logging.getLogger("seecript.plans")

_LEGACY_OWNER = "__legacy"


def _var_root() -> Path:
    settings = get_settings()
    return settings.log_dir.parent / "var"


def _projects_root() -> Path:
    root = _var_root() / "projects"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _plans_dir(project_id: Optional[str]) -> Path:
    owner = project_id or _LEGACY_OWNER
    d = _projects_root() / owner / "plans"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


class PlanStore:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._plans: dict[str, Plan] = {}
        self._load()

    def _load(self) -> None:
        """启动扫盘：var/projects/*/plans/*.json 全量进内存。"""
        root = _projects_root()
        if not root.exists():
            return
        loaded = 0
        for owner_dir in root.iterdir():
            plans_dir = owner_dir / "plans"
            if not plans_dir.exists():
                continue
            for f in plans_dir.glob("*.json"):
                try:
                    plan = Plan.model_validate_json(f.read_text(encoding="utf-8"))
                    self._plans[plan.plan_id] = plan
                    loaded += 1
                except Exception as exc:  # noqa: BLE001
                    log.warning("[plans] skip broken plan %s: %s", f, exc)
        log.info("[plans] loaded %d plan(s) from disk", loaded)

    def put(self, plan: Plan) -> None:
        with self._lock:
            self._plans[plan.plan_id] = plan
            path = _plans_dir(plan.project_id) / f"{plan.plan_id}.json"
            try:
                _atomic_write_json(path, plan.model_dump())
            except Exception as exc:  # noqa: BLE001
                log.error("[plans] persist %s failed: %s", plan.plan_id, exc)
        log.info("[plan] stored plan_id=%s sample=%s scenes=%d project=%s",
                 plan.plan_id, plan.sample_id, len(plan.main_track), plan.project_id or _LEGACY_OWNER)

    def get(self, plan_id: str) -> Optional[Plan]:
        with self._lock:
            return self._plans.get(plan_id)

    def all_ids(self) -> list[str]:
        with self._lock:
            return list(self._plans.keys())

    def list_by_project(self, project_id: str) -> list[Plan]:
        """返回该 project 下所有 plans，按 plan_id 时间倒序（plan_id 含 uuid，没真时间戳，按字母倒序近似）。"""
        with self._lock:
            items = [p for p in self._plans.values() if p.project_id == project_id]
        items.sort(key=lambda p: p.plan_id, reverse=True)
        return items

    def replace(self, plan: Plan) -> None:
        """编辑场景：以同 plan_id 覆盖（含落盘）。"""
        self.put(plan)


plan_store = PlanStore()
