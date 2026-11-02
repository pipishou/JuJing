"""Slice 1 注意力基线：窗口/打断/票/WIP 峰值/账本 append-only。"""
import datetime as dt
import json
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import attention  # noqa: E402

TZ = dt.timezone(dt.timedelta(hours=8))
NOW = dt.datetime(2026, 11, 1, 12, 0, 0, tzinfo=TZ)


def iso(days_ago=0, hour=9):
    return (NOW - dt.timedelta(days=days_ago)).replace(hour=hour, minute=0, second=0).isoformat()


def ev(name, when, seq, payload=None, actor="test"):
    return {"seq": seq, "ts": when, "event": name, "actor": actor,
            "event_id": f"e{seq}", "payload": payload or {}}


class TestMetrics(unittest.TestCase):
    def test_empty_returns_zero_and_no_fake_rate(self):
        out = attention.metrics(events=[], days=3, now=NOW)
        self.assertEqual(out["answers"], {"interruptions": 0, "tickets_processed": 0, "wip_peak": 0})
        self.assertFalse(out["data_quality"]["history"])
        self.assertEqual(out["window"]["days"], 3)

    def test_window_filters_old_events(self):
        rows = [
            ev("bot_team_reminder_issued", iso(1), 1, {"kind": "hard", "source": "ledger_due"}),
            ev("bot_team_reminder_issued", iso(4), 2, {"kind": "soft", "source": "ai"}),
        ]
        out = attention.metrics(events=rows, days=3, now=NOW)
        self.assertEqual(out["answers"]["interruptions"], 1)
        self.assertEqual(out["interruptions"]["by_kind"], {"hard": 1})
        self.assertEqual(out["data_quality"]["events_in_window"], 1)

    def test_tickets_and_wip_peak(self):
        rows = [
            ev("bot_team_reminder_issued", iso(2), 1, {"kind": "hard", "source": "ledger_due"}),
            ev("bot_team_reminder_issued", iso(1), 2, {"kind": "soft", "source": "ai"}),
            ev("bot_team_item_created", iso(2, 10), 3, {"item_id": "WI-1"}),
            ev("bot_team_item_created", iso(2, 11), 4, {"item_id": "WI-2"}),
            ev("bot_team_item_closed", iso(1, 10), 5, {"item_id": "WI-1"}),
            ev("bot_team_item_created", iso(1, 11), 6, {"item_id": "WI-3"}),
            ev("attention_task_switch", iso(1, 12), 7, {"from": "A", "to": "B"}),
        ]
        out = attention.metrics(events=rows, days=3, now=NOW)
        self.assertEqual(out["answers"]["interruptions"], 2)
        self.assertEqual(out["answers"]["tickets_processed"], 1)
        self.assertEqual(out["answers"]["wip_peak"], 2)
        self.assertEqual(out["tickets"], {"opened": 3, "resolved": 1, "open_now": 2})
        self.assertEqual(out["task_switches"], 1)


class TestLedger(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_append_writes_event_and_projects_ledger(self):
        a = attention.append("attention_interrupt", {"kind": "hard"}, root=self.root, ts=iso(0))
        b = attention.append("attention_task_switch", {"from": "A", "to": "B"}, root=self.root, ts=iso(0))
        # 事实源：全局事件流（bot_team_attention_*），seq 严格递增。
        events = attention.eventlog.iter_events(attention.events_path(self.root))
        self.assertEqual((a["seq"], b["seq"]), (1, 2))
        self.assertEqual([e["event"] for e in events],
                         ["bot_team_attention_interrupt", "bot_team_attention_task_switch"])
        # 账本只是投影：内容必须等于事件流重算的结果。
        path = attention.ledger_path(self.root)
        lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
        self.assertEqual(len(lines), 2)
        self.assertEqual([json.loads(ln) for ln in lines], attention.project_ledger(events))
        self.assertEqual(json.loads(lines[0])["event"], "attention_interrupt")
        self.assertEqual(json.loads(lines[0])["source_seq"], 1)

    def test_bad_name_rejected(self):
        with self.assertRaises(attention.AttentionError):
            attention.append("bot_team_nope", {}, root=self.root)

    def test_snapshot_records_metrics(self):
        out = attention.snapshot(root=self.root, days=3, now=NOW)
        self.assertTrue(out["ok"])
        ledger = attention.read_ledger(self.root)
        self.assertEqual(ledger[-1]["event"], "attention_snapshot")
        self.assertEqual(ledger[-1]["payload"]["metrics"]["answers"]["interruptions"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
