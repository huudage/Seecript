"""profile 模块的存储层。

Trace 走 JSONL append（高频写、低频读、不需要 random access）；
settings / project KB 走原子 JSON（覆盖写，全文读）。
线程锁颗粒度按文件级——单用户场景并发不高。
"""
from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Iterable

from .paths import (
    project_kb_dir,
    project_kb_path,
    settings_path,
    trace_a_path,
    trace_b_path,
)
from .schemas import ProfileSettings, ProjectKB, TraceA, TraceB

log = logging.getLogger("seecript.profile")

# 文件级写锁——append JSONL 在 POSIX 下其实可以多进程 append，但这里走单进程，
# 用全局 dict 锁就够了，避免重复落盘 / 部分写入交错。
_file_locks: dict[str, threading.Lock] = {}
_locks_lock = threading.Lock()


def _file_lock(path: Path) -> threading.Lock:
    key = str(path.resolve())
    with _locks_lock:
        lk = _file_locks.get(key)
        if lk is None:
            lk = threading.Lock()
            _file_locks[key] = lk
        return lk


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _append_jsonl(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(payload, ensure_ascii=False)
    with _file_lock(path):
        with path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")


def _read_jsonl(path: Path) -> Iterable[dict]:
    if not path.exists():
        return []
    rows: list[dict] = []
    with path.open("r", encoding="utf-8") as f:
        for ln in f:
            ln = ln.strip()
            if not ln:
                continue
            try:
                rows.append(json.loads(ln))
            except json.JSONDecodeError as exc:
                log.warning("[profile] bad jsonl line in %s: %s", path, exc)
    return rows


# ---------------------- settings ----------------------

def load_settings(user_id: str) -> ProfileSettings:
    p = settings_path(user_id)
    if not p.exists():
        return ProfileSettings()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return ProfileSettings.model_validate(data)
    except Exception as exc:  # noqa: BLE001
        log.warning("[profile] settings 解析失败 %s: %s（用默认值）", p, exc)
        return ProfileSettings()


def save_settings(user_id: str, s: ProfileSettings) -> None:
    _atomic_write_json(settings_path(user_id), s.model_dump())


# ---------------------- traces ----------------------

def append_trace_a(user_id: str, trace: TraceA) -> None:
    _append_jsonl(trace_a_path(user_id), trace.model_dump())


def append_trace_b(user_id: str, trace: TraceB) -> None:
    _append_jsonl(trace_b_path(user_id), trace.model_dump())


def read_traces_a(user_id: str, *, project_id: str | None = None) -> list[TraceA]:
    rows = list(_read_jsonl(trace_a_path(user_id)))
    out: list[TraceA] = []
    for r in rows:
        if project_id and r.get("project_id") != project_id:
            continue
        try:
            out.append(TraceA.model_validate(r))
        except Exception as exc:  # noqa: BLE001
            log.warning("[profile] trace A row 校验失败：%s", exc)
    return out


def read_traces_b(user_id: str, *, project_id: str | None = None) -> list[TraceB]:
    rows = list(_read_jsonl(trace_b_path(user_id)))
    out: list[TraceB] = []
    for r in rows:
        if project_id and r.get("project_id") != project_id:
            continue
        try:
            out.append(TraceB.model_validate(r))
        except Exception as exc:  # noqa: BLE001
            log.warning("[profile] trace B row 校验失败：%s", exc)
    return out


# ---------------------- project KB ----------------------

def save_project_kb(user_id: str, kb: ProjectKB) -> None:
    _atomic_write_json(project_kb_path(user_id, kb.project_id), kb.model_dump())


def load_project_kb(user_id: str, project_id: str) -> ProjectKB | None:
    p = project_kb_path(user_id, project_id)
    if not p.exists():
        return None
    try:
        return ProjectKB.model_validate(json.loads(p.read_text(encoding="utf-8")))
    except Exception as exc:  # noqa: BLE001
        log.warning("[profile] project KB 解析失败 %s: %s", p, exc)
        return None


def list_project_kbs(user_id: str) -> list[ProjectKB]:
    """按 render_committed_at DESC 排序返回所有项目知识包。"""
    d = project_kb_dir(user_id)
    if not d.exists():
        return []
    out: list[ProjectKB] = []
    for f in d.glob("*.json"):
        try:
            kb = ProjectKB.model_validate(json.loads(f.read_text(encoding="utf-8")))
            out.append(kb)
        except Exception as exc:  # noqa: BLE001
            log.warning("[profile] list_project_kbs 跳过 %s: %s", f, exc)
    out.sort(key=lambda k: k.render_committed_at, reverse=True)
    return out
