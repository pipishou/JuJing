"""注意力票（Slice 2）：一问 + 2–3 选项 + 推荐 + 截止。

票不是聊天消息，而是可判定、可过期的决策单元：
  - 缺 recommendation / options / deadline → 拒绝开票
  - 一个问题只能有一个问号；两个问题必须拆票
  - 只追加 `runtime/attention/tickets.jsonl`，不修改历史

状态机（2026-11-02 P0-1）：
  open | deferred | snoozed | decided | dropped
  - decide 可作用于 open/deferred/snoozed（deferred/snoozed 裁决即“复活”）
  - resume 把 deferred/snoozed 拉回 open
  - decided/dropped 是终态，任何迁移都抛 TicketConflict（HTTP 409）

每日配额（P0-3）：
  按 agent_id + 本地日期从票账本计数；默认 3 次/天。超限不静默丢弃，
  写 `attention_ticket_suppressed`（含 reason/limit/used）并返回 suppressed 票，
  由 /today 降级进当日摘要。one_way=True 可穿透配额与 focus。

focus（P0-3 最小实现）：
  同一时刻只允许 1 个 focus item；切换必须显式 override=True。
  focus 内的普通票被抑制（reason=focus_active），单向门照常开。

对外：
  open_ticket(...) -> dict
  list_tickets(...) -> list[dict]
  decide/defer/snooze/resume/drop(ticket_id, ...) -> dict
  list_suppressed(...) -> list[dict]
  daily_usage(agent_id, ...) -> dict
  focus_state / start_focus / end_focus
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import pathlib
import uuid

ROOT = pathlib.Path(__file__).resolve().parents[1]
TICKET_EVENT_RE = ("attention_ticket_opened", "attention_ticket_decided",
                   "attention_ticket_deferred")
MAX_OPTIONS = 3
DAILY_INTERRUPT_LIMIT = 3
DEFAULT_FOCUS_MIN = 90
MAX_FOCUS_MIN = 480

TICKET_STATUSES = ("open", "deferred", "snoozed", "decided", "dropped")
TERMINAL_STATUSES = ("decided", "dropped")
DECIDABLE_STATUSES = ("open", "deferred", "snoozed")
RESUMABLE_STATUSES = ("deferred", "snoozed")
DEFERRABLE_STATUSES = ("open", "deferred", "snoozed")
SNOOZABLE_STATUSES = ("open", "deferred")
DROPPABLE_STATUSES = ("open", "deferred", "snoozed")


class TicketError(ValueError):
    """票不合法或状态不允许。"""


class TicketConflict(TicketError):
    """状态迁移不允许（HTTP 409），与字段校验（400）区分。"""

    http_status = 409


def now_iso() -> str:
    return _dt.datetime.now().astimezone().isoformat(timespec="seconds")


def _stamp(now=None) -> str:
    if not now:
        return now_iso()
    raw = str(now).strip()
    try:
        _dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise TicketError(f"now 不是 ISO 时间：{raw!r}") from exc
    return raw


def _date_of(value) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        return _dt.datetime.fromisoformat(raw.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return raw[:10]


def tickets_path(root=None) -> pathlib.Path:
    env = os.environ.get("BOT_TICKETS_FILE")
    if env:
        return pathlib.Path(env)
    return pathlib.Path(root or ROOT) / "runtime" / "attention" / "tickets.jsonl"


def focus_path(root=None) -> pathlib.Path:
    env = os.environ.get("BOT_FOCUS_FILE")
    if env:
        return pathlib.Path(env)
    # 与票账本同目录：测试用 BOT_TICKETS_FILE 隔离时 focus 自动隔离。
    return tickets_path(root).parent / "focus.json"


def _read(path: pathlib.Path) -> list[dict]:
    if not path.is_file():
        return []
    rows = []
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            try:
                rec = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(rec, dict):
                rows.append(rec)
    return rows


def _append(record: dict, root=None) -> dict:
    path = tickets_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        os.write(fd, line.encode("utf-8"))
    finally:
        os.close(fd)
    return record


def _parse_deadline(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        raise TicketError("截止时间必填（ISO 日期或时间）")
    try:
        if len(raw) == 10:
            _dt.date.fromisoformat(raw)
        else:
            _dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise TicketError(f"截止时间不是 ISO 日期/时间：{raw!r}") from exc
    return raw


def _clean_options(options) -> list[str]:
    if not isinstance(options, (list, tuple)):
        raise TicketError("options 必须是数组")
    cleaned = []
    for opt in options:
        text = str(opt or "").strip()
        if text and text not in cleaned:
            cleaned.append(text)
    if len(cleaned) < 2:
        raise TicketError("至少 2 个选项")
    if len(cleaned) > MAX_OPTIONS:
        raise TicketError(f"最多 {MAX_OPTIONS} 个选项")
    return cleaned


def _require_status(ticket: dict, allowed, action: str) -> str:
    status = str(ticket.get("status") or "")
    if status in allowed:
        return status
    hint = "；decided/dropped 是终态，不可再变更" if status in TERMINAL_STATUSES else ""
    raise TicketConflict(
        f"票 {ticket.get('id')} 状态是 {status}，不允许{action}"
        f"（允许：{'/'.join(allowed)}）{hint}")


def open_ticket(question: str, options, recommendation: str, deadline: str,
                *, work_item_id: str = "", cost_min: int = 0,
                actor: str = "owner", agent_id: str = "", one_way: bool = False,
                reason: str = "", now: str | None = None, root=None) -> dict:
    """开一张票；缺字段直接拒绝，不写入账本。

    每日配额或 focus 命中时返回 status=suppressed 的草稿并写
    attention_ticket_suppressed（不静默丢弃）；one_way=True 可穿透。
    """
    q = str(question or "").strip()
    if not q:
        raise TicketError("question 必填")
    if q.count("?") + q.count("？") > 1:
        raise TicketError("一张票只能问一个问题；多个问题请拆票")
    opts = _clean_options(options)
    rec = str(recommendation or "").strip()
    if not rec:
        raise TicketError("recommendation 必填")
    if rec not in opts:
        raise TicketError("recommendation 必须是 options 之一")
    due = _parse_deadline(deadline)
    try:
        cost = max(0, int(cost_min or 0))
    except (TypeError, ValueError) as exc:
        raise TicketError("cost_min 必须是整数") from exc
    ts = _stamp(now)
    agent = str(agent_id or actor or "owner").strip() or "owner"
    ticket = {
        "id": "AT-" + _dt.datetime.now().strftime("%Y%m%d") + "-" + uuid.uuid4().hex[:6],
        "question": q,
        "options": opts,
        "recommendation": rec,
        "deadline": due,
        "work_item_id": str(work_item_id or "").strip(),
        "cost_min": cost,
        "status": "open",
        "one_way": bool(one_way),
        "reason": str(reason or "").strip(),
        "created_at": ts,
        "created_by": agent,
    }
    if not ticket["one_way"]:
        usage = daily_usage(agent, root=root, date=_date_of(ts))
        if usage["used"] >= DAILY_INTERRUPT_LIMIT:
            return _suppress(ticket, "daily_quota", usage=usage,
                             focus=focus_state(root=root, now=ts), root=root)
        focus = focus_state(root=root, now=ts)
        if focus.get("active"):
            return _suppress(ticket, "focus_active", usage=usage,
                             focus=focus, root=root)
    _append({"event": "attention_ticket_opened", "ts": ts, "ticket": ticket}, root=root)
    return ticket


def _suppress(ticket: dict, reason: str, *, usage: dict, focus: dict, root=None) -> dict:
    """写 suppressed 记录并返回降级草稿；票不进 open 队列，但可在摘要里追溯。"""
    draft = dict(ticket)
    draft["status"] = "suppressed"
    draft["suppressed"] = True
    draft["suppress_reason"] = reason
    if reason == "focus_active":
        detail = f"focus 进行中：{focus.get('item') or '未命名'}（至 {focus.get('ends_at') or ''}）"
    else:
        detail = (f"每日打断上限 {DAILY_INTERRUPT_LIMIT} 已用尽"
                  f"（{usage.get('used', 0)}/{DAILY_INTERRUPT_LIMIT}）")
    draft["suppress_detail"] = detail
    payload = {
        "agent_id": ticket.get("created_by"),
        "date": _date_of(ticket.get("created_at")),
        "reason": reason,
        "limit": DAILY_INTERRUPT_LIMIT,
        "used": usage.get("used", 0),
        "detail": detail,
        "focus": focus.get("item") if focus.get("active") else "",
        "ticket": draft,
    }
    _append({"event": "attention_ticket_suppressed", "ts": ticket.get("created_at"),
             "agent_id": ticket.get("created_by"), "payload": payload}, root=root)
    return draft


def daily_usage(agent_id: str = "", *, root=None, date: str | None = None,
                now: str | None = None) -> dict:
    """按 agent_id + 本地日期统计配额：只计成功开的普通票（one_way 不计）。"""
    agent = str(agent_id or "")
    day = str(date or _date_of(now or now_iso()))
    used = suppressed = one_way = 0
    for rec in _read(tickets_path(root)):
        event = rec.get("event")
        if _date_of(rec.get("ts")) != day:
            continue
        if event == "attention_ticket_opened":
            ticket = rec.get("ticket") if isinstance(rec.get("ticket"), dict) else {}
            if str(ticket.get("created_by") or "") != agent:
                continue
            if ticket.get("one_way"):
                one_way += 1
            else:
                used += 1
        elif event == "attention_ticket_suppressed":
            payload = rec.get("payload") if isinstance(rec.get("payload"), dict) else {}
            if str(payload.get("agent_id") or "") != agent:
                continue
            suppressed += 1
    return {
        "agent_id": agent,
        "date": day,
        "limit": DAILY_INTERRUPT_LIMIT,
        "used": used,
        "remaining": max(0, DAILY_INTERRUPT_LIMIT - used),
        "suppressed": suppressed,
        "one_way": one_way,
    }


def list_suppressed(*, root=None, date: str | None = None,
                    agent_id: str = "") -> list[dict]:
    """当日/指定 agent 被抑制的票（含原因），供 /today 摘要与审计。"""
    day = str(date or "")
    agent = str(agent_id or "")
    rows = []
    for rec in _read(tickets_path(root)):
        if rec.get("event") != "attention_ticket_suppressed":
            continue
        payload = rec.get("payload") if isinstance(rec.get("payload"), dict) else {}
        if day and _date_of(rec.get("ts")) != day:
            continue
        if agent and str(payload.get("agent_id") or "") != agent:
            continue
        row = dict(payload)
        row["ts"] = rec.get("ts")
        rows.append(row)
    rows.sort(key=lambda r: str(r.get("ts") or ""), reverse=True)
    return rows


def list_tickets(*, root=None, status: str | None = None) -> list[dict]:
    """折叠事件为当前票视图；后写事件覆盖先写事件。"""
    state: dict[str, dict] = {}
    for rec in _read(tickets_path(root)):
        event = rec.get("event")
        ticket = rec.get("ticket") if isinstance(rec.get("ticket"), dict) else None
        if event == "attention_ticket_opened" and ticket and ticket.get("id"):
            state[str(ticket["id"])] = dict(ticket)
            continue
        tid = str(rec.get("ticket_id") or "")
        if not tid or tid not in state:
            continue
        if event == "attention_ticket_decided":
            state[tid]["status"] = "decided"
            state[tid]["choice"] = rec.get("choice")
            state[tid]["note"] = rec.get("note") or ""
            state[tid]["decided_at"] = rec.get("ts")
        elif event == "attention_ticket_deferred":
            state[tid]["status"] = "deferred"
            state[tid]["defer_until"] = rec.get("until")
            state[tid]["deferred_at"] = rec.get("ts")
        elif event == "attention_ticket_snoozed":
            state[tid]["status"] = "snoozed"
            state[tid]["snooze_until"] = rec.get("until")
            state[tid]["snoozed_at"] = rec.get("ts")
        elif event == "attention_ticket_resumed":
            state[tid]["status"] = "open"
            state[tid]["resumed_at"] = rec.get("ts")
            state[tid]["resumed_by"] = rec.get("actor") or ""
        elif event == "attention_ticket_dropped":
            state[tid]["status"] = "dropped"
            state[tid]["drop_reason"] = rec.get("reason") or ""
            state[tid]["dropped_at"] = rec.get("ts")
    rows = list(state.values())
    if status:
        rows = [r for r in rows if r.get("status") == status]
    return sorted(rows, key=lambda r: str(r.get("created_at") or ""), reverse=True)


def get_ticket(ticket_id: str, *, root=None) -> dict | None:
    tid = str(ticket_id or "").strip()
    for ticket in list_tickets(root=root):
        if ticket.get("id") == tid:
            return ticket
    return None


def _ticket_or_error(ticket_id: str, root=None) -> dict:
    ticket = get_ticket(ticket_id, root=root)
    if not ticket:
        raise TicketError(f"找不到票：{ticket_id}")
    return ticket


def decide(ticket_id: str, choice: str, note: str = "", *, root=None,
           actor: str = "owner", now: str | None = None) -> dict:
    """裁决；deferred/snoozed 也允许（等价显式复活后裁决）。"""
    ticket = _ticket_or_error(ticket_id, root=root)
    previous = _require_status(ticket, DECIDABLE_STATUSES, "裁决")
    picked = str(choice or "").strip()
    if picked not in ticket.get("options", []):
        raise TicketError("choice 必须是这张票的选项之一")
    _append({"event": "attention_ticket_decided", "ts": _stamp(now),
             "ticket_id": ticket["id"], "choice": picked,
             "note": str(note or ""), "actor": str(actor or "owner"),
             "from_status": previous,
             "resurrected": previous in RESUMABLE_STATUSES}, root=root)
    return get_ticket(ticket_id, root=root) or ticket


def defer(ticket_id: str, until: str, *, root=None, actor: str = "owner",
          now: str | None = None) -> dict:
    """推迟到指定时间；deferred 可再次 defer（更新截止）。"""
    ticket = _ticket_or_error(ticket_id, root=root)
    previous = _require_status(ticket, DEFERRABLE_STATUSES, "推迟")
    due = _parse_deadline(until)
    _append({"event": "attention_ticket_deferred", "ts": _stamp(now),
             "ticket_id": ticket["id"], "until": due,
             "actor": str(actor or "owner"), "from_status": previous}, root=root)
    return get_ticket(ticket_id, root=root) or ticket


def snooze(ticket_id: str, until: str = "", *, root=None, actor: str = "owner",
           now: str | None = None) -> dict:
    """稍后处理（snoozed）；与 deferred 分开，便于今日视图分类。"""
    ticket = _ticket_or_error(ticket_id, root=root)
    previous = _require_status(ticket, SNOOZABLE_STATUSES, "稍后处理")
    ts = _stamp(now)
    if str(until or "").strip():
        due = _parse_deadline(until)
    else:
        base = _dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
        due = (base + _dt.timedelta(hours=1)).isoformat(timespec="seconds")
    _append({"event": "attention_ticket_snoozed", "ts": ts,
             "ticket_id": ticket["id"], "until": due,
             "actor": str(actor or "owner"), "from_status": previous}, root=root)
    return get_ticket(ticket_id, root=root) or ticket


def resume(ticket_id: str, *, root=None, actor: str = "owner",
           now: str | None = None) -> dict:
    """把 deferred/snoozed 拉回 open；open/decided/dropped 明确报 409。"""
    ticket = _ticket_or_error(ticket_id, root=root)
    previous = _require_status(ticket, RESUMABLE_STATUSES, "恢复")
    _append({"event": "attention_ticket_resumed", "ts": _stamp(now),
             "ticket_id": ticket["id"], "actor": str(actor or "owner"),
             "from_status": previous}, root=root)
    return get_ticket(ticket_id, root=root) or ticket


def drop(ticket_id: str, reason: str = "", *, root=None, actor: str = "owner",
         now: str | None = None) -> dict:
    """作废票（dropped，终态）；已裁决/已作废的票返回 409。"""
    ticket = _ticket_or_error(ticket_id, root=root)
    _require_status(ticket, DROPPABLE_STATUSES, "作废")
    _append({"event": "attention_ticket_dropped", "ts": _stamp(now),
             "ticket_id": ticket["id"], "reason": str(reason or ""),
             "actor": str(actor or "owner")}, root=root)
    return get_ticket(ticket_id, root=root) or ticket


# ---------------------------------------------------------------- focus（P0-3 最小实现）

def _read_focus(root=None) -> dict:
    path = focus_path(root)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8") or "{}")
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_focus(state: dict, root=None) -> dict:
    path = focus_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(str(tmp), str(path))
    return state


def _parse_dt(value):
    try:
        dt = _dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_dt.datetime.now().astimezone().tzinfo)
    return dt


def focus_state(*, root=None, now: str | None = None) -> dict:
    """当前 focus；过期如实返回 active=False（不静默续期）。"""
    state = _read_focus(root)
    if not state.get("active"):
        return {"active": False, "item": "", "ends_at": "", "remaining_sec": 0}
    now_dt = _parse_dt(_stamp(now))
    end_dt = _parse_dt(state.get("ends_at"))
    if not now_dt or not end_dt or end_dt <= now_dt:
        out = dict(state)
        out.update({"active": False, "expired": True, "remaining_sec": 0})
        return out
    out = dict(state)
    out.update({"active": True, "expired": False,
                "remaining_sec": max(0, int((end_dt - now_dt).total_seconds()))})
    return out


def start_focus(item: str, minutes: int = DEFAULT_FOCUS_MIN, *,
                actor: str = "owner", override: bool = False,
                root=None, now: str | None = None) -> dict:
    """开始/延长 focus；换 item 必须 override=True（否则 409）。"""
    target = str(item or "").strip()
    if not target:
        raise TicketError("focus 必须指定一个事项（item）")
    try:
        span = int(minutes or DEFAULT_FOCUS_MIN)
    except (TypeError, ValueError) as exc:
        raise TicketError("minutes 必须是整数") from exc
    span = max(1, min(MAX_FOCUS_MIN, span))
    ts = _stamp(now)
    now_dt = _parse_dt(ts)
    current = focus_state(root=root, now=ts)
    if (current.get("active") and current.get("item")
            and str(current["item"]) != target and not override):
        raise TicketConflict(
            f"focus 已在进行：{current['item']}（至 {current.get('ends_at')}）；"
            f"切换需显式 override=true")
    ends = (now_dt + _dt.timedelta(minutes=span)).isoformat(timespec="seconds")
    state = {
        "active": True,
        "item": target,
        "minutes": span,
        "started_at": ts,
        "ends_at": ends,
        "actor": str(actor or "owner"),
        "overridden": bool(override and current.get("active")),
        "previous_item": str(current.get("item") or ""),
    }
    _write_focus(state, root=root)
    _append({"event": "attention_focus_started", "ts": ts,
             "payload": {"item": target, "minutes": span,
                         "override": bool(override),
                         "previous_item": state["previous_item"],
                         "actor": state["actor"]}}, root=root)
    return focus_state(root=root, now=ts)


def end_focus(*, actor: str = "owner", root=None,
              now: str | None = None) -> dict:
    """结束 focus；本来没在 focus 就返回 ended=False（幂等，不报错）。"""
    ts = _stamp(now)
    current = focus_state(root=root, now=ts)
    if not current.get("active"):
        return {"active": False, "ended": False, "item": str(current.get("item") or "")}
    state = dict(_read_focus(root))
    state["active"] = False
    state["ended_at"] = ts
    state["ended_by"] = str(actor or "owner")
    _write_focus(state, root=root)
    _append({"event": "attention_focus_ended", "ts": ts,
             "payload": {"item": state.get("item") or "",
                         "actor": str(actor or "owner")}}, root=root)
    return {"active": False, "ended": True, "item": state.get("item") or ""}
