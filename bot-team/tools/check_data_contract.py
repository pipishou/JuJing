#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""数据合同检查器（方法论 v1.3 §3 的规则化）。

输入：T/T30 JSON（demo 结构）
输出：11 因素可得性、核心三项、空值率、降级等级（normal/counterfactual/manual）

用法：
  python tools/check_data_contract.py --t demo/material_refill_T.json --t30 demo/material_refill_T30.json
  python tools/check_data_contract.py --t ... --strict   # manual 时非零退出，可做 G0 门
"""
import argparse
import json
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CORE = ["refill_rate", "mold_status", "maintenance_records"]


def load(p):
    return json.loads(Path(p).read_text(encoding="utf-8-sig"))


def present(rec, key):
    return key in rec and rec.get(key) is not None


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8-sig")
        sys.stderr.reconfigure(encoding="utf-8-sig")
    except Exception:
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--t", required=True)
    ap.add_argument("--t30", default="")
    ap.add_argument("--strict", action="store_true")
    args = ap.parse_args()

    factors = (yaml.safe_load((ROOT / "assets/factors_table.yaml").read_text(encoding="utf-8-sig")) or {}).get("factors") or []
    ids = [f["id"] for f in factors]
    t = load(args.t)
    recs = t.get("records") or []
    if not recs:
        print(json.dumps({"ok": False, "error": "T records empty"}, ensure_ascii=False))
        return 2

    availability = {}
    cell_missing = 0
    for fid in ids:
        miss = sum(0 if present(r, fid) else 1 for r in recs)
        cell_missing += miss
        availability[fid] = {
            "missing_records": miss,
            "availability": "available" if miss == 0 else ("partial" if miss <= max(1, len(recs) // 5) else "missing"),
        }

    missing_factors = [k for k, v in availability.items() if v["availability"] == "missing"]
    partial = [k for k, v in availability.items() if v["availability"] == "partial"]
    core_missing = [k for k in CORE if availability.get(k, {}).get("availability") == "missing"]
    null_rate = round(len(missing_factors) / len(ids), 4) if ids else 0.0
    cell_rate = round(cell_missing / (len(ids) * len(recs)), 4) if ids and recs else 0.0

    if null_rate <= 0.10:
        level, method = "normal", "exclusion_six_steps"
    elif null_rate <= 0.30:
        level, method = "counterfactual", "counterfactual"
    else:
        level, method = "manual", "manual_review"
    if core_missing:
        level, method = "manual", "manual_review"

    out = {
        "ok": True,
        "records": len(recs),
        "factors": len(ids),
        "missing_factors": missing_factors,
        "partial_factors": partial,
        "core_missing": core_missing,
        "null_rate_factor": null_rate,
        "null_rate_cell": cell_rate,
        "degradation_level": level,
        "method": method,
        "availability": availability,
        "t30_provided": bool(args.t30),
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))
    if args.strict and level == "manual":
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

