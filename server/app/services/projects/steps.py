"""StepStore：每步产物快照 JSON 落盘 + 联动 Project.step_states。

存储结构：
  var/projects/<project_id>/steps/<step>.json    # StepSnapshot 序列化

设计：
- 「下一步」= commit = step_store.save(project_id, snapshot)
- save 内部：写 step.json + 调 project_store.update 把状态机推到 (current_step=该步,
  step_states[该步]=saved, 下游 saved→dirty / pending 保持)
- 回退到老步骤（GET /step/<name>）不做任何 mutate，仅返回最近一次 snapshot
- 「保留下游」语义：下游 plan/gap/render 文件不删，只把 step_states 改 dirty 提示视觉

启动时不预扫盘——StepStore 状态全部来自 Project.step_states 自身（落在 project.json
里）；步骤 payload 只在前端进对应页面 GET 时按需读盘。这样无须维护额外内存索引。
"""
from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Optional

from ...schemas import (
    Project,
    ProjectStepState,
    StepName,
    StepSnapshot,
    StepStatus,
)
from .store import _atomic_write_json, _project_dir, project_store

log = logging.getLogger("seecript.projects.steps")


# 步骤顺序——前端 STEP_ORDER 必须与此一致。
STEP_ORDER: tuple[StepName, ...] = ("library", "decompose", "compose", "render")


def _steps_dir(project_id: str) -> Path:
    d = _project_dir(project_id) / "steps"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _step_path(project_id: str, step: StepName) -> Path:
    return _steps_dir(project_id) / f"{step}.json"


def _next_status_after_commit(prev: StepStatus) -> StepStatus:
    """新提交时下游各步的状态过渡：
    - 之前 saved → dirty（提示「上游变了，建议刷新」，但产物保留）
    - 其它（pending/in_progress/dirty）→ pending（清回未开始；in_progress 不会出现在下游因
      为线性推进只允许一个 in_progress）
    """
    if prev == "saved":
        return "dirty"
    return "pending"


def _build_new_state(
    current: ProjectStepState,
    committed: StepName,
) -> tuple[ProjectStepState, StepName]:
    """根据本次 commit 的步骤推导新 step_states 与 current_step。

    被 commit 的步骤 → saved；其下游全部按 _next_status_after_commit 退档；
    current_step 推进到下一步（若已是末步则停留在末步）。
    """
    data = current.model_dump()
    idx = STEP_ORDER.index(committed)
    data[committed] = "saved"
    for downstream in STEP_ORDER[idx + 1 :]:
        data[downstream] = _next_status_after_commit(data[downstream])
    next_step = STEP_ORDER[min(idx + 1, len(STEP_ORDER) - 1)]
    return ProjectStepState.model_validate(data), next_step


class StepStore:
    """所有 mutate 都先写 step.json，再回写 Project.step_states。

    出错时先写到一半的 step.json 留盘是可接受的（下次 commit 会覆盖；GET 也会成功返回
    上次值）；状态机不一致比文件半截更值得避免，所以 step.json 先于 project.json 写。
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()

    # ---------- save ----------
    def save(self, project_id: str, snapshot: StepSnapshot) -> Project:
        """落盘单步快照 + 推进 Project.step_states。

        返回最新 Project（含 step_states），调用方可直接回前端不必再 GET 一次。
        """
        with self._lock:
            project = project_store.require(project_id)
            _atomic_write_json(_step_path(project_id, snapshot.step), snapshot.model_dump())
            new_state, next_step = _build_new_state(project.step_states, snapshot.step)
            updated = project_store.update(
                project_id,
                step_states=new_state.model_dump(),
                current_step=next_step,
            )
            log.info(
                "[steps] commit project=%s step=%s → states=%s current=%s",
                project_id, snapshot.step, updated.step_states.model_dump(), updated.current_step,
            )
            return updated

    # ---------- get ----------
    def get(self, project_id: str, step: StepName) -> Optional[StepSnapshot]:
        """返回该步最近一次 commit 的 snapshot；从未 commit 过 → None。"""
        # 先确保 project 存在；缺失抛 ProjectNotFoundError 让路由层 404
        project_store.require(project_id)
        path = _step_path(project_id, step)
        if not path.exists():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            return StepSnapshot.model_validate(raw)
        except Exception as exc:  # noqa: BLE001
            log.warning("[steps] %s/%s broken: %s", project_id, step, exc)
            return None

    def list(self, project_id: str) -> list[StepSnapshot]:
        """返回该项目所有已 commit 的快照，按 STEP_ORDER 顺序。"""
        project_store.require(project_id)
        items: list[StepSnapshot] = []
        for step in STEP_ORDER:
            snap = self.get(project_id, step)
            if snap is not None:
                items.append(snap)
        return items


step_store = StepStore()
