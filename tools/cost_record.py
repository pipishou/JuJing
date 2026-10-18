#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""成本记录器（成本透明层）。

record：算净收益/ROI，追加 cost.jsonl
summary：汇总某个目录下所有成本记录

用法：
  python tools/cost_record.py record --task T-20261019-B --ai-cost 0.02 --tokens 79000 \
      --hours 2.5 --hourly 50 --action 500 --avoided 2000 --source estimate --outdir runs/task-B
  python tools/cost_record.py summary --file runs/task-B/cost.jsonl
"""
import argparse
import json
import sys
from datetime import date
from pathlib import Path


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("record")
    r.add_argument("--task", required=True)
    r.add_argument("--ai-cost", type=float, default=0.0)
    r.add_argument("--tokens", type=int, default=0)
    r.add_argument("--hours", type=float, default=0.0)
    r.add_argument("--hourly", type=float, default=50.0)
    r.add_argument("--action", type=float, default=0.0)
    r.add_argument("--avoided", type=float, default=0.0)
    r.add_argument("--source", choices=["billing", "estimate"], default="estimate")
    r.add_argument("--outdir", default="")

    s = sub.add_parser("summary")
    s.add_argument("--file", required=True)

    args = ap.parse_args()
    if args.cmd == "summary":
        rows = [json.loads(x) for x in Path(args.file).read_text(encoding="utf-8-sig").splitlines() if x.strip()]
        out = {
            "ok": True, "records": len(rows),
            "ai_cost_cny": round(sum(r.get("ai_cost_cny", 0) for r in rows), 4),
            "human_hours_saved": round(sum(r.get("human_hours_saved", 0) for r in rows), 2),
            "net_benefit_cny": round(sum(r.get("net_benefit_cny", 0) for r in rows), 2),
            "billing_records": sum(1 for r in rows if r.get("source") == "billing"),
            "estimate_records": sum(1 for r in rows if r.get("source") == "estimate"),
        }
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0

    human_saved = round(args.hours * args.hourly, 2)
    net = round(args.avoided - args.action - args.ai_cost, 2)
    denom = args.action + args.ai_cost
    rec = {
        "cost_id": f"C-{date.today().strftime('%Y%m%d')}-{args.task}",
        "task_id": args.task,
        "date": date.today().isoformat(),
        "ai_cost_cny": args.ai_cost,
        "ai_tokens": args.tokens,
        "human_hours_saved": args.hours,
        "human_hourly_cny": args.hourly,
        "human_cost_saved_cny": human_saved,
        "action_cost_cny": args.action,
        "avoided_loss_cny": args.avoided,
        "net_benefit_cny": net,
        "roi": round(net / denom, 2) if denom else None,
        "source": args.source,
        "verified": False,
        "notes": "estimate 为估算；billing 需账单核对后 verified=true",
    }
    outdir = Path(args.outdir or ".")
    outdir.mkdir(parents=True, exist_ok=True)
    p = outdir / "cost.jsonl"
    with open(p, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print(json.dumps({"ok": True, "file": str(p), "record": rec}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
