#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""tobs_chat_export.py — 把 Tobs 群聊/频道事件导成可 diff、可 git 同步的 jsonl。

为什么需要它：
    Tobs 的群聊数据在 `~/.dsh/botharness/botharness.db`（sqlite）里。sqlite **不能**进 git
    （二进制、每机一份、合并即冲突）。所以「跨设备」的可行做法是：
        事实源 = 本机 sqlite
        同步面 = 每天一个 jsonl 追加文件（append-only，git 合并几乎不出冲突）

导什么（都是只读 `mode=ro`）：
    channel_records      频道（群/私聊）清单与成员
    inbox_admissions     每条群消息给每个 Bot 生成的「准入记录」（reason=group-ordinary 等）
    messaging_outbox     出站意图（若为空说明没有程序化发消息）
    messaging_reception_history  入站历史（若为空说明消息不落这张表）

用法：
    python -B tools/tobs_chat_export.py --out D:/.../bot-memory/_chat      # 默认导出到当天文件
    python -B tools/tobs_chat_export.py --out ... --day 2026-11-03
    python -B tools/tobs_chat_export.py --schema          # 只打印各表结构（排查用）
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import date, datetime, timezone
from pathlib import Path

TABLES = (
    "channel_records",
    "inbox_admissions",
    "messaging_outbox",
    "messaging_reception_history",
)


def db_path() -> Path:
    explicit = os.environ.get("BOTHARNESS_DB")
    if explicit:
        return Path(explicit)
    home = os.environ.get("USERPROFILE") or os.path.expanduser("~")
    return Path(home) / ".dsh" / "botharness" / "botharness.db"


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("select name from sqlite_master where type='table'")}


def schema(conn: sqlite3.Connection) -> dict:
    out = {}
    for t in TABLES:
        if t in _tables(conn):
            out[t] = [r[1] for r in conn.execute(f'PRAGMA table_info("{t}")')]
        else:
            out[t] = None
    return out


def _rows(conn: sqlite3.Connection, table: str) -> list[dict]:
    if table not in _tables(conn):
        return []
    cols = [r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')]
    return [dict(zip(cols, r)) for r in conn.execute(f'select * from "{table}"')]


def _jsonable(v):
    if isinstance(v, bytes):
        return f"<bytes:{len(v)}>"
    if isinstance(v, (str, int, float, bool)) or v is None:
        return v
    return str(v)


def export(db: Path, out_dir: Path, day: str) -> dict:
    """导出某一天。返回统计，便于 CI 与人工核对。"""
    conn = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        stats: dict[str, int] = {}
        for t in TABLES:
            rows = _rows(conn, t)
            kept = []
            for r in rows:
                stamp = r.get("opened_at") or r.get("observed_at") or r.get("created_at")
                if isinstance(stamp, str) and stamp[:10] != day:
                    continue
                kept.append({k: _jsonable(v) for k, v in r.items()})
            path = out_dir / f"{day}.{t}.jsonl"
            with path.open("a", encoding="utf-8") as fh:
                for r in kept:
                    fh.write(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n")
            stats[t] = len(kept)
        return {"ok": True, "day": day, "out": str(out_dir), "stats": stats}
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="导出 Tobs 群聊事件为 jsonl（跨设备可 git 同步）")
    ap.add_argument("--out", help="输出目录（建议 bot-memory/_chat）")
    ap.add_argument("--day", help="导出哪一天 YYYY-MM-DD（默认今天）")
    ap.add_argument("--db", help="指定 botharness.db")
    ap.add_argument("--schema", action="store_true", help="只打印表结构")
    args = ap.parse_args(argv)

    db = Path(args.db) if args.db else db_path()
    if not db.is_file():
        print(json.dumps({"ok": False, "error": f"db 不存在: {db}"}, ensure_ascii=False))
        return 2

    if args.schema:
        conn = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            print(json.dumps(schema(conn), ensure_ascii=False, indent=2))
        finally:
            conn.close()
        return 0

    if not args.out:
        print(json.dumps({"ok": False, "error": "缺少 --out（导出目录）"}, ensure_ascii=False))
        return 2

    day = args.day or date.today().isoformat()
    result = export(db, Path(args.out), day)
    result["exported_at"] = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
