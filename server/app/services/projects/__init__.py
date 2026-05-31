"""项目存储 —— 项目（Project）是后端唯一隔离键。

`project_id` 串起用户素材、资产库、plans、gaps、fills；样例 manifest 仍共享。
本模块对外只暴露单例 `project_store`，业务路由通过它做 CRUD。
"""
from .steps import STEP_ORDER, step_store
from .store import project_store

__all__ = ["STEP_ORDER", "project_store", "step_store"]
