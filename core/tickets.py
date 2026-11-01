"""注意力票（Slice 2）：一问 + 2–3 选项 + 推荐 + 截止。

票不是聊天消息，而是可判定、可过期的决策单元：
  - 缺 recommendation / options / deadline → 拒绝开票
  - 一个问题只能有一个问号；两个问题必须拆票
  - 只追加 `runtime/attention/tickets.jsonl`，不修改历史

对外：
  open_ticket(...) -> dict
  list_tickets(...) -> list[dict]
  decide(ticket_id, choice, ...) -> dict
  defer(ticket_id, until, ...) -> dict
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


class TicketError(ValueError):
    """票不合法或状态不允许。"""


def now_iso() -> str:
    return _dt.datetime.now().astimezone().isoformat(timespec="seconds")


def tickets_path(root=None) -> pathlib.Path:
    env = os.environ.get("BOT_TICKETS_FILE")
    if env:
        return pathlib.Path(env)
    return pathlib.Path(root or ROOT) / "runtime" / "attention" / "tickets.jsonl"


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


def open_ticket(question: str, options, recommendation: str, deadline: str,
                *, work_item_id: str = "", cost_min: int = 0,
                actor: str = "owner", root=None) -> dict:
    """开一张票；缺字段直接拒绝，不写入账本。"""
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
    ticket = {
        "id": "AT-" + _dt.datetime.now().strftime("%Y%m%d") + "-" + uuid.uuid4().hex[:6],
        "question": q,
        "options": opts,
        "recommendation": rec,
        "deadline": due,
        "work_item_id": str(work_item_id or "").strip(),
        "cost_min": cost,
        "status": "open",
        "created_at": now_iso(),
        "created_by": str(actor or "owner"),
    }
    _append({"event": "attention_ticket_opened", "ts": now_iso(), "ticket": ticket}, root=root)
    return ticket


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


def decide(ticket_id: str, choice: str, note: str = "", *, root=None,
           actor: str = "owner") -> dict:
    ticket = get_ticket(ticket_id, root=root)
    if not ticket:
        raise TicketError(f"找不到票：{ticket_id}")
    if ticket.get("status") != "open":
        raise TicketError(f"票 {ticket_id} 状态是 {ticket.get('status')}，不能重复裁决")
    picked = str(choice or "").strip()
    if picked not in ticket.get("options", []):
        raise TicketError("choice 必须是这张票的选项之一")
    _append({"event": "attention_ticket_decided", "ts": now_iso(),
             "ticket_id": ticket["id"], "choice": picked,
             "note": str(note or ""), "actor": str(actor or "owner")}, root=root)
    return get_ticket(ticket_id, root=root) or ticket


def defer(ticket_id: str, until: str, *, root=None, actor: str = "owner") -> dict:
    ticket = get_ticket(ticket_id, root=root)
    if not ticket:
        raise TicketError(f"找不到票：{ticket_id}")
    if ticket.get("status") != "open":
        raise TicketError(f"票 {ticket_id} 状态是 {ticket.get('status')}，不能推迟")
    due = _parse_deadline(until)
    _append({"event": "attention_ticket_deferred", "ts": now_iso(),
             "ticket_id": ticket["id"], "until": due,
             "actor": str(actor or "owner")}, root=root)
    return get_ticket(ticket_id, root=root) or ticket
