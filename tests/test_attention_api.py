"""Slice 1 API 路由：/api/v1/attention/metrics 只读、参数校验、可序列化。"""
import importlib.util
import os
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "server") not in sys.path:
    sys.path.insert(0, str(ROOT / "server"))

from core import attention  # noqa: E402

SPEC = importlib.util.spec_from_file_location("bot_team_app", ROOT / "server" / "app.py")
APP = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(APP)


class TestAttentionApi(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        (self.root / "runtime").mkdir(parents=True, exist_ok=True)
        self.events = self.root / "runtime" / "events.jsonl"
        rows = [
            {"seq": 1, "ts": attention.now_iso(), "event": "bot_team_reminder_issued",
             "event_id": "e1", "payload": {"kind": "hard", "source": "ledger_due"}},
            {"seq": 2, "ts": attention.now_iso(), "event": "bot_team_item_created",
             "event_id": "e2", "payload": {"item_id": "WI-1"}},
        ]
        self.events.write_text("\n".join(__import__("json").dumps(r, ensure_ascii=False) for r in rows) + "\n",
                               encoding="utf-8")
        self.old_events = os.environ.get("BOT_EVENTS_FILE")
        os.environ["BOT_EVENTS_FILE"] = str(self.events)
        self.cfg = APP.Config(self.root)

    def tearDown(self):
        if self.old_events is None:
            os.environ.pop("BOT_EVENTS_FILE", None)
        else:
            os.environ["BOT_EVENTS_FILE"] = self.old_events
        self.tmp.cleanup()

    def test_route_returns_baseline(self):
        status, body = APP.route_api(self.cfg, "GET", "/api/v1/attention/metrics",
                                     {"days": ["3"]}, None)
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["answers"]["interruptions"], 1)
        self.assertEqual(body["tickets"]["opened"], 1)

    def test_bad_days_is_400(self):
        with self.assertRaises(APP.ApiError) as ctx:
            APP.route_api(self.cfg, "GET", "/api/v1/attention/metrics", {"days": ["0"]}, None)
        self.assertEqual(ctx.exception.status, 400)


if __name__ == "__main__":
    unittest.main(verbosity=2)
