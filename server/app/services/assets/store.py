"""AssetStore：JSON 持久化 + sha256 去重 + 启动加载。

存储结构（见 docs/ARCHITECTURE 资产库章节，未文档化时以此为准）：

  var/assets/local/
    manifest.json                # 唯一可信源
    bgm/
      ass-xxx.mp3
      ass-xxx.meta.json          # 单条冗余备份，索引坏了可重建
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
- 单进程内存 dict 是 hot path；manifest.json 是冷备份，每次 mutate 后原子写盘
- content_hash sha256 去重：同一文件二次上传返回老 asset_id，不撑爆磁盘
- 启动 load：从 manifest.json 重建内存索引；manifest 缺失/坏 → 扫描磁盘 meta.json 重建
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

from ...config import get_settings
from ...schemas import Asset, AssetKind, AssetStatus

log = logging.getLogger("seecript.assets")


class AssetStoreError(Exception):
    """资产库异常基类。"""


def _assets_root(owner: str = "local") -> Path:
    settings = get_settings()
    root = settings.log_dir.parent / "var" / "assets" / owner
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


class AssetStore:
    """线程安全的 Asset 存储——HTTP 路由从多个 worker 进来时也只有一个写者。

    单机演示场景一切 in-memory + json 持久化即可；要上规模换 SQLite/Postgres 只换实现，
    不改外部 API（list/get/upsert/delete/touch）。
    """

    def __init__(self, owner: str = "local") -> None:
        self._owner = owner
        self._lock = threading.RLock()
        self._by_id: dict[str, Asset] = {}
        self._by_hash: dict[str, str] = {}  # content_hash → asset_id
        self._manifest_path = _assets_root(owner) / "manifest.json"
        self._load()

    # ---------- 持久化 ----------
    def _load(self) -> None:
        if not self._manifest_path.exists():
            log.info("[assets] manifest 不存在，初始化空库 owner=%s", self._owner)
            return
        try:
            raw = json.loads(self._manifest_path.read_text(encoding="utf-8"))
            items = raw.get("items") if isinstance(raw, dict) else raw
            if not isinstance(items, list):
                raise AssetStoreError("manifest items 字段非 list")
            for entry in items:
                try:
                    asset = Asset.model_validate(entry)
                except Exception as exc:  # noqa: BLE001
                    log.warning("[assets] skip 损坏条目 %s: %s", entry.get("asset_id"), exc)
                    continue
                self._by_id[asset.asset_id] = asset
                if asset.content_hash:
                    self._by_hash[asset.content_hash] = asset.asset_id
            log.info("[assets] 加载 %d 条资产 owner=%s", len(self._by_id), self._owner)
        except Exception as exc:  # noqa: BLE001
            log.error("[assets] manifest 解析失败，尝试从 meta.json 重建：%s", exc)
            self._rebuild_from_meta()

    def _rebuild_from_meta(self) -> None:
        root = _assets_root(self._owner)
        recovered = 0
        for meta_path in root.glob("*/*.meta.json"):
            try:
                entry = json.loads(meta_path.read_text(encoding="utf-8"))
                asset = Asset.model_validate(entry)
                self._by_id[asset.asset_id] = asset
                if asset.content_hash:
                    self._by_hash[asset.content_hash] = asset.asset_id
                recovered += 1
            except Exception as exc:  # noqa: BLE001
                log.warning("[assets] meta 重建跳过 %s: %s", meta_path, exc)
        log.info("[assets] 从磁盘 meta 重建 %d 条", recovered)
        self._flush()

    def _flush(self) -> None:
        """把当前内存 index 整体写回 manifest.json。"""
        items = [a.model_dump(mode="json") for a in self._by_id.values()]
        _atomic_write_json(self._manifest_path, {"version": 1, "items": items})

    def _write_meta(self, asset: Asset) -> None:
        """写单条冗余 meta.json，靠它能在 manifest 坏掉时恢复。"""
        meta_path = _kind_dir(asset.owner, asset.kind) / f"{asset.asset_id}.meta.json"
        _atomic_write_json(meta_path, asset.model_dump(mode="json"))

    # ---------- 公共 API ----------
    def find_by_hash(self, content_hash: str) -> Optional[Asset]:
        with self._lock:
            aid = self._by_hash.get(content_hash)
            return self._by_id.get(aid) if aid else None

    def get(self, asset_id: str) -> Optional[Asset]:
        with self._lock:
            return self._by_id.get(asset_id)

    def list(
        self,
        *,
        kind: Optional[AssetKind] = None,
        query: Optional[str] = None,
        tag: Optional[str] = None,
    ) -> list[Asset]:
        with self._lock:
            items = list(self._by_id.values())
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
        # 最近使用倒序；从未用过的按 created_at 倒序
        items.sort(
            key=lambda a: (a.last_used_at or a.created_at),
            reverse=True,
        )
        return items

    def upsert(self, asset: Asset) -> Asset:
        """新建或覆盖（按 asset_id）。content_hash 索引同步刷。"""
        with self._lock:
            old = self._by_id.get(asset.asset_id)
            if old and old.content_hash and old.content_hash != asset.content_hash:
                self._by_hash.pop(old.content_hash, None)
            self._by_id[asset.asset_id] = asset
            if asset.content_hash:
                self._by_hash[asset.content_hash] = asset.asset_id
            self._write_meta(asset)
            self._flush()
        log.info(
            "[assets] upsert id=%s kind=%s status=%s name=%s",
            asset.asset_id, asset.kind, asset.status, asset.file_name,
        )
        return asset

    def update_fields(self, asset_id: str, **patch) -> Optional[Asset]:
        """部分字段就地更新。空值/None 不覆盖。"""
        with self._lock:
            cur = self._by_id.get(asset_id)
            if cur is None:
                return None
            data = cur.model_dump()
            for k, v in patch.items():
                if v is not None:
                    data[k] = v
            new_asset = Asset.model_validate(data)
            self._by_id[asset_id] = new_asset
            self._write_meta(new_asset)
            self._flush()
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
            cur = self._by_id.get(asset_id)
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
            self._by_id[asset_id] = updated
            self._write_meta(updated)
            self._flush()
        return updated

    def touch(self, asset_id: str) -> Optional[Asset]:
        with self._lock:
            cur = self._by_id.get(asset_id)
            if cur is None:
                return None
            data = cur.model_dump()
            data["use_count"] = cur.use_count + 1
            data["last_used_at"] = time.time()
            updated = Asset.model_validate(data)
            self._by_id[asset_id] = updated
            self._write_meta(updated)
            self._flush()
            return updated

    def delete(self, asset_id: str) -> bool:
        """删除 asset：从索引摘除 + 删磁盘文件（含缩略图/抽帧目录）。"""
        with self._lock:
            asset = self._by_id.pop(asset_id, None)
            if asset is None:
                return False
            if asset.content_hash:
                self._by_hash.pop(asset.content_hash, None)
            self._flush()
        # 在锁外做文件 IO
        try:
            file_path = self._local_path_of(asset.file_url, asset.owner)
            if file_path and file_path.exists():
                file_path.unlink()
            meta_path = _kind_dir(asset.owner, asset.kind) / f"{asset.asset_id}.meta.json"
            if meta_path.exists():
                meta_path.unlink()
            # 缩略图
            for thumb_key in ("thumbnail_url",):
                url = asset.metadata.get(thumb_key)
                if isinstance(url, str):
                    p = self._local_path_of(url, asset.owner)
                    if p and p.exists():
                        p.unlink()
            # video 抽帧目录
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
        log.info("[assets] deleted id=%s kind=%s", asset_id, asset.kind)
        return True

    # ---------- 路径辅助 ----------
    def _local_path_of(self, url: str, owner: str) -> Optional[Path]:
        """`/assets/<owner>/<kind>/<file>` → 本地 var/assets/<owner>/<kind>/<file>。"""
        if not url or not url.startswith("/assets/"):
            return None
        rel = url[len("/assets/"):]
        return _assets_root(owner).parent / rel

    @staticmethod
    def new_asset_id() -> str:
        return f"ass-{uuid.uuid4().hex[:12]}"


# 模块级单例。Boot 时 lifespan 不需要显式初始化——首次 import 即 load。
asset_store = AssetStore(owner="local")
