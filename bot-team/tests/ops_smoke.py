#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""运营层冒烟：回看扫描 / 成本月报 / 企微报告模式（不真发、不依赖真实数据）。"""
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable
TMP = Path(tempfile.mkdtemp(prefix="bot-ops-smoke-"))


def run(cmd):
    p = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", errors="replace")
    if p.returncode != 0:
        print(p.stdout)
        print(p.stderr, file=sys.stderr)
        raise SystemExit(1)
    return p.stdout


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    loop = TMP / "task" / "loop"
    loop.mkdir(parents=True)
    (loop / "decision.json").write_text(json.dumps({
        "id": "D-20261001-001", "question": "补料率异常", "expected_outcome": "T+7 ≤5.40%",
        "review_due": "2026-10-08"}, ensure_ascii=False), encoding="utf-8")
    (loop / "closure.json").write_text(json.dumps({
        "id": "C-20261001-001", "decision_id": "D-20261001-001", "due": "2026-10-08",
        "verdict": "unverified"}, ensure_ascii=False), encoding="utf-8")
    out = run([PY, "tools/review_due.py", "--root", str(TMP), "--today", "2026-10-18"])
    assert "D-20261001-001" in out, "review_due did not list due decision"

    costdir = TMP / "cost"
    run([PY, "tools/cost_record.py", "record", "--task", "T-A", "--ai-cost", "0.02",
         "--hours", "2.5", "--action", "500", "--avoided", "2000", "--outdir", str(costdir)])
    run([PY, "tools/cost_record.py", "record", "--task", "T-B", "--ai-cost", "0.03",
         "--hours", "1.0", "--action", "0", "--avoided", "600", "--source", "billing",
         "--outdir", str(costdir)])
    out = run([PY, "tools/cost_monthly.py", "--file", str(costdir / "cost.jsonl"),
               "--month", "2026-10", "--outdir", str(costdir)])
    assert (costdir / "cost-report-2026-10.md").is_file(), "monthly report missing"

    report = TMP / "report.json"
    report.write_text(json.dumps({
        "goal": "B线补料率异常归因", "is_demo": False,
        "finding": [{"factor": "模具状态", "confidence": 0.63, "evidence": ["x"]}],
        "cannot_attribute": ["信息员换人"],
        "recommendation": [{"action": "补保养", "roi": {"cost": "¥500", "avoided": "¥2000", "anchor": "a"}, "irreversibility": "reversible"}],
        "decision_log_entry": {"expected_result": "T+7 ≤5.40%"},
    }, ensure_ascii=False), encoding="utf-8")
    out = run([PY, "tools/wecom_notify.py", "--kind", "delivery", "--report", str(report),
               "--cost", "AI ¥0.02 / 人工节省 2.5h", "--dry-run"])
    assert "主因" in out and "模具状态" in out, "wecom report mode failed"

    print(json.dumps({"ok": True, "tmp": str(TMP)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
