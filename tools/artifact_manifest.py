#!/usr/bin/env python3
"""任务产物清单（F2.5）：`runs/<task>/artifacts/manifest.json`。

每张卡背后要有一套产物，产物本身要进采集——否则飞轮只知道结论，不知道依据。
本工具做四件事：扫描 → 算 sha256 → 写清单 → 发 `bot_team_artifact_saved` 事件；
另有 `verify` 反向核对（文件与清单是否一致）。

纪律：
- 清单是**唯一事实源**：报告里的 data_refs 必须能在清单里找到。
- 同名不覆盖：自动加 `-2`、`-3` 后缀（产物是证据，不能互相顶掉）。
- 敏感文件拒绝入清单（`*.key/*.pem/.env*/*credential*`），不写也不发事件。
- 路径必须是 artifacts/ 下的相对名，禁止 `..` 与绝对路径。

用法：
    python -B tools/artifact_manifest.py build  --task-dir runs/T-20261103-001 --task-id T-20261103-001
    python -B tools/artifact_manifest.py verify --task-dir runs/T-20261103-001
    python -B tools/artifact_manifest.py list   --task-dir runs/T-20261103-001
退出码：0 一致/成功；1 校验不一致；2 参数或路径错误。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core import eventlog  # noqa: E402

ARTIFACT_DIR = "artifacts"
MANIFEST_NAME = "manifest.json"
MANIFEST_SCHEMA = "artifact.manifest.v1"

TYPE_BY_EXT = {
    ".csv": "table", ".xlsx": "table", ".xls": "table", ".tsv": "table",
    ".png": "image", ".jpg": "image", ".jpeg": "image", ".webp": "image", ".svg": "image",
    ".md": "doc", ".txt": "doc", ".docx": "doc", ".pdf": "doc",
    ".json": "json", ".jsonl": "json",
    ".log": "log",
}
SENSITIVE = re.compile(r"(?i)(\.key$|\.pem$|^\.env|credential|secret|passwd|password)")
MAX_FILE = 64 * 1024 * 1024  # 单产物 64MB 上限（超出只登记引用，不搬字节）


class ManifestError(RuntimeError):
    """参数/路径/敏感文件等可归因错误。"""


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def artifact_dir(task_dir: Path) -> Path:
    return Path(task_dir) / ARTIFACT_DIR


def safe_name(name: str) -> str:
    if not name or name in (".", ".."):
        raise ManifestError(f"非法文件名：{name!r}")
    if Path(name).is_absolute() or "/" in name or "\\" in name or ".." in Path(name).parts:
        raise ManifestError(f"只接受 artifacts/ 下的相对文件名：{name!r}")
    if SENSITIVE.search(name):
        raise ManifestError(f"拒绝入清单的敏感文件：{name}")
    return name


def classify(name: str) -> str:
    return TYPE_BY_EXT.get(Path(name).suffix.lower(), "other")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def unique_name(directory: Path, name: str) -> str:
    """不覆盖：同名自动加序号。"""
    target = directory / name
    if not target.exists():
        return name
    stem, suffix = Path(name).stem, Path(name).suffix
    for i in range(2, 1000):
        cand = f"{stem}-{i}{suffix}"
        if not (directory / cand).exists():
            return cand
    raise ManifestError(f"同名文件过多：{name}")


def scan(task_dir: Path, is_demo: bool = False) -> list[dict]:
    """扫描 artifacts/ 下真实存在的文件（不含 manifest 自身）。"""
    adir = artifact_dir(task_dir)
    if not adir.is_dir():
        return []
    items = []
    for p in sorted(adir.iterdir()):
        if not p.is_file() or p.name == MANIFEST_NAME:
            continue
        try:
            name = safe_name(p.name)
        except ManifestError:
            continue  # 敏感文件静默跳过（不写清单、不发事件）
        size = p.stat().st_size
        items.append(
            {
                "name": name,
                "type": classify(name),
                "size": size,
                "sha256": sha256_file(p) if size <= MAX_FILE else None,
                "source": "upload",
                "created_at": datetime.fromtimestamp(p.stat().st_mtime).astimezone().isoformat(timespec="seconds"),
                "is_demo": bool(is_demo),
                "omitted": size > MAX_FILE,
            }
        )
    return items


def manifest_path(task_dir: Path) -> Path:
    return artifact_dir(task_dir) / MANIFEST_NAME


def load(task_dir: Path) -> dict | None:
    p = manifest_path(task_dir)
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ManifestError(f"清单不可解析：{exc}") from exc


def build(task_dir: Path, task_id: str, is_demo: bool = False, emit: bool = True,
          events_path: str | None = None) -> dict:
    task_dir = Path(task_dir)
    if not task_dir.is_dir():
        raise ManifestError(f"任务目录不存在：{task_dir}")
    if not task_id:
        raise ManifestError("task_id 必填")
    items = scan(task_dir, is_demo=is_demo)

    # 去重：同 sha256 + 同 name 只留一条
    dedup, seen = [], set()
    for it in items:
        key = (it["name"], it["sha256"])
        if key in seen:
            continue
        seen.add(key)
        dedup.append(it)

    doc = {
        "schema": MANIFEST_SCHEMA,
        "task_id": task_id,
        "generated_at": _now(),
        "is_demo": bool(is_demo),
        "count": len(dedup),
        "total_bytes": sum(i["size"] for i in dedup),
        "artifacts": dedup,
    }
    path = manifest_path(task_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)

    emitted = 0
    if emit:
        for it in dedup:
            eventlog.emit(
                "artifact_saved",
                actor="bot-team",
                refs=[task_id, f"{ARTIFACT_DIR}/{it['name']}"],
                payload={
                    "task_id": task_id,
                    "name": it["name"],
                    "type": it["type"],
                    "size": it["size"],
                    "sha256": it["sha256"],
                    "is_demo": it["is_demo"],
                    "origin": "artifact_manifest",
                },
                path=events_path,
            )
            emitted += 1
    doc["emitted_events"] = emitted
    return doc


def verify(task_dir: Path) -> dict:
    """清单 ↔ 实际文件 双向核对。"""
    task_dir = Path(task_dir)
    doc = load(task_dir)
    if doc is None:
        return {"ok": False, "errors": ["清单不存在"], "missing": [], "changed": [], "extra": []}
    recorded = {i["name"]: i for i in doc.get("artifacts", [])}
    actual = {i["name"]: i for i in scan(task_dir, is_demo=bool(doc.get("is_demo")))}

    missing = sorted(set(recorded) - set(actual))
    extra = sorted(set(actual) - set(recorded))
    changed = []
    for name in sorted(set(recorded) & set(actual)):
        r, a = recorded[name], actual[name]
        if r.get("sha256") != a.get("sha256") or r.get("size") != a.get("size"):
            changed.append(name)
    errors = []
    if missing:
        errors.append(f"清单有、磁盘无：{len(missing)} 个")
    if extra:
        errors.append(f"磁盘有、清单无：{len(extra)} 个")
    if changed:
        errors.append(f"内容被改：{len(changed)} 个")
    return {
        "ok": not errors,
        "task_id": doc.get("task_id"),
        "count": len(recorded),
        "missing": missing,
        "extra": extra,
        "changed": changed,
        "errors": errors,
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="任务产物清单")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--task-dir", required=True)
    b.add_argument("--task-id", required=True)
    b.add_argument("--is-demo", action="store_true")
    b.add_argument("--no-emit", action="store_true")
    v = sub.add_parser("verify")
    v.add_argument("--task-dir", required=True)
    ls = sub.add_parser("list")
    ls.add_argument("--task-dir", required=True)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    try:
        if args.cmd == "build":
            res = build(Path(args.task_dir), args.task_id, args.is_demo, not args.no_emit)
            code = 0
        elif args.cmd == "verify":
            res = verify(Path(args.task_dir))
            code = 0 if res["ok"] else 1
        else:
            doc = load(Path(args.task_dir))
            res = {"ok": doc is not None, "manifest": doc}
            code = 0 if doc else 1
    except ManifestError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(res, ensure_ascii=False) if args.json else
          f"[{'OK' if res.get('ok') else 'FAIL'}] {res}")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
