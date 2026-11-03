#!/usr/bin/env python3
"""Tobs → 我方事件流：白名单采集器（F1）。

只读 Tobs 的 botharness.db（SQLite，WAL），把**少数有判断价值**的行归一成
`bot_team_*` 事件写进我方 `runtime/events.jsonl`，供飞轮与决策链消费。

三条硬纪律（违反即视为缺陷）：
1. 只读：连接串固定 `?mode=ro`，本文件**没有任何 UPDATE/INSERT/DELETE** 语句。
   写它库会与引擎单写者 lease 抢锁，触发 `enterRecovery` → 全库只读。
2. 白名单：不采集 `group-ordinary`（占 90%+ 噪音）、不采集 `self-record`（空正文）。
3. 幂等：以 `rowid` 游标 + 已见 id 集合双保险，重跑不重复发事件。

用法：
    python -B tools/tobs_collect.py --once            # 采一次（cron/手动）
    python -B tools/tobs_collect.py --once --dry-run  # 只打印，不发事件
    python -B tools/tobs_collect.py --stats           # 看游标与库现状
退出码：0 正常；2 参数/库不可用；3 库被锁（可重试）。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core import eventlog  # noqa: E402

STATE_DEFAULT = ROOT / "runtime" / "tobs_collect.state.json"
SEEN_CAP = 5000

# 白名单：source_kind → 我方事件名（不含 bot_team_ 前缀，emit 时自动补）
SOURCE_KIND_MAP = {
    "human-message": "msg_human",
    "bot-message": "msg_bot",
    "memory-change": "memory_change",
    "assignment-lifecycle": "assignment_lifecycle",
}

# inbox_admissions 只保留「真人的、点名的、派活回来的」三类
INBOX_REASON_MAP = {
    "human-dm": "inbound_human_dm",
    "group-mention": "inbound_group_mention",
    "assignment-report": "inbound_assignment_report",
}

SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"oc_sk_[A-Za-z0-9_\-]{8,}"),
    re.compile(r"(?i)(app[_-]?secret|client[_-]?secret|api[_-]?key|token)\s*[:=]\s*\S+"),
    re.compile(r"(?i)Bearer\s+[A-Za-z0-9._\-]{10,}"),
]
MAX_BODY = 800


class CollectError(RuntimeError):
    """库不可用/结构不符。"""


class LockedError(RuntimeError):
    """库被写者占住（可重试）。"""


def redact(text: str) -> str:
    if not text:
        return ""
    out = text
    for pat in SECRET_PATTERNS:
        out = pat.sub("[redacted]", out)
    out = re.sub(r"\s+", " ", out).strip()
    return out[:MAX_BODY]


def db_path(override: str | None = None) -> Path:
    if override:
        return Path(override)
    home = os.environ.get("DSH_HOME") or os.path.expanduser("~/.dsh")
    return Path(home) / "botharness" / "botharness.db"


def connect(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise CollectError(f"数据库不存在：{path}")
    try:
        conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=2)
    except sqlite3.OperationalError as exc:
        raise LockedError(str(exc)) from exc
    conn.row_factory = sqlite3.Row
    return conn


def _rowid_cursor(conn: sqlite3.Connection, table: str) -> int:
    row = conn.execute(f"select max(rowid) as m from {table}").fetchone()
    return int(row["m"] or 0)


def load_state(path: Path) -> dict:
    if not path.is_file():
        return {"cursors": {}, "seen": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"cursors": {}, "seen": []}
    data.setdefault("cursors", {})
    data.setdefault("seen", [])
    return data


def save_state(path: Path, state: dict) -> None:
    seen = state.get("seen", [])[-SEEN_CAP:]
    state["seen"] = seen
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def collect_source_events(conn, cursor: int, limit: int):
    rows = conn.execute(
        "select rowid, source_event_id, source_kind, bot_slug, channel_id, body, created_at"
        " from source_events where rowid > ? order by rowid limit ?",
        (cursor, limit),
    ).fetchall()
    out = []
    for r in rows:
        name = SOURCE_KIND_MAP.get((r["source_kind"] or "").strip())
        if not name:
            continue
        out.append(
            {
                "event": name,
                "actor": r["bot_slug"] or "tobs",
                "source_id": r["source_event_id"],
                "refs": [x for x in (r["bot_slug"], r["channel_id"]) if x],
                "payload": {
                    "source_kind": r["source_kind"],
                    "channel_id": r["channel_id"],
                    "created_at": r["created_at"],
                    "body": redact(r["body"] or ""),
                    "origin": "tobs.db/source_events",
                },
                "rowid": r["rowid"],
            }
        )
    return out, (rows[-1]["rowid"] if rows else cursor)


def collect_inbox(conn, cursor: int, limit: int):
    rows = conn.execute(
        "select rowid, source_event_id, bot_slug, reason, attempt_state, opened_at"
        " from inbox_admissions where rowid > ? order by rowid limit ?",
        (cursor, limit),
    ).fetchall()
    out = []
    for r in rows:
        name = INBOX_REASON_MAP.get((r["reason"] or "").strip())
        if not name:
            continue
        out.append(
            {
                "event": name,
                "actor": r["bot_slug"] or "tobs",
                "source_id": r["source_event_id"],
                "refs": [x for x in (r["bot_slug"],) if x],
                "payload": {
                    "reason": r["reason"],
                    "attempt_state": r["attempt_state"],
                    "opened_at": r["opened_at"],
                    "origin": "tobs.db/inbox_admissions",
                },
                "rowid": r["rowid"],
            }
        )
    return out, (rows[-1]["rowid"] if rows else cursor)


def collect_assignments(conn, cursor: int, limit: int):
    rows = conn.execute(
        "select rowid, session_id, bot_slug, title, stop_state, latest_report_state,"
        " latest_report_at, created_at, updated_at, primary_cwd"
        " from assignments where rowid > ? order by rowid limit ?",
        (cursor, limit),
    ).fetchall()
    out = []
    for r in rows:
        reported = bool(r["latest_report_state"])
        event = "assignment_reported" if reported else "assignment_stopped"
        out.append(
            {
                "event": event,
                "actor": r["bot_slug"] or "tobs",
                "source_id": r["session_id"],
                "refs": [x for x in (r["bot_slug"], r["session_id"]) if x],
                "payload": {
                    "title": redact(r["title"] or ""),
                    "stop_state": r["stop_state"],
                    "report_state": r["latest_report_state"],
                    "report_at": r["latest_report_at"],
                    "created_at": r["created_at"],
                    "updated_at": r["updated_at"],
                    "primary_cwd": r["primary_cwd"],
                    "origin": "tobs.db/assignments",
                },
                "rowid": r["rowid"],
            }
        )
    return out, (rows[-1]["rowid"] if rows else cursor)


TABLES = (
    ("source_events", collect_source_events),
    ("inbox_admissions", collect_inbox),
    ("assignments", collect_assignments),
)


def run_once(db: Path, state_path: Path, limit: int = 200, dry_run: bool = False,
             events_path: str | None = None) -> dict:
    conn = connect(db)
    state = load_state(state_path)
    seen = set(state.get("seen", []))
    emitted, skipped, errors = [], 0, []
    try:
        for table, fn in TABLES:
            cursor = int(state["cursors"].get(table, 0))
            try:
                items, page_max = fn(conn, cursor, limit)
            except sqlite3.OperationalError as exc:
                errors.append(f"{table}: {exc}")
                continue
            max_rowid = max(cursor, page_max)
            for it in items:
                key = f"{table}:{it['source_id']}"
                max_rowid = max(max_rowid, int(it["rowid"]))
                if key in seen:
                    skipped += 1
                    continue
                if not dry_run:
                    eventlog.emit(
                        it["event"],
                        actor=it["actor"],
                        refs=it["refs"],
                        payload=it["payload"],
                        path=events_path,
                    )
                seen.add(key)
                emitted.append({k: v for k, v in it.items() if k != "rowid"})
            state["cursors"][table] = max_rowid
    finally:
        conn.close()
    state["seen"] = list(seen)[-SEEN_CAP:]
    if not dry_run:
        save_state(state_path, state)
    return {
        "ok": not errors,
        "db": str(db),
        "emitted": len(emitted),
        "skipped": skipped,
        "errors": errors,
        "by_event": _count(emitted),
        "dry_run": dry_run,
        "items": emitted[:20],
    }


def _count(items):
    out = {}
    for it in items:
        out[it["event"]] = out.get(it["event"], 0) + 1
    return out


def stats(db: Path, state_path: Path) -> dict:
    conn = connect(db)
    try:
        counts = {}
        for table, _ in TABLES:
            counts[table] = _rowid_cursor(conn, table)
        by_kind = {
            r["source_kind"]: r["c"]
            for r in conn.execute(
                "select source_kind, count(*) c from source_events group by source_kind"
            )
        }
        by_reason = {
            r["reason"]: r["c"]
            for r in conn.execute(
                "select reason, count(*) c from inbox_admissions group by reason"
            )
        }
    finally:
        conn.close()
    state = load_state(state_path)
    return {
        "ok": True,
        "db": str(db),
        "cursors": state.get("cursors", {}),
        "seen": len(state.get("seen", [])),
        "table_rowids": counts,
        "source_kinds": by_kind,
        "inbox_reasons": by_reason,
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Tobs 白名单采集器（只读）")
    ap.add_argument("--once", action="store_true", help="采集一次")
    ap.add_argument("--stats", action="store_true", help="打印游标与库现状")
    ap.add_argument("--db", default=None)
    ap.add_argument("--state", default=str(STATE_DEFAULT))
    ap.add_argument("--events", default=None, help="覆盖事件流路径（测试用）")
    ap.add_argument("--limit", type=int, default=200)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    db = db_path(args.db)
    state_path = Path(args.state)
    try:
        if args.stats:
            res = stats(db, state_path)
        else:
            res = run_once(db, state_path, args.limit, args.dry_run, args.events)
    except CollectError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 2
    except LockedError as exc:
        print(json.dumps({"ok": False, "error": f"库被占用：{exc}", "retry": True},
                         ensure_ascii=False))
        return 3
    if args.json:
        print(json.dumps(res, ensure_ascii=False))
    else:
        print(f"[{'OK' if res.get('ok') else 'WARN'}] {res}")
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
