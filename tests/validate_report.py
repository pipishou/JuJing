#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""报告校验：demo 报告 or 真实报告是否满足产出契约。

判据（任一 FAIL → 非零退出）：
  1. finding：0–2 条，每条 factor/confidence/evidence 齐，evidence 非空
  2. cannot_attribute：非空
  3. scorecard：六维键齐；--require-real 时必须是数值 0-5（旧口径）或 0-100（B16 百分制），
     且 total 0-100 / verdict ∈ {output,force_manual_review,reference_only,no_output}
  4. recommendation：非空，每条 action/roi(cost,avoided,anchor)/irreversibility 齐
  5. decision_log_entry：id/actor/context/advice/expected_result/verification_plan 齐
  6. 若 any irreversibility=irreversible → security_gate 必须非空
  7. --require-real（M1 可信门，A2）：
     is_demo=false、task_id 非空、data_refs 不含 demo/、
     core.gates.real_report_errors 全绿（占位/缺证据/refs 不存在或 sha 不符/
     scorecard.total 与 verdict 缺失或与独立计算不一致）
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

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
            # B16：旧 0-5 口径与 100 分制新口径都接受（代码按 ≤5 / >5 自动识别）
            ok = (isinstance(v, (int, float)) and not isinstance(v, bool)
                  and 0 <= float(v) <= 100)
            check(f"real scorecard {k} numeric 0-5/0-100", ok, repr(v))
        check("real is_demo=false", r.get("is_demo") is False)
        check("real task_id", bool(r.get("task_id")))
        # B16：100 分制总分 + 档位必须落地且域内
        total = sc.get("total")
        check("real scorecard.total numeric 0-100",
              isinstance(total, (int, float)) and not isinstance(total, bool) and 0 <= float(total) <= 100,
              repr(total))
        check("real scorecard.verdict",
              sc.get("verdict") in ("output", "force_manual_review", "reference_only", "no_output"),
              repr(sc.get("verdict")))
        # M1.5 §3 B4 硬化：data_refs 可能在顶层与 decision_log_entry 两处，两处都要查 demo/
        top_refs = r.get("data_refs")
        dl_refs = (r.get("decision_log_entry") or {}).get("data_refs")
        for where, refs in (("data_refs", top_refs), ("decision_log_entry.data_refs", dl_refs)):
            if refs is None:
                continue
            check(f"real {where} 无 demo/",
                  isinstance(refs, list) and all("demo/" not in str(x) for x in refs), str(refs))
        try:
            from core import gates as _gates

            errs = _gates.real_report_errors(r, ROOT)
        except Exception as exc:  # noqa: BLE001
            errs = [f"可信门执行异常：{exc}"]
        check("real 可信门（占位/证据/溯源/total-verdict）", not errs, "；".join(str(e) for e in errs)[:500])

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
