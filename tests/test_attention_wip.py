"""Slice 2：WIP≤2、今日三件、停泊/恢复、票的硬校验。"""
import importlib.util
import json
import os
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
for p in (ROOT, ROOT / "server"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from core import attention, tickets  # noqa: E402

SPEC = importlib.util.spec_from_file_location("bot_team_app_wip", ROOT / "server" / "app.py")
APP = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(APP)


class TestWipAndTickets(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.env = {}
        for key, name in (("BOT_ATTENTION_FILE", "attention.jsonl"),
                          ("BOT_EVENTS_FILE", "events.jsonl"),
                          ("BOT_WORKITEMS_FILE", "workitems.json"),
                          ("BOT_TICKETS_FILE", "tickets.jsonl")):
            self.env[key] = os.environ.get(key)
            os.environ[key] = str(self.root / name)
        self.cfg = APP.Config(ROOT)
        self.ws = APP.import_tool(self.cfg, "workitems_store")
        store = self.ws.empty_store()
        for i, title in enumerate(("A 任务", "B 任务"), 1):
            store, _ = self.ws.add_item(store, {
                "title": title, "owner": "老板", "due": "2026-11-05",
                "status": "doing", "priority": "I" if i == 1 else "II",
            }, by="老板")
        self.ws.save(store, APP.workitems_path(self.cfg))

    def tearDown(self):
        for key, old in self.env.items():
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old
        self.tmp.cleanup()

    def _store(self):
        return self.ws.load(APP.workitems_path(self.cfg))

    def test_third_task_is_parked(self):
        body = {"action": "add", "item": {"title": "C 任务", "owner": "老板",
                                          "due": "2026-11-06", "status": "todo"}}
        result = APP.workitems_mutate(self.cfg, body)
        self.assertTrue(result["item"]["attention"]["parked"])
        self.assertEqual(result["item"]["status"], "paused")
        self.assertEqual(attention.wip_count(self._store()["items"]), 2)

    def test_today_has_three_and_queue_has_parked(self):
        APP.workitems_mutate(self.cfg, {"action": "add", "item": {
            "title": "C 任务", "owner": "老板", "due": "2026-11-06", "status": "todo"}})
        APP.workitems_mutate(self.cfg, {"action": "add", "item": {
            "title": "D 任务", "owner": "老板", "due": "2026-11-07", "status": "todo"}})
        today = APP.route_api(self.cfg, "GET", "/api/v1/attention/today", {}, None)[1]
        self.assertEqual(len(today["items"]), 2)          # only 2 active slots
        self.assertIn("今日 2 件", today["brief"])
        queue = APP.route_api(self.cfg, "GET", "/api/v1/attention/queue", {}, None)[1]
        self.assertEqual(len(queue["active"]), 2)
        self.assertEqual(len(queue["parked"]), 2)

    def test_park_releases_then_resume(self):
        APP.workitems_mutate(self.cfg, {"action": "add", "item": {
            "title": "C 任务", "owner": "老板", "due": "2026-11-06", "status": "todo"}})
        store = self._store()
        parked = [it for it in store["items"] if attention.is_parked(it)][0]
        with self.assertRaises(APP.ApiError) as ctx:
            APP.route_api(self.cfg, "POST", f"/api/v1/workitems/{parked['id']}/resume", {}, {})
        self.assertEqual(ctx.exception.status, 409)
        active = [it for it in store["items"] if not attention.is_parked(it)][0]
        APP.route_api(self.cfg, "POST", f"/api/v1/workitems/{active['id']}/park", {},
                      {"reason": "manual"})
        result = APP.route_api(self.cfg, "POST", f"/api/v1/workitems/{parked['id']}/resume", {}, {})
        self.assertEqual(result[0], 200)
        self.assertFalse(result[1]["item"]["attention"]["parked"])

    def test_ticket_requires_recommendation_and_choice(self):
        with self.assertRaises(tickets.TicketError):
            tickets.open_ticket("选哪个？", ["A", "B"], "", "2026-11-05",
                                root=self.root)
        ticket = tickets.open_ticket("选哪个？", ["A", "B"], "A", "2026-11-05",
                                     root=self.root)
        self.assertEqual(ticket["status"], "open")
        with self.assertRaises(tickets.TicketError):
            tickets.decide(ticket["id"], "C", root=self.root)
        decided = tickets.decide(ticket["id"], "A", root=self.root)
        self.assertEqual(decided["status"], "decided")
        self.assertEqual(decided["choice"], "A")

    def test_ticket_two_questions_rejected(self):
        with self.assertRaises(tickets.TicketError):
            tickets.open_ticket("做 A 还是 B？什么时候做？", ["A", "B"], "A",
                                "2026-11-05", root=self.root)

    def test_ticket_api_open_and_decide(self):
        status, body = APP.route_api(self.cfg, "POST", "/api/v1/attention/tickets", {}, {
            "question": "选哪个？", "options": ["A", "B"], "recommendation": "A",
            "deadline": "2026-11-05",
        })
        self.assertEqual(status, 201)
        tid = body["ticket"]["id"]
        status, body = APP.route_api(self.cfg, "POST", f"/api/v1/attention/tickets/{tid}/decide", {},
                                     {"choice": "A", "note": "测试"})
        self.assertEqual(status, 200)
        self.assertEqual(body["ticket"]["status"], "decided")


if __name__ == "__main__":
    unittest.main(verbosity=2)
