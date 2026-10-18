#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""host 收口：把 workflow 返回的 s1–s4 结果装配成报告（s5）。

用法：
  # demo（默认）
  python assemble_report.py <workflow-result.json> <out-report.json> [--date YYYY-MM-DD]
  # 真实任务：必须给 task-id/context/data-refs/expected；禁止 demo 占位
  python assemble_report.py <workflow-result.json> <out-report.json> --real \
      --task-id T-20261019-B --context "B线补料率7日 5.1%→7.2%" \
      --data-refs "runs/task-B/material_refill_T.json,runs/task-B/material_refill_T30.json" \
      --expected "T+7 补料率 ≤5.40%" [--goal "..."]

纪律：
  - finding/cannot_attribute 原样来自归因专家，不替它补结论；
  - demo 模式缺 scorecard 时用占位并标 is_demo=true；
  - real 模式缺 scorecard 直接失败，不补占位；
  - 任一建议 irreversible → security_gate 待确认（不自动确认）。
"""
import json
import subprocess
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SIX = ["data_completeness", "timeliness", "significance", "exclusivity", "history", "human_ai_consistency"]


def get_flag(args, flag):
    if flag in args:
        i = args.index(flag)
        if i + 1 < len(args):
            return args[i + 1]
    return ""


def main():
    raw = sys.argv[1:]
    real = "--real" in raw
    args = [a for a in raw if a != "--real"]
    if len(args) < 2:
        print("usage: assemble_report.py <workflow-result.json> <out-report.json> [--real --task-id ... --context ... --data-refs ... --expected ...]")
        return 2
    src_path, out_path = args[0], args[1]
    task_id = get_flag(args, "--task-id")
    context = get_flag(args, "--context")
    data_refs = get_flag(args, "--data-refs")
    expected = get_flag(args, "--expected")
    goal = get_flag(args, "--goal")
    records_dir = get_flag(args, "--records-dir")
    analysis_json = get_flag(args, "--analysis-json")
    goal_json = get_flag(args, "--goal-json")
    cost_json = get_flag(args, "--cost-json")
    approver = get_flag(args, "--approver")
    day = get_flag(args, "--date") or date.today().isoformat()

    if real:
        missing = [k for k, v in (("--task-id", task_id), ("--context", context),
                                  ("--data-refs", data_refs), ("--expected", expected)) if not v]
        if missing:
            print(f"[ERROR] real 模式缺少参数: {missing}")
            return 2

    src = json.loads(open(src_path, encoding="utf-8-sig").read())
    attribution = src.get("attribution") or {}
    advice = src.get("advice") or {}
    finding = attribution.get("finding") or []
    ca = attribution.get("cannot_attribute") or []
    rec = advice.get("recommendation") or []

    scorecard = attribution.get("scorecard") or {}
    if real:
        lacking = [k for k in SIX if k not in scorecard]
        if lacking:
            print(f"[ERROR] real 模式 scorecard 缺维度: {lacking}；请先用 tools/score_scorecard.py 计算")
            return 2
    else:
        for k in SIX:
            if k not in scorecard:
                scorecard[k] = {"score": 3, "evidence": "demo 占位：归因专家未提供该维（不得当真实结论）"}

    irreversible = any(x.get("irreversibility") == "irreversible" for x in rec)
    gate = None
    if irreversible or advice.get("security_gate_requested"):
        gate = {"requested_at": day, "confirmed_by": None, "status": "pending_user_confirmation"}

    refs = [x.strip() for x in data_refs.split(",") if x.strip()] if data_refs else ["demo/material_refill_T.json", "demo/material_refill_T30.json"]
    logs = {
        "id": f"DL-{day.replace('-', '')}-001",
        "actor": "manufacturing-ops-team-dsh",
        "task_id": task_id or None,
        "context": context or "B产线补料率7日爬升(5.1%→7.2%) 归因（demo 数据）",
        "data_refs": refs,
        "advice": (rec[0]["action"] if rec else ""),
        "expected_result": expected or "T+7 补料率回落至 ≤5.5%（demo 占位预期，真实试点须按数据合同重设）",
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
        "is_demo": not real,
        "task_id": task_id or None,
        "goal": goal or (src.get("goal") or ("真实任务：" + context if real else "B产线补料率7日爬升(5.1%→7.2%)的根因归因与行动建议")),
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
    print(f"assembled: {out_path} (real={real}, is_demo={report['is_demo']}, finding={len(finding)}, cannot_attribute={len(ca)}, rec={len(rec)})")

    if records_dir:
        cmd = [sys.executable, str(ROOT / "tools/write_loop_bundle.py"), "--report", out_path, "--outdir", records_dir]
        for flag, val in (("--analysis", analysis_json), ("--goal", goal_json), ("--cost", cost_json), ("--approver", approver)):
            if val:
                cmd += [flag, val]
        p = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", errors="replace")
        print(p.stdout.strip())
        if p.returncode != 0:
            print(p.stderr, file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

