#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""提醒供给层机检（core/reminder_feed.py）。

    python tests/test_reminder_feed.py
"""
from __future__ import annotations

import datetime as dt
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core import eventlog, reminder_feed  # noqa: E402

TZ = dt.timezone(dt.timedelta(hours=8))
NOW = dt.datetime(2026, 10, 29, 10, 0, tzinfo=TZ)
FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    if ok:
        print(f"[OK]   {name}")
    else:
        FAILS.append(name)
        print(f"[FAIL] {name} {detail}")


def items(*rows):
    return {"schema": "workitems.store.v1", "revision": 1, "items": list(rows)}


def item(i, **kw):
    base = {"id": f"WI-{i:04d}", "title": f"事项{i}", "owner": "赵蓉", "status": "doing", "due": None}
    base.update(kw)
    return base


def ev(event, ts, **payload):
    return {"event": f"bot_team_{event}", "ts": ts, "payload": payload}


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "events.jsonl"

        # 1) 账本到期兜底
        doc = items(item(1, due="2026-10-27"), item(2, due="2026-11-05"), item(3, due="2026-10-20", status="done"))
        f = reminder_feed.feed([], doc, NOW)
        ids = [r["item_id"] for r in f["rows"]]
        check("账本到期兜底只提醒已过期的在办事项", ids == ["WI-0001"], str(ids))
        check("提醒带 kind=hard 与中文标签", f["rows"][0]["kind"] == "hard" and f["rows"][0]["kind_label"] == "到期未完成")
        check("逾期天数被算出来", f["rows"][0]["overdue_days"] == 2, str(f["rows"][0].get("overdue_days")))

        # 2) 空账本
        empty = reminder_feed.feed([], items(), NOW)
        check("空账本零提醒", empty["counts"]["total"] == 0 and empty["to_emit"] == [])

        # 3) 纯函数同输入同输出
        a = json.dumps(reminder_feed.feed([], doc, NOW), ensure_ascii=False, sort_keys=True)
        b = json.dumps(reminder_feed.feed([], doc, NOW), ensure_ascii=False, sort_keys=True)
        check("feed 是纯函数", a == b)

        # 4) 发事件 + 当日幂等
        written = reminder_feed.emit_issued(f["to_emit"], path=path, actor="lead")
        check("emit_issued 写事件且带前缀", len(written) == 1 and written[0]["event"] == "bot_team_reminder_issued")
        events = eventlog.iter_events(path)
        f2 = reminder_feed.feed(events, doc, NOW)
        check("同日已发 → issued_today 且不再重复发", f2["rows"][0]["issued_today"] is True and f2["to_emit"] == [])

        # 5) 跨天可以再发
        tomorrow = NOW + dt.timedelta(days=1)
        f3 = reminder_feed.feed(events, doc, tomorrow)
        check("跨天后重新可发", len(f3["to_emit"]) == 1)

        # 6) 判定 ok / not_ok
        reminder_feed.judge("WI-0001", "ok", path=path, actor="老板")
        events = eventlog.iter_events(path)
        f4 = reminder_feed.feed(events, doc, NOW)
        check("判 ok 后当日不再发（已发过）", f4["to_emit"] == [])
        check("判 ok 不算抑制", f4["suppressed"] == [], str(f4["suppressed"]))

        doc2 = items(item(5, due="2026-10-25"))
        reminder_feed.judge("WI-0005", "not_ok", path=path, actor="老板")
        events = eventlog.iter_events(path)
        f5 = reminder_feed.feed(events, doc2, NOW)
        row5 = [r for r in f5["rows"] if r["item_id"] == "WI-0005"]
        check("判不准后当日抑制且不发", row5 and row5[0]["suppressed_today"] is True and f5["to_emit"] == [])

        # 7) 非法输入 fail-closed
        for bad in ("na", "", "yes"):
            try:
                reminder_feed.judge("WI-0001", bad, path=path)
                check(f"非法 verdict {bad!r} 被拒", False)
            except ValueError:
                check(f"非法 verdict {bad!r} 被拒", True)
        try:
            reminder_feed.judge("", "ok", path=path)
            check("空 item_id 被拒", False)
        except ValueError:
            check("空 item_id 被拒", True)

        # 8) 事件侧提醒优先于账本兜底（due_confirmed）
        doc3 = items(item(7, due="2026-10-27"))
        e = [ev("duedate_confirmed", "2026-10-28T09:00:00+08:00", item_id="WI-0007", value="2026-10-28", by="老板")]
        f6 = reminder_feed.feed(e, doc3, NOW)
        rows7 = [r for r in f6["rows"] if r["item_id"] == "WI-0007"]
        check("事件侧到期占位，不重复出账本行", len(rows7) == 1 and rows7[0]["source"] != "ledger_due",
              str(rows7[:1]))

        # 8.5) 当日主发上限
        many = items(*[item(20 + i, due="2026-10-28") for i in range(6)])
        f7 = reminder_feed.feed([], many, NOW)
        check("单日主发上限 %d 条，其余只留在列表" % reminder_feed.MAX_EMIT_PER_DAY,
              len(f7["to_emit"]) == reminder_feed.MAX_EMIT_PER_DAY and f7["counts"]["total"] == 6,
              f'{len(f7["to_emit"])}/{f7["counts"]["total"]}')

        # 9) stats
        st = reminder_feed.stats(eventlog.iter_events(path))
        check("stats 计数一致", st["issued"] == 1 and st["judged"] == 2 and st["ok"] == 1 and st["not_ok"] == 1, json.dumps(st))
        check("judge_rate = 判定/发出", st["judge_rate"] == 2.0, str(st["judge_rate"]))
        check("无发出时 judge_rate 为 None", reminder_feed.stats([])["judge_rate"] is None)

    print()
    if FAILS:
        print(f"RESULT: FAIL（{len(FAILS)} 项）：{FAILS}")
        return 1
    print("RESULT: PASS（提醒供给层全绿）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
