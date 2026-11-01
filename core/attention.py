"""注意力账本与基线（Slice 1，2026-11-01）。

定位：**只读埋点 + 可复算基线**，不改提醒、不改任务状态、不改 UI 行为。

数据源：
  - 现有全局事件流 `runtime/events.jsonl`（对话/提醒/事项/任务）
  - 本模块自己的 `runtime/attention/ledger.jsonl`（attention_* 事件与快照）

口径（K-07 可复算）：
  - 窗口默认 3 天；只按事件 `ts` 过滤，不改写原事件
  - 打断：`bot_team_reminder_issued` + `attention_interrupt`
  - 票：`bot_team_item_created` / `attention_ticket_opened` 开头，
        `bot_team_item_closed` / `attention_ticket_resolved` 收口
  - WIP 峰值：按事件顺序维护“未收口票”集合的峰值；数据不足时如实返回 0，
    并在 `data_quality.wip_source` 标明来源
  - 任务切换：显式 `attention_task_switch` 事件；不从对话文本猜
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import pathlib
import re
import threading
import uuid

ROOT = pathlib.Path(__file__).resolve().parents[1]
DEFAULT_DAYS = 3
MAX_DAYS = 90
LEDGER_FILENAME = "ledger.jsonl"
EVENT_RE = re.compile(r"^attention_[a-z0-9_]+$")

INTERRUPT_EVENTS = ("bot_team_reminder_issued", "attention_interrupt")
TICKET_OPEN_EVENTS = ("bot_team_item_created", "attention_ticket_opened")
TICKET_CLOSE_EVENTS = ("bot_team_item_closed", "attention_ticket_resolved")
TASK_SWITCH_EVENTS = ("attention_task_switch",)

_lock = threading.Lock()


class AttentionError(ValueError):
    """注意力事件不合法。"""


def now_iso() -> str:
    return _dt.datetime.now().astimezone().isoformat(timespec="seconds")


def events_path(root=None) -> pathlib.Path:
    env = os.environ.get("BOT_EVENTS_FILE")
    if env:
        return pathlib.Path(env)
    return pathlib.Path(root or ROOT) / "runtime" / "events.jsonl"


def ledger_path(root=None) -> pathlib.Path:
    env = os.environ.get("BOT_ATTENTION_FILE")
    if env:
        return pathlib.Path(env)
    return pathlib.Path(root or ROOT) / "runtime" / "attention" / LEDGER_FILENAME


def _read_jsonl(path: pathlib.Path) -> tuple[list[dict], int]:
    if not path.is_file():
        return [], 0
    rows, bad = [], 0
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            try:
                rec = json.loads(raw)
            except json.JSONDecodeError:
                bad += 1
                continue
            if isinstance(rec, dict):
                rows.append(rec)
            else:
                bad += 1
    return rows, bad


def read_ledger(root=None) -> list[dict]:
    rows, _ = _read_jsonl(ledger_path(root))
    return rows


def _parse_ts(value) -> _dt.datetime | None:
    if not value:
        return None
    try:
        ts = _dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=_dt.datetime.now().astimezone().tzinfo)
    return ts


def _local_now() -> _dt.datetime:
    return _dt.datetime.now().astimezone()


def _ref_id(rec: dict) -> str:
    payload = rec.get("payload") if isinstance(rec.get("payload"), dict) else {}
    refs = rec.get("refs") if isinstance(rec.get("refs"), dict) else {}
    for key in ("ticket_id", "item_id", "task_id", "id"):
        value = payload.get(key) or refs.get(key)
        if value not in (None, ""):
            return str(value)
    return str(rec.get("event_id") or f"seq-{rec.get('seq')}")


def _validate(name: str, payload: dict) -> None:
    if not EVENT_RE.match(str(name or "")):
        raise AttentionError(f"事件名必须匹配 attention_<lower_snake>：{name!r}")
    if not isinstance(payload, dict):
        raise AttentionError("payload 必须是对象")


def append(name: str, payload: dict | None = None, *, root=None,
           actor: str = "system", ts: str | None = None) -> dict:
    """追加一条 attention_* 事件；只 append，不修改历史。"""
    payload = payload or {}
    _validate(name, payload)
    path = ledger_path(root)
    with _lock:
        rows, _ = _read_jsonl(path)
        last_seq = max((int(r.get("seq") or 0) for r in rows), default=0)
        rec = {
            "seq": last_seq + 1,
            "event_id": uuid.uuid4().hex,
            "ts": ts or now_iso(),
            "event": name,
            "actor": actor,
            "payload": payload,
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(rec, ensure_ascii=False, separators=(",", ":")) + "\n"
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        try:
            os.write(fd, line.encode("utf-8"))
        finally:
            os.close(fd)
        return rec


def _window(events: list[dict], *, days: int, now: _dt.datetime) -> tuple[list[dict], _dt.datetime]:
    since = now - _dt.timedelta(days=max(1, int(days)))
    selected = []
    for rec in events:
        ts = _parse_ts(rec.get("ts"))
        if ts is not None and since <= ts <= now:
            selected.append(rec)
    selected.sort(key=lambda r: (_parse_ts(r.get("ts")) or since, int(r.get("seq") or 0)))
    return selected, since


def metrics(*, root=None, days: int = DEFAULT_DAYS, now: _dt.datetime | None = None,
            events: list[dict] | None = None) -> dict:
    """返回过去 N 天的注意力基线；纯读，不写账本。"""
    days = max(1, min(MAX_DAYS, int(days or DEFAULT_DAYS)))
    now = now or _local_now()
    if now.tzinfo is None:
        now = now.replace(tzinfo=_local_now().tzinfo)
    source_path = events_path(root)
    if events is None:
        events, bad = _read_jsonl(source_path)
    else:
        bad = 0
    selected, since = _window(events, days=days, now=now)

    interruptions = 0
    by_kind: dict[str, int] = {}
    by_source: dict[str, int] = {}
    tickets_opened = 0
    tickets_resolved = 0
    task_switches = 0
    open_ids: set[str] = set()
    wip_peak = 0
    event_counts: dict[str, int] = {}

    for rec in selected:
        name = str(rec.get("event") or "")
        payload = rec.get("payload") if isinstance(rec.get("payload"), dict) else {}
        event_counts[name] = event_counts.get(name, 0) + 1

        if name in INTERRUPT_EVENTS:
            interruptions += 1
            kind = str(payload.get("kind") or "unknown")
            by_kind[kind] = by_kind.get(kind, 0) + 1
            source = str(payload.get("source") or "unknown")
            by_source[source] = by_source.get(source, 0) + 1
        elif name in TICKET_OPEN_EVENTS:
            tickets_opened += 1
            open_ids.add(_ref_id(rec))
        elif name in TICKET_CLOSE_EVENTS:
            tickets_resolved += 1
            open_ids.discard(_ref_id(rec))
        elif name in TASK_SWITCH_EVENTS:
            task_switches += 1

        if name in TICKET_OPEN_EVENTS:
            wip_peak = max(wip_peak, len(open_ids))

    return {
        "ok": True,
        "as_of": now.isoformat(timespec="seconds"),
        "window": {
            "days": days,
            "since": since.isoformat(timespec="seconds"),
            "until": now.isoformat(timespec="seconds"),
        },
        "answers": {
            "interruptions": interruptions,
            "tickets_processed": tickets_resolved,
            "wip_peak": wip_peak,
        },
        "tickets": {
            "opened": tickets_opened,
            "resolved": tickets_resolved,
            "open_now": len(open_ids),
        },
        "task_switches": task_switches,
        "interruptions": {
            "total": interruptions,
            "by_kind": dict(sorted(by_kind.items())),
            "by_source": dict(sorted(by_source.items())),
        },
        "by_event": dict(sorted(event_counts.items())),
        "data_quality": {
            "events_scanned": len(events),
            "events_in_window": len(selected),
            "bad_lines": bad,
            "wip_source": "item_events",
            "history": bool(selected),
        },
        "sources": {
            "events": str(source_path),
            "ledger": str(ledger_path(root)),
        },
    }


def snapshot(*, root=None, days: int = DEFAULT_DAYS, now: _dt.datetime | None = None) -> dict:
    """把当前基线写成一条 attention_snapshot（显式调用，GET 不落盘）。"""
    data = metrics(root=root, days=days, now=now)
    rec = append("attention_snapshot", {"metrics": data}, root=root, actor="attention")
    return {"ok": True, "snapshot": data, "ledger_event": rec}


# ---------------------------------------------------------------- WIP / 今日 / 队列（Slice 2）

WIP_LIMIT = 2
TODAY_LIMIT = 3
ACTIVE_STATUSES = ("todo", "doing", "blocked")
PRIORITY_RANK = {"I": 0, "II": 1, "III": 2}


def _meta(item: dict) -> dict:
    value = item.get("attention") if isinstance(item, dict) else None
    return value if isinstance(value, dict) else {}


def is_parked(item: dict) -> bool:
    return bool(_meta(item).get("parked"))


def active_items(items: list[dict]) -> list[dict]:
    return [it for it in (items or [])
            if isinstance(it, dict) and it.get("status") in ACTIVE_STATUSES and not is_parked(it)]


def wip_count(items: list[dict]) -> int:
    return len(active_items(items))


def _sort_key(item: dict):
    return (
        PRIORITY_RANK.get(str(item.get("priority") or ""), 3),
        str(item.get("due") or "9999-12-31"),
        str(item.get("updated_at") or ""),
        str(item.get("id") or ""),
    )


def today(items: list[dict], *, limit: int = TODAY_LIMIT) -> dict:
    """今日最多三件；停泊项不进入今日，不改变任何事项。"""
    limit = max(1, min(TODAY_LIMIT, int(limit or TODAY_LIMIT)))
    active = sorted(active_items(items), key=_sort_key)[:limit]
    parked = [it for it in (items or []) if isinstance(it, dict) and is_parked(it)]
    lines = []
    for i, it in enumerate(active, 1):
        owner = str(it.get("owner") or "未指定")
        due = str(it.get("due") or "无截止")
        lines.append(f"{i}. {it.get('title') or '未命名'}（{owner}，{due}）")
    brief = "今日 " + str(len(active)) + " 件：\n" + ("\n".join(lines) if lines else "没有必须推进的事。")
    if parked:
        brief += f"\n停泊 {len(parked)} 件；要换入请先 /park 或 /resume。"
    return {
        "ok": True,
        "date": _local_now().date().isoformat(),
        "limit": limit,
        "wip": {"active": wip_count(items), "limit": WIP_LIMIT,
                "slots_left": max(0, WIP_LIMIT - wip_count(items))},
        "items": active,
        "parked": parked,
        "brief": brief,
    }


def queue(items: list[dict]) -> dict:
    return {
        "ok": True,
        "wip": {"active": wip_count(items), "limit": WIP_LIMIT},
        "active": sorted(active_items(items), key=_sort_key),
        "parked": sorted([it for it in (items or []) if isinstance(it, dict) and is_parked(it)],
                         key=_sort_key),
    }


def park(item: dict, *, reason: str = "manual", resume_at: str = "",
         actor: str = "owner") -> dict:
    """把事项停泊：释放一个 WIP 槽；保留原状态，便于 /resume 恢复。"""
    if not isinstance(item, dict):
        raise AttentionError("item 必须是对象")
    if is_parked(item):
        return item
    meta = _meta(item)
    meta.update({
        "parked": True,
        "parked_at": now_iso(),
        "resume_at": str(resume_at or ""),
        "reason": str(reason or "manual"),
        "previous_status": str(item.get("status") or "todo"),
    })
    item["attention"] = meta
    item["status"] = "paused"
    item["updated_at"] = now_iso()
    item["updated_by"] = str(actor or "owner")
    return item


def resume(item: dict, items: list[dict], *, actor: str = "owner") -> dict:
    """恢复停泊项；WIP 已满则拒绝，不静默突破。"""
    if not is_parked(item):
        return item
    if wip_count(items) >= WIP_LIMIT:
        raise AttentionError(f"WIP 已满（{wip_count(items)}/{WIP_LIMIT}）；先 /park 一个再 /resume")
    meta = _meta(item)
    previous = str(meta.get("previous_status") or "todo")
    item["status"] = previous if previous in ACTIVE_STATUSES else "todo"
    meta["parked"] = False
    meta["resumed_at"] = now_iso()
    item["attention"] = meta
    item["updated_at"] = now_iso()
    item["updated_by"] = str(actor or "owner")
    return item


def enforce_wip(item: dict, items: list[dict], *, actor: str = "owner") -> bool:
    """新事项超过 WIP 时强制停泊；返回是否被停泊。"""
    if wip_count(items) <= WIP_LIMIT:
        return False
    park(item, reason="wip_full", actor=actor)
    return True


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="注意力账本与基线（Slice 1）")
    ap.add_argument("--root", help="bot-team 根目录")
    ap.add_argument("--days", type=int, default=DEFAULT_DAYS)
    ap.add_argument("--record", action="store_true", help="写一条 attention_snapshot")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args(argv)
    data = snapshot(root=args.root, days=args.days) if args.record else metrics(root=args.root, days=args.days)
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        a = data.get("answers", {})
        print(f"窗口 {data['window']['days']} 天：打断 {a.get('interruptions', 0)} 次，"
              f"处理票 {a.get('tickets_processed', 0)} 张，WIP 峰值 {a.get('wip_peak', 0)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
