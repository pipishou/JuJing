#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""成本月报（成本透明层）。

从 cost.jsonl 汇总某月：AI 成本、人工节省、净收益、billing/estimate 占比；写 Markdown + JSON。

用法：
  python tools/cost_monthly.py --file runs/task-B/cost.jsonl --month 2026-10 --outdir runs
"""
import argparse
import json
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", required=True)
    ap.add_argument("--month", default=date.today().strftime("%Y-%m"))
    ap.add_argument("--outdir", default="runs")
    args = ap.parse_args()

    rows = []
    for line in Path(args.file).read_text(encoding="utf-8-sig").splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        if str(r.get("date", "")).startswith(args.month):
            rows.append(r)

    by_task = defaultdict(lambda: {"ai": 0.0, "hours": 0.0, "net": 0.0, "count": 0})
    for r in rows:
        t = by_task[r.get("task_id", "-")]
        t["ai"] += float(r.get("ai_cost_cny", 0))
        t["hours"] += float(r.get("human_hours_saved", 0))
        t["net"] += float(r.get("net_benefit_cny", 0))
        t["count"] += 1

    summary = {
        "month": args.month,
        "records": len(rows),
        "ai_cost_cny": round(sum(r.get("ai_cost_cny", 0) for r in rows), 4),
        "human_hours_saved": round(sum(r.get("human_hours_saved", 0) for r in rows), 2),
        "net_benefit_cny": round(sum(r.get("net_benefit_cny", 0) for r in rows), 2),
        "billing_records": sum(1 for r in rows if r.get("source") == "billing"),
        "estimate_records": sum(1 for r in rows if r.get("source") == "estimate"),
        "by_task": {k: {**v, "ai": round(v["ai"], 4), "hours": round(v["hours"], 2), "net": round(v["net"], 2)} for k, v in by_task.items()},
    }
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    md = [
        f"# 成本与价值月报 · {args.month}", "",
        f"- 记录数：{summary['records']}（billing {summary['billing_records']} / estimate {summary['estimate_records']}）",
        f"- AI 成本：¥{summary['ai_cost_cny']}",
        f"- 人工节省：{summary['human_hours_saved']}h",
        f"- 净收益：¥{summary['net_benefit_cny']}", "",
        "| 任务 | 记录 | AI 成本 | 人工节省 | 净收益 |", "|---|---|---|---|---|",
    ]
    for task, v in summary["by_task"].items():
        md.append(f"| {task} | {v['count']} | ¥{v['ai']} | {v['hours']}h | ¥{v['net']} |")
    md += ["", "> estimate 未经账单核对，不能当已验证；billing 需 verified=true。"]
    (outdir / f"cost-report-{args.month}.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    (outdir / f"cost-report-{args.month}.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"ok": True, "month": args.month, "report": str(outdir / f"cost-report-{args.month}.md"), **summary}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
