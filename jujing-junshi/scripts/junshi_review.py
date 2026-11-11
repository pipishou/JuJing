#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""junshi_review.py — 军师回执的机器聚合（相位5 与聚合复盘共用；只读，不改历史行）。

用法：
  python scripts/junshi_review.py            # 汇总 + 命中率
  python scripts/junshi_review.py --due      # 仅列 review_due 已到且未复盘的会话（相位0/5 用）
  python scripts/junshi_review.py --json     # 机器可读

口径（与 SKILL.md 效果自评一致）：
  命中率 = hit / (hit + falsified)；unverified 与 legacy（无 outcome 字段）不计入分母但单独报数。
"""
import json
import sys
from datetime import date
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / "data" / "sessions.jsonl"


def load():
    rows = []
    if not DATA.is_file():
        return rows
    for ln in DATA.read_text(encoding="utf-8").splitlines():
        if ln.strip():
            try:
                rows.append(json.loads(ln))
            except json.JSONDecodeError:
                pass
    return rows


def main(argv):
    rows = load()
    due = "--due" in argv
    if due:
        today = date.today().isoformat()
        pending = [r for r in rows
                   if r.get("review_due") and r["review_due"] <= today and not r.get("reviewed")]
        for r in pending:
            print(f"{r.get('date','?')} | {r.get('project','?')} | due {r['review_due']} | "
                  f"outcome={r.get('outcome','(legacy)')}")
        print(f"[junshi] 到期未复盘 {len(pending)} 条")
        return 0

    hit = sum(1 for r in rows if r.get("outcome") == "hit")
    fal = sum(1 for r in rows if r.get("outcome") == "falsified")
    unv = sum(1 for r in rows if r.get("outcome") == "unverified")
    legacy = sum(1 for r in rows if "outcome" not in r)
    denom = hit + fal
    rate = f"{hit / denom:.0%}" if denom else "n/a（无已证伪/已命中样本）"
    reasons = {}
    for r in rows:
        k = r.get("search_used_reason") or "(未填)"
        reasons[k] = reasons.get(k, 0) + 1
    def total_searches(r):
        v = r.get("searches")
        if isinstance(v, int):
            return v
        if isinstance(v, dict):
            return sum(int(x or 0) for x in v.values() if isinstance(x, (int, float)))
        return 0

    summary = {
        "sessions": len(rows), "hit": hit, "falsified": fal, "unverified": unv,
        "legacy_missing_outcome": legacy, "hit_rate": rate,
        "searches_total": sum(total_searches(r) for r in rows),
        "searched_sessions": sum(1 for r in rows if total_searches(r) > 0),
        "cost_cny": round(sum(float(r.get("cost_cny") or 0) for r in rows), 2),
        "search_used_reason": reasons,
    }
    br_rows = [r for r in rows if isinstance(r.get("branches"), int) and r["branches"] > 0]
    br_n = len(br_rows)
    br_adopted = sum(1 for r in br_rows
                     if isinstance(r.get("branch_adopted"), int) and r["branch_adopted"] > 0)
    hq = [r["handoff_quality"] for r in br_rows if isinstance(r.get("handoff_quality"), (int, float))]
    summary["branch_sessions"] = br_n
    summary["branch_adopted_rate"] = (f"{br_adopted / br_n:.0%}" if br_n else "n/a（无建树会话）")
    summary["handoff_quality_avg"] = (round(sum(hq) / len(hq), 1) if hq else "n/a（未回填）")

    if "--json" in argv:
        print(json.dumps(summary, ensure_ascii=False))
    else:
        print(f"会话 {len(rows)}｜命中 {hit}｜证伪 {fal}｜未验证 {unv}｜legacy(无 outcome) {legacy}")
        print(f"命中率: {rate}｜检索次数 {summary['searches_total']}（有检索会话 {summary['searched_sessions']}）｜累计成本 ¥{summary['cost_cny']}")
        print(f"成本闸归因: {reasons}")
        print(f"建树会话 {br_n}｜分支持采纳率 {summary['branch_adopted_rate']}｜回传质量均分 {summary['handoff_quality_avg']}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
