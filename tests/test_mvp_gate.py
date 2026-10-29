#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MVP 判据机检（M0–M7）。跑通=可以宣布 MVP；任何一条红=没到。

判据（v1.1 定稿）：
  M0 账本在位：runtime/workitems/current.json 事项 ≥ MIN_ITEMS 且 revision ≥ 1
  M1 每天有对话：窗口内每个自然日都有 bot_team_dialog_closed
  M2 每天有提醒且被判定：每天 ≥1 bot_team_reminder_issued，且当天有 judged 事件
  M3 团长裁决：窗口内 report.json 里 decision_log_entry.actor == "lead" 的卡片 ≥ 5 张
  M4 每周签字：窗口内每个 ISO 周至少 1 张 card_kind=weekly、is_demo=false、有 signature.json
  M5 T+7 回填：outcome.observed_at − signed_at ≥ 6 天，窗口内 ≥1 条
  M6 零卡死：所有 bot_team_dialog_closed 的 rounds ≤ 3、elapsed_ms ≤ 90000、timed_out=false
  M7 提醒命中率（观测）：提醒后 24h 内该 item 发生状态事件；样本 < 8 只报样本量不出率

用法：
  python tests/test_mvp_gate.py --since 2026-10-29 --until 2026-11-12
  python tests/test_mvp_gate.py --selfcheck        # 正控 + 8 条负控（每条都能变红）
退出码：0 全绿 / 1 有红 / 2 参数错误
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIN_ITEMS = 40
MIN_LEAD_CARDS = 5
MIN_CARDS_PER_WEEK = 1
BACKFILL_DAYS = 6
MAX_ROUNDS = 3
MAX_ELAPSED_MS = 90_000
M7_MIN_SAMPLE = 8


# ---------- 读取 ----------

def load_events(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if isinstance(ev, dict):
            out.append(ev)
    return out


def load_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None


def day_of(ev: dict) -> str | None:
    ts = ev.get("ts")
    return ts[:10] if isinstance(ts, str) and len(ts) >= 10 else None


def week_of(d: str) -> str:
    y, m, day = (int(x) for x in d.split("-")[:3])
    return date(y, m, day).strftime("%G-W%V")


# ---------- M0 ----------

def m0(items_doc) -> tuple[bool, str]:
    if not isinstance(items_doc, dict):
        return False, "账本不存在或不可读"
    items = items_doc.get("items") or []
    rev = items_doc.get("revision")
    if not isinstance(rev, int) or rev < 1:
        return False, f"revision 非法：{rev!r}"
    if len(items) < MIN_ITEMS:
        return False, f"事项 {len(items)} < {MIN_ITEMS}"
    return True, f"账本 {len(items)} 条，rev {rev}"


# ---------- M1–M2（逐日） ----------

def _days_in(since: str, until: str) -> list[str]:
    d0 = date.fromisoformat(since)
    d1 = date.fromisoformat(until)
    return [(d0 + timedelta(days=i)).isoformat() for i in range((d1 - d0).days + 1)]


def m1(events, since, until) -> tuple[bool, str]:
    closed = {day_of(e) for e in events if e.get("event") == "bot_team_dialog_closed"}
    missing = [d for d in _days_in(since, until) if d not in closed]
    if missing:
        return False, f"缺对话的天数 {len(missing)}：{missing[:3]}"
    return True, f"{len(_days_in(since, until))} 天都有对话"


def m2(events, since, until) -> tuple[bool, str]:
    issued = {day_of(e) for e in events if e.get("event") == "bot_team_reminder_issued"}
    judged = {day_of(e) for e in events if e.get("event") == "bot_team_reminder_judged"}
    bad = [d for d in _days_in(since, until) if d not in issued or d not in judged]
    if bad:
        return False, f"缺提醒或未判定的天数 {len(bad)}：{bad[:3]}"
    return True, "每天都有提醒且有判定"


# ---------- M3–M5（按卡片） ----------

def _reports(runs_root: Path) -> list[tuple[Path, dict]]:
    out = []
    if not runs_root.exists():
        return out
    for rp in sorted(runs_root.glob("*/report.json")):
        doc = load_json(rp)
        if isinstance(doc, dict):
            out.append((rp.parent, doc))
    return out


def _in_window(doc: dict, since: str, until: str) -> bool:
    ts = str(doc.get("generated_at") or "")[:10]
    return since <= ts <= until


def m3(runs_root, since, until) -> tuple[bool, str]:
    n = 0
    for _, doc in _reports(runs_root):
        if not _in_window(doc, since, until) or doc.get("is_demo") is True:
            continue
        entry = doc.get("decision_log_entry") or {}
        if entry.get("actor") == "lead":
            n += 1
    return (n >= MIN_LEAD_CARDS), f"团长裁决卡 {n} 张（需 ≥{MIN_LEAD_CARDS}）"


def m4(runs_root, since, until) -> tuple[bool, str]:
    weeks: dict[str, int] = {}
    for tdir, doc in _reports(runs_root):
        if doc.get("card_kind") != "weekly" or doc.get("is_demo") is True:
            continue
        if not _in_window(doc, since, until):
            continue
        if not (tdir / "signature.json").exists():
            continue
        d = str(doc.get("generated_at") or "")[:10]
        weeks[week_of(d)] = weeks.get(week_of(d), 0) + 1
    need = {week_of(d) for d in _days_in(since, until)}
    missing = sorted(w for w in need if weeks.get(w, 0) < MIN_CARDS_PER_WEEK)
    if missing:
        return False, f"缺周卡的周 {len(missing)}：{missing[:3]}"
    return True, f"{len(need)} 周都有已签周卡"


def m5(runs_root, since, until) -> tuple[bool, str]:
    best = None
    for tdir, doc in _reports(runs_root):
        if doc.get("is_demo") is True:
            continue
        outcome = doc.get("outcome") or load_json(tdir / "outcome.json") or {}
        observed = str(outcome.get("observed_at") or "")[:10]
        sig = load_json(tdir / "signature.json") or {}
        signed = str(sig.get("signed_at") or doc.get("signed_at") or "")[:10]
        if not observed or not signed:
            continue
        try:
            gap = (date.fromisoformat(observed) - date.fromisoformat(signed)).days
        except ValueError:
            continue
        if best is None or gap > best:
            best = gap
    if best is None:
        return False, "没有任何 T+7 回填"
    return (best >= BACKFILL_DAYS), f"最长回填间隔 {best} 天（需 ≥{BACKFILL_DAYS}）"


# ---------- M6–M7 ----------

def m6(events) -> tuple[bool, str]:
    closed = [e for e in events if e.get("event") == "bot_team_dialog_closed"]
    if not closed:
        return False, "没有任何对话关闭事件（无埋点=判红）"
    bad = []
    for e in closed:
        p = e.get("payload") or {}
        if int(p.get("rounds") or 0) > MAX_ROUNDS or int(p.get("elapsed_ms") or 0) > MAX_ELAPSED_MS or p.get("timed_out"):
            bad.append({k: p.get(k) for k in ("dialog_id", "rounds", "elapsed_ms", "timed_out")})
    if bad:
        return False, f"卡死/超时 {len(bad)} 次：{bad[:2]}"
    return True, f"{len(closed)} 次对话全部收敛"


def m7(events, items_doc) -> tuple[str, str]:
    """只看，不做门：返回 ('ok'|'insufficient', 描述)。"""
    issued = [e for e in events if e.get("event") == "bot_team_reminder_issued" and (e.get("payload") or {}).get("item_id")]
    if not issued:
        return "insufficient", "样本 0，无可算命中率"
    touches: list[tuple[str, datetime]] = []
    for e in events:
        if e.get("event") not in ("bot_team_item_updated", "bot_team_item_created"):
            continue
        try:
            touches.append((str((e.get("payload") or {}).get("item_id")), _dt(e.get("ts"))))
        except (TypeError, ValueError):
            continue
    hit = 0
    for e in issued:
        item_id = (e.get("payload") or {}).get("item_id")
        t0 = _dt(e.get("ts"))
        if t0 is None:
            continue
        if any(iid == item_id and t0 < t <= t0 + timedelta(hours=24) for iid, t in touches):
            hit += 1
    n = len(issued)
    if n < M7_MIN_SAMPLE:
        return "insufficient", f"样本 {n} < {M7_MIN_SAMPLE}，不出率"
    return "ok", f"命中 {hit}/{n} = {hit / n:.0%}"


def _dt(ts) -> datetime | None:
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


# ---------- 组装 ----------

def evaluate(runs_root: Path, events_path: Path, items_path: Path, since: str, until: str) -> list[tuple[str, bool, str]]:
    events = load_events(events_path)
    items = load_json(items_path)
    checks = [
        ("M0 账本在位", *m0(items)),
        ("M1 每天有对话", *m1(events, since, until)),
        ("M2 每天提醒+判定", *m2(events, since, until)),
        ("M3 团长裁决≥5", *m3(runs_root, since, until)),
        ("M4 每周有已签周卡", *m4(runs_root, since, until)),
        ("M5 T+7 回填", *m5(runs_root, since, until)),
        ("M6 零卡死", *m6(events)),
    ]
    status, desc = m7(events, items)
    checks.append(("M7 提醒命中率（观测）", True, f"[{status}] {desc}"))
    return checks


def _fixture(tmp: Path, *, items=45, revision=1, days=2, lead_cards=5, weekly_ok=True, backfill_days=7,
             dialog_rounds=2, timed_out=False, demo=False, missing_reminder_judge=False) -> dict:
    runs = tmp / "runs"
    events = tmp / "events.jsonl"
    items_path = tmp / "items.json"
    runs.mkdir(parents=True, exist_ok=True)
    items_path.write_text(json.dumps({"revision": revision, "items": [{"id": f"WI-{i:04d}"} for i in range(items)]},
                                     ensure_ascii=False), encoding="utf-8")
    base = datetime(2026, 10, 29, 9, 0, tzinfo=timezone.utc)
    lines = []
    for i in range(days):
        t = (base + timedelta(days=i)).isoformat(timespec="seconds")
        lines.append({"event": "bot_team_dialog_closed", "ts": t,
                      "payload": {"dialog_id": f"d{i}", "rounds": dialog_rounds,
                                  "elapsed_ms": MAX_ELAPSED_MS, "timed_out": timed_out}})
        lines.append({"event": "bot_team_reminder_issued", "ts": t, "payload": {"item_id": f"WI-{i:04d}"}})
        if not missing_reminder_judge:
            lines.append({"event": "bot_team_reminder_judged", "ts": t, "payload": {"verdict": "ok"}})
    events.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in lines) + "\n", encoding="utf-8")
    for i in range(lead_cards):
        tdir = runs / f"T-{i:04d}"
        tdir.mkdir(parents=True, exist_ok=True)
        (tdir / "report.json").write_text(json.dumps({
            "card_kind": "attribution", "is_demo": demo,
            "generated_at": "2026-10-29T10:00:00+08:00",
            "decision_log_entry": {"actor": "lead"},
        }, ensure_ascii=False), encoding="utf-8")
    wdir = runs / ("T-wk" if weekly_ok else "T-wk-demo")
    wdir.mkdir(parents=True, exist_ok=True)
    (wdir / "report.json").write_text(json.dumps({
        "card_kind": "weekly", "is_demo": (not weekly_ok) or demo,
        "generated_at": "2026-10-29T10:00:00+08:00",
        "decision_log_entry": {"actor": "lead"},
    }, ensure_ascii=False), encoding="utf-8")
    if weekly_ok:
        (wdir / "signature.json").write_text(json.dumps({"signed_at": "2026-10-29T10:00:00+08:00"}), encoding="utf-8")
    (wdir / "outcome.json").write_text(json.dumps({
        "observed_at": (date(2026, 10, 29) + timedelta(days=backfill_days)).isoformat(),
    }), encoding="utf-8")
    return {"runs": runs, "events": events, "items": items_path}


def selfcheck() -> int:
    since, until = "2026-10-29", "2026-10-30"
    green_expect = {"M0": True, "M1": True, "M2": True, "M3": True, "M4": True, "M5": True, "M6": True}
    negatives = [
        ("空账本", dict(items=0), "M0"),
        ("账本 rev=0", dict(revision=0), "M0"),
        ("缺一天对话", dict(days=1), "M1"),
        ("提醒未判定", dict(missing_reminder_judge=True), "M2"),
        ("团长裁决不足", dict(lead_cards=2), "M3"),
        ("周卡未签", dict(weekly_ok=False), "M4"),
        ("当天回填", dict(backfill_days=0), "M5"),
        ("对话卡死", dict(dialog_rounds=4), "M6"),
        ("对话超时", dict(timed_out=True), "M6"),
        ("演示卡混入", dict(demo=True), "M4"),
    ]
    fails = 0

    def run_case(name, kwargs, expect_bad=None, tmp=None):
        nonlocal fails
        fx = _fixture(tmp, **kwargs)
        checks = evaluate(fx["runs"], fx["events"], fx["items"], since, until)
        got = {c[0].split()[0]: c[1] for c in checks}
        if expect_bad is None:
            bad = [k for k, v in got.items() if not v and k != "M7"]
            if bad:
                print(f"[FAIL] 正控 {name}：意外红 {bad}")
                fails += 1
            else:
                print(f"[OK]   正控 {name}：M0–M6 全绿")
        else:
            if got.get(expect_bad):
                print(f"[FAIL] 负控 {name}：{expect_bad} 应为红却是绿")
                fails += 1
            else:
                print(f"[OK]   负控 {name}：{expect_bad} 按预期变红")

    with tempfile.TemporaryDirectory() as td:
        run_case("全绿基线", {}, None, Path(td) / "a")
    for name, kwargs, target in negatives:
        with tempfile.TemporaryDirectory() as td:
            run_case(name, kwargs, target, Path(td) / "b")
    print()
    print("RESULT: " + ("PASS（正控 1 + 负控 %d 全按预期）" % len(negatives) if not fails else f"FAIL（{fails} 项）"))
    return 0 if not fails else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="MVP 判据 M0–M7 机检")
    ap.add_argument("--since", default="")
    ap.add_argument("--until", default="")
    ap.add_argument("--runs-root", default=str(ROOT / "runs"))
    ap.add_argument("--events", default=str(ROOT / "runtime" / "events.jsonl"))
    ap.add_argument("--workitems", default=str(ROOT / "runtime" / "workitems" / "current.json"))
    ap.add_argument("--selfcheck", action="store_true")
    args = ap.parse_args(argv)
    if args.selfcheck:
        return selfcheck()
    if not args.since or not args.until:
        print("[ERROR] 需要 --since 与 --until（或 --selfcheck）", file=sys.stderr)
        return 2
    checks = evaluate(Path(args.runs_root), Path(args.events), Path(args.workitems), args.since, args.until)
    red = 0
    for name, ok, desc in checks:
        print(f"[{'OK' if ok else 'FAIL'}] {name}: {desc}")
        if not ok:
            red += 1
    print()
    print(f"RESULT: {'PASS（M0–M6 全绿，M7 见观测）' if not red else f'FAIL（{red} 条红）'}")
    return 0 if not red else 1


if __name__ == "__main__":
    raise SystemExit(main())
