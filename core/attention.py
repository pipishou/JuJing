"""注意力账本与基线（Slice 1，2026-11-01；2026-11-02 改为单一事实源）。

定位：**只读埋点 + 可复算基线**，不改提醒、不改任务状态、不改 UI 行为。

单一事实源（P0-4，2026-11-02）：
  - 事实源只有一条：`runtime/events.jsonl`（经 `core.eventlog.emit()` 写入）。
    注意力动作统一落成 `bot_team_attention_*` 事件，例如
    `attention.append("attention_interrupt", ...)` → `bot_team_attention_interrupt`。
  - `runtime/attention/ledger.jsonl` 降级为**投影缓存（projection cache）**：
    内容完全可由事件流重算，删掉不丢事实。
      重建方式：`attention.rebuild_ledger()`（纯函数 `project_ledger(events)`），
      或命令行 `python -B core/attention.py --rebuild`。
  - `metrics()` 只读事件流；**禁止**把 ledger 当事实源（旧实现因此恒 0：
    写 ledger 不写 events，metrics 又只读 events）。

口径（K-07 可复算）：
  - 窗口默认 3 天；只按事件 `ts` 过滤，不改写原事件
  - 打断：`bot_team_reminder_issued` + `bot_team_attention_interrupt`
  - 票：`bot_team_item_created` / `bot_team_attention_ticket_opened` 开头，
        `bot_team_item_closed` / `bot_team_attention_ticket_resolved` 收口
  - WIP 峰值：按事件顺序维护“未收口票”集合的峰值；数据不足时如实返回 0，
    并在 `data_quality.wip_source` 标明来源
  - 任务切换：显式 `bot_team_attention_task_switch` 事件；不从对话文本猜
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:  # 允许 `python -B core/attention.py`
    sys.path.insert(0, str(ROOT))

from core import eventlog  # noqa: E402
from core import fsutil  # noqa: E402
from core.filelock import FileLock  # noqa: E402

DEFAULT_DAYS = 3
MAX_DAYS = 90
LEDGER_FILENAME = "ledger.jsonl"
EVENT_RE = re.compile(r"^attention_[a-z0-9_]+$")
ATTENTION_PREFIX = "bot_team_attention_"

# 事件名按「去掉 bot_team_ 前缀后的短名」比对，这样
# `bot_team_reminder_issued`（真实提醒链路）与 `bot_team_attention_*`（注意力动作）
# 共用一套口径，且历史事件无需迁移。
INTERRUPT_EVENTS = ("reminder_issued", "attention_interrupt")
TICKET_OPEN_EVENTS = ("item_created", "attention_ticket_opened")
TICKET_CLOSE_EVENTS = ("item_closed", "attention_ticket_resolved")
TASK_SWITCH_EVENTS = ("attention_task_switch",)


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
    """读账本投影缓存（非事实源；删掉可用 rebuild_ledger 从事件流重建）。"""
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


def _validate(name: str, payload: dict) -> tuple[str, str]:
    """校验事件名并返回 (短名 attention_xxx, 事件流全名 bot_team_attention_xxx)。"""
    if not isinstance(payload, dict):
        raise AttentionError("payload 必须是对象")
    raw = str(name or "").strip()
    if raw.startswith(ATTENTION_PREFIX):
        short = "attention_" + raw[len(ATTENTION_PREFIX):]
        if not EVENT_RE.match(short):
            raise AttentionError(f"事件名必须匹配 attention_<lower_snake>：{name!r}")
        return short, raw
    if EVENT_RE.match(raw):
        return raw, ATTENTION_PREFIX + raw[len("attention_"):]
    raise AttentionError(
        f"事件名必须匹配 attention_<lower_snake> 或 bot_team_attention_<lower_snake>：{name!r}"
    )


def _short_event(name) -> str:
    """事件流全名 → 短名（去掉 bot_team_ 前缀）；非 bot_team_* 原样返回。"""
    raw = str(name or "")
    return raw[len("bot_team_"):] if raw.startswith("bot_team_") else raw


def _is_attention_event(rec: dict) -> bool:
    return str(rec.get("event") or "").startswith(ATTENTION_PREFIX)


def project_ledger(events: list[dict]) -> list[dict]:
    """纯函数：事件流 → 账本投影行（不落盘）。

    只收 `bot_team_attention_*`；`seq` 是注意力事件自己的 1..N 序号，
    `source_seq` 保留它在全局事件流里的序号，便于对账。
    相同输入必然得到相同输出——这是「删掉 ledger 可重建」的依据。
    """
    rows: list[dict] = []
    for rec in events or []:
        if not isinstance(rec, dict) or not _is_attention_event(rec):
            continue
        payload = rec.get("payload") if isinstance(rec.get("payload"), dict) else {}
        rows.append({
            "seq": len(rows) + 1,
            "event_id": rec.get("event_id"),
            "ts": rec.get("ts"),
            "event": _short_event(rec.get("event")),
            "actor": rec.get("actor") or "system",
            "payload": dict(payload),
            "source_seq": int(rec.get("seq") or 0),
        })
    return rows


def _ledger_lock(root=None) -> FileLock:
    """账本投影的跨进程锁；emit + 重算 + 落盘必须在同一把锁内，避免缓存回退。"""
    path = ledger_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    return FileLock(path.with_name(path.name + ".lock"))


def _write_rows(rows: list[dict], root=None) -> None:
    path = ledger_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "".join(
        json.dumps(r, ensure_ascii=False, separators=(",", ":")) + "\n" for r in rows
    )
    fsutil.atomic_write_text(path, text)


def rebuild_ledger(events: list[dict] | None = None, *, root=None,
                   write: bool = True) -> list[dict]:
    """从事件流重建账本投影缓存；返回重建后的行。

    events 为空/None 时读事件流；给了 events 即纯函数式（可测）。
    重建是幂等的：同样的 events → 同样的 ledger 字节。
    """
    if not write:
        if events is None:
            events = eventlog.iter_events(events_path(root))
        return project_ledger(events)
    with _ledger_lock(root):
        if events is None:
            events = eventlog.iter_events(events_path(root))
        rows = project_ledger(events)
        _write_rows(rows, root)
        return rows


def append(name: str, payload: dict | None = None, *, root=None,
           actor: str = "system", ts: str | None = None) -> dict:
    """写一条注意力动作：emit 到全局事件流，再刷新账本投影（P0-4 单一事实源）。

    返回事件流记录（`event` 为 `bot_team_attention_*`）。
    """
    payload = payload or {}
    _, canonical = _validate(name, payload)
    with _ledger_lock(root):
        rec = eventlog.emit(canonical, actor=actor, payload=payload,
                            path=events_path(root), ts=ts)
        # ledger 只是投影缓存；从事件流重算保证「删掉也能恢复且内容一致」。
        _write_rows(project_ledger(eventlog.iter_events(events_path(root))), root)
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
        short = _short_event(name)
        payload = rec.get("payload") if isinstance(rec.get("payload"), dict) else {}
        event_counts[name] = event_counts.get(name, 0) + 1

        if short in INTERRUPT_EVENTS:
            interruptions += 1
            kind = str(payload.get("kind") or "unknown")
            by_kind[kind] = by_kind.get(kind, 0) + 1
            source = str(payload.get("source") or "unknown")
            by_source[source] = by_source.get(source, 0) + 1
        elif short in TICKET_OPEN_EVENTS:
            tickets_opened += 1
            open_ids.add(_ref_id(rec))
        elif short in TICKET_CLOSE_EVENTS:
            tickets_resolved += 1
            open_ids.discard(_ref_id(rec))
        elif short in TASK_SWITCH_EVENTS:
            task_switches += 1

        if short in TICKET_OPEN_EVENTS:
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
            "source_of_truth": "events",
            "ledger_role": "projection",
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
    ap.add_argument("--rebuild", action="store_true",
                    help="从事件流重建账本投影缓存（删掉 ledger 后可用）")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args(argv)
    if args.rebuild:
        rows = rebuild_ledger(root=args.root)
        print(json.dumps({"ok": True, "rebuilt": len(rows),
                          "ledger": str(ledger_path(args.root))}, ensure_ascii=False))
        return 0
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
