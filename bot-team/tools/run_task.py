#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""真实任务一键收口（s5 host）：ingest → 合同 → real 报告 → 四卡 → 签字 → decision approved。

前置：workflow-result.json 已由 DSH workflow 跑出（s1–s4）。
用法：
  python tools/run_task.py --csv task.csv --line B \
      --t-start 2026-10-05 --t-end 2026-10-11 \
      --base-start 2026-09-05 --base-end 2026-10-04 \
      --field-map templates/field-map.example.yaml \
      --outdir runs/task-B --workflow-result runs/task-B/workflow-result.json \
      --analysis-json team/loop/example-analysis.json \
      --goal-json team/goals/example-goal.json \
      --cost-json team/cost/example-cost.json \
      --expected "T+7 补料率 ≤5.40%" --signer "业务负责人"
"""
import argparse
import json
import subprocess
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable


def run(name, cmd):
    p = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", errors="replace")
    print(f"[{name}] exit={p.returncode}")
    if p.returncode != 0:
        print(p.stdout[-2000:])
        print(p.stderr[-2000:], file=sys.stderr)
        raise SystemExit(p.returncode)
    return p


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", required=True)
    ap.add_argument("--line", default="")
    ap.add_argument("--t-start", required=True)
    ap.add_argument("--t-end", required=True)
    ap.add_argument("--base-start", required=True)
    ap.add_argument("--base-end", required=True)
    ap.add_argument("--field-map", default="")
    ap.add_argument("--outdir", required=True)
    ap.add_argument("--workflow-result", required=True)
    ap.add_argument("--task-id", default="")
    ap.add_argument("--context", default="")
    ap.add_argument("--expected", required=True)
    ap.add_argument("--analysis-json", default="")
    ap.add_argument("--goal-json", default="")
    ap.add_argument("--cost-json", default="")
    ap.add_argument("--signer", required=True)
    args = ap.parse_args()

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    task_id = args.task_id or f"T-{date.today().strftime('%Y%m%d')}-{(args.line or 'ALL')}"
    context = args.context or f"{args.line or ''}线补料率异常（真实任务）"

    ingest = [PY, "tools/ingest_task_data.py", "--input", args.csv, "--line", args.line,
              "--t-start", args.t_start, "--t-end", args.t_end,
              "--base-start", args.base_start, "--base-end", args.base_end,
              "--outdir", str(outdir)]
    if args.field_map:
        ingest += ["--field-map", args.field_map]
    run("ingest", ingest)

    t = outdir / "material_refill_T.json"
    t30 = outdir / "material_refill_T30.json"
    run("contract", [PY, "tools/check_data_contract.py", "--t", str(t), "--t30", str(t30), "--strict"])

    report = outdir / "report.json"
    records = outdir / "loop"
    assemble = [PY, "orchestration/assemble_report.py", args.workflow_result, str(report), "--real",
                "--task-id", task_id, "--context", context,
                "--data-refs", f"{t},{t30}", "--expected", args.expected,
                "--records-dir", str(records)]
    for flag, val in (("--analysis-json", args.analysis_json), ("--goal-json", args.goal_json),
                      ("--cost-json", args.cost_json)):
        if val:
            assemble += [flag, val]
    run("assemble", assemble)

    run("validate-report", [PY, "tests/validate_report.py", str(report), "--require-real"])
    loop_cmd = [PY, "tools/validate_loop.py", "--task-dir", str(records)]
    if args.analysis_json and args.goal_json and args.cost_json:
        loop_cmd.append("--require-complete")
    run("validate-loop", loop_cmd)

    sig = outdir / "report.signature.json"
    run("sign", [PY, "tools/sign_delivery.py", str(report), "--signer", args.signer, "--out", str(sig)])
    run("finalize", [PY, "tools/finalize_delivery.py", "--report", str(report),
                    "--loop-dir", str(records), "--signature", str(sig)])

    dec = json.loads((records / "decision.json").read_text(encoding="utf-8-sig"))
    summary = {"ok": True, "task_id": task_id, "report": str(report), "loop": str(records),
               "signature": str(sig), "decision_status": dec.get("status"),
               "is_demo": json.loads(report.read_text(encoding="utf-8-sig")).get("is_demo")}
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
