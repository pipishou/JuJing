#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""数据合同检查器（方法论 v1.3 §3 的规则化）。

输入：T/T30 JSON（demo 结构）
输出：11 因素可得性、核心三项、空值率、降级等级（normal/counterfactual/manual）、
      降级原因（degraded/degradation_reasons）与合同违规（contract_violations）

用法：
  python tools/check_data_contract.py --t demo/material_refill_T.json --t30 demo/material_refill_T30.json
  python tools/check_data_contract.py --t ... --strict   # 仅**合同违规**时非零退出（降级不是失败）
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

    # ---- 合同违规 vs 降级（B19 裁定：降级不是失败）----
    # assets/degradation_rules.yaml：「降级不是失败：每级降级本身都要写决策日志条目」；
    # docs/REAL_TASK_PLAYBOOK：核心三项缺失时「按 degradation_rules.yaml 降级并写清原因」。
    # 因此 --strict 只对**真违规**（值级错误 / 重复日期 / 空记录）fail-closed，
    # 降级态照常出报告，由呈现层只显示降级原因、不出建议。
    contract_violations = list(value_errors)
    core_cell_missing = sum(availability.get(k, {}).get("missing_records", 0) for k in CORE)
    core_cell_rate = round(core_cell_missing / (len(CORE) * len(recs)), 4) if recs and CORE else 0.0
    if core_missing or contract_violations:
        level, method = "manual", "manual_review"

    degradation_reasons = []
    if core_missing:
        degradation_reasons.append("核心字段整体缺失：" + "、".join(core_missing))
    if core_cell_rate > 0.10:
        degradation_reasons.append(f"核心字段空值率 {core_cell_rate:.0%} 超过 10% 阈值")
    elif cell_rate > 0.10:
        degradation_reasons.append(f"整体空值率 {cell_rate:.0%} 超过 10% 阈值")
    elif missing_factors:
        degradation_reasons.append("缺失因素：" + "、".join(missing_factors))
    degraded = bool(degradation_reasons)

    out = {
        "ok": True,
        "records": len(recs),
        "factors": len(ids),
        "missing_factors": missing_factors,
        "partial_factors": partial,
        "core_missing": core_missing,
        "null_rate_factor": null_rate,
        "null_rate_cell": cell_rate,
        "core_cell_missing_rate": core_cell_rate,
        "value_errors": value_errors,
        "duplicate_dates": duplicate_dates,
        "date_gaps": date_gaps,
        "degradation_level": level,
        "method": method,
        "degraded": degraded,
        "degradation_reasons": degradation_reasons,
        "contract_violations": contract_violations,
        "availability": availability,
        "t30_provided": bool(args.t30),
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))
    if args.strict and contract_violations:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

