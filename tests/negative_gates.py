#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""负向门测试：确保门能拒绝（不是只对正例绿）。

覆盖：
  1. demo 报告 + --require-real 必须 FAIL
  2. demo 报告签字必须被拒
  3. demo 叙述 scorecard + --strict 必须 FAIL
  4. 只有 4 列的真实 CSV → 数据合同 --strict 必须 FAIL（manual）

用法：python tests/negative_gates.py
"""
import json
import subprocess
import sys
import tempfile
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable
TMP = Path(tempfile.mkdtemp(prefix="bot-negative-"))
FAILS = []


def expect_fail(name, cmd):
    p = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", errors="replace")
    ok = p.returncode != 0
    print(f"[{'OK' if ok else 'FAIL'}] {name} exit={p.returncode}")
    if not ok:
        FAILS.append(name)


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass
    demo = ROOT / "runs" / "demo-report.json"
    expect_fail("demo real gate", [PY, "tests/validate_report.py", str(demo), "--require-real"])
    expect_fail("demo sign", [PY, "tools/sign_delivery.py", str(demo), "--signer", "x", "--out", str(TMP / "d.json")])
    expect_fail("narrative scorecard strict", [PY, "tools/score_scorecard.py", "--from-report", str(demo), "--strict"])

    start = pd.Timestamp("2026-09-05")
    rows = [{"日期": (start + pd.Timedelta(days=i)).strftime("%Y-%m-%d"), "产线": "B",
             "补料率": 5 + i * 0.05, "产量": 1000 + i} for i in range(37)]
    csv = TMP / "four-cols.csv"
    pd.DataFrame(rows).to_csv(csv, index=False)
    outdir = TMP / "four-cols"
    subprocess.run([PY, "tools/ingest_task_data.py", "--input", str(csv), "--line", "B",
                    "--t-start", "2026-10-05", "--t-end", "2026-10-11",
                    "--base-start", "2026-09-05", "--base-end", "2026-10-04",
                    "--outdir", str(outdir), "--field-map", "templates/field-map.example.yaml"],
                   cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", errors="replace")
    expect_fail("4-col contract strict", [PY, "tools/check_data_contract.py",
                                          "--t", str(outdir / "material_refill_T.json"), "--strict"])

    print(json.dumps({"ok": not FAILS, "fails": FAILS}, ensure_ascii=False, indent=2))
    return 0 if not FAILS else 1


if __name__ == "__main__":
    raise SystemExit(main())
