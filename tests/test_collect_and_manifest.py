#!/usr/bin/env python3
"""F1 白名单采集器 / F2.5 产物清单 的负例测试（stdlib，无需 pytest）。

跑法：python -B tests/test_collect_and_manifest.py
输出：RESULT: PASS（N 项）或 FAIL 明细；退出码 0/1。
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import artifact_manifest as am  # noqa: E402
import tobs_collect as tc  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    if not cond:
        print(f"  FAIL {name} {detail}")


def make_db(path: Path) -> Path:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        create table source_events (
          source_event_id text, source_kind text, bot_slug text, channel_id text,
          message_id text, assignment_session_id text, body text, created_at text,
          handled_at text, attempt_state text, side_effect_started_at text,
          expects_reply integer, observed_at text, payload_json text);
        create table inbox_admissions (
          source_event_id text, bot_slug text, reason text, attempt_state text,
          side_effect_started_at text, handled_at text, last_error text, wake_count integer,
          wake_interval_ms integer, wake_policy_revision integer, observed_at text,
          ignored_at text, ignored_by_session_id text, wake_mode text,
          source_policy_revision integer, source_policy_wake_mode text,
          external_thread_policy_revision integer, external_default_revision integer,
          opened_at text, resolved_at text, resolution text, resolution_note text,
          resolved_by_session_id text, snoozed_until text, snoozed_at text, snooze_note text);
        create table assignments (
          session_id text, source_event_id text, bot_slug text, description text,
          activity text, latest_report_state text, latest_report_summary text,
          latest_report_at text, created_at text, updated_at text, continuity_key text,
          open_ask_source_event_id text, open_ask_at text, grant_id text, workspace_id text,
          primary_cwd text, permission_mode text, approval_policy text, preset_revision integer,
          stop_state text, model_route_json text, archived_at text, title text);
        """
    )
    conn.execute(
        "insert into source_events(source_event_id,source_kind,bot_slug,channel_id,body,created_at)"
        " values ('se1','human-message','bot-a','ch1','请用 sk-abcdefgh12345678 查一下','2026-11-03T10:00:00Z')"
    )
    conn.execute(
        "insert into source_events(source_event_id,source_kind,bot_slug,channel_id,body,created_at)"
        " values ('se2','self-record','bot-a','ch1','','2026-11-03T10:00:01Z')"
    )
    conn.execute(
        "insert into source_events(source_event_id,source_kind,bot_slug,channel_id,body,created_at)"
        " values ('se3','memory-change','bot-b','','memory updated','2026-11-03T10:00:02Z')"
    )
    conn.execute(
        "insert into inbox_admissions(source_event_id,bot_slug,reason,attempt_state,opened_at)"
        " values ('se1','bot-a','group-ordinary','handled','2026-11-03T10:00:00Z')"
    )
    conn.execute(
        "insert into inbox_admissions(source_event_id,bot_slug,reason,attempt_state,opened_at)"
        " values ('se4','bot-b','human-dm','handled','2026-11-03T10:00:03Z')"
    )
    conn.execute(
        "insert into inbox_admissions(source_event_id,bot_slug,reason,attempt_state,opened_at)"
        " values ('se5','bot-b','group-mention','pending','2026-11-03T10:00:04Z')"
    )
    conn.execute(
        "insert into assignments(session_id,bot_slug,title,stop_state,latest_report_state,"
        "created_at,updated_at,primary_cwd) values "
        "('as1','bot-a','家居外贸询价机器人','stopped',NULL,'2026-11-03T00:40:50Z','2026-11-03T00:47:19Z','D:/x')"
    )
    conn.commit()
    conn.close()
    return path


def read_events(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def test_collect(tmp: Path):
    db = make_db(tmp / "botharness.db")
    ev = tmp / "events.jsonl"
    st = tmp / "state.json"

    res = tc.run_once(db, st, limit=50, dry_run=False, events_path=str(ev))
    check("collect.ok", res["ok"] is True, str(res.get("errors")))
    check("collect.emitted_5", res["emitted"] == 5, f"got {res['emitted']} {res['by_event']}")
    check("collect.skip_self_record", "msg_bot" not in res["by_event"], str(res["by_event"]))
    check("collect.skip_group_ordinary", "inbound_group_ordinary" not in str(res["by_event"]))

    events = read_events(ev)
    names = [e["event"] for e in events]
    check("collect.event_prefix", all(n.startswith("bot_team_") for n in names), str(names))
    check("collect.has_human_msg", "bot_team_msg_human" in names, str(names))
    check("collect.has_assignment_stopped", "bot_team_assignment_stopped" in names, str(names))
    check("collect.inbox_filtered", "bot_team_inbound_human_dm" in names and
          "bot_team_inbound_group_mention" in names, str(names))

    body = next(e["payload"]["body"] for e in events if e["event"] == "bot_team_msg_human")
    check("collect.redacted", "sk-abcdefgh" not in body and "[redacted]" in body, body)

    # 幂等：再跑一次不重复发
    res2 = tc.run_once(db, st, limit=50, dry_run=False, events_path=str(ev))
    check("collect.idempotent", res2["emitted"] == 0, str(res2["by_event"]))
    check("collect.events_not_duplicated", len(read_events(ev)) == len(events),
          f"{len(read_events(ev))} vs {len(events)}")

    # 新增一行 → 只采新的
    conn = sqlite3.connect(db)
    conn.execute("insert into source_events(source_event_id,source_kind,bot_slug,body,created_at)"
                 " values ('se9','bot-message','bot-c','好的','2026-11-03T11:00:00Z')")
    conn.commit()
    conn.close()
    res3 = tc.run_once(db, st, limit=50, dry_run=False, events_path=str(ev))
    check("collect.incremental", res3["emitted"] == 1 and "msg_bot" in res3["by_event"],
          str(res3["by_event"]))

    # dry-run 不写事件
    ev2 = tmp / "events2.jsonl"
    res4 = tc.run_once(db, tmp / "state2.json", limit=50, dry_run=True, events_path=str(ev2))
    check("collect.dry_run_no_write", not ev2.exists(), "dry-run 写了文件")
    check("collect.dry_run_counts", res4["emitted"] == 6, str(res4["emitted"]))

    # 库不存在 → rc 2
    code = tc.main(["--once", "--db", str(tmp / "nope.db"), "--state", str(st)])
    check("collect.missing_db_rc2", code == 2, f"rc={code}")

    st_doc = json.loads(st.read_text(encoding="utf-8"))
    check("collect.state_cursors", st_doc["cursors"].get("source_events", 0) >= 4, str(st_doc["cursors"]))
    check("collect.state_seen_capped", len(st_doc["seen"]) <= tc.SEEN_CAP)
    # 游标必须推进到「扫过的最后一行」，即使该行被白名单过滤掉（否则尾部纯 self-record 会永远重扫）
    conn = sqlite3.connect(db)
    for i in range(5):
        conn.execute("insert into source_events(source_event_id,source_kind,bot_slug,body,created_at)"
                     " values (?,?,?,?,?)", (f"z{i}", "self-record", "bot-z", "", "2026-11-03T12:00:0%dZ" % i))
    conn.commit()
    maxrow = conn.execute("select max(rowid) m from source_events").fetchone()[0]
    conn.close()
    st3 = tmp / "state3.json"
    st3.write_text(st.read_text(encoding="utf-8"), encoding="utf-8")  # 继承已有游标，只扫尾部的 self-record
    fatal, total_emit = 0, 0
    for _ in range(60):
        r = tc.run_once(db, st3, limit=2, dry_run=False, events_path=str(tmp / "e3.jsonl"))
        total_emit += r["emitted"]
        if json.loads(st3.read_text(encoding="utf-8"))["cursors"]["source_events"] >= maxrow:
            break
        fatal += 1
    cur3 = json.loads(st3.read_text(encoding="utf-8"))["cursors"]["source_events"]
    check("collect.cursor_converges", cur3 >= maxrow, f"cursor={cur3} max={maxrow} rounds={fatal}")
    check("collect.filtered_rows_not_emitted", total_emit == 0, f"emitted={total_emit}")

    stt = tc.stats(db, st)
    check("collect.stats", stt["ok"] and "human-message" in stt["source_kinds"], str(stt)[:120])


def test_manifest(tmp: Path):
    task_dir = tmp / "runs" / "T-1"
    adir = task_dir / "artifacts"
    adir.mkdir(parents=True)
    (adir / "report.md").write_text("# 结论\n", encoding="utf-8")
    (adir / "data.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    (adir / "id_rsa.key").write_text("secret", encoding="utf-8")

    ev = tmp / "events_m.jsonl"
    doc = am.build(task_dir, "T-1", is_demo=False, events_path=str(ev))
    names = [a["name"] for a in doc["artifacts"]]
    check("manifest.count_2", doc["count"] == 2, f"got {doc['count']} {names}")
    check("manifest.skip_sensitive", "id_rsa.key" not in names, str(names))
    check("manifest.types", {a["name"]: a["type"] for a in doc["artifacts"]} ==
          {"data.csv": "table", "report.md": "doc"}, str(doc["artifacts"]))
    check("manifest.sha_len", all(len(a["sha256"]) == 64 for a in doc["artifacts"]))
    check("manifest.total_bytes", doc["total_bytes"] == sum(a["size"] for a in doc["artifacts"]))
    events = read_events(ev)
    check("manifest.events_2", len(events) == 2 and all(
        e["event"] == "bot_team_artifact_saved" for e in events), str([e["event"] for e in events]))
    check("manifest.event_payload", events[0]["payload"]["task_id"] == "T-1" and
          events[0]["payload"]["sha256"], json.dumps(events[0]["payload"], ensure_ascii=False)[:120])

    v = am.verify(task_dir)
    check("manifest.verify_ok", v["ok"] is True, str(v["errors"]))

    # 文件被改 → changed
    (adir / "report.md").write_text("# 结论（改）\n", encoding="utf-8")
    v2 = am.verify(task_dir)
    check("manifest.verify_changed", v2["ok"] is False and v2["changed"] == ["report.md"], str(v2))

    # 删文件 → missing
    (adir / "data.csv").unlink()
    v3 = am.verify(task_dir)
    check("manifest.verify_missing", "data.csv" in v3["missing"], str(v3))

    # 多文件 → extra
    (adir / "extra.png").write_bytes(b"\x89PNG\r\n")
    v4 = am.verify(task_dir)
    check("manifest.verify_extra", "extra.png" in v4["extra"], str(v4))

    # 路径穿越/绝对路径/敏感名
    for bad in ("../x.csv", "C:/x.csv", "id_rsa.key"):
        try:
            am.safe_name(bad)
            check(f"manifest.reject_{bad}", False, "未拒绝")
        except am.ManifestError:
            check(f"manifest.reject_{Path(bad).name}", True)

    # 不覆盖：同名自动加序号
    (adir / "dup.md").write_text("1", encoding="utf-8")
    n = am.unique_name(adir, "dup.md")
    check("manifest.unique_name", n == "dup-2.md", n)

    # is_demo 透传
    doc2 = am.build(task_dir, "T-1", is_demo=True, emit=False, events_path=str(ev))
    check("manifest.is_demo", doc2["is_demo"] is True and all(a["is_demo"] for a in doc2["artifacts"]))

    # 任务目录不存在 → rc 2
    code = am.main(["build", "--task-dir", str(tmp / "nope"), "--task-id", "X"])
    check("manifest.missing_dir_rc2", code == 2, f"rc={code}")

    # verify 无清单 → rc 1
    empty = tmp / "runs" / "T-2"
    empty.mkdir(parents=True)
    check("manifest.verify_nomanifest_rc1", am.main(["verify", "--task-dir", str(empty)]) == 1)

    # 超大文件只登记不哈希
    old = am.MAX_FILE
    am.MAX_FILE = 4
    big = tmp / "runs" / "T-3" / "artifacts"
    big.mkdir(parents=True)
    (big / "big.bin").write_bytes(b"0123456789")
    doc3 = am.build(tmp / "runs" / "T-3", "T-3", emit=False)
    am.MAX_FILE = old
    check("manifest.omitted", doc3["artifacts"][0]["omitted"] is True and
          doc3["artifacts"][0]["sha256"] is None, str(doc3["artifacts"]))


def main() -> int:
    t0 = time.time()
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        env_bak = os.environ.get("BOT_EVENTS_FILE")
        try:
            test_collect(tmp)
            test_manifest(tmp)
        finally:
            if env_bak is None:
                os.environ.pop("BOT_EVENTS_FILE", None)
            else:
                os.environ["BOT_EVENTS_FILE"] = env_bak
    total = len(PASS) + len(FAIL)
    if FAIL:
        print(f"\nRESULT: FAIL（{total - len(FAIL)}/{total} 通过，{len(FAIL)} 红，{time.time()-t0:.2f}s）")
        for n in FAIL:
            print(f"  - {n}")
        return 1
    print(f"\nRESULT: PASS（{total} 项，{time.time()-t0:.2f}s）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
