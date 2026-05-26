"""In-memory MaterialStore + GapStore。

阶段 5 之前都是进程内单字典：
- MaterialStore：session_id → 用户上传的 Material 列表，gap/detect 用来查真素材
- GapStore：plan_id → detect 输出的 Gap 列表，gap/fill 用 gap_id lookup 时复用，
  省掉 fill 每次重跑 detect 的开销，也保证 detect/fill 看到的是同一组 gap_id。

跟 plans/store.py 同构。比赛后做持久化时换成 Redis 即可。
"""
from __future__ import annotations

import logging
from typing import Optional

from ...schemas import Gap, Material

log = logging.getLogger("seecript.materials")


class MaterialStore:
    def __init__(self) -> None:
        self._by_session: dict[str, list[Material]] = {}

    def put(self, session_id: str, materials: list[Material]) -> None:
        """追加（不覆盖）—— upload 端点支持分批传，新批次接在原 list 末尾。"""
        existing = self._by_session.setdefault(session_id, [])
        existing.extend(materials)
        log.info("[materials] session=%s appended=%d total=%d",
                 session_id, len(materials), len(existing))

    def list(self, session_id: str) -> list[Material]:
        return list(self._by_session.get(session_id, []))

    def remove(self, session_id: str, material_id: str) -> bool:
        items = self._by_session.get(session_id)
        if not items:
            return False
        before = len(items)
        self._by_session[session_id] = [m for m in items if m.material_id != material_id]
        return len(self._by_session[session_id]) < before


class GapStore:
    def __init__(self) -> None:
        self._by_plan: dict[str, list[Gap]] = {}
        self._by_gap_id: dict[str, Gap] = {}

    def put(self, plan_id: str, gaps: list[Gap]) -> None:
        # 覆盖该 plan 的旧 gap，但全局 gap_id → Gap 字典保持累加
        # （rerank/copy/aigc 链路里 detect 重发是常态，fill 还能查到上一次的）
        self._by_plan[plan_id] = list(gaps)
        for g in gaps:
            self._by_gap_id[g.gap_id] = g
        log.info("[gaps] stored plan_id=%s gaps=%d", plan_id, len(gaps))

    def list_by_plan(self, plan_id: str) -> list[Gap]:
        return list(self._by_plan.get(plan_id, []))

    def get(self, gap_id: str) -> Optional[Gap]:
        return self._by_gap_id.get(gap_id)


material_store = MaterialStore()
gap_store = GapStore()
