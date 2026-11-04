#!/usr/bin/env python3
"""落盘即回交 —— 把 Bot 落在磁盘上的交付物接进我方证据链。

背景：工作助理习惯把成果写到 `deliverables/` 目录就不管了（实测 1298 次工具调用里
对 127.0.0.1:8787 的调用 = 0）。本工具不依赖 Bot 自觉，扫目录、建单、回交、登记产物。

只写我方 runs/ 目录，不碰 Tobs 的库与进程（外挂式原则）。

用法：
    python -B tools/artifact_bridge.py --dir "D:/work/产品类/个人产品/企微+飞书/deliverables" --dry-run
    python -B tools/artifact_bridge.py --dir <同上> --by 工作助理
    python -B tools/artifact_bridge.py --dir <同上> --status        # 看已回交记录
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import eventlog  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
import artifact_manifest  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
STATE_NAME = "artifact_bridge.json"
TEXT_EXT = (".md", ".txt", ".json", ".csv")
SKIP_PREFIX = ("00_", "_")  # 说明件 / 原件不在交接面
MAX_DELIVERABLE = 200_000


class BridgeError(Exception):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def state_path(runtime: Path | None = None) -> Path:
    base = Path(runtime) if runtime else ROOT / "runtime"
    return base / STATE_NAME


def load_state(path: Path | None = None) -> dict:
    p = Path(path) if path else state_path()
    if not p.exists():
        return {"schema": "artifact.bridge.state.v1", "processed": {}, "tasks": {}}
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BridgeError(f"桥接状态文件损坏：{p}（{exc}）") from exc
    if not isinstance(doc, dict) or "processed" not in doc:
        raise BridgeError(f"桥接状态文件格式不对：{p}")
    return doc


def save_state(doc: dict, path: Path | None = None) -> Path:
    p = Path(path) if path else state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, p)
    return p


def scan(directory: Path) -> list[dict]:
    """列出可回交的交付物（跳过说明件、内部原件、隐藏文件）。"""
    d = Path(directory)
    if not d.is_dir():
        raise BridgeError(f"目录不存在：{d}")
    out = []
    for p in sorted(d.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
        if not p.is_file():
            continue
        if p.name.startswith(".") or p.name.startswith(SKIP_PREFIX):
            continue
        if p.suffix.lower() not in TEXT_EXT:
            continue
        st = p.stat()
        out.append({"name": p.name, "path": p, "bytes": st.st_size, "mtime": int(st.st_mtime)})
    return out


def next_task_id(runs: Path, when: datetime | None = None) -> str:
    day = (when or datetime.now()).strftime("%Y%m%d")
    seq = 1
    while (runs / f"T-{day}-{seq:03d}").exists():
        seq += 1
    return f"T-{day}-{seq:03d}"


def compose(files: list[dict], title: str) -> str:
    """把交付物合成一份 deliverable.md（保留逐文件来源与 sha）。"""
    lines = [f"# {title}", "", f"> 来源：{len(files)} 个落盘文件，由 artifact_bridge 于 {_now()} 汇总", ""]
    for f in files:
        body = f["path"].read_text(encoding="utf-8", errors="replace")
        lines += [f"## {f['name']}", "", f"<!-- sha256:{_sha(f['path'])} bytes:{f['bytes']} -->", "", body.strip(), ""]
    text = "\n".join(lines)
    if len(text) > MAX_DELIVERABLE:
        text = text[:MAX_DELIVERABLE] + "\n\n> 已截断：超过 200000 字上限\n"
    return text


def bridge(directory: Path, *, by: str = "老板", title: str = "", runs_root: Path | None = None,
           dry_run: bool = False, state: Path | None = None, runtime: Path | None = None) -> dict:
    files = scan(directory)
    st = load_state(state)
    fresh = [f for f in files if _sha(f["path"]) not in st["processed"]]
    if not fresh:
        return {"ok": True, "created": False, "reason": "no_new_files", "scanned": len(files)}

    runs = Path(runs_root) if runs_root else ROOT / "runs"
    task_id = next_task_id(runs)
    ttl = title or f"{Path(directory).parent.name or '交付'} · {datetime.now().strftime('%Y-%m-%d')} 交付"
    body = compose(fresh, ttl)
    payload = {
        "ok": True, "created": True, "task_id": task_id, "title": ttl, "by": by,
        "files": [{"name": f["name"], "bytes": f["bytes"], "sha256": _sha(f["path"])} for f in fresh],
        "deliverable_bytes": len(body.encode("utf-8")),
        "deliverable_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
        "dry_run": dry_run,
    }
    if dry_run:
        return payload

    tdir = runs / task_id
    adir = tdir / "artifacts"
    adir.mkdir(parents=True, exist_ok=True)
    for f in fresh:
        dst = adir / artifact_manifest.unique_name(adir, artifact_manifest.safe_name(f["name"]))
        dst.write_bytes(f["path"].read_bytes())
    (tdir / "deliverable.md").write_text(body, encoding="utf-8")
    (tdir / "task.json").write_text(json.dumps({
        "task_id": task_id, "title": ttl, "source": "artifact_bridge",
        "scene_id": "scene-workitems-weekly", "crew_id": "c-workops-review",
        "created_at": _now(), "created_by": by,
        "deliverable": {"path": "deliverable.md", "sha256": payload["deliverable_sha256"]},
        "sources": [{"name": f["name"], "bytes": f["bytes"], "sha256": _sha(f["path"])} for f in fresh],
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    (tdir / "status.json").write_text(json.dumps(
        {"task_id": task_id, "step": "delivered", "state": "created", "updated_at": _now()},
        ensure_ascii=False, indent=2), encoding="utf-8")

    for f in fresh:
        st["processed"][_sha(f["path"])] = {"name": f["name"], "task_id": task_id, "at": _now()}
    st["tasks"][task_id] = {"dir": str(directory), "files": len(fresh), "at": _now()}
    save_state(st, state)

    events = eventlog_path(runtime)
    eventlog.emit("bot_team_deliverable_saved", actor=by, path=events,
                  payload={"task_id": task_id, "name": "deliverable.md",
                           "bytes": payload["deliverable_bytes"],
                           "sha256": payload["deliverable_sha256"], "by": by,
                           "sources": len(fresh)})
    man = artifact_manifest.build(tdir, task_id=task_id, events_path=str(events), is_demo=False)
    payload["manifest"] = {"count": man.get("count", 0), "ok": man.get("ok", True)}
    return payload


def eventlog_path(runtime: Path | None = None) -> Path:
    base = Path(runtime) if runtime else ROOT / "runtime"
    return base / "events.jsonl"


def status(path: Path | None = None) -> dict:
    st = load_state(path)
    return {"ok": True, "processed": len(st["processed"]), "tasks": st["tasks"]}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="落盘即回交桥")
    ap.add_argument("--dir", help="交付物目录")
    ap.add_argument("--by", default="老板")
    ap.add_argument("--title", default="")
    ap.add_argument("--runs", default="")
    ap.add_argument("--state", default="")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    try:
        if args.status:
            out = status(Path(args.state) if args.state else None)
        elif args.dir:
            out = bridge(Path(args.dir), by=args.by, title=args.title,
                         runs_root=Path(args.runs) if args.runs else None,
                         dry_run=args.dry_run,
                         state=Path(args.state) if args.state else None)
        else:
            ap.print_help()
            return 2
    except BridgeError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 2

    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        if out.get("created") is False:
            print(f"[SKIP] 无新文件（已扫 {out.get('scanned')} 个）")
        else:
            print(f"[OK] {out['task_id']} ← {len(out['files'])} 个文件，deliverable "
                  f"{out['deliverable_bytes']}B sha {out['deliverable_sha256'][:12]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
