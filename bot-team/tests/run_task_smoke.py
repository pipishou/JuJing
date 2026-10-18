#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一键收口冒烟：合成 11 列数据 → run_task.py → is_demo=false + decision=approved。"""
import datetime as dt
import json
import subprocess
import sys
import tempfile
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable
TMP = Path(tempfile.mkdtemp(prefix="bot-runtask-"))


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    start = dt.date(2026, 9, 5)
    rows = []
    for i in range(37):
        d = start + dt.timedelta(days=i)
        rows.append({
            "日期": d.isoformat(), "产线": "B", "补料率": round(5.0 + 0.06 * i, 2), "产量": 1000 + i,
            "模具状态": "normal", "保养记录": "[]", "班组": "甲班", "原料批次": "RM-2026",
            "环境温湿度": "{}", "换模记录": "[]", "停机记录": "[]", "信息员": "李强",
        })
    csv = TMP / "task.csv"
    pd.DataFrame(rows).to_csv(csv, index=False, encoding="utf-8-sig")
    result = {
        "goal": "合成：B线补料率异常一键收口",
        "attribution": {
            "finding": [{"factor": "模具状态", "confidence": 0.63, "evidence": ["合成数据证据"]}],
            "cannot_attribute": ["信息员换人"],
            "scorecard": {"data_completeness": 4, "timeliness": 4, "significance": 5,
                          "exclusivity": 3, "history": 3, "human_ai_consistency": 0},
        },
        "advice": {"recommendation": [{"action": "补保养",
                                        "roi": {"cost": "¥500", "avoided": "¥2000", "anchor": "保养<换模+停机"},
                                        "irreversibility": "reversible"}]},
    }
    wr = TMP / "workflow-result.json"
    wr.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    outdir = TMP / "task-B"
    p = subprocess.run([
        PY, "tools/run_task.py", "--csv", str(csv), "--line", "B",
        "--t-start", "2026-10-05", "--t-end", "2026-10-11",
        "--base-start", "2026-09-05", "--base-end", "2026-10-04",
        "--field-map", "templates/field-map.example.yaml", "--outdir", str(outdir),
        "--workflow-result", str(wr),
        "--analysis-json", "team/loop/example-analysis.json",
        "--goal-json", "team/goals/example-goal.json",
        "--cost-json", "team/cost/example-cost.json",
        "--expected", "T+7 ≤5.40%", "--signer", "smoke",
    ], cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", errors="replace")
    if p.returncode != 0:
        print(p.stdout[-3000:])
        print(p.stderr[-3000:], file=sys.stderr)
        raise SystemExit(1)
    report = json.loads((outdir / "report.json").read_text(encoding="utf-8-sig"))
    dec = json.loads((outdir / "loop" / "decision.json").read_text(encoding="utf-8-sig"))
    assert report.get("is_demo") is False, "report must not be demo"
    assert dec.get("status") == "approved", "decision must be approved"
    assert (outdir / "report.signature.json").is_file(), "signature missing"
    print(json.dumps({"ok": True, "task_id": report.get("task_id"),
                      "decision": dec.get("id"), "status": dec.get("status")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
