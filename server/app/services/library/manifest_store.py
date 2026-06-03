"""结构知识库 manifest 多版本槽存储。

每个 sample 目录下保留**最近 2 个版本**的 manifest，加 1 个 active 指针：

| 文件 | 角色 |
|---|---|
| `manifest.v_<8hex>.json` | 一个版本槽；最多 2 个，按 mtime 排序后老的为 v1 / 新的为 v2 |
| `manifest.active`        | 一行文本，存当前 active slot id；Compose / library 列表读这个槽 |

设计取舍：
- **slot id 用 8-hex 不暴露给用户**——前端只看到 v1/v2 标签（按 updated_at 排序得来）。
  这样无论用户怎么编辑/重生，slot 物理 id 不动，避免 rename 文件。
- **手动编辑 = 写回当前 slot**（不开新版本）；只有「重新生成」才创建新版本。
- **超过 2 个版本时拒写**（create_version 抛 SlotsFullError），由路由层在调用前
  让用户手动选 replace_slot——不做后端 LRU，因为用户可能想保留更早的"原始版本"。
- **active 自动跟随**：新版本默认 active；当 active 槽被删除时自动跳到剩下那个；都没了就清空。

历史背景：
- 旧 v1 版用 `manifest.json` (published) + `manifest.draft.json` + `manifest.prev.json`
  实现「draft → publish → 1 次 undo」三态。被替换是因为：
  ① UI 概念太多（draft/published/可撤销/弃草稿），认知负担重；
  ② 用户实际诉求是"对比新旧两版"，1 槽的 published 没法做对比；
  ③ 编辑/重生没有清晰的版本边界，undo 兜底只覆盖 1 步。
"""
from __future__ import annotations

import json
import logging
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from pydantic import ValidationError

from ...config import get_settings
from ...schemas import SampleManifest

log = logging.getLogger("seecript.library.manifest_store")

MAX_VERSIONS = 2
"""每个 sample 至多保留的版本槽数。再多得让用户先删一个。"""

_VERSION_PREFIX = "manifest.v_"
_VERSION_SUFFIX = ".json"
_ACTIVE_NAME = "manifest.active"

_SLOT_ID_BYTES = 4  # → 8 hex chars，足以避免误碰；不需要密码学强度


class SlotsFullError(Exception):
    """已有 MAX_VERSIONS 个槽时调用 create_version 不指定 replace_slot 抛这个。

    路由层接住 → 转 409，body 里附带 list_versions() 让前端弹"删除哪个"对话框。
    """


@dataclass(frozen=True)
class VersionInfo:
    """单个版本槽的元信息——`/sample/{id}/versions` 列表项。

    label 由调用方根据列表中的位置计算（v1=最旧/v2=最新），不存在文件里——
    单纯按 updated_at 排序就行，避免 rename 文件维护编号。
    """

    slot_id: str
    updated_at: float  # mtime of manifest.v_<id>.json
    is_active: bool


# -----------------------------------------------------------------------------
# 路径解析
# -----------------------------------------------------------------------------

def _samples_root() -> Path:
    return Path(__file__).resolve().parents[3] / "samples"


def _user_uploads_root() -> Path:
    return get_settings().log_dir.parent / "var" / "uploads" / "decompose"


def locate_sample_dir(sample_id: str) -> Optional[Path]:
    """根据 sample_id 找物理目录。内置 + sys-* 在 server/samples/；user-* 在 var/uploads/decompose/。"""
    if not sample_id:
        return None
    sys_dir = _samples_root() / sample_id
    if sys_dir.is_dir():
        return sys_dir
    user_dir = _user_uploads_root() / sample_id
    if user_dir.is_dir():
        return user_dir
    return None


def _ensure_sample_dir(sample_id: str) -> Path:
    d = locate_sample_dir(sample_id)
    if d is None:
        raise FileNotFoundError(f"sample directory not found for {sample_id}")
    return d


def _version_path(sample_dir: Path, slot_id: str) -> Path:
    return sample_dir / f"{_VERSION_PREFIX}{slot_id}{_VERSION_SUFFIX}"


def _active_path(sample_dir: Path) -> Path:
    return sample_dir / _ACTIVE_NAME


# -----------------------------------------------------------------------------
# 底层 IO
# -----------------------------------------------------------------------------

def _write_atomic(path: Path, payload: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _load_manifest_file(path: Path) -> Optional[SampleManifest]:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return SampleManifest.model_validate(data)
    except (json.JSONDecodeError, ValidationError, OSError) as exc:
        log.warning("[manifest_store] %s 解析失败：%s", path, exc)
        return None


def _new_slot_id() -> str:
    return secrets.token_hex(_SLOT_ID_BYTES)


def _scan_slots(sample_dir: Path) -> list[Path]:
    """目录内所有 manifest.v_<id>.json 文件，按 mtime 升序（最旧在前）。

    迁移兜底：若没有任何 slot 文件，但存在 legacy `manifest.json`（precompute_samples.py
    时代的产物 / 内置 3 条样例的预拆解输出），把它就地迁成第一个 slot 并写 active 指针。
    这一步只发生一次，迁完后再扫一遍即可。
    """
    if not sample_dir.is_dir():
        return []
    out = [
        p for p in sample_dir.iterdir()
        if p.is_file()
        and p.name.startswith(_VERSION_PREFIX)
        and p.name.endswith(_VERSION_SUFFIX)
    ]
    if not out:
        legacy = sample_dir / "manifest.json"
        if legacy.is_file():
            slot_id = _new_slot_id()
            target = _version_path(sample_dir, slot_id)
            try:
                legacy.rename(target)
                _write_active(sample_dir, slot_id)
                log.info("[manifest_store] %s 迁移 legacy manifest.json → slot=%s", sample_dir.name, slot_id)
                out = [target]
            except OSError as exc:
                log.warning("[manifest_store] %s 迁移 legacy manifest.json 失败: %s", sample_dir, exc)
    out.sort(key=lambda p: p.stat().st_mtime)
    return out


def _slot_id_from_path(p: Path) -> str:
    return p.name[len(_VERSION_PREFIX): -len(_VERSION_SUFFIX)]


# -----------------------------------------------------------------------------
# Active 指针
# -----------------------------------------------------------------------------

def _read_active(sample_dir: Path) -> Optional[str]:
    p = _active_path(sample_dir)
    if not p.is_file():
        return None
    try:
        sid = p.read_text(encoding="utf-8").strip()
        return sid or None
    except OSError as exc:
        log.warning("[manifest_store] read active failed %s: %s", p, exc)
        return None


def _write_active(sample_dir: Path, slot_id: Optional[str]) -> None:
    p = _active_path(sample_dir)
    if slot_id is None:
        p.unlink(missing_ok=True)
        return
    p.write_text(slot_id, encoding="utf-8")


# -----------------------------------------------------------------------------
# 公共 API
# -----------------------------------------------------------------------------

def list_versions(sample_id: str) -> list[VersionInfo]:
    """返回 sample 的所有版本槽（≤ MAX_VERSIONS），按 updated_at 升序。

    没目录或没版本时返空列表。is_active 由 manifest.active 指向的 slot 决定；
    指针指向不存在的 slot 时所有 is_active=False（_get_active_slot_id 会自愈）。
    """
    d = locate_sample_dir(sample_id)
    if d is None:
        return []
    paths = _scan_slots(d)
    if not paths:
        return []
    active = _get_active_slot_id(d, paths)
    return [
        VersionInfo(
            slot_id=_slot_id_from_path(p),
            updated_at=p.stat().st_mtime,
            is_active=(_slot_id_from_path(p) == active),
        )
        for p in paths
    ]


def _get_active_slot_id(sample_dir: Path, paths: Optional[list[Path]] = None) -> Optional[str]:
    """读 manifest.active；如果指向不存在的 slot 就自愈到最新一个，返回更正后的 id。"""
    if paths is None:
        paths = _scan_slots(sample_dir)
    if not paths:
        # 没版本——清掉残留的 active 指针
        _active_path(sample_dir).unlink(missing_ok=True)
        return None
    valid_ids = {_slot_id_from_path(p) for p in paths}
    cur = _read_active(sample_dir)
    if cur and cur in valid_ids:
        return cur
    # 自愈：active 缺失或指向已删除的 slot → 默认最新一个
    new_active = _slot_id_from_path(paths[-1])
    _write_active(sample_dir, new_active)
    log.info("[manifest_store] %s active 自愈 → %s", sample_dir.name, new_active)
    return new_active


def get_active_slot(sample_id: str) -> Optional[str]:
    d = locate_sample_dir(sample_id)
    if d is None:
        return None
    return _get_active_slot_id(d)


def version_count(sample_id: str) -> int:
    d = locate_sample_dir(sample_id)
    if d is None:
        return 0
    return len(_scan_slots(d))


def load_version(sample_id: str, slot_id: str) -> Optional[SampleManifest]:
    d = locate_sample_dir(sample_id)
    if d is None:
        return None
    return _load_manifest_file(_version_path(d, slot_id))


def load_active(sample_id: str) -> Optional[SampleManifest]:
    """Compose / library 列表展示用——拿 active 指针对应的版本。"""
    d = locate_sample_dir(sample_id)
    if d is None:
        return None
    active = _get_active_slot_id(d)
    if active is None:
        return None
    return _load_manifest_file(_version_path(d, active))


# load_published 保留为 load_active 别名——plan_agent / gap_agent 等调用方仍用这个名字，
# 但语义已从「published」简化为「current」。
def load_published(sample_id: str) -> Optional[SampleManifest]:
    """Deprecated 名字，等价于 load_active。仍由 router.library / plan / gap 引用。"""
    return load_active(sample_id)


def has_active(sample_id: str) -> bool:
    """sample 是否有任意已拆解版本——Compose 拦截判断的依据。"""
    return load_active(sample_id) is not None


def update_version(sample_id: str, slot_id: str, manifest: SampleManifest) -> Path:
    """**就地编辑**：把 slot 的内容整段替换。不开新版本，不动 active 指针。

    用户在 Decompose 页编辑当前版本后调这个。失败抛 FileNotFoundError。
    """
    d = _ensure_sample_dir(sample_id)
    target = _version_path(d, slot_id)
    if not target.is_file():
        raise FileNotFoundError(f"slot {slot_id} 不存在")
    _write_atomic(target, manifest.model_dump())
    log.info(
        "[manifest_store] %s slot=%s updated (%d shots)",
        sample_id, slot_id, len(manifest.shots),
    )
    return target


def create_version(
    sample_id: str,
    manifest: SampleManifest,
    *,
    replace_slot: Optional[str] = None,
    activate: bool = True,
) -> str:
    """创建一个新版本槽，返回 slot_id。

    - 当前槽数 < MAX_VERSIONS：直接新建，replace_slot 必须为 None。
    - 当前槽数 == MAX_VERSIONS：必须传 replace_slot，覆盖该槽（旧 slot 文件原地替换 manifest 内容，
      slot_id 不复用 → 删旧 + 写新）。
    - replace_slot 不存在或 < MAX_VERSIONS 时传 → 抛 ValueError。
    - 槽满且未传 replace_slot → 抛 SlotsFullError，让路由层让用户选。

    activate=True 时新槽设为 active；False 时保持原 active（用于"先生成再让用户对比"流程）。
    """
    d = _ensure_sample_dir(sample_id)
    paths = _scan_slots(d)

    if replace_slot is not None:
        if len(paths) < MAX_VERSIONS:
            raise ValueError(
                f"slot 还有空位（{len(paths)}/{MAX_VERSIONS}），不应传 replace_slot"
            )
        existing_ids = {_slot_id_from_path(p) for p in paths}
        if replace_slot not in existing_ids:
            raise ValueError(f"replace_slot={replace_slot} 不存在")
        # 删除旧槽文件（不复用 slot_id：避免「旧版本读缓存还活着」之类的脏读）
        _version_path(d, replace_slot).unlink(missing_ok=True)
        # 如果被删的 slot 恰好是 active，下面 _write_active 会接手
    elif len(paths) >= MAX_VERSIONS:
        raise SlotsFullError(
            f"sample {sample_id} 已有 {MAX_VERSIONS} 个版本，请先选一个删除"
        )

    new_id = _new_slot_id()
    # 极小概率撞 id（4 字节 hex 重复）—— 重抽到一个不冲突的
    while _version_path(d, new_id).exists():
        new_id = _new_slot_id()

    _write_atomic(_version_path(d, new_id), manifest.model_dump())

    if activate:
        _write_active(d, new_id)
    else:
        # 保险一手——active 指向的可能就是被 replace 的那个，自愈到剩下那个
        _get_active_slot_id(d)

    log.info(
        "[manifest_store] %s create_version slot=%s replace=%s activate=%s (%d shots)",
        sample_id, new_id, replace_slot, activate, len(manifest.shots),
    )
    return new_id


def activate(sample_id: str, slot_id: str) -> None:
    """切换 active 指针。slot 不存在抛 FileNotFoundError。"""
    d = _ensure_sample_dir(sample_id)
    if not _version_path(d, slot_id).is_file():
        raise FileNotFoundError(f"slot {slot_id} 不存在")
    _write_active(d, slot_id)
    log.info("[manifest_store] %s activate slot=%s", sample_id, slot_id)


def delete_version(sample_id: str, slot_id: str) -> bool:
    """删除一个版本槽。被删的若是 active，自动跳到剩下那个；都没了清空 active。

    返回 True=确实删了，False=slot 不存在。
    """
    d = locate_sample_dir(sample_id)
    if d is None:
        return False
    target = _version_path(d, slot_id)
    if not target.is_file():
        return False
    target.unlink()
    # _get_active_slot_id 内部会自愈
    _get_active_slot_id(d)
    log.info("[manifest_store] %s delete slot=%s", sample_id, slot_id)
    return True
