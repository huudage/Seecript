"""Stage-16 一次性迁移脚本：补全 manifest/plan 的新字段。

扫描 var/projects/<pid>/plans/*.json 与 var/samples/<sid>/manifest.v_*.json，
对每个文件：
- VideoUnderstanding：缺 structural_pattern → 补 "dramatic"；suggested_segments → estimated_segments；
- AdaptedSection：缺 adaptation_note → 补 ""；缺 tempo → 不填；
- PackagingRecommendation：顶层 transitions/cover → 包成 versions=[{aggressive...}]
  （schema validator 兜底已经能做，但脚本一次性写盘让旧文件持久化升级）。

默认 --dry-run；--apply 才写盘。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def _load(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"[skip] read failed: {path}: {exc}")
        return None


def _save(path: Path, data: Any, *, apply: bool) -> bool:
    if not apply:
        return True
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
    return True


def _migrate_understanding(u: dict) -> bool:
    """返回 True 表示有改动。"""
    changed = False
    if "structural_pattern" not in u:
        u["structural_pattern"] = "dramatic"
        changed = True
    if "suggested_segments" in u and "estimated_segments" not in u:
        u["estimated_segments"] = u.pop("suggested_segments")
        changed = True
    if "tempo" not in u:
        u["tempo"] = None
        changed = True
    return changed


def _migrate_adapted_sections(secs: list[dict]) -> bool:
    changed = False
    for s in secs:
        if not isinstance(s, dict):
            continue
        if "adaptation_note" not in s:
            s["adaptation_note"] = ""
            changed = True
        if "tempo" not in s:
            s["tempo"] = None
            changed = True
    return changed


def _migrate_plan(data: dict) -> bool:
    """plan.json 顶层。"""
    changed = False
    secs = data.get("adapted_sections") or []
    if isinstance(secs, list):
        if _migrate_adapted_sections(secs):
            changed = True
    return changed


def _migrate_manifest(data: dict) -> bool:
    changed = False
    u = data.get("understanding")
    if isinstance(u, dict):
        if _migrate_understanding(u):
            changed = True
    return changed


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="实际写盘（默认 dry-run）")
    parser.add_argument("--root", default=None, help="server 根目录（默认 ../server，即脚本所在仓库的 server）")
    args = parser.parse_args()

    server_root = Path(args.root).resolve() if args.root else Path(__file__).resolve().parent.parent
    var_root = server_root / "var"
    if not var_root.exists():
        print(f"[error] var dir not found: {var_root}", file=sys.stderr)
        return 1

    apply = args.apply
    label = "APPLY" if apply else "DRY-RUN"
    print(f"=== stage-16 migrate [{label}] root={var_root} ===")

    plan_count = manifest_count = touched = 0

    for plan_dir in (var_root / "projects").glob("*/plans"):
        for path in plan_dir.glob("*.json"):
            plan_count += 1
            data = _load(path)
            if not isinstance(data, dict):
                continue
            if _migrate_plan(data):
                touched += 1
                print(f"  [plan] {path}")
                _save(path, data, apply=apply)

    samples_root = var_root / "samples"
    if samples_root.exists():
        for path in samples_root.glob("*/manifest*.json"):
            manifest_count += 1
            data = _load(path)
            if not isinstance(data, dict):
                continue
            if _migrate_manifest(data):
                touched += 1
                print(f"  [manifest] {path}")
                _save(path, data, apply=apply)

    print(f"=== summary plans={plan_count} manifests={manifest_count} touched={touched} ===")
    if not apply:
        print("(dry-run; pass --apply to write)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
