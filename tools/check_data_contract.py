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
from datetime import date, datetime
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
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
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

    # ---- 值级检查（键存在 ≠ 值可用）----
    value_errors = []
    dates = []
    for i, r in enumerate(recs):
        raw = r.get("date")
        try:
            dates.append(datetime.strptime(str(raw), "%Y-%m-%d").date())
        except Exception:
            value_errors.append(f"record[{i}] date 非法: {raw}")
        if not str(r.get("production_line", "")).strip():
            value_errors.append(f"record[{i}] production_line 为空")
        for key, lo, hi in (("refill_rate", 0.0, 100.0), ("output_volume", 0.0, None)):
            if r.get(key) is None:
                continue
            try:
                v = float(r[key])
            except Exception:
                value_errors.append(f"record[{i}] {key} 非数值: {r[key]}")
                continue
            if v < lo or (hi is not None and v > hi):
                value_errors.append(f"record[{i}] {key} 越界: {v}")
    duplicate_dates = len(dates) - len(set(dates)) if dates else 0
    date_gaps = 0
    if dates:
        uniq = sorted(set(dates))
        date_gaps = max(0, (uniq[-1] - uniq[0]).days + 1 - len(uniq))
    if duplicate_dates:
        value_errors.append(f"重复日期 {duplicate_dates} 条")

    if null_rate <= 0.10:
        level, method = "normal", "exclusion_six_steps"
    elif null_rate <= 0.30:
        level, method = "counterfactual", "counterfactual"
    else:
        level, method = "manual", "manual_review"
    if core_missing or value_errors or duplicate_dates:
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
        "value_errors": value_errors,
        "duplicate_dates": duplicate_dates,
        "date_gaps": date_gaps,
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

