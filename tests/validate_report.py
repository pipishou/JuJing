#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""报告校验：demo 报告 or 真实报告是否满足产出契约。

判据（任一 FAIL → 非零退出）：
  1. finding：1–2 条，每条 factor/confidence/evidence 齐，evidence 非空
  2. cannot_attribute：非空
  3. scorecard：六维键齐；--require-real 时必须是数值 0-5（拒绝叙述/占位）
  4. recommendation：非空，每条 action/roi(cost,avoided,anchor)/irreversibility 齐
  5. decision_log_entry：id/actor/context/advice/expected_result/verification_plan 齐
  6. 若 any irreversibility=irreversible → security_gate 必须非空
  7. --require-real：is_demo=false、task_id 非空、data_refs 不含 demo/
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
    raw = sys.argv[1:]
    require_real = "--require-real" in raw
    args = [a for a in raw if a != "--require-real"]
    if len(args) != 1:
        print("usage: validate_report.py <report.json> [--require-real]")
        return 2
    p = Path(args[0])
    if not p.is_file():
        print(f"report not found: {p}")
        return 2
    r = json.loads(p.read_text(encoding="utf-8-sig"))

    finding = r.get("finding") or []
    check("finding 0-2 条", 0 <= len(finding) <= 2, str(len(finding)))
    if len(finding) == 0:
        check("no_finding 需 cannot_attribute 非空", isinstance(r.get("cannot_attribute"), list) and len(r["cannot_attribute"]) > 0)
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
    if require_real:
        for k in SIX:
            v = sc.get(k)
            if isinstance(v, dict):
                v = v.get("score")
            ok = isinstance(v, (int, float)) and not isinstance(v, bool) and 0 <= float(v) <= 5
            check(f"real scorecard {k} numeric 0-5", ok, repr(v))
        check("real is_demo=false", r.get("is_demo") is False)
        check("real task_id", bool(r.get("task_id")))
        refs = (r.get("decision_log_entry") or {}).get("data_refs") or []
        check("real data_refs 无 demo/", isinstance(refs, list) and all("demo/" not in str(x) for x in refs), str(refs))

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

