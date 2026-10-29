#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""提醒供给层：把「事件侧提醒」和「账本侧到期」合成一张今日提醒表，并负责发/判事件。

分工：
  core/reminders.py  纯判断：给定事件与事项，算出该提醒谁（不写文件）
  core/reminder_feed.py 供给：加账本到期兜底、去重、当天只发一次、记录判定

事件（全部带 bot_team_ 前缀，写 runtime/events.jsonl）：
  bot_team_reminder_issued  {item_id, kind, source, reason, n_th, title, owner}
  bot_team_reminder_judged  {item_id, verdict}   verdict ∈ ok / not_ok / snooze

口径：
  - 硬提醒（kind=hard）来自人确认的到期日或账本里人填的计划完成时间；软提醒（soft）来自 AI 推断。
  - 同一事项同一天只发一次 issued：靠「当日已有 issued」判重，保证 M2 的分子不会靠重复刷。
  - 判「不准/别烦」后当日不再发；判 ok 记录命中。
"""
from __future__ import annotations

import datetime as _dt
from pathlib import Path

try:
    from core import eventlog
    from core import reminders as _rem
except ImportError:  # 直接跑脚本时
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from core import eventlog  # type: ignore
    from core import reminders as _rem  # type: ignore

VERDICTS = ("ok", "not_ok", "snooze")
SUPPRESS = ("not_ok", "snooze")
KIND_LABEL = {"hard": "到期未完成", "soft": "可能到期", "stale": "久未处理"}
MAX_EMIT_PER_DAY = 3          # 一天最多主发 3 条：提醒不是列表轰炸


def _today(now: _dt.datetime | None = None) -> str:
    return (now or _dt.datetime.now().astimezone()).date().isoformat()


def _parse_date(value) -> _dt.date | None:
    if isinstance(value, _dt.date) and not isinstance(value, _dt.datetime):
        return value
    try:
        return _dt.date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def _ids_on(events: list[dict], day: str, event: str, verdicts: tuple[str, ...] | None = None) -> set[str]:
    out = set()
    for ev in events:
        if ev.get("event") != event:
            continue
        ts = str(ev.get("ts") or "")[:10]
        if ts != day:
            continue
        p = ev.get("payload") or {}
        if verdicts is not None and str(p.get("verdict") or "") not in verdicts:
            continue
        if p.get("item_id"):
            out.add(str(p["item_id"]))
    return out


def feed(events: list[dict], items_doc, now: _dt.datetime | None = None) -> dict:
    """返回 {today, rows[], counts, to_emit[], suppressed[]}；纯函数，不发事件。"""
    now = now or _dt.datetime.now().astimezone()
    day = _today(now)
    doc = items_doc if isinstance(items_doc, dict) else {}
    items = [it for it in (doc.get("items") or []) if isinstance(it, dict)]

    rows: dict[str, dict] = {}
    for r in _rem.evaluate(events, items, now):
        rows[str(r["item_id"])] = dict(r)

    # 账本兜底：人填的计划完成时间已过且未完成 —— 事件侧没记录到期时也能提醒
    for it in items:
        item_id = str(it.get("id") or "")
        if not item_id or item_id in rows:
            continue
        if it.get("status") in ("done", "cancelled"):
            continue
        due = _parse_date(it.get("due") or it.get("due_raw"))
        if not due or due > now.date():
            continue
        rows[item_id] = {
            "item_id": item_id,
            "kind": "hard",
            "source": "ledger_due",
            "reason": f"计划完成 {due.isoformat()} 已过",
            "priority": 2,
            "n_th": 1,
            "title": it.get("title"),
            "owner": it.get("owner"),
            "overdue_days": (now.date() - due).days,
        }

    issued_today = _ids_on(events, day, "bot_team_reminder_issued")
    suppressed = _ids_on(events, day, "bot_team_reminder_judged", SUPPRESS)

    out_rows = []
    to_emit = []
    for item_id, row in rows.items():
        row = dict(row)
        row["kind_label"] = KIND_LABEL.get(row.get("kind"), row.get("kind"))
        row["issued_today"] = item_id in issued_today
        row["suppressed_today"] = item_id in suppressed
        out_rows.append(row)
        if item_id not in issued_today and item_id not in suppressed:
            to_emit.append(row)

    out_rows.sort(key=lambda r: (-int(r.get("priority") or 0), -int(r.get("overdue_days") or 0), r["item_id"]))
    budget = max(0, MAX_EMIT_PER_DAY - len(issued_today & {r["item_id"] for r in out_rows}))
    to_emit = to_emit[:budget]
    return {
        "today": day,
        "rows": out_rows,
        "to_emit": to_emit,
        "suppressed": sorted(suppressed),
        "counts": {
            "total": len(out_rows),
            "hard": sum(1 for r in out_rows if r.get("kind") == "hard"),
            "soft": sum(1 for r in out_rows if r.get("kind") == "soft"),
            "issued_today": len(issued_today & {r["item_id"] for r in out_rows}),
        },
    }


def emit_issued(rows: list[dict], *, path=None, actor: str = "lead", dialog_id: str = "") -> list[dict]:
    """把待发提醒写成事件（幂等交给调用方：rows 来自 feed().to_emit）。"""
    written = []
    for row in rows:
        ev = eventlog.emit(
            "reminder_issued",
            actor=actor,
            dialog_id=dialog_id,
            payload={
                "item_id": row["item_id"],
                "kind": row.get("kind"),
                "source": row.get("source"),
                "reason": row.get("reason"),
                "n_th": row.get("n_th"),
                "title": row.get("title"),
                "owner": row.get("owner"),
            },
            path=path,
        )
        written.append(ev)
    return written


def judge(item_id: str, verdict: str, *, path=None, actor: str = "owner", note: str = "") -> dict:
    """记录人对提醒的判定。verdict 非法直接抛 ValueError（fail-closed）。"""
    verdict = (verdict or "").strip().lower()
    if verdict not in VERDICTS:
        raise ValueError(f"verdict 必须是 {'/'.join(VERDICTS)}（收到 {verdict!r}）")
    if not str(item_id or "").strip():
        raise ValueError("item_id 不能为空")
    return eventlog.emit(
        "reminder_judged",
        actor=actor,
        payload={"item_id": item_id, "verdict": verdict, "note": note},
        path=path,
    )


def stats(events: list[dict]) -> dict:
    """提醒总量与命中（M2/M7 的上游计数）。"""
    issued = [e for e in events if e.get("event") == "bot_team_reminder_issued"]
    judged = [e for e in events if e.get("event") == "bot_team_reminder_judged"]
    ok = sum(1 for e in judged if (e.get("payload") or {}).get("verdict") == "ok")
    not_ok = sum(1 for e in judged if (e.get("payload") or {}).get("verdict") == "not_ok")
    snooze = sum(1 for e in judged if (e.get("payload") or {}).get("verdict") == "snooze")
    return {
        "issued": len(issued),
        "judged": len(judged),
        "ok": ok,
        "not_ok": not_ok,
        "snooze": snooze,
        "judge_rate": (len(judged) / len(issued)) if issued else None,
    }
