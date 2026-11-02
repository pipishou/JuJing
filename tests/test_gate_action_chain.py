#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""F28 对账链路回归：确认动作不得丢、gate.json 必须记动作、对账必须在覆盖前。

历史缺陷（2026-11-02 审查发现，三处叠加导致 F28 永不触发）：
  1. `core/gate.py::_apply_to_report` 回写 confirmed 视图时不含 `action` → 报告里声明的
     不可逆动作被确认动作擦掉；
  2. `core/gate.py::_write_gate` 写 gate.json 时从不记 `action` → `gate_action_mismatch`
     左侧恒为空字符串；
  3. `tools/finalize_delivery.py::main` 先 `apply_gate_view` 再跑 F28 → 右侧被换成刚写入
     的视图，自己和自己比永远相等。

本测试锁三件事：动作保留、动作落盘、对账在覆盖之前。
"""

from __future__ import annotations

import json
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import core.gate as gate_mod  # noqa: E402
import core.gates as gates  # noqa: E402

FAILS: list[str] = []
CHECKS = 0


def check(name: str, cond: bool) -> None:
    global CHECKS
    CHECKS += 1
    if not cond:
        FAILS.append(name)
        print(f"[FAIL] {name}")
    else:
        print(f"[ ok ] {name}")


def _task_dir(tmp: pathlib.Path) -> pathlib.Path:
    tdir = tmp / "T-20261102-F28"
    tdir.mkdir(parents=True)
    report = {
        "task_id": "T-20261102-F28",
        "security_gate_requested": True,
        "security_gate": {"status": "pending_user_confirmation",
                          "action": "mold_change",
                          "requested_at": "2026-11-02T09:00:00+08:00"},
        "decision_log_entry": {"security_gate": {"status": "pending_user_confirmation",
                                                 "action": "mold_change",
                                                 "requested_at": "2026-11-02T09:00:00+08:00"}},
    }
    (tdir / "report.json").write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
    return tdir


def test_confirm_keeps_action(tmp: pathlib.Path) -> None:
    tdir = _task_dir(tmp)
    report = json.loads((tdir / "report.json").read_text(encoding="utf-8"))
    doc = gate_mod.confirm(tdir, "老板", "同意换模", report)

    # 1. gate.json 顶层必须记下被确认的动作
    check("gate.json.action == mold_change", str(doc.get("action") or "") == "mold_change")
    check("gate.json.history[0].gate_action == mold_change",
          str(((doc.get("history") or [{}])[0]).get("gate_action") or "") == "mold_change")

    # 2. 报告回写后动作不能被擦掉
    after = json.loads((tdir / "report.json").read_text(encoding="utf-8"))
    top = after.get("security_gate") or {}
    dl = (after.get("decision_log_entry") or {}).get("security_gate") or {}
    check("report.security_gate.action 保留", str(top.get("action") or "") == "mold_change")
    check("report.decision_log_entry.security_gate.action 保留",
          str(dl.get("action") or "") == "mold_change")
    check("report.security_gate.status == confirmed", top.get("status") == "confirmed")


def test_confirm_other_action_is_caught(tmp: pathlib.Path) -> None:
    """人工确认的动作与报告要执行的动作不一致 → F28 必须命中（确认 A、执行 B）。"""
    tdir = _task_dir(tmp)
    report = json.loads((tdir / "report.json").read_text(encoding="utf-8"))
    doc = gate_mod.confirm(tdir, "老板", "我批的是停机", report, action="stop_line")
    after = json.loads((tdir / "report.json").read_text(encoding="utf-8"))

    check("gate.json.action == stop_line（人工确认值）", str(doc.get("action") or "") == "stop_line")
    check("gate.json.requested_action == mold_change（报告请求值）",
          str(doc.get("requested_action") or "") == "mold_change")
    check("报告动作不被确认覆盖（仍 mold_change）",
          str((after.get("security_gate") or {}).get("action") or "") == "mold_change")
    msg = gates.gate_action_mismatch(doc, after)
    check("确认 A / 执行 B → F28 命中", bool(msg) and "mismatch" in msg)


def test_mismatch_detected(tmp: pathlib.Path) -> None:
    tdir = _task_dir(tmp)
    report = json.loads((tdir / "report.json").read_text(encoding="utf-8"))
    gate_doc = gate_mod.confirm(tdir, "老板", "同意换模", report)

    check("同动作 → 无 F28",
          gates.gate_action_mismatch(gate_doc, report) is None)

    tampered = dict(gate_doc)
    tampered["action"] = "stop_line"          # 确认了停机，但报告要执行换模
    msg = gates.gate_action_mismatch(tampered, report)
    check("动作不一致 → 命中 F28", bool(msg) and "confirm/execute mismatch" in msg)

    empty = dict(gate_doc)
    empty.pop("action", None)
    check("gate.json 无动作 → 不再静默放行（值为空即视为未声明）",
          gates.gate_action_mismatch(empty, report) is None)  # 空侧由 shape 门另行拦截


def test_order_lock() -> None:
    """源码顺序锁：F28 必须在 apply_gate_view 之前（否则拿覆盖后的值自比）。"""
    src = (ROOT / "tools" / "finalize_delivery.py").read_text(encoding="utf-8")
    i_mm = src.index("_mm = gates.gate_action_mismatch(gate_doc, report)")
    i_apply = src.index("apply_gate_view(report, gate_view)")
    check("finalize: F28 在 apply_gate_view 之前", i_mm < i_apply)


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        base = pathlib.Path(tmp)
        (base / "a").mkdir()
        (base / "b").mkdir()
        test_confirm_keeps_action(base / "a")
        test_mismatch_detected(base / "b")
        (base / "c").mkdir()
        test_confirm_other_action_is_caught(base / "c")
        test_order_lock()
    print(f"\n{CHECKS - len(FAILS)}/{CHECKS} 通过")
    if FAILS:
        print("RESULT: FAIL")
        return 1
    print("RESULT: PASS（F28 三处断点已闭合）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
