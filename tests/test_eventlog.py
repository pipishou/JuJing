"""E1 全局事件流单测：append-only / seq 单调 / 命名校验 / 坏行容错 / 并发。"""
import importlib
import json
import os
import pathlib
import sys
import tempfile
import threading
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import eventlog  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = pathlib.Path(self.tmp.name) / "events.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    def emit(self, event="dialog_opened", **kw):
        kw.setdefault("path", self.path)
        return eventlog.emit(event, **kw)


class TestEmit(Base):
    def test_prefix_auto_added(self):
        rec = self.emit("dialog_opened")
        self.assertEqual(rec["event"], "bot_team_dialog_opened")

    def test_prefix_not_duplicated(self):
        rec = self.emit("bot_team_utterance")
        self.assertEqual(rec["event"], "bot_team_utterance")

    def test_seq_starts_at_one_and_increments(self):
        a = self.emit("dialog_opened")
        b = self.emit("utterance")
        self.assertEqual((a["seq"], b["seq"]), (1, 2))

    def test_event_id_unique(self):
        ids = {self.emit("utterance")["event_id"] for _ in range(5)}
        self.assertEqual(len(ids), 5)

    def test_optional_fields(self):
        rec = self.emit("reminder_issued", dialog_id="d1", refs={"item_id": "WI-0001"},
                        payload={"kind": "hard"})
        self.assertEqual(rec["dialog_id"], "d1")
        self.assertEqual(rec["refs"], {"item_id": "WI-0001"})
        self.assertEqual(rec["payload"], {"kind": "hard"})

    def test_ts_is_tz_aware_iso(self):
        ts = self.emit()["ts"]
        self.assertRegex(ts, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}$")

    def test_bad_event_name_rejected(self):
        with self.assertRaises(eventlog.EventError):
            eventlog.emit("Bot_Team_Bad", path=self.path)

    def test_payload_must_be_object(self):
        with self.assertRaises(eventlog.EventError):
            eventlog.emit("utterance", payload=["not", "dict"], path=self.path)

    def test_append_only_existing_lines_untouched(self):
        self.emit("dialog_opened")
        first = self.path.read_text(encoding="utf-8")
        self.emit("utterance")
        second = self.path.read_text(encoding="utf-8")
        self.assertTrue(second.startswith(first))


class TestRead(Base):
    def test_empty_file(self):
        self.assertEqual(eventlog.iter_events(self.path), [])
        self.assertEqual(eventlog.last_seq(self.path), 0)

    def test_roundtrip_and_stats(self):
        self.emit("dialog_opened", dialog_id="d1")
        self.emit("utterance", dialog_id="d1")
        self.emit("reminder_issued")
        st = eventlog.stats(self.path)
        self.assertEqual(st["count"], 3)
        self.assertEqual(st["last_seq"], 3)
        self.assertEqual(st["dialogs"], 1)
        self.assertEqual(st["by_event"]["bot_team_utterance"], 1)

    def test_bad_line_skipped_by_default(self):
        self.emit("dialog_opened")
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write("{oops not json\n")
        self.assertEqual(len(eventlog.iter_events(self.path)), 1)

    def test_bad_line_raises_in_strict(self):
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write("garbage\n")
        with self.assertRaises(eventlog.EventError):
            eventlog.iter_events(self.path, strict=True)

    def test_seq_continues_after_bad_line(self):
        self.emit("dialog_opened")
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write("garbage\n")
        self.assertEqual(self.emit("utterance")["seq"], 2)


class TestConcurrency(Base):
    def test_parallel_emit_has_unique_contiguous_seq(self):
        def worker(n):
            for _ in range(n):
                eventlog.emit("utterance", path=self.path)

        threads = [threading.Thread(target=worker, args=(10,)) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        seqs = sorted(int(r["seq"]) for r in eventlog.iter_events(self.path))
        self.assertEqual(seqs, list(range(1, 51)))


class TestEnvPath(unittest.TestCase):
    def test_env_override(self):
        saved = os.environ.get("BOT_EVENTS_FILE")
        with tempfile.TemporaryDirectory() as tmp:
            target = pathlib.Path(tmp) / "x.jsonl"
            os.environ["BOT_EVENTS_FILE"] = str(target)
            try:
                self.assertEqual(eventlog.events_path(), target)
            finally:
                if saved is None:
                    os.environ.pop("BOT_EVENTS_FILE", None)
                else:
                    os.environ["BOT_EVENTS_FILE"] = saved


if __name__ == "__main__":
    unittest.main(verbosity=2)
