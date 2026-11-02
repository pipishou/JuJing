"""注意力层 P0 回归（2026-11-02）。

覆盖三条审查结论：
- P0-1 票状态机：open/deferred/snoozed 可 decide；decided 终态 409；
  deferred 在 /today 可见；有 /resume 恢复路径。
- P0-2 WIP 可见性：超过 3 件时 /today 必给「另有 N 件未显示」；paused 可见；
  import 走 enforce_wip；/resume 对非停泊项 409。
- P0-3 每日配额：每 agent 每天 3 次打断，第 4 张写 suppressed 留痕；
  次日重置；one_way 穿透；focus 同时只允许一个，切换需 override。
"""
from __future__ import annotations

import datetime
import importlib.util
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

SPEC = importlib.util.spec_from_file_location("bot_team_app_p0", ROOT / "server" / "app.py")
APP = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(APP)

TICKET_FIELDS = {"question": "选哪个方案？", "options": ["A", "B"],
                 "recommendation": "A", "deadline": "2026-11-05"}


class TestAttentionP0(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.env = {}
        for key, name in (("BOT_ATTENTION_FILE", "attention.jsonl"),
                          ("BOT_EVENTS_FILE", "events.jsonl"),
                          ("BOT_WORKITEMS_FILE", "workitems.json"),
                          ("BOT_TICKETS_FILE", "tickets.jsonl"),
                          ("BOT_FOCUS_FILE", "focus.json")):
            self.env[key] = os.environ.get(key)
            os.environ[key] = str(self.root / name)
        # cfg.root 必须是仓库根（tools/ 在这里）；各账本路径由上面的 env 指到临时目录。
        self.cfg = APP.Config(ROOT)
        self.ws = APP.import_tool(self.cfg, "workitems_store")
        self.path = APP.workitems_path(self.cfg)
        self.ws.save(self.ws.empty_store(), self.path)

    def tearDown(self):
        for key, old in self.env.items():
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old
        self.tmp.cleanup()

    # ---------------------------------------------------------------- helpers
    def _store(self):
        return self.ws.load(self.path)

    def _add(self, n, status="doing"):
        store = self._store()
        for i in range(n):
            store, item = self.ws.add_item(store, {
                "title": f"事项{len(store['items']) + 1}", "owner": "老板",
                "due": "2026-12-31", "status": status,
                "priority": "II", "section": "管理建设",
            }, by="老板")
        self.ws.save(store, self.path)
        return store

    def _post(self, path, payload):
        return APP.route_api(self.cfg, "POST", path, {}, payload)

    def _open(self, **extra):
        body = dict(TICKET_FIELDS)
        body.update(extra)
        return self._post("/api/v1/attention/tickets", body)

    def _today(self, query=None):
        return APP.route_api(self.cfg, "GET", "/api/v1/attention/today", query or {}, None)[1]

    # ------------------------------------------------------- P0-1 状态机
    def test_defer_then_decide_succeeds_and_deferred_visible(self):
        self._add(1)
        status, body = self._open()
        self.assertEqual(status, 201)
        tid = body["ticket"]["id"]
        status, body = self._post(f"/api/v1/attention/tickets/{tid}/defer",
                                  {"until": "2026-11-03"})
        self.assertEqual(status, 200)
        self.assertEqual(body["ticket"]["status"], "deferred")
        today = self._today()
        self.assertIn(tid, [t["id"] for t in today["tickets_deferred"]])
        self.assertIn("已推迟", today["brief"])
        status, body = self._post(f"/api/v1/attention/tickets/{tid}/decide", {"choice": "A"})
        self.assertEqual(status, 200)
        self.assertEqual(body["ticket"]["status"], "decided")
        self.assertEqual(tickets.get_ticket(tid, root=self.root)["status"], "decided")

    def test_decided_is_terminal_and_conflict_has_reason(self):
        status, body = self._open()
        tid = body["ticket"]["id"]
        self._post(f"/api/v1/attention/tickets/{tid}/decide", {"choice": "A"})
        with self.assertRaises(APP.ApiError) as ctx:
            self._post(f"/api/v1/attention/tickets/{tid}/decide", {"choice": "A"})
        self.assertEqual(ctx.exception.status, 409)
        self.assertIn("decided", str(ctx.exception))
        self.assertIn("终态", str(ctx.exception))

    def test_snoozed_then_resume_returns_to_open(self):
        status, body = self._open()
        tid = body["ticket"]["id"]
        status, body = self._post(f"/api/v1/attention/tickets/{tid}/snooze",
                                  {"until": "2026-11-04T09:00:00+08:00"})
        self.assertEqual(status, 200)
        self.assertEqual(body["ticket"]["status"], "snoozed")
        status, body = self._post(f"/api/v1/attention/tickets/{tid}/resume", {})
        self.assertEqual(status, 200)
        self.assertEqual(body["ticket"]["status"], "open")

    def test_snoozed_can_be_decided_directly(self):
        status, body = self._open()
        tid = body["ticket"]["id"]
        self._post(f"/api/v1/attention/tickets/{tid}/snooze",
                   {"until": "2026-11-04T09:00:00+08:00"})
        status, body = self._post(f"/api/v1/attention/tickets/{tid}/decide", {"choice": "B"})
        self.assertEqual(status, 200)
        self.assertEqual(body["ticket"]["status"], "decided")
        self.assertEqual(body["ticket"]["choice"], "B")

    # ------------------------------------------------------- P0-2 WIP 可见
    def test_today_never_hides_silently_and_paused_is_visible(self):
        self._add(8)
        self._add(1, status="paused")
        today = self._today()
        self.assertEqual(len(today["items"]), 3)
        self.assertEqual(today["wip"]["active"], 8)
        self.assertEqual(today["hidden_count"], 5)
        self.assertIn("另有 5 件未显示", today["brief"])
        self.assertTrue(today["wip"]["over_hard_limit"])
        self.assertIn("超过硬上限", today["brief"])
        self.assertEqual(today["paused_count"], 1)
        self.assertIn("paused", today["brief"])
        with_paused = self._today({"include": ["paused"]})
        self.assertEqual(len(with_paused["items"]), 4)

    def test_import_goes_through_enforce_wip(self):
        self._add(2)
        csv_path = self.root / "导入.csv"
        csv_path.write_text(
            "问题点描述,责任人,计划完成时间,状态\n"
            "导入一,老板,2026-12-01,待办\n"
            "导入二,老板,2026-12-02,待办\n"
            "导入三,老板,2026-12-03,待办\n",
            encoding="utf-8-sig")
        result = APP.workitems_import(self.cfg, {"path": str(csv_path), "by": "测试导入"})
        self.assertEqual(result["stats"]["added"], 3)
        self.assertEqual(result["stats"]["wip_parked"], 3)
        self.assertEqual(result["wip"]["active"], 2)
        store = self._store()
        new = [it for it in store["items"] if str(it.get("title", "")).startswith("导入")]
        self.assertEqual(len(new), 3)
        self.assertTrue(all(it["attention"]["parked"] for it in new))

    def test_resume_non_parked_paused_item_409(self):
        self._add(1, status="paused")
        item = self._store()["items"][0]
        with self.assertRaises(APP.ApiError) as ctx:
            APP.route_api(self.cfg, "POST", f"/api/v1/workitems/{item['id']}/resume", {}, {})
        self.assertEqual(ctx.exception.status, 409)
        self.assertIn("不是停泊项", str(ctx.exception))

    # ---------------------------------------------------- P0-3 配额/focus
    def test_fourth_ticket_suppressed_with_trace(self):
        for i in range(3):
            status, _ = self._open(agent_id="agent-a", question=f"第{i + 1}个问题？")
            self.assertEqual(status, 201)
        status, body = self._open(agent_id="agent-a", question="第4个问题？")
        self.assertEqual(status, 200)
        self.assertTrue(body["suppressed"])
        self.assertEqual(body["reason"], "daily_quota")
        self.assertEqual(body["ticket"]["status"], "suppressed")
        usage = tickets.daily_usage("agent-a", root=self.root)
        self.assertEqual(usage["used"], 3)
        self.assertEqual(usage["suppressed"], 1)
        day = datetime.datetime.now().astimezone().date().isoformat()
        self.assertEqual(len(tickets.list_suppressed(root=self.root, date=day)), 1)
        today = self._today()
        self.assertEqual(len(today["suppressed_today"]), 1)
        self.assertIn("被抑制", today["brief"])

    def test_quota_resets_next_day(self):
        for i in range(3):
            tickets.open_ticket("问？", ["A", "B"], "A", "2026-11-05",
                                agent_id="agent-b", now="2026-11-02T09:00:00+08:00",
                                root=self.root)
        fourth = tickets.open_ticket("问？", ["A", "B"], "A", "2026-11-05",
                                     agent_id="agent-b", now="2026-11-02T18:00:00+08:00",
                                     root=self.root)
        self.assertEqual(fourth["status"], "suppressed")
        next_day = tickets.open_ticket("问？", ["A", "B"], "A", "2026-11-05",
                                       agent_id="agent-b", now="2026-11-03T09:00:00+08:00",
                                       root=self.root)
        self.assertEqual(next_day["status"], "open")

    def test_one_way_bypasses_quota(self):
        for i in range(3):
            tickets.open_ticket("问？", ["A", "B"], "A", "2026-11-05",
                                agent_id="agent-c", root=self.root)
        blocked = tickets.open_ticket("问？", ["A", "B"], "A", "2026-11-05",
                                      agent_id="agent-c", root=self.root)
        self.assertEqual(blocked["status"], "suppressed")
        gate = tickets.open_ticket("单向门？", ["A", "B"], "A", "2026-11-05",
                                   agent_id="agent-c", one_way=True, root=self.root)
        self.assertEqual(gate["status"], "open")

    def test_focus_single_item_requires_override_and_suppresses_tickets(self):
        status, body = self._post("/api/v1/attention/focus",
                                  {"item": "写方案", "minutes": 90})
        self.assertEqual(status, 200)
        self.assertTrue(body["focus"]["active"])
        self.assertEqual(body["focus"]["item"], "写方案")
        with self.assertRaises(APP.ApiError) as ctx:
            self._post("/api/v1/attention/focus", {"item": "另一个"})
        self.assertEqual(ctx.exception.status, 409)
        status, body = self._post("/api/v1/attention/focus",
                                  {"item": "另一个", "override": True})
        self.assertEqual(status, 200)
        self.assertEqual(body["focus"]["item"], "另一个")
        status, body = self._open(agent_id="agent-d")
        self.assertEqual(status, 200)
        self.assertEqual(body["reason"], "focus_active")
        status, body = APP.route_api(self.cfg, "DELETE", "/api/v1/attention/focus", {}, {})[0:2]
        self.assertEqual(status, 200)
        self.assertFalse(body["focus"]["active"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
