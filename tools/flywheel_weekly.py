#!/usr/bin/env python3
"""F3 飞轮周报：从 `runtime/events.jsonl` 算周度指标。

只读事件流，不写库。样本不足一律出 `null`，不出漂亮数字——虚荣指标比没指标更危险。
（与旧 `tools/flywheel_report.py` 的分工：那个读 `.telemetry/flywheel.jsonl` 的历史埋点，
本文件读统一事件流 `runtime/events.jsonl`，口径以 `docs/product/FLYWHEEL_PLAN_2026-11-03.md` 为准。）

口径：
- **N1 采纳率** = card_signed / card_issued（分母 < 8 → null + note）
- **N3 定案耗时** = 同一 task 从 card_issued 到 card_signed 的秒数中位数
- **回填率** = outcome_recorded / card_issued
- **值得率** = 判「准」/ (判准 + 判不准)
- **采集健康度** = 窗口事件数、覆盖天数、是否断档

用法：
    python -B tools/flywheel_weekly.py --days 7
    python -B tools/flywheel_weekly.py --json --write
退出码：0 出报告；2 事件流不存在。
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core import eventlog  # noqa: E402

MIN_SAMPLE = 8
REPORT_DIR = ROOT / "runtime" / "reports"


def _parse_ts(s: str) -> datetime | None:
    try:
        return datetime.fromisoformat(str(s))
    except (TypeError, ValueError):
        return None


def _median(values) -> float | None:
    vals = [v for v in values if v is not None]
    return round(statistics.median(vals), 1) if vals else None


def _ratio(num: int, den: int) -> float | None:
    if not den or den < MIN_SAMPLE:
        return None
    return round(num / den, 4)


def build(events: list[dict], days: int = 7, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc).astimezone()
    since = now - timedelta(days=days)
    window: list[tuple[datetime, dict]] = []
    for e in events:
        ts = _parse_ts(e.get("ts", ""))
        if ts is None:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if ts >= since:
            window.append((ts, e))

    issued: dict[str, datetime] = {}
    by_event: dict[str, int] = {}
    by_kind: dict[str, int] = {}
    signed = outcomes = agree = disagree = memory_changes = 0
    durations: list[float] = []

    for ts, e in sorted(window, key=lambda x: x[0]):
        name = str(e.get("event", "")).removeprefix("bot_team_")
        by_event[name] = by_event.get(name, 0) + 1
        p = e.get("payload") or {}
        tid = str(p.get("task_id") or "")
        if name == "card_issued" and tid:
            issued[tid] = ts
            kind = str(p.get("card_kind") or "attribution")
            by_kind[kind] = by_kind.get(kind, 0) + 1
        elif name == "card_signed" and tid:
            signed += 1
            start = issued.get(tid)
            if start is not None:
                durations.append((ts - start).total_seconds())
        elif name == "outcome_recorded" and tid:
            outcomes += 1
        elif name in ("reminder_judged", "reminder_feedback"):
            verdict = str(p.get("verdict") or p.get("judgement") or "").lower()
            if verdict in ("ok", "accurate", "准", "hit"):
                agree += 1
            elif verdict in ("not_ok", "wrong", "不准", "miss"):
                disagree += 1
        elif name == "memory_change":
            memory_changes += 1

    days_seen = {ts.date() for ts, _ in window}
    issued_n = len(issued)
    judged_n = agree + disagree
    return {
        "ok": True,
        "window_days": days,
        "generated_at": now.isoformat(timespec="seconds"),
        "events_in_window": len(window),
        "days_covered": len(days_seen),
        "by_event": dict(sorted(by_event.items(), key=lambda kv: -kv[1])),
        "by_card_kind": by_kind,
        "issued": issued_n,
        "signed": signed,
        "outcomes": outcomes,
        "n1_adoption_rate": _ratio(signed, issued_n),
        "n3_decision_seconds_median": _median(durations),
        "backfill_rate": _ratio(outcomes, issued_n),
        "worth_rate": _ratio(agree, judged_n),
        "memory_change_events": memory_changes,
        "collection_health": "ok" if len(days_seen) >= min(days, 3) else "gap",
        "notes": [
            n for n in (
                "" if issued_n >= MIN_SAMPLE else f"issued={issued_n} < {MIN_SAMPLE}，采纳率/回填率不出数",
                "" if judged_n >= MIN_SAMPLE else f"判定样本={judged_n} < {MIN_SAMPLE}，值得率不出数",
            ) if n
        ],
    }


def load_events(path: str | None = None) -> list[dict]:
    p = Path(path) if path else Path(eventlog.events_path())
    if not p.is_file():
        raise FileNotFoundError(f"事件流不存在：{p}")
    return eventlog.iter_events(path=str(p))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="飞轮周报（事件流口径）")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--events", default=None)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args(argv)
    try:
        events = load_events(args.events)
    except FileNotFoundError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 2
    rep = build(events, days=args.days)
    if args.write:
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        out = REPORT_DIR / f"flywheel-{rep['generated_at'][:10]}.json"
        out.write_text(json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
        rep["written_to"] = str(out)
    if args.json:
        print(json.dumps(rep, ensure_ascii=False))
    else:
        print(f"[OK] {rep['window_days']} 天：事件 {rep['events_in_window']}，覆盖 {rep['days_covered']} 天")
        print(f"     出卡 {rep['issued']} / 签字 {rep['signed']} / 回填 {rep['outcomes']}")
        print(f"     N1 采纳率 {rep['n1_adoption_rate']}｜N3 定案中位 {rep['n3_decision_seconds_median']}s"
              f"｜回填率 {rep['backfill_rate']}｜值得率 {rep['worth_rate']}")
        for n in rep["notes"]:
            print(f"     注：{n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
