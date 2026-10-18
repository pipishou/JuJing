#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""host 收口：把 workflow 返回的 s1–s4 结果装配成报告（s5）。

用法：
  python assemble_report.py <workflow-result.json> <out-report.json> [--date YYYY-MM-DD]

纪律：
  - finding/cannot_attribute 原样来自归因专家，不替它补结论；
  - 若 scorecard 缺失，用六维「等权占位」并显式标注 demo 占位（不得当真实结论）；
  - 若任一建议 irreversible，security_gate 置「待确认」（非 confirmed），并置 is_demo=true。
"""
import json
import sys
from datetime import date

SIX = ["data_completeness", "timeliness", "significance", "exclusivity", "history", "human_ai_consistency"]


def main():
    if len(sys.argv) < 3:
        print("usage: assemble_report.py <workflow-result.json> <out-report.json> [--date YYYY-MM-DD]")
        return 2
    src = json.loads(open(sys.argv[1], encoding="utf-8").read())
    out_path = sys.argv[2]
    day = date.today().isoformat()
    if "--date" in sys.argv:
        day = sys.argv[sys.argv.index("--date") + 1]

    attribution = src.get("attribution") or {}
    advice = src.get("advice") or {}
    finding = attribution.get("finding") or []
    ca = attribution.get("cannot_attribute") or []
    rec = advice.get("recommendation") or []

    scorecard = attribution.get("scorecard") or {}
    for k in SIX:
        if k not in scorecard:
            scorecard[k] = {"score": 3, "evidence": "demo 占位：归因专家未提供该维（不得当真实结论）"}

    irreversible = any(x.get("irreversibility") == "irreversible" for x in rec)
    gate = None
    if irreversible or advice.get("security_gate_requested"):
        gate = {"requested_at": day, "confirmed_by": None, "status": "pending_user_confirmation"}

    logs = {
        "id": f"DL-{day.replace('-', '')}-001",
        "actor": "manufacturing-ops-team-dsh",
        "context": "B产线补料率7日爬升(5.1%→7.2%) 归因（demo 数据）",
        "data_refs": ["demo/material_refill_T.json", "demo/material_refill_T30.json"],
        "advice": (rec[0]["action"] if rec else ""),
        "expected_result": "T+7 补料率回落至 ≤5.5%（demo 占位预期，真实试点须按数据合同重设）",
        "irreversibility": "irreversible" if irreversible else "reversible",
        "security_gate": gate,
        "verification_plan": "T+7 复查并回填实际值；决策日志与运行回执对账",
        "actual_result": None,
        "clause": "G2事实纪律",
        "trace": "s1:snapshot,s3:attribution,s4:advice",
    }

    report = {
        "schema": "manufacturing-ops-report/0.1",
        "generated_at": day,
        "is_demo": True,
        "goal": (src.get("goal") or "B产线补料率7日爬升(5.1%→7.2%)的根因归因与行动建议"),
        "finding": finding,
        "cannot_attribute": ca,
        "scorecard": scorecard,
        "recommendation": rec,
        "decision_log_entry": logs,
        "role_coverage": {
            "host": "s5",
            "consulting-analyst": "s1",
            "ai-attribution-expert": "s3",
            "manufacturing-ops-expert": ["s2", "s4"],
        },
        "source": {"workflow_result": src.get("source") or "team-workflow.mjs"},
    }
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"assembled: {out_path} (demo={report['is_demo']}, finding={len(finding)}, cannot_attribute={len(ca)}, rec={len(rec)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
