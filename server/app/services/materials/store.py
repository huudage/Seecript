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

    def add_aigc(self, session_id: str, material: Material) -> Material:
        """AIGC 自动入库：仅追加；调用方负责先用 clear_aigc_by_gap 删除老记录。

        多镜头/多 chunk fill 一次产出 N 条记录共享同 gap_id+origin，因此本方法
        不能在 append 时按 gap_id 自删（否则后追加的会把先追加的擦掉）。
        """
        if material.origin not in ("aigc_image", "aigc_video"):
            raise ValueError(f"add_aigc 仅接受 aigc_image / aigc_video，当前 origin={material.origin}")
        with self._lock:
            items = self._by_session.setdefault(session_id, [])
            items.append(material)
            self._persist(session_id)
        log.info(
            "[materials] aigc append session=%s mid=%s origin=%s gap=%s",
            session_id, material.material_id, material.origin, material.gap_id,
        )
        return material

    def clear_aigc_by_gap(self, session_id: str, gap_id: str, origin: str) -> int:
        """AIGC 重生前调用：删除该 (gap_id, origin) 下所有老记录。返回删除条数。"""
        if not gap_id or not origin:
            return 0
        with self._lock:
            items = self._by_session.get(session_id) or []
            before = len(items)
            kept = [m for m in items if not (m.gap_id == gap_id and m.origin == origin)]
            removed = before - len(kept)
            if removed:
                self._by_session[session_id] = kept
                self._persist(session_id)
            return removed


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


# === 系统素材库 (__system__) 启动 seed ===
# 用户可以从「系统素材库」picker 选共享视频克隆到自己的项目。这个 picker 调
# `GET /material?project_id=__system__`——但仓库里 `__system__` 的 MaterialStore
# 一直是空的（没人手动 upload 过），导致 picker 永远空。
#
# 修复：启动时自动从 `server/samples/sample-*` 目录扫出已有的内置爆款视频，
# 给 `__system__` 落库一份占位 Material。每个 sample 一条 Material，使用：
# - 确定性 material_id `sys_<sample_id>`，所以重复启动不会重复 seed
# - file_url=`/samples/<id>/video.mp4`（已经被 main.py 挂载为 StaticFiles，浏览器可读）
# - thumbnail_url=`/samples/<id>/cover.jpg`
# - origin="system_clone"（最贴近现有语义；前端 originLabel "系统"）
# - preprocess_status="skipped"（picker 不需要 shots；clone 后用户自己跑 decompose）
def _seed_system_library() -> None:
    """启动钩子：把 server/samples/sample-* 注入 __system__ 项目，让 picker 不再空空。

    幂等：以 sys_<sample_id> 为 material_id 查重；已有就跳过该条。
    samples 目录不存在或没匹配子目录时是 no-op，不报错。
    """
    from ...schemas import Material  # 局部 import 避免循环

    samples_root = _var_root().parent / "samples"
    if not samples_root.is_dir():
        log.info("[materials/seed] samples dir missing, skip __system__ seed: %s", samples_root)
        return

    # 友好标题：与 routers/library.py:_SYSTEM_LIBRARY 保持一致；扫到的其它目录用
    # 目录名兜底（运维自己丢进去的爆款样例也能被 picker 看到）。
    titles: dict[str, tuple[str, list[str]]] = {
        "sample-marketing-01": ("营销样例｜痛点开场+产品演示+行动引导", ["营销", "卖点演示", "行动引导"]),
        "sample-vlog-01": ("剪辑样例｜Vlog 节奏 · 氛围铺垫到高潮收尾", ["剪辑", "Vlog", "氛围铺垫"]),
        "sample-motion-01": ("Motion Graph 样例｜标题入场+信息铺陈+爆点落版", ["Motion Graph", "标题入场", "信息铺陈"]),
    }

    existing = {m.material_id for m in material_store.list("__system__")}
    fresh: list[Material] = []
    for child in sorted(samples_root.iterdir()):
        if not child.is_dir():
            continue
        sample_id = child.name
        # 只 seed 共享类目录：内置爆款 sample-* 与运维上传的 sys-*。
        # user-* 是用户私人样例（decompose.py 注释明确「decompose 决不会碰内置目录」），
        # 不能跨用户共享，否则违反素材隔离原则。
        if not (sample_id.startswith("sample-") or sample_id.startswith("sys-")):
            continue
        video_path = child / "video.mp4"
        cover_path = child / "cover.jpg"
        if not video_path.is_file():
            continue
        mid = f"sys_{sample_id}"
        if mid in existing:
            continue
        title, tags = titles.get(sample_id, (sample_id, ["系统样例"]))
        fresh.append(Material(
            material_id=mid,
            filename=f"{sample_id}.mp4",
            media_type="video",
            file_url=f"/samples/{sample_id}/video.mp4",
            thumbnail_url=f"/samples/{sample_id}/cover.jpg" if cover_path.is_file() else None,
            tags=tags,
            subjects=[],
            recommended_section="development",
            highlight_score=0.7,
            highlight_reason=title,
            origin="system_clone",
            preprocess_status="skipped",
            sort_order=len(existing) + len(fresh),
        ))
    if fresh:
        material_store.put("__system__", fresh)
        log.info("[materials/seed] __system__ seeded %d sample(s): %s",
                 len(fresh), [m.material_id for m in fresh])


# 启动时自动 seed。__init__.py 在 import store 时执行；幂等，所以多次启动安全。
try:
    _seed_system_library()
except Exception as exc:  # noqa: BLE001
    log.warning("[materials/seed] failed: %s", exc)
