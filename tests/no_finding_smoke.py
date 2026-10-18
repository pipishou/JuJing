#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""无归因（finding=0）端到端冒烟：真实数据证据不足时必须能诚实交付。"""
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable
TMP = Path(tempfile.mkdtemp(prefix="bot-nofind-"))


def run(cmd):
    p = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", errors="replace")
    if p.returncode != 0:
        print(p.stdout)
        print(p.stderr, file=sys.stderr)
        raise SystemExit(1)
    return p


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    result = {
        "goal": "合成：数据不足，不可归因",
        "attribution": {
            "finding": [],
            "cannot_attribute": ["保养记录缺失", "信息员换人偏差无法排除"],
            "scorecard": {"data_completeness": 2, "timeliness": 3, "significance": 4,
                          "exclusivity": 1, "history": 1, "human_ai_consistency": 0},
        },
        "advice": {"recommendation": [{"action": "先补数据，不行动",
                                        "roi": {"cost": "0", "avoided": "0", "anchor": "无行动"},
                                        "irreversibility": "reversible"}]},
    }
    rp = TMP / "workflow-result.json"
    rp.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    report = TMP / "report.json"
    run([PY, "orchestration/assemble_report.py", str(rp), str(report), "--real",
         "--task-id", "T-NOFIND", "--context", "数据不足演练", "--data-refs", "runs/task-nofind/T.json",
         "--expected", "补齐保养记录后重跑", "--records-dir", str(TMP / "loop"),
         "--analysis-json", "team/loop/example-analysis.json",
         "--goal-json", "team/goals/example-goal.json",
         "--cost-json", "team/cost/example-cost.json"])
    run([PY, "tests/validate_report.py", str(report), "--require-real"])
    run([PY, "tools/validate_loop.py", "--task-dir", str(TMP / "loop"), "--require-complete"])
    dec = json.loads((TMP / "loop" / "decision.json").read_text(encoding="utf-8-sig"))
    assert dec.get("no_finding") is True, "decision.no_finding must be true"
    assert dec.get("chosen") == "不做", "no_finding chosen must be 不做"
    print(json.dumps({"ok": True, "report": str(report), "decision": dec.get("id"),
                      "no_finding": True}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
