#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""真实模式端到端冒烟测试（合成数据，不依赖真实工厂数据）。

覆盖：
  CSV(11 列) --field-map--> T/T30 JSON
  -> check_data_contract --strict（应 normal）
  -> assemble_report --real（is_demo=false）
  -> validate_report --require-real（应 PASS）
  -> sign_delivery（应成功，verify 对账）

用法：python tests/real_mode_smoke.py
退出码：0 全绿；1 任一步失败。
"""
import datetime as dt
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable
TMP = Path(tempfile.mkdtemp(prefix="bot-real-smoke-"))


def run(name, cmd):
    p = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", errors="replace")
    print(f"[{name}] exit={p.returncode}")
    if p.returncode != 0:
        print(p.stdout[-1500:])
        print(p.stderr[-1500:], file=sys.stderr)
        raise SystemExit(1)
    return p


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass
    start = dt.date(2026, 9, 5)
    rows = []
    for i in range(37):
        d = start + dt.timedelta(days=i)
        rows.append({
            "日期": d.isoformat(), "产线": "B", "补料率": round(5.0 + 0.06 * i, 2), "产量": 1000 + i,
            "模具状态": "normal", "保养记录": "[]", "班组": "甲班", "原料批次": "RM-2026",
            "环境温湿度": '{"temp_c":24.0,"humidity_pct":55.0}', "换模记录": "[]",
            "停机记录": "[]", "信息员": "李强",
        })
    csv = TMP / "task.csv"
    pd.DataFrame(rows).to_csv(csv, index=False, encoding="utf-8-sig")
    print(f"tmp={TMP}")

    outdir = TMP / "task-B"
    run("ingest", [PY, "tools/ingest_task_data.py", "--input", str(csv), "--line", "B",
                   "--t-start", "2026-10-05", "--t-end", "2026-10-11",
                   "--base-start", "2026-09-05", "--base-end", "2026-10-04",
                   "--outdir", str(outdir), "--field-map", "templates/field-map.example.yaml"])
    run("contract", [PY, "tools/check_data_contract.py", "--t", str(outdir / "material_refill_T.json"),
                     "--t30", str(outdir / "material_refill_T30.json"), "--strict"])

    result = {
        "goal": "合成数据：B 线补料率异常归因（非真实工厂数据）",
        "attribution": {
            "finding": [{"factor": "模具状态", "confidence": 0.63, "evidence": ["合成数据：M-03 状态从 normal 变为 wear"]}],
            "cannot_attribute": ["合成数据：信息员换人偏差未建模"],
            "scorecard": {"data_completeness": 4, "timeliness": 4, "significance": 5,
                          "exclusivity": 3, "history": 3, "human_ai_consistency": 0},
        },
        "advice": {"recommendation": [{"action": "补保养 M-03（合成）",
                                        "roi": {"cost": "¥500", "avoided": "¥2000", "anchor": "保养<换模+停机"},
                                        "irreversibility": "reversible"}]},
    }
    result_path = TMP / "workflow-result.json"
    result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    report = TMP / "report.json"
    run("assemble-real", [PY, "orchestration/assemble_report.py", str(result_path), str(report),
                          "--real", "--task-id", "T-SMOKE-B", "--context", "B线补料率合成数据",
                          "--data-refs", f"{outdir}/material_refill_T.json,{outdir}/material_refill_T30.json",
                          "--expected", "T+7 ≤5.40%",
                          "--records-dir", str(TMP / "loop"),
                          "--analysis-json", "team/loop/example-analysis.json",
                          "--goal-json", "team/goals/example-goal.json",
                          "--cost-json", "team/cost/example-cost.json"])
    run("validate-real", [PY, "tests/validate_report.py", str(report), "--require-real"])
    run("validate-loop", [PY, "tools/validate_loop.py", "--task-dir", str(TMP / "loop"), "--require-complete"])
    sig = TMP / "signature.json"
    run("sign", [PY, "tools/sign_delivery.py", str(report), "--signer", "smoke-test", "--out", str(sig)])
    run("verify", [PY, "tools/sign_delivery.py", "--verify", str(sig), "--report", str(report)])

    doc = json.loads(report.read_text(encoding="utf-8"))
    assert doc.get("is_demo") is False, "real report must not be demo"
    assert doc.get("task_id") == "T-SMOKE-B"
    print(json.dumps({"ok": True, "tmp": str(TMP), "report": str(report), "signature": str(sig)}, ensure_ascii=False, indent=2))
    shutil.rmtree(TMP, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
