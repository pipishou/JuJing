"""E2 提醒引擎单测：优先级 / 硬软 / 抑制 / 次数与间隔 / 3 天转周汇总 / 幂等。"""
import datetime as dt
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import reminders as R  # noqa: E402

NOW = dt.datetime(2026, 10, 29, 15, 0, tzinfo=dt.timezone(dt.timedelta(hours=8)))


def ev(seq, event, payload=None, refs=None, actor="system", ts=None):
    return {"seq": seq, "event": event, "payload": payload or {}, "refs": refs or {},
            "actor": actor, "ts": ts or "2026-10-29T10:00:00+08:00", "event_id": f"e{seq}"}


def item(iid="WI-1", status="doing", title="跟进 M-03 报价", updated_at=None, owner="老板"):
    return {"id": iid, "status": status, "title": title, "owner": owner, "updated_at": updated_at}


class TestProjections(unittest.TestCase):
    def test_confirmed_wins_over_proposal(self):
        events = [
            ev(1, "bot_team_duedate_proposed", {"value": "2026-10-28", "confidence": 0.6}, {"item_id": "WI-1"}),
            ev(2, "bot_team_duedate_confirmed", {"value": "2026-10-30", "by": "老板"}, {"item_id": "WI-1"}),
        ]
        dd = R.projections(events)["duedates"]["WI-1"]
        self.assertEqual(dd["proposal"]["value"], "2026-10-28")
        self.assertEqual(dd["confirmed"]["value"], "2026-10-30")
        self.assertEqual(dd["confirmed"]["by"], "老板")

    def test_verdict_attached_to_reminder(self):
        events = [
            ev(1, "bot_team_reminder_issued", {"reminder_id": "rm1", "kind": "hard"}, {"item_id": "WI-1"}),
            ev(2, "bot_team_reminder_judged", {"reminder_id": "rm1", "verdict": "不准"}),
        ]
        self.assertEqual(R.projections(events)["reminders"]["WI-1"][0]["verdict"], "不准")

    def test_followups_open_closed(self):
        events = [
            ev(1, "bot_team_followup_queued", {"followup_id": "f1", "next_action": "问报价"}),
            ev(2, "bot_team_followup_queued", {"followup_id": "f2", "next_action": "催样件"}),
            ev(3, "bot_team_followup_closed", {"followup_id": "f1"}),
        ]
        proj = R.projections(events)
        self.assertEqual([f["followup_id"] for f in proj["open_followups"]], ["f2"])

    def test_projection_is_order_independent(self):
        events = [
            ev(2, "bot_team_duedate_confirmed", {"value": "2026-10-30"}, {"item_id": "WI-1"}),
            ev(1, "bot_team_duedate_proposed", {"value": "2026-10-28"}, {"item_id": "WI-1"}),
        ]
        a = R.projections(events)["duedates"]
        b = R.projections(list(reversed(events)))["duedates"]
        self.assertEqual(a, b)


class TestEvaluate(unittest.TestCase):
    def test_confirmed_due_today_is_hard(self):
        events = [ev(1, "bot_team_duedate_confirmed", {"value": "2026-10-29"}, {"item_id": "WI-1"})]
        plan = R.evaluate(events, [item()], NOW)
        self.assertEqual(len(plan), 1)
        self.assertEqual(plan[0]["kind"], "hard")
        self.assertEqual(plan[0]["n_th"], 1)

    def test_proposal_only_is_soft(self):
        events = [ev(1, "bot_team_duedate_proposed", {"value": "2026-10-29", "confidence": 0.5}, {"item_id": "WI-1"})]
        self.assertEqual(R.evaluate(events, [item()], NOW)[0]["kind"], "soft")

    def test_future_due_not_reminded(self):
        events = [ev(1, "bot_team_duedate_confirmed", {"value": "2026-11-05"}, {"item_id": "WI-1"})]
        self.assertEqual(R.evaluate(events, [item()], NOW), [])

    def test_done_item_not_reminded(self):
        events = [ev(1, "bot_team_duedate_confirmed", {"value": "2026-10-20"}, {"item_id": "WI-1"})]
        self.assertEqual(R.evaluate(events, [item(status="done")], NOW), [])

    def test_state_change_outranks_due(self):
        events = [ev(1, "bot_team_duedate_confirmed", {"value": "2026-10-20"}, {"item_id": "WI-1"})]
        it = item(status="blocked", updated_at="2026-10-29T09:00:00+08:00")
        plan = R.evaluate(events, [it], NOW)
        self.assertEqual(len(plan), 1)
        self.assertEqual(plan[0]["source"], "state_change")
        self.assertEqual(plan[0]["priority"], 3)

    def test_stale_state_change_not_reminded(self):
        it = item(status="blocked", updated_at="2026-10-20T09:00:00+08:00")
        self.assertEqual(R.evaluate([], [it], NOW), [])

    def test_verdict_no_suppresses(self):
        events = [
            ev(1, "bot_team_duedate_confirmed", {"value": "2026-10-29"}, {"item_id": "WI-1"}),
            ev(2, "bot_team_reminder_issued", {"reminder_id": "r1", "kind": "hard", "n_th": 1}, {"item_id": "WI-1"},
               ts="2026-10-28T09:00:00+08:00"),
            ev(3, "bot_team_reminder_judged", {"reminder_id": "r1", "verdict": "别烦"}),
        ]
        self.assertEqual(R.evaluate(events, [item()], NOW), [])

    def test_20h_gap_respected(self):
        events = [
            ev(1, "bot_team_duedate_confirmed", {"value": "2026-10-29"}, {"item_id": "WI-1"}),
            ev(2, "bot_team_reminder_issued", {"reminder_id": "r1", "kind": "hard", "n_th": 1}, {"item_id": "WI-1"},
               ts="2026-10-29T05:00:00+08:00"),
        ]
        self.assertEqual(R.evaluate(events, [item()], NOW), [])

    def test_second_reminder_after_gap(self):
        events = [
            ev(1, "bot_team_duedate_confirmed", {"value": "2026-10-29"}, {"item_id": "WI-1"}),
            ev(2, "bot_team_reminder_issued", {"reminder_id": "r1", "kind": "hard", "n_th": 1}, {"item_id": "WI-1"},
               ts="2026-10-28T09:00:00+08:00"),
        ]
        plan = R.evaluate(events, [item()], NOW)
        self.assertEqual(plan[0]["n_th"], 2)

    def test_max_two_hard_reminders(self):
        events = [
            ev(1, "bot_team_duedate_confirmed", {"value": "2026-10-29"}, {"item_id": "WI-1"}),
            ev(2, "bot_team_reminder_issued", {"reminder_id": "r1", "kind": "hard", "n_th": 1}, {"item_id": "WI-1"},
               ts="2026-10-27T09:00:00+08:00"),
            ev(3, "bot_team_reminder_issued", {"reminder_id": "r2", "kind": "hard", "n_th": 2}, {"item_id": "WI-1"},
               ts="2026-10-28T09:00:00+08:00"),
        ]
        self.assertEqual(R.evaluate(events, [item()], NOW), [])

    def test_one_row_per_item_highest_priority(self):
        events = [
            ev(1, "bot_team_duedate_proposed", {"value": "2026-10-29"}, {"item_id": "WI-1"}),
            ev(2, "bot_team_duedate_confirmed", {"value": "2026-10-29"}, {"item_id": "WI-1"}),
        ]
        plan = R.evaluate(events, [item()], NOW)
        self.assertEqual(len(plan), 1)
        self.assertEqual(plan[0]["source"], "due_confirmed")

    def test_pure_function_same_input_same_output(self):
        events = [ev(1, "bot_team_duedate_confirmed", {"value": "2026-10-29"}, {"item_id": "WI-1"})]
        items = [item()]
        self.assertEqual(R.evaluate(events, items, NOW), R.evaluate(list(events), list(items), NOW))

    def test_sorted_by_priority_then_id(self):
        events = [
            ev(1, "bot_team_duedate_proposed", {"value": "2026-10-29"}, {"item_id": "WI-2"}),
            ev(2, "bot_team_duedate_confirmed", {"value": "2026-10-29"}, {"item_id": "WI-3"}),
            ev(3, "bot_team_duedate_confirmed", {"value": "2026-10-29"}, {"item_id": "WI-1"}),
        ]
        ids = [r["item_id"] for r in R.evaluate(events, [item("WI-1"), item("WI-2"), item("WI-3")], NOW)]
        self.assertEqual(ids, ["WI-1", "WI-3", "WI-2"])


class TestStale(unittest.TestCase):
    def test_three_days_goes_to_review(self):
        events = [
            ev(1, "bot_team_reminder_issued", {"reminder_id": "r1", "n_th": 2}, {"item_id": "WI-1"},
               ts="2026-10-20T09:00:00+08:00"),
        ]
        self.assertEqual([r["item_id"] for r in R.due_for_review(events, NOW, stale_days=3)], ["WI-1"])

    def test_fresh_reminder_not_in_review(self):
        events = [ev(1, "bot_team_reminder_issued", {"reminder_id": "r1"}, {"item_id": "WI-1"},
                     ts="2026-10-29T09:00:00+08:00")]
        self.assertEqual(R.due_for_review(events, NOW, stale_days=3), [])

    def test_suppressed_not_in_review(self):
        events = [
            ev(1, "bot_team_reminder_issued", {"reminder_id": "r1"}, {"item_id": "WI-1"},
               ts="2026-10-10T09:00:00+08:00"),
            ev(2, "bot_team_reminder_judged", {"reminder_id": "r1", "verdict": "不准"}),
        ]
        self.assertEqual(R.due_for_review(events, NOW, stale_days=3), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
