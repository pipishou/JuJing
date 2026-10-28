#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""validate_report 的 ROI 判据：0 是有效值（¥0 复核/对照试跑真存在）。

背景：C-2 联调时内容卡被 `bool(roi.cost)` 判成「空值」拒了——
demo 报告里「数据复核 ¥0 / 换批对照试跑 ¥0」本来是合法建议。
判据改为与 core.report_contract.is_empty 同口径：None/空串/空容器才算缺。

跑法：python -B tests/test_validate_report_roi.py
"""
import copy
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TARGET = ROOT / "tests" / "validate_report.py"
DEMO = ROOT / "runs" / "demo-report-2026-10-19.json"

CHECKS = []


def check(name, ok, detail=""):
    CHECKS.append((name, bool(ok), detail))


def run(report, extra=()):
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "report.json"
        path.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
        proc = subprocess.run(
            [sys.executable, "-B", str(TARGET), str(path), *extra],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def main():
    base = json.loads(DEMO.read_text(encoding="utf-8"))

    # 对照组：原 demo 报告仍 PASS（含 ¥0 建议）
    rc, out = run(base)
    check("demo 报告含 ¥0 建议仍 PASS", rc == 0, out.strip().splitlines()[-1] if out.strip() else "")

    def mutate(fn):
        doc = copy.deepcopy(base)
        fn(doc["recommendation"][0])
        return doc

    # cost=0 / avoided=0 是有效值
    rc, out = run(mutate(lambda r: r["roi"].update({"cost": 0, "avoided": 0})))
    check("roi.cost=0 / avoided=0 通过", rc == 0, out.strip().splitlines()[-1] if out.strip() else "")

    # None / 空串 / 缺键仍必须拒
    for label, value, key in (
        ("roi.cost=None 拒", None, "cost"),
        ("roi.cost='' 拒", "", "cost"),
        ("roi.anchor=None 拒", None, "anchor"),
    ):
        rc, out = run(mutate(lambda r, k=key, v=value: r["roi"].update({k: v})))
        check(label, rc != 0, out.strip().splitlines()[-1] if out.strip() else "")

    # 整个 roi 缺失仍必须拒
    def drop_roi(r):
        r.pop("roi", None)

    rc, out = run(mutate(drop_roi))
    check("roi 整块缺失 拒", rc != 0, out.strip().splitlines()[-1] if out.strip() else "")

    failed = [c for c in CHECKS if not c[1]]
    for name, ok, detail in CHECKS:
        print(("  PASS " if ok else "  FAIL ") + name + (f"  ← {detail}" if detail and not ok else ""))
    print(f"\n共 {len(CHECKS)} 项检查，失败 {len(failed)} 项")
    print("RESULT: PASS（ROI 判据全绿）" if not failed else "RESULT: FAIL")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
