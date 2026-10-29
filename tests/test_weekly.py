"""E4 周汇总卡单测：口径、样本门、判据、证据快照、端到端可签。"""
import datetime as dt
import json
import os
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
for p in (ROOT, ROOT / "tools"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from core import gates  # noqa: E402
import weekly_digest as W  # noqa: E402

TZ = dt.timezone(dt.timedelta(hours=8))
NOW = dt.datetime(2026, 11, 2, 15, 0, tzinfo=TZ)
START = dt.datetime(2026, 10, 26, 0, 0, tzinfo=TZ)


def ev(seq, event, payload=None, ts="2026-10-28T10:00:00+08:00"):
    return {"seq": seq, "event": event, "payload": payload or {}, "ts": ts,
            "actor": "lead", "event_id": f"e{seq}"}


def items():
    return [
        {"id": "WI-1", "title": "跟进 M-03 报价", "status": "doing", "owner": "老板",
         "duedate": {"confirmed": {"value": "2026-10-28"}}},
        {"id": "WI-2", "title": "样件确认", "status": "done", "owner": "小李",
         "duedate": {"confirmed": {"value": "2026-10-01"}}},
        {"id": "WI-3", "title": "下周计划", "status": "doing", "owner": "老板",
         "duedate": {"confirmed": {"value": "2026-11-10"}}},
    ]


class TestBuild(unittest.TestCase):
    def test_counts_reminders_and_verdicts(self):
        events = [ev(1, "bot_team_reminder_issued"), ev(2, "bot_team_reminder_issued"),
                  ev(3, "bot_team_reminder_judged", {"verdict": "准"}),
                  ev(4, "bot_team_reminder_judged", {"verdict": "不准"})]
        doc = W.build(events, items(), START, NOW)
        m = doc["metrics"]
        self.assertEqual(m["reminders_issued"], 2)
        self.assertEqual(m["judged_ok"], 1)
        self.assertEqual(m["judged_not_ok"], 1)
        self.assertIsNone(m["hit_rate"])  # 样本 2 < 8，不出率

    def test_hit_rate_when_sample_enough(self):
        events = [ev(i, "bot_team_reminder_judged", {"verdict": "准" if i % 2 else "不准"}) for i in range(1, 11)]
        m = W.build(events, [], START, NOW)["metrics"]
        self.assertEqual(m["judged_ok"] + m["judged_not_ok"], 10)
        self.assertEqual(m["hit_rate"], 0.5)

    def test_events_outside_window_ignored(self):
        events = [ev(1, "bot_team_reminder_issued", ts="2026-10-01T10:00:00+08:00"),
                  ev(2, "bot_team_reminder_issued", ts="2026-10-28T10:00:00+08:00")]
        self.assertEqual(W.build(events, [], START, NOW)["metrics"]["reminders_issued"], 1)

    def test_overdue_excludes_done_and_future(self):
        m = W.build([], items(), START, NOW)["metrics"]
        self.assertEqual(m["overdue_now"], 1)  # 只有 WI-1

    def test_summary_mentions_overdue_item(self):
        doc = W.build([], items(), START, NOW)
        self.assertIn("WI-1", doc["weekly"]["summary"])
        self.assertEqual(doc["card_kind"], "weekly")
        self.assertFalse(doc["is_demo"])

    def test_period_and_metrics_present(self):
        doc = W.build([], items(), START, NOW)
        self.assertEqual(doc["period"]["start"], "2026-10-26")
        self.assertEqual(doc["period"]["end"], "2026-11-02")
        for key in W and ("reminders_issued", "judged_ok", "judged_not_ok", "items_changed", "overdue_now"):
            self.assertIn(key, doc["metrics"])


class TestGates(unittest.TestCase):
    def _ref(self):
        import hashlib
        body = (ROOT / "README.md").read_bytes()
        return {"path": "README.md", "sha256": hashlib.sha256(body).hexdigest(), "size": len(body)}

    def _doc(self, **over):
        doc = W.build([], items(), START, NOW)
        doc["task_id"] = "T-20261102-001"
        doc["data_refs"] = [self._ref()]
        doc.update(over)
        return doc

    def test_valid_weekly_passes(self):
        self.assertEqual(gates.weekly_errors(self._doc()), [])

    def test_missing_period_fails(self):
        d = self._doc()
        d.pop("period")
        self.assertTrue(any("period" in e for e in gates.weekly_errors(d)))

    def test_missing_metric_key_fails(self):
        d = self._doc()
        d["metrics"].pop("overdue_now")
        self.assertTrue(any("overdue_now" in e for e in gates.weekly_errors(d)))

    def test_bad_hit_rate_sample_fails(self):
        d = self._doc()
        d["metrics"]["hit_rate"] = 0.9
        self.assertTrue(any("样本" in e for e in gates.weekly_errors(d)))

    def test_negative_metric_fails(self):
        d = self._doc()
        d["metrics"]["reminders_issued"] = -1
        self.assertTrue(any("非负整数" in e for e in gates.weekly_errors(d)))

    def test_real_report_errors_routes_weekly(self):
        d = self._doc()
        d["data_refs"] = []
        errs = gates.real_report_errors(d, base=ROOT)
        self.assertTrue(any("data_refs" in e for e in errs))

    def test_is_demo_true_fails_real_gate(self):
        d = self._doc()
        d["is_demo"] = True
        self.assertTrue(any("is_demo" in e for e in gates.real_report_errors(d, base=ROOT)))


class TestCli(unittest.TestCase):
    def test_cli_writes_advisory_and_snapshot(self):
        tmp = tempfile.TemporaryDirectory()
        root = pathlib.Path(tmp.name)
        events_file = root / "events.jsonl"
        events_file.write_text("\n".join(json.dumps(ev(i, "bot_team_reminder_issued"), ensure_ascii=False)
                                         for i in range(1, 4)) + "\n", encoding="utf-8")
        wi = root / "store.json"
        wi.write_text(json.dumps({"items": items()}, ensure_ascii=False), encoding="utf-8")
        out = root / "weekly" / "advisory.json"
        rc = W.main(["--out", str(out), "--events", str(events_file), "--workitems", str(wi),
                     "--start", "2026-10-26", "--end", "2026-11-02"])
        self.assertEqual(rc, 0)
        doc = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(doc["metrics"]["reminders_issued"], 3)
        self.assertTrue(doc["sources"])
        self.assertTrue(pathlib.Path(doc["sources"][0]).exists())
        self.assertEqual(len(doc["sources"]), 1)
        tmp.cleanup()

    def test_cli_dry_run_does_not_write(self):
        tmp = tempfile.TemporaryDirectory()
        root = pathlib.Path(tmp.name)
        out = root / "advisory.json"
        rc = W.main(["--out", str(out), "--events", str(root / "none.jsonl"),
                     "--start", "2026-10-26", "--end", "2026-11-02", "--dry-run"])
        self.assertEqual(rc, 0)
        self.assertFalse(out.exists())
        tmp.cleanup()


if __name__ == "__main__":
    unittest.main(verbosity=2)
