"""P0-4：注意力账本单一事实源（events.jsonl）与 ledger 投影缓存。

这里刻意**不裸写 events.jsonl**：写入一律走 `attention.append()`
（内部 `core.eventlog.emit()`），纯函数测试则显式构造 events 列表。
"""
import datetime as dt
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import textwrap
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import attention, eventlog  # noqa: E402

TZ = dt.timezone(dt.timedelta(hours=8))
NOW = dt.datetime(2026, 11, 1, 12, 0, 0, tzinfo=TZ)
ISO = NOW.replace(hour=9).isoformat()


def env_without_bot_paths():
    env = dict(os.environ)
    for key in ("BOT_EVENTS_FILE", "BOT_ATTENTION_FILE", "BOT_WORKITEMS_FILE"):
        env.pop(key, None)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


class LedgerCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self._saved_env = {k: os.environ.pop(k, None)
                           for k in ("BOT_EVENTS_FILE", "BOT_ATTENTION_FILE")}

    def tearDown(self):
        for key, value in self._saved_env.items():
            if value is not None:
                os.environ[key] = value
            else:
                os.environ.pop(key, None)
        self.tmp.cleanup()

    def events(self):
        return eventlog.iter_events(attention.events_path(self.root))


class TestRealEntryPoint(LedgerCase):
    def test_metrics_nonzero_after_real_append(self):
        """真实入口写的事件必须让 metrics 非 0（修复“恒 0”）。"""
        attention.append("attention_interrupt", {"kind": "hard", "source": "ledger_due"},
                         root=self.root, ts=ISO)
        attention.append("attention_ticket_opened", {"ticket_id": "T1", "item_id": "WI-1"},
                         root=self.root, ts=ISO)
        attention.append("attention_ticket_resolved", {"ticket_id": "T1"},
                         root=self.root, ts=ISO)
        out = attention.metrics(root=self.root, days=3, now=NOW)
        self.assertTrue(out["data_quality"]["history"])
        self.assertEqual(out["answers"]["interruptions"], 1)
        self.assertEqual(out["answers"]["tickets_processed"], 1)
        self.assertEqual(out["answers"]["wip_peak"], 1)
        self.assertEqual(out["tickets"], {"opened": 1, "resolved": 1, "open_now": 0})
        self.assertEqual(out["data_quality"]["source_of_truth"], "events")
        self.assertIn("bot_team_attention_interrupt", out["by_event"])

    def test_canonical_name_is_not_double_prefixed(self):
        rec = attention.append("bot_team_attention_interrupt", {"kind": "hard"},
                               root=self.root, ts=ISO)
        self.assertEqual(rec["event"], "bot_team_attention_interrupt")
        self.assertEqual([e["event"] for e in self.events()],
                         ["bot_team_attention_interrupt"])

    def test_bad_names_rejected_without_writing(self):
        for bad in ("bot_team_nope", "attention_Interrupt", "plain_name", ""):
            with self.assertRaises(attention.AttentionError):
                attention.append(bad, {}, root=self.root)
        self.assertEqual(self.events(), [])

    def test_bad_payload_rejected(self):
        with self.assertRaises(attention.AttentionError):
            attention.append("attention_interrupt", ["not", "dict"], root=self.root)
        self.assertEqual(self.events(), [])


class TestRebuild(LedgerCase):
    def _seed(self):
        attention.append("attention_interrupt", {"kind": "hard"}, root=self.root, ts=ISO)
        attention.append("attention_task_switch", {"from": "A", "to": "B"},
                         root=self.root, ts=ISO)
        attention.append("attention_interrupt", {"kind": "soft"}, root=self.root, ts=ISO)

    def test_delete_ledger_then_rebuild_is_identical(self):
        self._seed()
        before_rows = attention.read_ledger(self.root)
        before_metrics = attention.metrics(root=self.root, days=3, now=NOW)
        ledger = attention.ledger_path(self.root)
        before_bytes = ledger.read_bytes()
        ledger.unlink()  # 删掉投影缓存，事实源还在

        self.assertEqual(attention.read_ledger(self.root), [])
        # metrics 不读 ledger：删掉后指标一模一样。
        self.assertEqual(attention.metrics(root=self.root, days=3, now=NOW), before_metrics)

        rebuilt = attention.rebuild_ledger(root=self.root)
        self.assertEqual(rebuilt, before_rows)
        self.assertEqual(ledger.read_bytes(), before_bytes)
        self.assertEqual(json.loads(before_bytes.decode("utf-8").splitlines()[0])["seq"], 1)

    def test_rebuild_is_idempotent_and_pure(self):
        self._seed()
        events = self.events()
        ledger = attention.ledger_path(self.root)
        before = ledger.read_bytes()
        rows1 = attention.rebuild_ledger(events, write=False)
        rows2 = attention.rebuild_ledger(list(events), write=False)
        self.assertEqual(rows1, rows2)
        self.assertEqual(rows1, attention.project_ledger(events))
        self.assertEqual([r["seq"] for r in rows1], [1, 2, 3])
        self.assertEqual([r["source_seq"] for r in rows1], [1, 2, 3])
        self.assertEqual(ledger.read_bytes(), before)  # write=False 没动磁盘

    def test_ledger_alone_is_not_truth(self):
        """只有 ledger 假数据、没有 events 时 metrics 仍 0，且 rebuild 会覆盖掉假行。"""
        ledger = attention.ledger_path(self.root)
        ledger.parent.mkdir(parents=True, exist_ok=True)
        fake = {"seq": 1, "event_id": "fake", "ts": ISO, "event": "attention_interrupt",
                "actor": "test", "payload": {"kind": "hard"}, "source_seq": 1}
        ledger.write_text(json.dumps(fake) + "\n", encoding="utf-8")

        out = attention.metrics(root=self.root, days=3, now=NOW)
        self.assertEqual(out["answers"]["interruptions"], 0)
        self.assertFalse(out["data_quality"]["history"])

        attention.rebuild_ledger(root=self.root)
        self.assertEqual(attention.read_ledger(self.root), [])

    def test_project_ledger_filters_and_preserves_source_seq(self):
        rows = [
            {"seq": 7, "event_id": "a", "ts": ISO, "event": "bot_team_attention_interrupt",
             "actor": "owner", "payload": {"kind": "hard"}},
            {"seq": 8, "event_id": "b", "ts": ISO, "event": "bot_team_workitem_updated",
             "payload": {}},
            {"seq": 9, "event_id": "c", "ts": ISO, "event": "bot_team_attention_task_switch",
             "payload": {"from": "A", "to": "B"}},
        ]
        out = attention.project_ledger(rows)
        self.assertEqual([r["event"] for r in out],
                         ["attention_interrupt", "attention_task_switch"])
        self.assertEqual([r["source_seq"] for r in out], [7, 9])
        self.assertEqual(out[0]["actor"], "owner")
        self.assertEqual(attention.project_ledger(rows), out)


class TestImportOrderAndCli(LedgerCase):
    def _run(self, code, timeout=60):
        proc = subprocess.run(
            [sys.executable, "-B", "-c", textwrap.dedent(code)],
            cwd=str(ROOT), env=env_without_bot_paths(), timeout=timeout,
            capture_output=True, text=True, encoding="utf-8",
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout.strip().splitlines()[-1]

    def _order_script(self, first, second):
        return f"""
            import json, sys
            sys.path.insert(0, {str(ROOT)!r})
            from core import {first}
            from core import {second}
            from core import attention
            root = {str(self.root)!r}
            attention.append("attention_interrupt", {{"kind": "hard"}}, root=root, ts={ISO!r})
            attention.append("attention_ticket_opened", {{"ticket_id": "T1"}}, root=root, ts={ISO!r})
            print(json.dumps({{
                "metrics": attention.metrics(root=root, days=3).get("answers"),
                # event_id 是 uuid，跨进程必然不同；其余必须逐字节一致。
                "ledger": [{{k: v for k, v in r.items() if k != "event_id"}}
                           for r in attention.read_ledger(root)],
            }}, ensure_ascii=False, sort_keys=True))
        """

    def test_import_order_does_not_matter(self):
        first = self._run(self._order_script("attention", "eventlog"))
        self.root.joinpath("runtime", "events.jsonl").unlink()
        self.root.joinpath("runtime", "attention", "ledger.jsonl").unlink()
        second = self._run(self._order_script("eventlog", "attention"))
        self.assertEqual(first, second)
        payload = json.loads(first)
        self.assertEqual(payload["metrics"]["interruptions"], 1)
        self.assertEqual(payload["metrics"]["tickets_processed"], 0)
        self.assertEqual(len(payload["ledger"]), 2)

    def test_cli_rebuild(self):
        attention.append("attention_interrupt", {"kind": "hard"}, root=self.root, ts=ISO)
        expected = attention.read_ledger(self.root)
        attention.ledger_path(self.root).unlink()

        proc = subprocess.run(
            [sys.executable, "-B", "core/attention.py", "--rebuild", "--root", str(self.root)],
            cwd=str(ROOT), env=env_without_bot_paths(), timeout=60,
            capture_output=True, text=True, encoding="utf-8",
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = json.loads(proc.stdout.strip().splitlines()[-1])
        self.assertEqual(out["ok"], True)
        self.assertEqual(out["rebuilt"], 1)
        self.assertEqual(attention.read_ledger(self.root), expected)


if __name__ == "__main__":
    unittest.main(verbosity=2)
