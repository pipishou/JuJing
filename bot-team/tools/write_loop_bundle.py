#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把已收口的报告写成四段闭环 bundle（s5 收口用）。

自动写：evidence.jsonl / decision.json / closure.json
可选拷贝：--analysis/--goal/--cost 提供的 JSON（分析卡由 s3 产出，目标/成本由业务输入）
不伪造：没有 --analysis/--goal/--cost 就不写，validate_loop --require-complete 会报缺。

用法：
  python tools/write_loop_bundle.py --report runs/task-B/report.json --outdir runs/task-B/loop \
      --analysis team/loop/example-analysis.json --goal team/goals/example-goal.json --cost team/cost/example-cost.json
"""
import argparse
import json
import shutil
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", required=True)
    ap.add_argument("--outdir", required=True)
    ap.add_argument("--analysis", default="")
    ap.add_argument("--goal", default="")
    ap.add_argument("--cost", default="")
    ap.add_argument("--approver", default="")
    ap.add_argument("--review-days", type=int, default=7)
    args = ap.parse_args()

    report = json.loads(Path(args.report).read_text(encoding="utf-8-sig"))
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    day = report.get("generated_at") or date.today().isoformat()
    compact = day.replace("-", "")
    did = f"D-{compact}-001"
    aid = f"A-{compact}-001"

    refs = (report.get("decision_log_entry") or {}).get("data_refs") or []
    findings = report.get("finding") or []
    evidence = []
    for fi, f in enumerate(findings):
        for ei, quote in enumerate(f.get("evidence") or []):
            evidence.append({
                "id": f"E-{compact}-{fi+1:02d}{ei+1:02d}",
                "query": f"报告证据：{f.get('factor')}",
                "source": {"url": f"data://{refs[0] if refs else 'report'}", "title": f.get("factor", "data evidence")},
                "quote": str(quote)[:200],
                "score": 1.0,
                "grade": "B",
                "data_quality_grade": "A",
                "freshness_days": 30,
                "retrieved_at": day,
                "stance": "supports",
                "used_for": [aid, did],
            })
    with open(outdir / "evidence.jsonl", "w", encoding="utf-8") as fh:
        for e in evidence:
            fh.write(json.dumps(e, ensure_ascii=False) + "\n")

    rec = (report.get("recommendation") or [{}])[0]
    no_finding = len(findings) == 0
    options = [f.get("factor") for f in findings if f.get("factor")] + ["不做"]
    chosen = options[0] if options else "不做"
    if no_finding:
        chosen = "不做"
    status = "approved" if args.approver else "proposed"
    decision = {
        "id": did,
        "question": report.get("goal") or "经营异常归因",
        "analysis_id": aid,
        "evidence_ids": [e["id"] for e in evidence],
        "options": options,
        "chosen": chosen,
        "rejected": [{"option": o, "reason": "证据/收益排序落后"} for o in options[1:]],
        "confidence": "LOW" if no_finding else "MED",
        "reversibility": rec.get("irreversibility", "reversible"),
        "approval": {"by": args.approver or "pending", "at": day, "mode": "explicit" if args.approver else "default"},
        "reversal_conditions": [report.get("decision_log_entry", {}).get("expected_result") or "T+7 未达预期"],
        "expected_outcome": report.get("decision_log_entry", {}).get("expected_result") or "待填",
        "review_due": (date.fromisoformat(day) + timedelta(days=args.review_days)).isoformat(),
        "status": status,
        "no_finding": no_finding,
        "entity_id": (report.get("task_id") or "task"),
        "metric": report.get("goal") or "",
        "context": report.get("decision_log_entry", {}).get("context") or "",
        "recommendation": rec.get("action") or "",
        "actual_action": "",
        "override_reason": "",
        "attribution_method": "exclusion",
        "outcome_window": f"{args.review_days}d",
        "action_cost": {"cny": 0, "items": []},
        "increment_value": (rec.get("roi") or {}).get("avoided", ""),
        "calibration_version": "cold-start-16.67",
        "notes": "由 write_loop_bundle 自动生成；approval.pending 表示尚未人工签字",
    }
    (outdir / "decision.json").write_text(json.dumps(decision, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    closure = {
        "id": f"C-{compact}-001",
        "decision_id": did,
        "due": decision["review_due"],
        "actual_outcome": "待回填",
        "metric_result": {"metric": decision["metric"], "expected": decision["expected_outcome"], "actual": "", "delta": ""},
        "evidence_ids": [],
        "verdict": "unverified",
        "lessons": ["待回填"],
        "action": "keep",
        "increment_value": "",
        "attribution_method": "exclusion",
        "scorecard_snapshot": report.get("scorecard") or {},
        "failure_mode": "none",
        "calibration_version": "cold-start-16.67",
        "wal_ref": "",
    }
    (outdir / "closure.json").write_text(json.dumps(closure, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    copied = []
    for key, src in (("analysis", args.analysis), ("goal", args.goal), ("cost", args.cost)):
        if src:
            dst = outdir / f"{key}.json"
            shutil.copyfile(src, dst)
            copied.append(key)
    summary = {"ok": True, "outdir": str(outdir), "decision_id": did, "analysis_id": aid,
               "evidence": len(evidence), "copied": copied,
               "missing": [k for k in ("analysis", "goal", "cost") if k not in copied]}
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
