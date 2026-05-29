"""AssetStore：多 owner 注册表 + JSON 持久化 + sha256 去重。

存储结构（每个 owner 一套独立资产库）：

  var/assets/<owner>/                # owner == project_id（v2 起；老库迁到 __legacy）
    manifest.json                    # 唯一可信源
    bgm/
      ass-xxx.mp3
      ass-xxx.meta.json              # 单条冗余备份，索引坏了可重建
    reference_image/
      ass-yyy.jpg
      ass-yyy.thumb.jpg
      ass-yyy.meta.json
    reference_video/
      ass-zzz.mp4
      ass-zzz.thumb.jpg
      ass-zzz.frames/frame-00.jpg ...
      ass-zzz.meta.json

设计点：
- 每个 owner 用 _OwnerState 隔离 by_id + by_hash + manifest_path
- AssetStore 通过 owner 路由到对应 _OwnerState；by_id 查询走全局 _owner_by_asset 反查
- content_hash 去重仅在 owner 内生效；同一文件在不同项目里独立计数
- 启动 load：扫 var/assets/*/manifest.json 全量进内存
- 兼容老数据：var/assets/local/ 一次性迁到 var/assets/__legacy/（含 URL 重写）
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

from ...config import get_settings
from ...schemas import Asset, AssetKind, AssetStatus

log = logging.getLogger("seecript.assets")

_LEGACY_OWNER = "__legacy"


class AssetStoreError(Exception):
    """资产库异常基类。"""


def _assets_base() -> Path:
    settings = get_settings()
    root = settings.log_dir.parent / "var" / "assets"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _assets_root(owner: str) -> Path:
    root = _assets_base() / owner
    root.mkdir(parents=True, exist_ok=True)
    return root


def _kind_dir(owner: str, kind: AssetKind) -> Path:
    d = _assets_root(owner) / kind
    d.mkdir(parents=True, exist_ok=True)
    return d


def _atomic_write_json(path: Path, payload: dict | list) -> None:
    """原子写：先写 .tmp 再 rename，避免崩溃留半截 manifest。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def sha256_of_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _migrate_local_to_legacy() -> None:
    """一次性迁移：var/assets/local/ → var/assets/__legacy/。

    - 仅当 local 存在且 __legacy 不存在时执行
    - 移动整个目录树
    - 重写 manifest.json + 每条 meta.json 中的 owner 字段 / file_url / thumbnail_url / frame_urls
    """
    base = _assets_base()
    local = base / "local"
    legacy = base / _LEGACY_OWNER
    if not local.exists() or legacy.exists():
        return
    log.warning("[assets] 检测到 var/assets/local/ 旧数据，迁移到 %s", legacy)
    shutil.move(str(local), str(legacy))

    def _rewrite(s):
        if isinstance(s, str) and s.startswith("/assets/local/"):
            return "/assets/" + _LEGACY_OWNER + "/" + s[len("/assets/local/"):]
        return s

    def _rewrite_meta(meta: dict) -> dict:
        out = dict(meta)
        out["owner"] = _LEGACY_OWNER
        if isinstance(out.get("file_url"), str):
            out["file_url"] = _rewrite(out["file_url"])
        md = out.get("metadata")
        if isinstance(md, dict):
            md = dict(md)
            if isinstance(md.get("thumbnail_url"), str):
                md["thumbnail_url"] = _rewrite(md["thumbnail_url"])
            frames = md.get("frame_urls")
            if isinstance(frames, list):
                md["frame_urls"] = [_rewrite(x) for x in frames]
            out["metadata"] = md
        return out

    # 改 manifest.json
    manifest = legacy / "manifest.json"
    if manifest.exists():
        try:
            raw = json.loads(manifest.read_text(encoding="utf-8"))
            items = raw.get("items") if isinstance(raw, dict) else raw
            if isinstance(items, list):
                new_items = [_rewrite_meta(it) if isinstance(it, dict) else it for it in items]
                _atomic_write_json(manifest, {"version": 1, "items": new_items})
        except Exception as exc:  # noqa: BLE001
            log.error("[assets] 迁移 manifest 重写失败: %s", exc)

    # 改每条 *.meta.json
    for meta_path in legacy.glob("*/*.meta.json"):
        try:
            entry = json.loads(meta_path.read_text(encoding="utf-8"))
            if isinstance(entry, dict):
                _atomic_write_json(meta_path, _rewrite_meta(entry))
        except Exception as exc:  # noqa: BLE001
            log.warning("[assets] 迁移 meta 跳过 %s: %s", meta_path, exc)

    log.info("[assets] local → __legacy 迁移完成")


class _OwnerState:
    """单个 owner 的索引与 manifest 路径。AssetStore 内部用，外部不直接持有。"""

    def __init__(self, owner: str) -> None:
        self.owner = owner
        self.by_id: dict[str, Asset] = {}
        self.by_hash: dict[str, str] = {}  # content_hash → asset_id
        self.manifest_path = _assets_root(owner) / "manifest.json"
        self._load()

    def _load(self) -> None:
        if not self.manifest_path.exists():
            log.info("[assets] manifest 不存在，初始化空库 owner=%s", self.owner)
            return
        try:
            raw = json.loads(self.manifest_path.read_text(encoding="utf-8"))
            items = raw.get("items") if isinstance(raw, dict) else raw
            if not isinstance(items, list):
                raise AssetStoreError("manifest items 字段非 list")
            for entry in items:
                try:
                    asset = Asset.model_validate(entry)
                except Exception as exc:  # noqa: BLE001
                    log.warning("[assets] skip 损坏条目 %s: %s", entry.get("asset_id"), exc)
                    continue
                self.by_id[asset.asset_id] = asset
                if asset.content_hash:
                    self.by_hash[asset.content_hash] = asset.asset_id
            log.info("[assets] 加载 %d 条资产 owner=%s", len(self.by_id), self.owner)
        except Exception as exc:  # noqa: BLE001
            log.error("[assets] manifest 解析失败 owner=%s，尝试从 meta.json 重建：%s", self.owner, exc)
            self._rebuild_from_meta()

    def _rebuild_from_meta(self) -> None:
        root = _assets_root(self.owner)
        recovered = 0
        for meta_path in root.glob("*/*.meta.json"):
            try:
                entry = json.loads(meta_path.read_text(encoding="utf-8"))
                asset = Asset.model_validate(entry)
                self.by_id[asset.asset_id] = asset
                if asset.content_hash:
                    self.by_hash[asset.content_hash] = asset.asset_id
                recovered += 1
            except Exception as exc:  # noqa: BLE001
                log.warning("[assets] meta 重建跳过 %s: %s", meta_path, exc)
        log.info("[assets] 从磁盘 meta 重建 %d 条 owner=%s", recovered, self.owner)
        self.flush()

    def flush(self) -> None:
        items = [a.model_dump(mode="json") for a in self.by_id.values()]
        _atomic_write_json(self.manifest_path, {"version": 1, "items": items})

    def write_meta(self, asset: Asset) -> None:
        meta_path = _kind_dir(asset.owner, asset.kind) / f"{asset.asset_id}.meta.json"
        _atomic_write_json(meta_path, asset.model_dump(mode="json"))


class AssetStore:
    """线程安全的多 owner Asset 存储。

    单机演示场景 in-memory + json 持久化即可；要上规模换 SQLite/Postgres 只换实现，
    不改外部 API（list/get/upsert/delete/touch）。
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._states: dict[str, _OwnerState] = {}
        self._owner_by_asset: dict[str, str] = {}  # asset_id → owner（全局反查）
        _migrate_local_to_legacy()
        self._load_all_owners()

    # ---------- 内部 ----------
    def _load_all_owners(self) -> None:
        base = _assets_base()
        if not base.exists():
            return
        for child in base.iterdir():
            if not child.is_dir():
                continue
            state = _OwnerState(child.name)
            self._states[child.name] = state
            for aid in state.by_id:
                self._owner_by_asset[aid] = child.name

    def _state(self, owner: str) -> _OwnerState:
        """按需创建 owner 的 state（首个 upsert 会触发）。"""
        with self._lock:
            st = self._states.get(owner)
            if st is None:
                st = _OwnerState(owner)
                self._states[owner] = st
        return st

    def _resolve_state(self, asset_id: str) -> Optional[_OwnerState]:
        owner = self._owner_by_asset.get(asset_id)
        if owner is None:
            return None
        return self._states.get(owner)

    # ---------- 公共 API（按 owner 路由） ----------
    def find_by_hash(self, owner: str, content_hash: str) -> Optional[Asset]:
        with self._lock:
            st = self._states.get(owner)
            if st is None:
                return None
            aid = st.by_hash.get(content_hash)
            return st.by_id.get(aid) if aid else None

    def list(
        self,
        owner: str,
        *,
        kind: Optional[AssetKind] = None,
        query: Optional[str] = None,
        tag: Optional[str] = None,
    ) -> list[Asset]:
        with self._lock:
            st = self._states.get(owner)
            items = list(st.by_id.values()) if st else []
        if kind:
            items = [a for a in items if a.kind == kind]
        if tag:
            items = [a for a in items if tag in a.tags]
        if query:
            q = query.lower()
            items = [
                a for a in items
                if q in a.title.lower()
                or q in a.description.lower()
                or any(q in t.lower() for t in a.tags)
                or q in a.file_name.lower()
            ]
        items.sort(
            key=lambda a: (a.last_used_at or a.created_at),
            reverse=True,
        )
        return items

    def upsert(self, asset: Asset) -> Asset:
        """新建或覆盖（按 asset_id）。owner 取自 asset.owner。"""
        st = self._state(asset.owner)
        with self._lock:
            old = st.by_id.get(asset.asset_id)
            if old and old.content_hash and old.content_hash != asset.content_hash:
                st.by_hash.pop(old.content_hash, None)
            st.by_id[asset.asset_id] = asset
            if asset.content_hash:
                st.by_hash[asset.content_hash] = asset.asset_id
            self._owner_by_asset[asset.asset_id] = asset.owner
            st.write_meta(asset)
            st.flush()
        log.info(
            "[assets] upsert id=%s owner=%s kind=%s status=%s name=%s",
            asset.asset_id, asset.owner, asset.kind, asset.status, asset.file_name,
        )
        return asset

    # ---------- 按 asset_id 单条查询（owner 内部反查） ----------
    def get(self, asset_id: str) -> Optional[Asset]:
        with self._lock:
            st = self._resolve_state(asset_id)
            return st.by_id.get(asset_id) if st else None

    def update_fields(self, asset_id: str, **patch) -> Optional[Asset]:
        with self._lock:
            st = self._resolve_state(asset_id)
            if st is None:
                return None
            cur = st.by_id.get(asset_id)
            if cur is None:
                return None
            data = cur.model_dump()
            for k, v in patch.items():
                if v is not None:
                    data[k] = v
            new_asset = Asset.model_validate(data)
            st.by_id[asset_id] = new_asset
            st.write_meta(new_asset)
            st.flush()
            return new_asset

    def set_status(
        self,
        asset_id: str,
        status: AssetStatus,
        *,
        metadata: Optional[dict] = None,
        error: Optional[str] = None,
    ) -> Optional[Asset]:
        with self._lock:
            st = self._resolve_state(asset_id)
            if st is None:
                return None
            cur = st.by_id.get(asset_id)
            if cur is None:
                return None
            data = cur.model_dump()
            data["status"] = status
            if metadata is not None:
                merged = dict(cur.metadata)
                merged.update(metadata)
                data["metadata"] = merged
            if error is not None:
                data["error"] = error
            updated = Asset.model_validate(data)
            st.by_id[asset_id] = updated
            st.write_meta(updated)
            st.flush()
        return updated

    def touch(self, asset_id: str) -> Optional[Asset]:
        with self._lock:
            st = self._resolve_state(asset_id)
            if st is None:
                return None
            cur = st.by_id.get(asset_id)
            if cur is None:
                return None
            data = cur.model_dump()
            data["use_count"] = cur.use_count + 1
            data["last_used_at"] = time.time()
            updated = Asset.model_validate(data)
            st.by_id[asset_id] = updated
            st.write_meta(updated)
            st.flush()
            return updated

    def delete(self, asset_id: str) -> bool:
        """删除 asset：从索引摘除 + 删磁盘文件（含缩略图/抽帧目录）。"""
        with self._lock:
            st = self._resolve_state(asset_id)
            if st is None:
                return False
            asset = st.by_id.pop(asset_id, None)
            if asset is None:
                return False
            if asset.content_hash:
                st.by_hash.pop(asset.content_hash, None)
            self._owner_by_asset.pop(asset_id, None)
            st.flush()
        try:
            file_path = self._local_path_of(asset.file_url, asset.owner)
            if file_path and file_path.exists():
                file_path.unlink()
            meta_path = _kind_dir(asset.owner, asset.kind) / f"{asset.asset_id}.meta.json"
            if meta_path.exists():
                meta_path.unlink()
            for thumb_key in ("thumbnail_url",):
                url = asset.metadata.get(thumb_key)
                if isinstance(url, str):
                    p = self._local_path_of(url, asset.owner)
                    if p and p.exists():
                        p.unlink()
            frames_dir = _kind_dir(asset.owner, asset.kind) / f"{asset.asset_id}.frames"
            if frames_dir.exists():
                for f in frames_dir.iterdir():
                    try:
                        f.unlink()
                    except OSError:
                        pass
                try:
                    frames_dir.rmdir()
                except OSError:
                    pass
        except Exception as exc:  # noqa: BLE001
            log.warning("[assets] delete IO 清理失败 id=%s: %s", asset_id, exc)
        log.info("[assets] deleted id=%s kind=%s owner=%s", asset_id, asset.kind, asset.owner)
        return True

    # ---------- 路径辅助 ----------
    def _local_path_of(self, url: str, owner: str) -> Optional[Path]:
        if not url or not url.startswith("/assets/"):
            return None
        rel = url[len("/assets/"):]
        return _assets_base() / rel

    @staticmethod
    def new_asset_id() -> str:
        return f"ass-{uuid.uuid4().hex[:12]}"


asset_store = AssetStore()
