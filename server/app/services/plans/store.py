"""In-memory PlanStore。

阶段 1：进程内字典即可，比赛 demo 单进程；后续若要持久化再换 Redis/SQLite。
"""
from __future__ import annotations

import logging
from typing import Optional

from ...schemas import Plan

log = logging.getLogger("seecript.plans")


class PlanStore:
    def __init__(self) -> None:
        self._plans: dict[str, Plan] = {}

    def put(self, plan: Plan) -> None:
        self._plans[plan.plan_id] = plan
        log.info("[plan] stored plan_id=%s sample=%s scenes=%d",
                 plan.plan_id, plan.sample_id, len(plan.main_track))

    def get(self, plan_id: str) -> Optional[Plan]:
        return self._plans.get(plan_id)

    def replace(self, plan: Plan) -> None:
        """编辑场景：以同 plan_id 覆盖。"""
        self._plans[plan.plan_id] = plan


plan_store = PlanStore()
