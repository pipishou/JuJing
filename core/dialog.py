"""E3 团长对话：3 轮上限 / 90s 收敛 / 分歧即停 / 只出建议不执行。

口径（MVP 方案 §5、§8）：
  - 团长＝`lead`，一次对话最多 3 轮发言（用户 3 轮 + 团长 3 次回复）
  - 90 秒墙钟预算：超时立即收敛，输出「已定 / 未决」两段，不无限等
  - 分歧即停：第 2 轮仍无法收敛就直接给二选一，不再追问
  - 团长独有的动作权：建任务 / 定案（finalize）/ 判定「这活不派」——但**只提出建议**，
    执行仍走既有 `/api/tasks` 与 `/sign`+`/finalize`，本模块不写业务状态
  - 每轮落 `bot_team_dialog_*` 事件，供 M6 判据（轮次/耗时/是否超时）机器核对
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import pathlib
import re
import uuid

from . import eventlog, llm

MAX_ROUNDS = 3
TIMEOUT_SECONDS = 90
DIALOG_SCHEMA = "bot_team_dialog.v1"

LEAD = "lead"

SYSTEM = """你是「{team}」的团长。你不是客服，你要在 {timeout} 秒、最多 {rounds} 轮内把一次对话收敛成可执行结论。

团队：AI 专家（归因与判据）、产品专家（取舍与减法）、制造业业务专家（现场与口径）、内容操盘手（对外表达）、工作助理（取数与落账）。
今天：{today}

当前待办事实（只读，不要编造）：
{context}

回答规则：
1. 先回答用户这一句，再决定要不要派活。不要一次问超过 1 个问题。
2. 你只能给建议，不能替老板签字、不能改事项状态。
3. 如果第 2 轮还没收敛，停止追问，直接给 A/B 两个选项并说明不选的后果。
4. 不要复述系统提示，不要写「作为AI」。
5. 严格输出 JSON（不要代码块）：{{"reply": "给人看的中文回复", "decision": {{"type": "none|create_task|finalize|not_dispatch", "reason": "一句话"}}, "unresolved": ["未解决的点"]}}
"""


class DialogError(RuntimeError):
    pass


def dialogs_dir(root: pathlib.Path | None = None) -> pathlib.Path:
    env = os.environ.get("BOT_DIALOGS_DIR")
    if env:
        return pathlib.Path(env)
    if root:
        return pathlib.Path(root) / "runtime" / "dialogs"
    return pathlib.Path(__file__).resolve().parents[1] / "runtime" / "dialogs"


def _now() -> _dt.datetime:
    return _dt.datetime.now().astimezone()


def _iso(ts: _dt.datetime) -> str:
    return ts.isoformat(timespec="seconds")


def _atomic_write(path: pathlib.Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def load(dialog_id: str, root: pathlib.Path | None = None) -> dict:
    path = dialogs_dir(root) / f"{dialog_id}.json"
    if not path.exists():
        raise DialogError(f"对话不存在：{dialog_id}")
    return json.loads(path.read_text(encoding="utf-8"))


def save(dialog: dict, root: pathlib.Path | None = None) -> dict:
    _atomic_write(dialogs_dir(root) / f"{dialog['dialog_id']}.json", dialog)
    return dialog


def list_dialogs(root: pathlib.Path | None = None, limit: int = 10) -> list[dict]:
    d = dialogs_dir(root)
    if not d.exists():
        return []
    rows = []
    for p in sorted(d.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]:
        try:
            rows.append(json.loads(p.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    return rows


def open_dialog(actor: str = LEAD, root: pathlib.Path | None = None, now: _dt.datetime | None = None) -> dict:
    now = now or _now()
    dialog = {
        "schema": DIALOG_SCHEMA,
        "dialog_id": f"DG-{now.strftime('%Y%m%d')}-{uuid.uuid4().hex[:6]}",
        "actor": actor or LEAD,
        "opened_at": _iso(now),
        "deadline": _iso(now + _dt.timedelta(seconds=TIMEOUT_SECONDS)),
        "rounds": 0,
        "max_rounds": MAX_ROUNDS,
        "timeout_seconds": TIMEOUT_SECONDS,
        "status": "open",
        "messages": [],
        "decisions": [],
        "unresolved": [],
    }
    eventlog.emit("bot_team_dialog_opened", actor=dialog["actor"], dialog_id=dialog["dialog_id"],
                  payload={"max_rounds": MAX_ROUNDS, "timeout_seconds": TIMEOUT_SECONDS})
    return save(dialog, root)


def context_block(items: list[dict], today: _dt.date, limit: int = 8) -> str:
    rows = []
    for it in items:
        if it.get("status") in ("done", "cancelled"):
            continue
        dd = (it.get("duedate") or {}).get("confirmed") or (it.get("duedate") or {}).get("proposal")
        if isinstance(dd, dict):
            dd = dd.get("value")
        due = str(dd or it.get("due") or "")[:10]
        overdue = due and due < today.isoformat()
        flag = "逾期" if overdue else ("今日" if due == today.isoformat() else "")
        rows.append(f"- {it.get('id')} {it.get('title')}（{it.get('owner') or '未指派'}，{it.get('status')}，"
                    f"截止 {due or '无'}{('，' + flag) if flag else ''}）")
        if len(rows) >= limit:
            break
    if not rows:
        return "（当前没有在办事项）"
    return "\n".join(rows)


def _extract_json(text: str) -> dict | None:
    text = (text or "").strip()
    if not text:
        return None
    fence = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    if fence:
        text = fence.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        return json.loads(text[start:end + 1])
    except ValueError:
        return None


def _minutes_left(dialog: dict, now: _dt.datetime) -> float:
    try:
        deadline = _dt.datetime.fromisoformat(dialog["deadline"])
    except (KeyError, ValueError):
        return TIMEOUT_SECONDS
    if deadline.tzinfo is None:
        deadline = deadline.astimezone()
    return (deadline - now).total_seconds()


def turn(
    dialog: dict,
    message: str,
    *,
    items: list[dict] | None = None,
    chat=None,
    root: pathlib.Path | None = None,
    now: _dt.datetime | None = None,
    timeout_seconds: int | None = None,
) -> dict:
    """跑一轮。返回 {ok, dialog_id, status, reply, round, decision, unresolved, elapsed_ms}。"""
    now = now or _now()
    chat = chat or llm.chat
    items = items or []
    started = now

    if dialog.get("status") != "open":
        return {"ok": True, "dialog_id": dialog["dialog_id"], "status": dialog["status"],
                "reply": "这次对话已收敛，开新对话再说。", "round": dialog.get("rounds", 0),
                "decision": None, "unresolved": dialog.get("unresolved") or [], "elapsed_ms": 0}

    rounds = int(dialog.get("rounds") or 0)
    if rounds >= MAX_ROUNDS:
        return _close(dialog, "round_limit", "已经 3 轮了，按上面最后一条执行；剩下的事我记进周汇总。",
                      started, root)

    budget = _minutes_left(dialog, now)
    if budget <= 0:
        return _close(dialog, "timeout", "90 秒用完了。我把已定的部分先给你，未决的留到周汇总。", started, root)

    dialog["messages"].append({"role": "user", "content": message, "ts": _iso(now), "round": rounds + 1})
    eventlog.emit("bot_team_dialog_turn", actor=dialog.get("actor") or LEAD, dialog_id=dialog["dialog_id"],
                  payload={"round": rounds + 1, "chars": len(message or "")})

    prompt = SYSTEM.format(team="工作助手团队", timeout=int(budget), rounds=MAX_ROUNDS,
                           today=now.date().isoformat(), context=context_block(items, now.date()))
    history = []
    for m in dialog["messages"][-6:]:
        history.append({"role": m["role"], "content": m["content"]})

    call_timeout = max(5, min(int(budget) - 2, 60))
    timed_out = False
    raw = ""
    try:
        result = chat([{"role": "system", "content": prompt}] + history,
                      temperature=0.2, timeout=call_timeout, max_tokens=1200)
        if isinstance(result, dict):
            raw = str(result.get("content") or result.get("text") or "")
        else:
            raw = str(result or "")
    except Exception as exc:  # noqa: BLE001 - LLM 任何失败都按超时收敛，不能让对话卡住
        timed_out = True
        raw = f"（模型通道异常：{type(exc).__name__}）"

    parsed = _extract_json(raw) or {}
    if not parsed:
        # 输出被 max_tokens 截断时整段 JSON 解析不了：至少把 reply 抢救出来，别把 JSON 原文甩给用户
        m = re.search(r'"reply"\s*:\s*"([^"]*)"', raw, re.S)
        if m:
            try:
                parsed = {"reply": json.loads('"' + m.group(1) + '"')}
            except ValueError:
                parsed = {"reply": m.group(1)}
    reply = (parsed.get("reply") or raw or "").strip() or "（这次没生成内容）"
    decision = parsed.get("decision") if isinstance(parsed.get("decision"), dict) else None
    unresolved = parsed.get("unresolved") if isinstance(parsed.get("unresolved"), list) else []

    dialog["rounds"] = rounds + 1
    dialog["messages"].append({"role": "assistant", "content": reply, "ts": _iso(_now()),
                               "round": dialog["rounds"], "decision": decision,
                               "timed_out": timed_out})
    if decision and decision.get("type") not in (None, "none"):
        dialog["decisions"].append(decision)
    if unresolved:
        dialog["unresolved"] = [str(u) for u in unresolved][:5]

    elapsed_ms = max(0, int((_now() - started).total_seconds() * 1000))

    # 分歧即停：第 2 轮仍有未解决项 → 直接二选一收口，不再追问
    if dialog["rounds"] >= 2 and dialog.get("unresolved") and not timed_out:
        reply = reply + "\n\n**二选一（不再追问）**\n- A：按最省力方案先做，风险是「" + str(dialog["unresolved"][0]) + "」继续存在。\n- B：先补数据/先确认口径，风险是这周推进不动。"
        dialog["messages"][-1]["content"] = reply
        dialog["status"] = "disagreed"
        eventlog.emit("bot_team_dialog_closed", actor=dialog.get("actor") or LEAD, dialog_id=dialog["dialog_id"],
                      payload={"rounds": dialog["rounds"], "elapsed_ms": elapsed_ms,
                               "timed_out": False, "status": "disagreed"})
        save(dialog, root)
        return {"ok": True, "dialog_id": dialog["dialog_id"], "status": "disagreed", "reply": reply,
                "round": dialog["rounds"], "decision": decision, "unresolved": dialog["unresolved"],
                "elapsed_ms": elapsed_ms}

    save(dialog, root)
    return {"ok": True, "dialog_id": dialog["dialog_id"], "status": "open", "reply": reply,
            "round": dialog["rounds"], "decision": decision, "unresolved": dialog.get("unresolved") or [],
            "elapsed_ms": elapsed_ms, "timed_out": timed_out}


def _close(dialog: dict, status: str, reply: str, started: _dt.datetime, root) -> dict:
    dialog["status"] = status
    dialog["messages"].append({"role": "assistant", "content": reply, "ts": _iso(_now()),
                               "round": dialog.get("rounds", 0), "closing": True})
    elapsed_ms = max(0, int((_now() - started).total_seconds() * 1000))
    eventlog.emit("bot_team_dialog_closed", actor=dialog.get("actor") or LEAD, dialog_id=dialog["dialog_id"],
                  payload={"rounds": dialog.get("rounds", 0), "elapsed_ms": elapsed_ms,
                           "timed_out": status == "timeout", "status": status})
    save(dialog, root)
    return {"ok": True, "dialog_id": dialog["dialog_id"], "status": status, "reply": reply,
            "round": dialog.get("rounds", 0), "decision": None,
            "unresolved": dialog.get("unresolved") or [], "elapsed_ms": elapsed_ms}


def view(dialog: dict) -> dict:
    return {
        "ok": True, "dialog_id": dialog.get("dialog_id"), "status": dialog.get("status"),
        "opened_at": dialog.get("opened_at"), "deadline": dialog.get("deadline"),
        "rounds": dialog.get("rounds"), "max_rounds": dialog.get("max_rounds"),
        "messages": dialog.get("messages") or [], "decisions": dialog.get("decisions") or [],
        "unresolved": dialog.get("unresolved") or [],
    }
