#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""J-05 状态机回归：不可签产物不得停在待签（status 必须 blocked）。

历史缺陷（2026-11-02 逻辑校验发现）：
  - `orchestration/assemble_report.py` 只在 status.json **已存在**时才写 blocked，
    而 run_task 走的是「先 assemble 后 set_status」——文件还没生成，整条跳过；
  - 随后 `tools/run_task.py` 无条件 `set_status(assembled)`，把「不许签」的信号擦掉。
结果：评分卡 force_manual_review 的报告，状态显示「待签字」，与 F1/J-05 相悖。
"""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
PY = sys.executable


def run_task(outdir: pathlib.Path, workflow: pathlib.Path, task_id: str) -> tuple[int, str]:
    cmd = [PY, "-B", str(ROOT / "tools" / "run_task.py"),
           "--task-id", task_id,
           "--workflow-result", str(workflow),
           "--outdir", str(outdir),
           "--demo"]
    p = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=180)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def main() -> int:
    fails: list[str] = []
    demo_result = ROOT / "runs" / "demo-result-2026-10-19.json"
    if not demo_result.is_file():
        print(f"[SKIP] 缺 {demo_result}（demo 结果不在仓内）")
        return 0

    with tempfile.TemporaryDirectory() as tmp:
        outdir = pathlib.Path(tmp) / "runs" / "T-20261102-BLK"
        rc, out = run_task(outdir, demo_result, "T-20261102-BLK")
        status_path = outdir / "status.json"
        if not status_path.is_file():
            fails.append("run_task 未产出 status.json")
            print(f"[FAIL] 无 status.json（rc={rc}）：{out[-500:]}")
        else:
            st = json.loads(status_path.read_text(encoding="utf-8-sig"))
            report = json.loads((outdir / "report.json").read_text(encoding="utf-8-sig"))
            verdict = str((report.get("scorecard") or {}).get("verdict") or "")
            print(f"[info] verdict={verdict!r} state={st.get('state')!r} step={st.get('step')!r}")
            if verdict and verdict != "output":
                if st.get("state") != "blocked":
                    fails.append(f"不可签报告 state={st.get('state')!r}（应为 blocked）")
                if str(st.get("step") or "") != "blocked_g25":
                    fails.append(f"step={st.get('step')!r}（应为 blocked_g25）")

    print(f"\n{1 - len(fails)}/1 通过")
    if fails:
        for f in fails:
            print(f"  - FAILED: {f}")
        print("RESULT: FAIL")
        return 1
    print("RESULT: PASS（不可签 → blocked，不再停在待签）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
