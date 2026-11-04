#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""tobs_probe.py — 只读探针：把 Tobs 运行时的可用性证据抓成一行 JSON。

用途：Bot「看着回话了」不等于「模型真被调用、活真被派出去」。
每次测试前后各跑一次，比对 diff 才有证据。

用法：
    python -B tools/tobs_probe.py            # 打印一行 JSON
    python -B tools/tobs_probe.py --pretty   # 缩进打印

读的表（都在 ~/.dsh/botharness/botharness.db）：
    usage_receipts            模型调用计费回执（>0 才算真调过模型）
    usage_daily               按天用量
    assignments               派活（工作助理 → 专家）
    messaging_reception_history 入站消息
    channel_records           频道（DM/群）
    persona_bots              Bot 清单
只读打开（mode=ro），不改任何东西。
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

TABLES = (
    "usage_receipts",
    "usage_daily",
    "assignments",
    "request_assignments",
    "messaging_reception_history",
    "inbox_admissions",
    "channel_records",
    "persona_bots",
    "model_presets",
)


def db_path() -> Path:
    explicit = os.environ.get("BOTHARNESS_DB")
    if explicit:
        return Path(explicit)
    home = os.environ.get("USERPROFILE") or os.path.expanduser("~")
    return Path(home) / ".dsh" / "botharness" / "botharness.db"


def counts(conn: sqlite3.Connection) -> dict:
    out: dict[str, object] = {}
    existing = {r[0] for r in conn.execute("select name from sqlite_master where type='table'")}
    for t in TABLES:
        if t not in existing:
            out[t] = None
            continue
        try:
            out[t] = conn.execute(f'select count(*) from "{t}"').fetchone()[0]
        except sqlite3.Error as exc:  # 表在但读不了
            out[t] = f"ERR:{exc.__class__.__name__}"
    return out


def last_usage(conn: sqlite3.Connection) -> list:
    existing = {r[0] for r in conn.execute("select name from sqlite_master where type='table'")}
    if "usage_receipts" not in existing:
        return []
    cols = [r[1] for r in conn.execute('PRAGMA table_info("usage_receipts")')]
    rows = conn.execute('select * from "usage_receipts" order by rowid desc limit 3').fetchall()
    return [dict(zip(cols, r)) for r in rows]


def bots(conn: sqlite3.Connection) -> list:
    out = []
    try:
        for slug, body in conn.execute("select slug, body from persona_bots"):
            try:
                d = json.loads(body)
            except Exception:
                continue
            plan = d.get("modelPlan") or {}
            out.append(
                {
                    "name": d.get("displayName"),
                    "orchestrator": (plan.get("orchestrator") or {}).get("model"),
                    "provider": (plan.get("orchestrator") or {}).get("provider"),
                    "reasoning": (plan.get("orchestrator") or {}).get("reasoningEffort"),
                    "assignment": bool(plan.get("assignmentDefault") or plan.get("assignmentModels")),
                    "assignment_model": ((plan.get("assignmentDefault") or {}).get("model")),
                    "assignment_candidates": len(plan.get("assignmentModels") or []),
                    "approval_template": (d.get("approvalTemplate") or ""),
                }
            )
    except sqlite3.Error:
        pass
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Tobs 运行时只读探针")
    ap.add_argument("--pretty", action="store_true", help="缩进输出")
    ap.add_argument("--db", help="指定 botharness.db 路径")
    args = ap.parse_args(argv)

    path = Path(args.db) if args.db else db_path()
    if not path.is_file():
        print(json.dumps({"ok": False, "error": f"db 不存在: {path}"}, ensure_ascii=False))
        return 2

    uri = path.resolve().as_uri() + "?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as exc:
        print(json.dumps({"ok": False, "error": f"{exc.__class__.__name__}: {exc}"}, ensure_ascii=False))
        return 2
    try:
        payload = {
            "ok": True,
            "db": str(path),
            "counts": counts(conn),
            "bots": bots(conn),
            "last_usage": last_usage(conn),
        }
    finally:
        conn.close()

    print(json.dumps(payload, ensure_ascii=False, indent=2 if args.pretty else None))
    return 0


if __name__ == "__main__":
    sys.exit(main())
