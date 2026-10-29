"""E3 团长对话单测：轮次上限 / 超时收敛 / 分歧即停 / 建议不执行 / 事件埋点 / 落盘。"""
import datetime as dt
import json
import os
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import dialog as D  # noqa: E402
from core import eventlog  # noqa: E402

TZ = dt.timezone(dt.timedelta(hours=8))
NOW = dt.datetime(2026, 10, 29, 15, 0, tzinfo=TZ)


def fake_chat(payload: dict | str, *, boom=False):
    def _chat(messages, **kwargs):
        if boom:
            raise RuntimeError("upstream 502")
        if isinstance(payload, str):
            return payload
        return json.dumps(payload, ensure_ascii=False)
    return _chat


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        os.environ["BOT_DIALOGS_DIR"] = str(self.root / "dialogs")
        os.environ["BOT_EVENTS_FILE"] = str(self.root / "events.jsonl")
        self.items = [
            {"id": "WI-1", "title": "跟进 M-03 报价", "status": "doing", "owner": "老板",
             "duedate": {"confirmed": {"value": "2026-10-28"}}},
            {"id": "WI-2", "title": "样件确认", "status": "done", "owner": "小李"},
        ]

    def tearDown(self):
        os.environ.pop("BOT_DIALOGS_DIR", None)
        os.environ.pop("BOT_EVENTS_FILE", None)
        self.tmp.cleanup()

    def events(self):
        return [json.loads(line) for line in (self.root / "events.jsonl").read_text(encoding="utf-8").splitlines() if line]


class TestDialog(Base):
    def test_open_emits_event_and_persists(self):
        dg = D.open_dialog(root=self.root, now=NOW)
        self.assertEqual(dg["status"], "open")
        self.assertEqual(dg["rounds"], 0)
        self.assertTrue((self.root / "dialogs" / f"{dg['dialog_id']}.json").exists())
        self.assertEqual(self.events()[0]["event"], "bot_team_dialog_opened")

    def test_single_turn_returns_reply(self):
        dg = D.open_dialog(root=self.root, now=NOW)
        out = D.turn(dg, "今天先处理什么", items=self.items, chat=fake_chat({"reply": "先做 WI-1"}),
                     root=self.root, now=NOW)
        self.assertTrue(out["ok"])
        self.assertEqual(out["status"], "open")
        self.assertEqual(out["round"], 1)
        self.assertIn("WI-1", out["reply"])

    def test_context_includes_overdue_and_skips_done(self):
        block = D.context_block(self.items, NOW.date())
        self.assertIn("WI-1", block)
        self.assertIn("逾期", block)
        self.assertNotIn("WI-2", block)

    def test_json_fence_is_parsed(self):
        dg = D.open_dialog(root=self.root, now=NOW)
        raw = '```json\n{"reply": "建议先复核口径", "decision": {"type": "none", "reason": "缺数据"}, "unresolved": ["口径未定"]}\n```'
        out = D.turn(dg, "这个月怎么复盘", chat=fake_chat(raw), root=self.root, now=NOW)
        self.assertEqual(out["reply"], "建议先复核口径")
        self.assertEqual(dg["unresolved"], ["口径未定"])

    def test_plain_text_reply_still_works(self):
        dg = D.open_dialog(root=self.root, now=NOW)
        out = D.turn(dg, "随便聊聊", chat=fake_chat("好，先看逾期那两件。"), root=self.root, now=NOW)
        self.assertIn("逾期", out["reply"])

    def test_lead_can_recommend_create_task_but_nothing_executes(self):
        dg = D.open_dialog(root=self.root, now=NOW)
        payload = {"reply": "我把这件事派给业务专家。", "decision": {"type": "create_task", "reason": "需要现场口径"}}
        out = D.turn(dg, "帮我查一下 B 线补料率", chat=fake_chat(payload), root=self.root, now=NOW)
        self.assertEqual(out["decision"]["type"], "create_task")
        # 只记录建议，不产生任务目录
        self.assertFalse((self.root / "runs").exists())
        self.assertEqual(dg["decisions"][0]["type"], "create_task")

    def test_llm_failure_converges_without_hanging(self):
        dg = D.open_dialog(root=self.root, now=NOW)
        out = D.turn(dg, "在吗", chat=fake_chat({}, boom=True), root=self.root, now=NOW)
        self.assertTrue(out["ok"])
        self.assertTrue(out["timed_out"])
        self.assertIn("模型通道异常", out["reply"])

    def test_disagreement_stops_after_second_round(self):
        dg = D.open_dialog(root=self.root, now=NOW)
        payload = {"reply": "还要再问一句。", "decision": {"type": "none"}, "unresolved": ["换不换模没定"]}
        D.turn(dg, "第一问", chat=fake_chat(payload), root=self.root, now=NOW)
        out = D.turn(dg, "第二问", chat=fake_chat(payload), root=self.root, now=NOW)
        self.assertEqual(out["status"], "disagreed")
        self.assertIn("二选一", out["reply"])
        self.assertIn("A：", out["reply"])

    def test_round_limit_closes_at_three(self):
        dg = D.open_dialog(root=self.root, now=NOW)
        payload = {"reply": "继续。", "decision": {"type": "none"}}
        D.turn(dg, "1", chat=fake_chat(payload), root=self.root, now=NOW)
        D.turn(dg, "2", chat=fake_chat(payload), root=self.root, now=NOW)
        D.turn(dg, "3", chat=fake_chat(payload), root=self.root, now=NOW)
        out = D.turn(dg, "4", chat=fake_chat(payload), root=self.root, now=NOW)
        self.assertEqual(out["status"], "round_limit")

    def test_timeout_when_deadline_passed(self):
        dg = D.open_dialog(root=self.root, now=NOW)
        dg["deadline"] = (NOW - dt.timedelta(seconds=5)).isoformat(timespec="seconds")
        out = D.turn(dg, "还在吗", chat=fake_chat({"reply": "x"}), root=self.root, now=NOW)
        self.assertEqual(out["status"], "timeout")
        self.assertTrue(out["elapsed_ms"] >= 0)

    def test_closed_dialog_rejects_new_turn(self):
        dg = D.open_dialog(root=self.root, now=NOW)
        dg["status"] = "disagreed"
        out = D.turn(dg, "再说一句", chat=fake_chat({"reply": "x"}), root=self.root, now=NOW)
        self.assertEqual(out["status"], "disagreed")
        self.assertIn("已收敛", out["reply"])

    def test_closed_event_records_rounds_and_timeout(self):
        dg = D.open_dialog(root=self.root, now=NOW)
        payload = {"reply": "继续。", "decision": {"type": "none"}, "unresolved": ["甲"]}
        D.turn(dg, "1", chat=fake_chat(payload), root=self.root, now=NOW)
        D.turn(dg, "2", chat=fake_chat(payload), root=self.root, now=NOW)
        closed = [e for e in self.events() if e["event"] == "bot_team_dialog_closed"]
        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0]["payload"]["rounds"], 2)
        self.assertIn("elapsed_ms", closed[0]["payload"])
        self.assertFalse(closed[0]["payload"]["timed_out"])

    def test_dialog_events_use_bot_team_prefix(self):
        dg = D.open_dialog(root=self.root, now=NOW)
        D.turn(dg, "hi", chat=fake_chat({"reply": "ok"}), root=self.root, now=NOW)
        names = {e["event"] for e in self.events()}
        self.assertTrue(all(n.startswith("bot_team_") for n in names))

    def test_view_shape(self):
        dg = D.open_dialog(root=self.root, now=NOW)
        v = D.view(dg)
        for key in ("dialog_id", "status", "rounds", "max_rounds", "messages"):
            self.assertIn(key, v)

    def test_persisted_turns_survive_reload(self):
        dg = D.open_dialog(root=self.root, now=NOW)
        D.turn(dg, "第一次", chat=fake_chat({"reply": "收到"}), root=self.root, now=NOW)
        again = D.load(dg["dialog_id"], root=self.root)
        self.assertEqual(again["rounds"], 1)
        self.assertEqual(len(again["messages"]), 2)

    def test_list_dialogs_newest_first(self):
        d1 = D.open_dialog(root=self.root, now=NOW)
        D.turn(d1, "a", chat=fake_chat({"reply": "1"}), root=self.root, now=NOW)
        rows = D.list_dialogs(root=self.root)
        self.assertGreaterEqual(len(rows), 1)

    def test_missing_dialog_raises(self):
        with self.assertRaises(D.DialogError):
            D.load("DG-NOPE", root=self.root)


if __name__ == "__main__":
    unittest.main(verbosity=2)
