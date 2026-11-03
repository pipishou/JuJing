#!/usr/bin/env python3
"""F2 锚点 / F3 周报 的负例测试（stdlib，无需 pytest）。

跑法：python -B tests/test_anchors_and_report.py
"""
from __future__ import annotations

import json
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from core import anchors, eventlog  # noqa: E402
import flywheel_weekly as fw  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    if not cond:
        print(f"  FAIL {name} {detail}")


def test_anchors(tmp: Path):
    ev = str(tmp / "ev.jsonl")
    ok1 = anchors.card_issued("T-1", {"card_kind": "weekly", "pack": "workops",
                                      "scorecard": {"total": 86.67, "verdict": "output", "label": "直出"},
                                      "is_demo": False, "evidence_tier": "measured"}, events_path=ev)
    ok2 = anchors.card_signed("T-1", signer="老板", report_sha256="a" * 64,
                              card_kind="weekly", elapsed_ms=120000, events_path=ev)
    ok3 = anchors.outcome_recorded("T-1", observed_outcome="逾期率降到 61%", window_days=7, events_path=ev)
    check("anchors.all_true", ok1 and ok2 and ok3)
    rows = eventlog.iter_events(path=ev)
    check("anchors.count3", len(rows) == 3, str(len(rows)))
    names = [r["event"] for r in rows]
    check("anchors.prefix", all(n.startswith("bot_team_") for n in names), str(names))
    check("anchors.order", names == ["bot_team_card_issued", "bot_team_card_signed",
                                     "bot_team_outcome_recorded"], str(names))
    issued = rows[0]["payload"]
    check("anchors.issued_payload", issued["total"] == 86.67 and issued["verdict"] == "output"
          and issued["card_kind"] == "weekly" and issued["is_demo"] is False, json.dumps(issued, ensure_ascii=False))
    check("anchors.signed_payload", rows[1]["payload"]["signer"] == "老板"
          and rows[1]["payload"]["report_sha256"] == "a" * 64)
    check("anchors.outcome_truncated", len(rows[2]["payload"]["observed_outcome"]) <= 200)
    # 坏路径不许抛：锚点是旁证，不能打断主链路
    bad = str(tmp / "no_such_dir_placeholder" / "x.jsonl") if False else str(tmp)
    check("anchors.fail_soft", anchors.card_issued("T-2", {}, events_path=bad) is False)


def mk(ev, ts):
    return {"ts": ts.isoformat(), "event": ev}


def test_report(tmp: Path):
    now = datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc)
    events = []
    for i in range(10):
        t0 = now - timedelta(days=1, minutes=300 - i * 10)
        events.append({**mk("bot_team_card_issued", t0),
                       "payload": {"task_id": f"T-{i}", "card_kind": "attribution", "is_demo": False}})
        if i < 6:
            t1 = t0 + timedelta(seconds=60 * (i + 1))
            events.append({**mk("bot_team_card_signed", t1),
                           "payload": {"task_id": f"T-{i}", "signer": "老板"}})
        if i < 3:
            events.append({**mk("bot_team_outcome_recorded", t0 + timedelta(days=7)),
                           "payload": {"task_id": f"T-{i}", "observed_outcome": "ok"}})
    for i in range(10):
        events.append({**mk("bot_team_reminder_judged", now - timedelta(days=1)),
                       "payload": {"verdict": "ok" if i < 7 else "not_ok"}})
    for i in range(4):
        events.append({**mk("bot_team_memory_change", now - timedelta(days=2)), "payload": {}})

    rep = fw.build(events, days=7, now=now)
    check("report.issued_10", rep["issued"] == 10, str(rep["issued"]))
    check("report.signed_6", rep["signed"] == 6, str(rep["signed"]))
    check("report.n1", rep["n1_adoption_rate"] == 0.6, str(rep["n1_adoption_rate"]))
    check("report.n3_median", rep["n3_decision_seconds_median"] == 210.0,
          str(rep["n3_decision_seconds_median"]))
    check("report.backfill", rep["backfill_rate"] == 0.3, str(rep["backfill_rate"]))
    check("report.worth", rep["worth_rate"] == 0.7, str(rep["worth_rate"]))
    check("report.memory", rep["memory_change_events"] == 4, str(rep["memory_change_events"]))
    check("report.kind", rep["by_card_kind"].get("attribution") == 10, str(rep["by_card_kind"]))
    check("report.health", rep["collection_health"] == "ok", rep["collection_health"])
    check("report.no_notes", rep["notes"] == [], str(rep["notes"]))

    # 样本不足 → 比率必须为 null（不许把 1/1 报成 100%）
    tiny = [{"ts": now.isoformat(), "event": "bot_team_card_issued", "payload": {"task_id": "X"}},
            {"ts": now.isoformat(), "event": "bot_team_card_signed", "payload": {"task_id": "X"}},
            {"ts": now.isoformat(), "event": "bot_team_reminder_judged", "payload": {"verdict": "ok"}}]
    r2 = fw.build(tiny, days=7, now=now)
    check("report.tiny_null_rates", r2["n1_adoption_rate"] is None and r2["worth_rate"] is None, str(r2))
    check("report.tiny_notes", len(r2["notes"]) == 2, str(r2["notes"]))
    check("report.empty", fw.build([], days=7, now=now)["events_in_window"] == 0)

    # 窗口外事件不计
    old = [{"ts": (now - timedelta(days=30)).isoformat(), "event": "bot_team_card_issued",
            "payload": {"task_id": "OLD"}}]
    check("report.window_filter", fw.build(old, days=7, now=now)["issued"] == 0)

    # 事件流不存在 → rc 2
    check("report.missing_rc2", fw.main(["--events", str(tmp / "nope.jsonl")]) == 2)
    # 坏 ts 行不炸
    check("report.bad_ts", fw.build([{"ts": "not-a-date", "event": "bot_team_card_issued"}],
                                    days=7, now=now)["events_in_window"] == 0)


def main() -> int:
    t0 = time.time()
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        test_anchors(tmp)
        test_report(tmp)
    total = len(PASS) + len(FAIL)
    if FAIL:
        print(f"\nRESULT: FAIL（{total - len(FAIL)}/{total} 通过，{len(FAIL)} 红，{time.time()-t0:.2f}s）")
        for n in FAIL:
            print(f"  - {n}")
        return 1
    print(f"\nRESULT: PASS（{total} 项，{time.time()-t0:.2f}s）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
