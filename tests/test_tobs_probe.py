#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""tools/tobs_probe.py 的自测：用临时 sqlite 造一个假 botharness.db。

不碰真实 ~/.dsh/botharness/botharness.db。
"""
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import tobs_probe  # noqa: E402


def build_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        create table usage_receipts (id text primary key, ts text, model text);
        create table usage_daily (day text primary key, n integer);
        create table assignments (id text primary key, bot text);
        create table request_assignments (id text primary key);
        create table messaging_reception_history (id text primary key);
        create table inbox_admissions (id text primary key);
        create table channel_records (id text primary key);
        create table persona_bots (slug text primary key, body text);
        create table model_presets (slug text primary key);
        insert into usage_receipts values ('u1', '2026-11-02T09:00:00Z', 'deepseek-v4.1-flash');
        insert into usage_receipts values ('u2', '2026-11-02T09:01:00Z', 'deepseek-v4.1-flash');
        insert into assignments values ('a1', '工作助理');
        """
    )
    body = {
        "displayName": "工作助理",
        "modelPlan": {
            "orchestrator": {
                "provider": "dsh-opencode-go",
                "model": "deepseek-v4.1-flash",
                "reasoningEffort": "high",
            }
        },
    }
    conn.execute("insert into persona_bots values ('assistant', ?)", (json.dumps(body),))
    body2 = {
        "displayName": "业务专家",
        "modelPlan": {
            "orchestrator": {"provider": "dsh-opencode-go", "model": "deepseek-v4.1-flash"},
            "assignmentDefault": {"provider": "dsh-opencode-go", "model": "deepseek-v4.1-flash", "reasoningEffort": "high"},
        },
    }
    conn.execute("insert into persona_bots values ('biz', ?)", (json.dumps(body2),))
    conn.commit()
    conn.close()


CASES: list[tuple[str, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    CASES.append((name, "PASS" if ok else "FAIL"))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="tobsprobe-"))
    db = tmp / "botharness.db"
    build_db(db)

    payload = {
        "ok": True,
        "counts": tobs_probe_counts(db),
        "bots": tobs_probe_bots(db),
        "last_usage": tobs_probe_usage(db),
    }
    check("counts.usage_receipts=2", payload["counts"]["usage_receipts"] == 2, str(payload["counts"]))
    check("counts.assignments=1", payload["counts"]["assignments"] == 1)
    check("counts.persona_bots=2", payload["counts"]["persona_bots"] == 2)
    check("counts.model_presets=0", payload["counts"]["model_presets"] == 0)
    names = [b["name"] for b in payload["bots"]]
    check("bots 名称解析", names == ["工作助理", "业务专家"], str(names))
    asg = {b["name"]: b["assignment"] for b in payload["bots"]}
    check("assignment 缺失=False", asg["工作助理"] is False)
    check("assignment 存在=True", asg["业务专家"] is True)
    check("assignment_model 带出型号", any(b.get("assignment_model") for b in payload["bots"]))
    check("reasoning 透出", payload["bots"][0]["reasoning"] == "high")
    check("last_usage 条数≤3", len(payload["last_usage"]) == 2)
    check("last_usage 字段带 model", payload["last_usage"][0].get("model") == "deepseek-v4.1-flash")

    # CLI：--db 指向真实文件 rc=0；不存在 rc=2
    rc_ok = tobs_probe.main(["--db", str(db)])
    check("CLI 正常 db rc=0", rc_ok == 0)
    rc_missing = tobs_probe.main(["--db", str(tmp / "nope.db")])
    check("CLI 缺 db rc=2", rc_missing == 2)

    # 空库（没有任何表）不应崩
    empty = tmp / "empty.db"
    sqlite3.connect(empty).close()
    try:
        c = tobs_probe_counts(empty)
        check("空库不崩、全部 None", all(v is None for v in c.values()), str(c))
    except Exception as exc:  # noqa: BLE001
        check("空库不崩、全部 None", False, repr(exc))

    fails = [n for n, s in CASES if s == "FAIL"]
    print(f"\n结果：{len(CASES) - len(fails)}/{len(CASES)} 通过" + (f"，失败：{fails}" if fails else ""))
    return 1 if fails else 0


# --- 直接复用 tobs_probe 的内部实现（避免重复解析逻辑） ---
def _conn(db: Path) -> sqlite3.Connection:
    return sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)


def tobs_probe_counts(db: Path) -> dict:
    conn = _conn(db)
    try:
        return tobs_probe.counts(conn)
    finally:
        conn.close()


def tobs_probe_bots(db: Path) -> list:
    conn = _conn(db)
    try:
        return tobs_probe.bots(conn)
    finally:
        conn.close()


def tobs_probe_usage(db: Path) -> list:
    conn = _conn(db)
    try:
        return tobs_probe.last_usage(conn)
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
