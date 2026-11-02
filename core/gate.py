# -*- coding: utf-8 -*-
"""securityGate 人工确认（M1.5 §2.3）：gate.json 的读写与报告回写。

用途：不可逆动作（停线/换模…）与 security_gate_requested 的报告，必须有人工确认
痕迹才能走 /finalize。本模块是唯一写 gate.json 的地方：

  read(tdir)                                   -> dict | None
  confirm(tdir, confirmer, comment="", report=None) -> dict   （status=confirmed）
  reject(tdir, confirmer, comment="")           -> dict        （status=rejected）

约定：
  - gate.json 与报告同目录（runs/<task_id>/gate.json），字段：
    {task_id, status, confirmed_by, confirmed_at, comment, requested_at, updated_at,
     history: [ {action, status, confirmed_by, confirmed_at, comment}, ... ]}   ← append-only
  - confirm 同时把 report["security_gate"]（及 decision_log_entry.security_gate）
    回写为 {status:"confirmed", confirmed_by, confirmed_at, comment}，原子落盘。
  - 每次 confirm/reject 追加一条 run.log 事件（phase="gate", kind="status"）。
  - confirmed_by 为空 → GateError（人工确认必须可追溯，M1 遗留硬化）。
只依赖 stdlib。
"""
import hashlib
import json
from datetime import datetime
from pathlib import Path

from . import events, fsutil
from .gates import REVIEW_FILE, GateError

GATE_FILE = "gate.json"
GATE_STATUSES = ("pending_user_confirmation", "confirmed", "rejected")

_REPORT_NAME = "report.json"


def now_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def gate_path(tdir):
    """gate.json 路径。"""
    return Path(tdir) / GATE_FILE


def read(tdir):
    """读 gate.json：不存在 / 坏 JSON / 非对象 → None（不抛）。"""
    return read_file(gate_path(tdir))


def read_file(path):
    """按显式路径读 gate.json 形状的文件；不存在 / 坏 JSON / 非对象 → None。"""
    p = Path(path)
    if not p.is_file():
        return None
    try:
        doc = fsutil.read_json(p)
    except Exception:  # noqa: BLE001 坏文件按"没有确认"处理，绝不放行
        return None
    return doc if isinstance(doc, dict) and doc else None


def _require_confirmer(confirmer, action):
    text = "" if confirmer is None else str(confirmer).strip()
    if not text:
        raise GateError(f"securityGate {action} 需要 confirmed_by（确认人）非空：人工确认必须可追溯")
    return text


def _event(tdir, doc):
    line = f"securityGate {doc.get('status')} by {doc.get('confirmed_by')}"
    comment = str(doc.get("comment") or "").strip()
    if comment:
        line += f"：{comment[:200]}"
    try:
        events.append_event(str(Path(tdir) / "run.log"), {
            "phase": "gate",
            "role": "host",
            "kind": "status",
            "line": line,
            "meta": {"status": doc.get("status"), "confirmed_by": doc.get("confirmed_by"),
                     "gate_file": str(gate_path(tdir)),
                     **({"review_invalidated": True} if doc.get("review_invalidated") else {})},
        })
    except Exception:  # noqa: BLE001 事件失败不影响 gate 本身（gate.json 已原子落盘）
        pass


def _report_target(tdir, report):
    """确认后报告回写目标：显式 _report_path > tdir/report.json（存在才写）。"""
    for key in ("_report_path", "report_path"):
        value = (report or {}).get(key)
        if value:
            return Path(str(value))
    target = Path(tdir) / _REPORT_NAME
    if target.is_file():
        return target
    return None


def _sha256_file(path):
    """文件 sha256；文件不存在/不可读 → 空串（当作「没有旧字节」）。"""
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return ""


def _review_report_sha(tdir):
    """review.json 钉住的报告 sha；无复核/坏文件 → None（不 fail-open）。"""
    try:
        doc = json.loads((Path(tdir) / REVIEW_FILE).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    if not isinstance(doc, dict):
        return None
    sha = str(doc.get("report_sha256") or "").strip()
    return sha or None


def _apply_to_report(tdir, report, doc):
    """把确认结果写进 report 字典并原子落盘（报告路径不存在则仅更新内存字典）。"""
    gate_view = {
        "status": "confirmed",
        "confirmed_by": doc.get("confirmed_by"),
        "confirmed_at": doc.get("confirmed_at"),
        "comment": doc.get("comment") or "",
    }
    # 报告里声明的「要执行的动作」**不被确认覆盖**：确认只改状态与人，不改动作。
    # 否则 F28 两侧同源（都等于确认值），只能抓手改 gate.json，抓不到「确认 A、执行 B」。
    # 人工确认的动作存 confirmed_action，供对账与审计追溯。
    _declared = ""
    _top_report = report.get("security_gate")
    if isinstance(_top_report, dict):
        _declared = str(_top_report.get("action") or "").strip()
    _confirmed = str(doc.get("action") or "").strip()
    if _confirmed:
        gate_view["confirmed_action"] = _confirmed
    if _declared:
        gate_view["action"] = _declared
    requested_at = None
    old_top = report.get("security_gate")
    if isinstance(old_top, dict):
        requested_at = old_top.get("requested_at")
    dl = report.get("decision_log_entry")
    if isinstance(dl, dict):
        old_dl = dl.get("security_gate")
        if isinstance(old_dl, dict) and old_dl.get("requested_at"):
            requested_at = old_dl.get("requested_at")
    if requested_at:
        gate_view["requested_at"] = requested_at
    report["security_gate"] = dict(gate_view)
    if isinstance(dl, dict):
        dl["security_gate"] = dict(gate_view)
    target = _report_target(tdir, report)
    if target is not None:
        fsutil.atomic_write_json(target, report)
    return report


def _write_gate(tdir, old, entry, status, confirmer, comment, task_id, requested_at):
    now = entry["confirmed_at"]
    history = list(old.get("history") or [])
    history.append(entry)
    doc = {
        "task_id": task_id,
        "status": status,
        "confirmed_by": confirmer,
        "confirmed_at": now,
        "comment": str(comment or ""),
        "requested_at": requested_at,
        "updated_at": now,
        # action＝人工确认的动作；requested_action＝报告当时请求的动作（两者不同即对账失败）
        "action": (entry.get("gate_action") or old.get("action") or ""),
        "requested_action": (entry.get("requested_action") or old.get("requested_action") or ""),
        "history": history,
    }
    fsutil.atomic_write_json(gate_path(tdir), doc)
    return doc


def confirm(tdir, confirmer, comment="", report=None, action=""):
    """人工确认 securityGate：写 gate.json + 回写报告 + 追加事件；返回 gate.json dict。

    action：**人工当场拍的那个动作**。只有显式传入才用，否则取报告声明的动作。
    语义要点（2026-11-02 审查）：若这里总是从报告抄 action，F28 就退化成
    「自己和自己比」，只能抓「确认后手改 gate.json」，抓不到「确认 A、执行 B」。
    """

    who = _require_confirmer(confirmer, "confirm")
    tdir = Path(tdir)
    tdir.mkdir(parents=True, exist_ok=True)
    old = read(tdir) or {}
    report = report if isinstance(report, dict) else {}
    task_id = str(report.get("task_id") or old.get("task_id") or tdir.name)
    requested_at = old.get("requested_at")
    if not requested_at:
        top = report.get("security_gate")
        if isinstance(top, dict):
            requested_at = top.get("requested_at")
    now = now_iso()
    _declared_action = ""
    if isinstance(report, dict):
        _t = report.get("security_gate")
        if isinstance(_t, dict):
            _declared_action = str(_t.get("action") or "").strip()
    gate_action = str(action or "").strip() or _declared_action
    if not gate_action and isinstance(report, dict):
        _top = report.get("security_gate")
        if isinstance(_top, dict):
            gate_action = str(_top.get("action") or "")
    entry = {"action": "confirm", "status": "confirmed", "confirmed_by": who,
             "confirmed_at": now, "comment": str(comment or ""),
             "gate_action": gate_action,
             "requested_action": _declared_action}
    doc = _write_gate(tdir, old, entry, "confirmed", who, comment, task_id, requested_at)
    # 复核（review.v1）钉的是报告 sha256；confirm 会回写 report.json 字节。
    # 回写一旦改变字节，旧复核必然失效——必须在这里显式回显，不能等到 /sign 才 409。
    target = _report_target(tdir, report)
    pre_sha = _sha256_file(target) if target is not None else ""
    review_sha = _review_report_sha(tdir)
    if report:
        _apply_to_report(tdir, report, doc)
    post_sha = _sha256_file(target) if target is not None else ""
    review_invalidated = bool(
        review_sha and target is not None
        and (review_sha != pre_sha or pre_sha != post_sha))
    out = dict(doc)
    out["review_invalidated"] = review_invalidated
    _event(tdir, out)
    return out


def reject(tdir, confirmer, comment=""):
    """人工驳回 securityGate：写 gate.json + 事件；报告不回写（/finalize 必拒）。"""
    who = _require_confirmer(confirmer, "reject")
    tdir = Path(tdir)
    tdir.mkdir(parents=True, exist_ok=True)
    old = read(tdir) or {}
    now = now_iso()
    entry = {"action": "reject", "status": "rejected", "confirmed_by": who,
             "confirmed_at": now, "comment": str(comment or "")}
    doc = _write_gate(tdir, old, entry, "rejected", who, comment,
                      str(old.get("task_id") or tdir.name), old.get("requested_at"))
    _event(tdir, doc)
    return doc


if __name__ == "__main__":
    import json
    import sys
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        tdir = Path(tmp) / "T-20261019-GATE-01"
        tdir.mkdir(parents=True)
        report = {"task_id": "T-20261019-GATE-01", "security_gate_requested": True,
                  "security_gate": {"status": "pending_user_confirmation", "requested_at": "2026-10-19"},
                  "decision_log_entry": {"security_gate": {"status": "pending_user_confirmation",
                                                           "requested_at": "2026-10-19"}}}
        (tdir / "report.json").write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
        print("read(before) =", read(tdir))
        doc = confirm(tdir, "老板", "同意停线检修", dict(report))
        print(json.dumps(doc, ensure_ascii=False, indent=2))
        print("gate.json =", json.dumps(read(tdir), ensure_ascii=False)[:120], "…")
        print("report.security_gate =", json.dumps(read(tdir) and json.loads((tdir / "report.json").read_text(encoding="utf-8"))["security_gate"], ensure_ascii=False))
        try:
            confirm(tdir, "  ", "", {})
            print("BUG: 空 confirmed_by 未拒绝")
        except GateError as exc:
            print("空 confirmed_by → GateError:", exc)
        reject(tdir, "老板", "数据口径待补")
        print("after reject:", read(tdir)["status"], "history=", len(read(tdir)["history"]))
        print("events =", json.dumps(events.iter_events(str(tdir / "run.log")), ensure_ascii=False))
    sys.exit(0)
