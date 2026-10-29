"""E2 提醒引擎：事件 → 投影 → 该提醒谁（纯函数，无 IO、无模型）。

设计口径（MVP 方案 §6–§7）：
  - **到期最终裁定是人**：`due_confirmed` 才出硬提醒；只有 `due_proposal` 出软提示
  - 优先级：状态变化 > 人确认日期 > AI 推断
  - 硬提醒最多 2 次、间隔 ≥20h；判「不准/别烦」立即抑制；3 天无反应并进周汇总
  - 纯函数：`evaluate(events, items, now)` 只吃数据、只吐计划，方便单测与重放（K-07）

对外：
  projections(events)      → {"duedates": {...}, "reminders": {...}, "followups": [...]}
  evaluate(events, items, now) → [{"item_id","kind","reason","priority","n_th","detail"}]
"""
from __future__ import annotations

import datetime as _dt

MAX_HARD = 2                 # 硬提醒最多 2 次
MIN_GAP_HOURS = 20           # 两次硬提醒最小间隔
STALE_DAYS = 3               # 3 天无反应 → 进周汇总
STATE_CHANGE_HOURS = 24      # 状态变化的新鲜窗口

SUPPRESS_VERDICTS = ("不准", "别烦", "wrong", "stop", "no")

PRIORITY = {"state_change": 3, "due_confirmed": 2, "due_proposal": 1}
KIND = {"state_change": "hard", "due_confirmed": "hard", "due_proposal": "soft"}


def _parse_ts(value) -> _dt.datetime | None:
    if not value:
        return None
    try:
        return _dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _parse_date(value) -> _dt.date | None:
    if not value:
        return None
    try:
        return _dt.date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def projections(events: list[dict]) -> dict:
    """把事件流投影成提醒需要的视图（幂等、只读）。"""
    duedates: dict[str, dict] = {}
    reminders: dict[str, list[dict]] = {}
    followups: list[dict] = []
    open_followups: dict[str, dict] = {}

    for ev in sorted(events, key=lambda e: int(e.get("seq") or 0)):
        name = ev.get("event")
        payload = ev.get("payload") or {}
        refs = ev.get("refs") or {}
        item_id = refs.get("item_id") or payload.get("item_id")

        if name == "bot_team_duedate_proposed" and item_id:
            duedates.setdefault(item_id, {})["proposal"] = {
                "value": payload.get("value"), "confidence": payload.get("confidence"),
                "ts": ev.get("ts"), "event_id": ev.get("event_id"),
            }
        elif name == "bot_team_duedate_confirmed" and item_id:
            duedates.setdefault(item_id, {})["confirmed"] = {
                "value": payload.get("value"), "by": payload.get("by") or ev.get("actor"),
                "ts": ev.get("ts"), "event_id": ev.get("event_id"),
            }
        elif name == "bot_team_reminder_issued":
            key = item_id or payload.get("followup_id") or ev.get("event_id")
            reminders.setdefault(str(key), []).append({
                "reminder_id": payload.get("reminder_id") or ev.get("event_id"),
                "item_id": item_id, "kind": payload.get("kind"), "ts": ev.get("ts"),
                "n_th": payload.get("n_th"),
            })
        elif name == "bot_team_reminder_judged":
            key = payload.get("reminder_id") or item_id
            for bucket in reminders.values():
                for r in bucket:
                    if r["reminder_id"] == key or (item_id and r["item_id"] == item_id):
                        r["verdict"] = payload.get("verdict")
                        r["judged_ts"] = ev.get("ts")
        elif name == "bot_team_followup_queued":
            fid = payload.get("followup_id") or ev.get("event_id")
            rec = {
                "followup_id": fid, "trigger": payload.get("trigger"),
                "next_action": payload.get("next_action"), "due": payload.get("due"),
                "ts": ev.get("ts"), "state": "open",
            }
            open_followups[str(fid)] = rec
            followups.append(rec)
        elif name == "bot_team_followup_closed":
            fid = payload.get("followup_id")
            if fid and str(fid) in open_followups:
                open_followups[str(fid)]["state"] = "closed"

    return {
        "duedates": duedates,
        "reminders": {k: v for k, v in reminders.items()},
        "followups": followups,
        "open_followups": [f for f in followups if f["state"] == "open"],
    }


def _last_issued(reminders: dict, item_id: str) -> dict | None:
    rows = reminders.get(item_id) or []
    if not rows:
        return None
    return sorted(rows, key=lambda r: str(r.get("ts") or ""))[-1]


def evaluate(
    events: list[dict],
    items: list[dict],
    now: _dt.datetime | None = None,
    *,
    state_change_hours: int = STATE_CHANGE_HOURS,
    min_gap_hours: int = MIN_GAP_HOURS,
    max_hard: int = MAX_HARD,
    stale_days: int = STALE_DAYS,
) -> list[dict]:
    """给出「现在该提醒什么」。纯函数：同样的输入永远同样的输出。"""
    now = now or _dt.datetime.now().astimezone()
    proj = projections(events)
    duedates = proj["duedates"]
    reminders = proj["reminders"]
    plan: list[dict] = []

    open_items = {str(it.get("id")): it for it in items if it.get("status") not in ("done", "cancelled")}

    # 1) 状态变化优先：blocked 且 24h 内发生
    for item_id, item in open_items.items():
        changed = _parse_ts(item.get("updated_at") or item.get("status_ts"))
        if item.get("status") == "blocked" and changed:
            if _hours_between(changed, now) <= state_change_hours:
                plan.append(_row(item_id, "state_change", "状态变成阻塞", now, reminders, item))

    # 2) 到期：人确认 → 硬；AI 推断 → 软
    for item_id, item in open_items.items():
        dd = duedates.get(item_id) or {}
        confirmed = dd.get("confirmed") or {}
        proposal = dd.get("proposal") or {}
        source = "due_confirmed" if confirmed.get("value") else ("due_proposal" if proposal.get("value") else None)
        if not source:
            continue
        due = _parse_date(confirmed.get("value") or proposal.get("value"))
        if not due or due > now.date():
            continue
        row = _row(item_id, source, f"到期 {due.isoformat()}", now, reminders, item,
                   min_gap_hours=min_gap_hours, max_hard=max_hard, stale_days=stale_days)
        if row:
            plan.append(row)

    # 去重：同一 item 只留优先级最高的一条
    best: dict[str, dict] = {}
    for row in plan:
        cur = best.get(row["item_id"])
        if cur is None or row["priority"] > cur["priority"]:
            best[row["item_id"]] = row

    return sorted(best.values(), key=lambda r: (-r["priority"], r["item_id"]))


def _hours_between(then: _dt.datetime, now: _dt.datetime) -> float:
    if then.tzinfo is None:
        then = then.astimezone()
    return (now - then).total_seconds() / 3600.0


def _row(item_id, source, reason, now, reminders, item, *, min_gap_hours=MIN_GAP_HOURS,
         max_hard=MAX_HARD, stale_days=STALE_DAYS) -> dict | None:
    last = _last_issued(reminders, item_id)
    n_th = 0
    if last:
        verdict = (last.get("verdict") or "").strip().lower()
        if verdict in SUPPRESS_VERDICTS:
            return None
        n_th = int(last.get("n_th") or 0) or len(reminders.get(item_id) or [])
        last_ts = _parse_ts(last.get("ts"))
        if last_ts and _hours_between(last_ts, now) < min_gap_hours:
            return None
        if n_th >= max_hard:
            # 次数用尽：超过 stale_days 就交给周汇总，不再单发
            if last_ts and _hours_between(last_ts, now) / 24 >= stale_days:
                return None
            return None
    return {
        "item_id": item_id,
        "kind": KIND[source],
        "source": source,
        "reason": reason,
        "priority": PRIORITY[source],
        "n_th": n_th + 1,
        "title": item.get("title"),
        "owner": item.get("owner"),
    }


def due_for_review(events: list[dict], now: _dt.datetime | None = None, *, stale_days: int = STALE_DAYS) -> list[dict]:
    """3 天没反应的提醒 → 周汇总候选（不再单独打扰）。"""
    now = now or _dt.datetime.now().astimezone()
    proj = projections(events)
    out = []
    for item_id, rows in proj["reminders"].items():
        last = sorted(rows, key=lambda r: str(r.get("ts") or ""))[-1]
        if (last.get("verdict") or "").strip().lower() in SUPPRESS_VERDICTS:
            continue
        ts = _parse_ts(last.get("ts"))
        if ts and _hours_between(ts, now) / 24 >= stale_days:
            out.append({"item_id": item_id, "last_ts": last.get("ts"), "n_th": last.get("n_th")})
    return out
