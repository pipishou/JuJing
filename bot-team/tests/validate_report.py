#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""报告校验：runs/demo-report.json 是否满足团队产出契约。

判据（任一 FAIL → 非零退出）：
  1. finding：1–2 条，每条 factor/confidence/evidence 齐，evidence 非空
  2. cannot_attribute：非空
  3. scorecard：六维键齐
  4. recommendation：非空，每条 action/roi(cost,avoided,anchor)/irreversibility 齐
  5. decision_log_entry：id/actor/context/advice/expected_result/verification_plan 齐
  6. 若 any irreversibility=irreversible → security_gate 必须非空
"""
import json
import sys
from pathlib import Path

SIX = ["data_completeness", "timeliness", "significance", "exclusivity", "history", "human_ai_consistency"]
FAIL = []


def check(name, ok, detail=""):
    print(("[OK]   " if ok else "[FAIL] ") + name + ("" if ok else "  ← " + detail))
    if not ok:
        FAIL.append(name)


def main():
    if len(sys.argv) != 2:
        print("usage: validate_report.py <report.json>")
        return 2
    p = Path(sys.argv[1])
    if not p.is_file():
        print(f"report not found: {p}")
        return 2
    r = json.loads(p.read_text(encoding="utf-8"))

    finding = r.get("finding") or []
    check("finding 1-2 条", 1 <= len(finding) <= 2, str(len(finding)))
    for i, f in enumerate(finding):
        check(f"finding[{i}] factor", bool(f.get("factor")))
        check(f"finding[{i}] confidence", f.get("confidence") not in (None, ""))
        ev = f.get("evidence") or []
        check(f"finding[{i}] evidence 非空", isinstance(ev, list) and len(ev) > 0)

    ca = r.get("cannot_attribute") or []
    check("cannot_attribute 非空", isinstance(ca, list) and len(ca) > 0)

    sc = r.get("scorecard") or {}
    for k in SIX:
        check(f"scorecard {k}", k in sc)

    rec = r.get("recommendation") or []
    check("recommendation 非空", len(rec) > 0)
    irreversible = False
    for i, x in enumerate(rec):
        check(f"rec[{i}] action", bool(x.get("action")))
        roi = x.get("roi") or {}
        for k in ("cost", "avoided", "anchor"):
            check(f"rec[{i}] roi.{k}", bool(roi.get(k)))
        irr = x.get("irreversibility")
        check(f"rec[{i}] irreversibility", irr in ("reversible", "irreversible"), str(irr))
        irreversible = irreversible or irr == "irreversible"

    dl = r.get("decision_log_entry") or {}
    for k in ("id", "actor", "context", "advice", "expected_result", "verification_plan"):
        check(f"decision_log {k}", bool(dl.get(k)))
    if irreversible:
        check("irreversible 需 security_gate", dl.get("security_gate") not in (None, {}, ""))

    print()
    if FAIL:
        print(f"RESULT: FAIL ({len(FAIL)} 项)")
        return 1
    print("RESULT: PASS（报告契约全绿）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
