#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""四段闭环 bundle 校验（G4 前门）。

核心三件：evidence.jsonl / decision.json / closure.json
完整五件（--require-complete）：+ analysis.json / goal.json / cost.json

用法：
  python tools/validate_loop.py --task-dir runs/task-B/loop
  python tools/validate_loop.py --task-dir runs/task-B/loop --require-complete
"""
import argparse
import json
import sys
from pathlib import Path

DIMS = ["data_completeness", "timeliness", "significance", "exclusivity", "history", "human_ai_consistency"]
DECISION_REQ = ["id", "question", "analysis_id", "evidence_ids", "options", "chosen", "confidence",
                "reversibility", "approval", "reversal_conditions", "expected_outcome", "review_due", "status"]


def readj(p):
    return json.loads(Path(p).read_text(encoding="utf-8-sig"))


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--task-dir", required=True)
    ap.add_argument("--require-complete", action="store_true")
    args = ap.parse_args()
    d = Path(args.task_dir)
    errors, warnings = [], []

    ev = d / "evidence.jsonl"
    if not ev.is_file():
        errors.append("missing evidence.jsonl")
    else:
        lines = [json.loads(x) for x in ev.read_text(encoding="utf-8-sig").splitlines() if x.strip()]
        if not lines:
            errors.append("evidence.jsonl empty")
        for i, e in enumerate(lines):
            for k in ("id", "source", "quote", "score", "grade"):
                if k not in e:
                    errors.append(f"evidence[{i}] missing {k}")
            if len(str(e.get("quote", ""))) > 200:
                warnings.append(f"evidence[{i}] quote >200 字")

    for name in ("decision.json", "closure.json"):
        if not (d / name).is_file():
            errors.append(f"missing {name}")

    if (d / "decision.json").is_file():
        dec = readj(d / "decision.json")
        for k in DECISION_REQ:
            if k not in dec:
                errors.append(f"decision missing {k}")
        appr = dec.get("approval") or {}
        if not all(k in appr for k in ("by", "at", "mode")):
            errors.append("decision approval 缺 by/at/mode")
        if not dec.get("reversal_conditions"):
            errors.append("decision reversal_conditions 为空")

    if (d / "closure.json").is_file():
        clo = readj(d / "closure.json")
        if clo.get("verdict") not in ("hit", "miss", "partial", "unverified"):
            errors.append("closure verdict 非法")
        if clo.get("verdict") == "unverified":
            warnings.append("closure 仍是 unverified（待 T+7 回填）")

    analysis = d / "analysis.json"
    if analysis.is_file():
        a = readj(analysis)
        for k in ("id", "options", "criteria", "weighted_scores", "assumptions", "disconfirming", "premortem"):
            if k not in a:
                errors.append(f"analysis missing {k}")
        if len(a.get("options") or []) < 2:
            errors.append("analysis options <2")
        w = sum(float(c.get("weight", 0)) for c in (a.get("criteria") or []))
        if abs(w - 1.0) > 0.01:
            errors.append(f"analysis criteria weight sum={w:.3f} != 1")
    elif args.require_complete:
        errors.append("missing analysis.json")

    goal = d / "goal.json"
    if goal.is_file():
        g = readj(goal)
        for k in ("goal_id", "metric", "baseline", "target", "deadline", "owner"):
            if k not in g:
                errors.append(f"goal missing {k}")
    elif args.require_complete:
        errors.append("missing goal.json")

    cost = d / "cost.json"
    if cost.is_file():
        c = readj(cost)
        for k in ("cost_id", "ai_cost_cny", "net_benefit_cny", "source"):
            if k not in c:
                errors.append(f"cost missing {k}")
    elif args.require_complete:
        errors.append("missing cost.json")

    out = {"ok": not errors, "task_dir": str(d), "errors": errors, "warnings": warnings,
           "complete": not args.require_complete or not errors}
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
