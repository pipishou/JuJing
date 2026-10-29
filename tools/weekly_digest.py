"""E4 周汇总：把一周事件压成一张可签字的周卡（card_kind=weekly）。

口径（MVP 方案 §7、§9）：
  - 每周一 15:00 出卡（`server/install_weekly_task.ps1` 注册计划任务）
  - 只报机器可核的数：提醒发了多少、判准/判不准多少、事项变了多少、现在逾期多少
  - 样本 <8 条判定时**不出命中率**（宁缺勿假）
  - 证据体 = 本周事件快照（写进 runs/<task>/data/，带 sha256，可重放）
  - 本工具只产 `advisory-report.v1` JSON；落盘/四卡/签字仍走 `tools/advisory_to_task.py`

用法：
    python -B tools/weekly_digest.py --out _tmp_weekly/advisory.json [--runs-root runs]
    python -B tools/advisory_to_task.py --advisory _tmp_weekly/advisory.json --task-id T-... --runs-root runs
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import eventlog  # noqa: E402

OK_VERDICTS = ("准", "对", "有用", "accurate", "ok", "yes", "对得上")
MISS_LIMIT = 8


def _dt_of(ts):
    try:
        return _dt.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _in_window(ts, start, end):
    t = _dt_of(ts)
    if t is None:
        return False
    if t.tzinfo is None:
        t = t.astimezone()
    return start <= t <= end


def build(events, items, start: _dt.datetime, end: _dt.datetime, *, now=None) -> dict:
    """纯函数：给事件与事项，出一张周卡用的 advisory 文档。"""
    now = now or _dt.datetime.now().astimezone()
    week_events = [e for e in events if _in_window(e.get("ts"), start, end)]

    issued = [e for e in week_events if e.get("event") == "bot_team_reminder_issued"]
    judged = [e for e in week_events if e.get("event") == "bot_team_reminder_judged"]
    ok = [e for e in judged if str((e.get("payload") or {}).get("verdict") or "").strip().lower() in OK_VERDICTS]
    miss = [e for e in judged if e not in ok]
    changed = [e for e in week_events if e.get("event") in ("bot_team_item_updated", "bot_team_item_closed")]
    dialogs = [e for e in week_events if e.get("event") == "bot_team_dialog_closed"]

    overdue = []
    today = end.date()
    for it in items or []:
        if it.get("status") in ("done", "cancelled"):
            continue
        due = (it.get("duedate") or {}).get("confirmed") or (it.get("duedate") or {}).get("proposal") or it.get("due")
        if isinstance(due, dict):
            due = due.get("value")
        try:
            d = _dt.date.fromisoformat(str(due)[:10])
        except ValueError:
            continue
        if d < today:
            overdue.append({"id": it.get("id"), "title": it.get("title"), "due": d.isoformat(),
                            "owner": it.get("owner"), "status": it.get("status")})

    sample = len(ok) + len(miss)
    metrics = {
        "reminders_issued": len(issued),
        "judged_ok": len(ok),
        "judged_not_ok": len(miss),
        "hit_rate": (round(len(ok) / sample, 3) if sample >= MISS_LIMIT else None),
        "items_changed": len(changed),
        "overdue_now": len(overdue),
        "dialogs_closed": len(dialogs),
        "events_in_window": len(week_events),
    }

    top = sorted(overdue, key=lambda r: r["due"])[:5]
    lines = [f"- 提醒 {metrics['reminders_issued']} 条，判准 {metrics['judged_ok']} / 判不准 {metrics['judged_not_ok']}"
             f"（样本 {sample}{'，够 8 条才出命中率' if sample < MISS_LIMIT else ''}）",
             f"- 事项变动 {metrics['items_changed']} 次，当前逾期 {metrics['overdue_now']} 件",
             f"- 对话收敛 {metrics['dialogs_closed']} 次"]
    if top:
        lines.append("- 最该先处理的逾期：")
        lines += [f"  - {r['id']} {r['title']}（{r['owner'] or '未指派'}，截止 {r['due']}）" for r in top]

    return {
        "schema": "advisory-report.v1",
        "card_kind": "weekly",
        "is_demo": False,
        "goal": f"本周工作复盘（{start.date().isoformat()} ~ {end.date().isoformat()}）",
        "period": {"start": start.date().isoformat(), "end": end.date().isoformat()},
        "metrics": metrics,
        "weekly": {
            "summary": "\n".join(lines),
            "overdue_top": top,
            "stale_reminders": [e.get("payload") for e in issued[-5:]],
        },
        "decision_log_entry": {
            "actor": "lead",
            "advice": "周复盘：先清逾期，再定下周唯一重点",
            "expected_result": "下周逾期件数下降，且只有 1 个主攻方向",
            "verification_plan": "下周一同一口径复算 overdue_now 与 items_changed",
            "date": now.date().isoformat(),
            "context": "自动周汇总（事件快照可重放）",
        },
        "generated_at": now.isoformat(timespec="seconds"),
        # real 门要求的三件（周卡是实测证据，不是推演）
        "evidence_tier": "measured",
        "verdict_one_line": "本周先清逾期，只留一个主攻方向",
        "sign_scope": "approve",
    }


def snapshot(events, out_path: pathlib.Path) -> dict:
    """把参与统计的事件写成证据文件，返回 {path, sha256}。"""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    body = "\n".join(json.dumps(e, ensure_ascii=False, sort_keys=True) for e in events)
    data = (body + ("\n" if body else "")).encode("utf-8")
    out_path.write_bytes(data)
    return {"path": str(out_path), "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}


def main(argv=None):
    parser = argparse.ArgumentParser(description="周汇总卡生成（E4）")
    parser.add_argument("--out", required=True, help="advisory-report.v1 JSON 输出路径")
    parser.add_argument("--events", default=None, help="事件文件（默认 runtime/events.jsonl / $BOT_EVENTS_FILE）")
    parser.add_argument("--workitems", default=None, help="工作事项账本（默认 runtime/workitems/current.json）")
    parser.add_argument("--start", default=None, help="窗口起（ISO 日期，默认 7 天前）")
    parser.add_argument("--end", default=None, help="窗口止（ISO 日期，默认今天）")
    parser.add_argument("--snapshot-dir", default=None, help="事件快照目录（默认与 --out 同目录）")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    now = _dt.datetime.now().astimezone()
    end = _dt.datetime.fromisoformat(args.end).astimezone() if args.end else now
    start = (_dt.datetime.fromisoformat(args.start).astimezone() if args.start
             else end - _dt.timedelta(days=7))

    events = eventlog.iter_events(path=args.events)
    items = []
    wi_path = pathlib.Path(args.workitems or os.environ.get("BOT_WORKITEMS_FILE")
                           or (ROOT / "runtime" / "workitems" / "current.json"))
    if wi_path.exists():
        try:
            items = json.loads(wi_path.read_text(encoding="utf-8")).get("items") or []
        except ValueError:
            items = []

    doc = build(events, items, start, end, now=now)
    out = pathlib.Path(args.out)
    snap_dir = pathlib.Path(args.snapshot_dir) if args.snapshot_dir else out.parent
    ref = snapshot([e for e in events if _in_window(e.get("ts"), start, end)],
                   snap_dir / f"weekly-events-{end.date().isoformat()}.jsonl")
    doc["sources"] = [ref["path"]]

    if args.dry_run:
        print(json.dumps(doc, ensure_ascii=False, indent=2))
        return 0
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    m = doc["metrics"]
    print(json.dumps({"ok": True, "out": str(out), "period": doc["period"], "metrics": m,
                      "snapshot": ref["path"], "sha256": ref["sha256"][:16]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
