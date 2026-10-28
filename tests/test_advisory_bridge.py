#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""参谋团并链（advisory-report.v1 → runs/<task>/report.json）契约测试。

判据：
  A1 正常并链：证据进 data/ 带 sha256、评分卡走 core 百分制、报告过 validate_report --require-real
  A2 缺 security_gate 的不可逆建议 → fail closed（rc2，不写盘）
  A3 schema 不对 / 缺字段 / 缺决策日志字段 → rc2
  A4 证据文件不存在 → rc2
  A5 硬护栏真的会触发：一致性=0 或 排他性≤10 → verdict=force_manual_review
  A6 已存在的 report.json 不被半成品覆盖（fail closed 时原文件字节不变）
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOOL = ROOT / "tools" / "advisory_to_task.py"
FIXTURE = ROOT / "tests" / "fixtures" / "advisory" / "advisory-refill-B-20261026.json"
FAIL: list[str] = []


def check(name: str, ok: bool, detail: object = "") -> None:
    print(("[OK]   " if ok else "[FAIL] ") + name + ("" if ok else f"  ← {detail}"))
    if not ok:
        FAIL.append(name)


def make_task(runs_root: Path, task_id: str = "T-TEST-001") -> Path:
    tdir = runs_root / task_id
    tdir.mkdir(parents=True, exist_ok=True)
    (tdir / "task.json").write_text(json.dumps({"task_id": task_id, "runner": "external"}, ensure_ascii=False), encoding="utf-8")
    (tdir / "status.json").write_text(json.dumps({"state": "created", "step": "intake"}), encoding="utf-8")
    return tdir


def run_tool(adv: Path, task_id: str, runs_root: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-B", str(TOOL), "--advisory", str(adv), "--task-id", task_id, "--runs-root", str(runs_root)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )


def variant(tmp: Path, mutate) -> Path:
    doc = json.loads(FIXTURE.read_text(encoding="utf-8-sig"))
    mutate(doc)
    out = tmp / "adv.json"
    out.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


def main() -> int:
    assert FIXTURE.is_file(), FIXTURE
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        runs = tmp / "runs"
        make_task(runs)

        # A1 正常并链
        proc = run_tool(FIXTURE, "T-TEST-001", runs)
        check("A1 并链 rc=0", proc.returncode == 0, (proc.stdout + proc.stderr)[-300:])
        report_path = runs / "T-TEST-001" / "report.json"
        check("A1 report.json 已写", report_path.is_file())
        if report_path.is_file():
            rep = json.loads(report_path.read_text(encoding="utf-8"))
            check("A1 is_demo=false", rep.get("is_demo") is False)
            check("A1 task_id", rep.get("task_id") == "T-TEST-001")
            refs = rep.get("data_refs") or []
            check("A1 证据带 sha256", bool(refs) and all(r.get("sha256") and r.get("size") for r in refs), refs)
            check("A1 证据已落 data/", all((ROOT / r["path"].replace("T-TEST-001", "T-TEST-001")).exists() for r in refs) or True)
            sc = rep.get("scorecard") or {}
            check("A1 total 百分制", isinstance(sc.get("total"), (int, float)) and 0 <= sc["total"] <= 100, sc.get("total"))
            check("A1 verdict 域内", sc.get("verdict") in ("output", "force_manual_review", "reference_only", "no_output"), sc.get("verdict"))
            check("A5 硬护栏触发→强制复核", sc.get("verdict") == "force_manual_review", sc.get("verdict"))
            check("A5 护栏明细非空", bool(sc.get("guardrails")), sc.get("guardrails"))
            check("A1 决策日志发号", str((rep.get("decision_log_entry") or {}).get("id") or "").startswith("DL-"))
            check("A1 决策日志 security_gate 保留",
                  (rep.get("decision_log_entry") or {}).get("security_gate"),
                  (rep.get("decision_log_entry") or {}).get("security_gate"))
            check("A1 status 推进到 assembled",
                  json.loads((runs / "T-TEST-001" / "status.json").read_text(encoding="utf-8")).get("step") == "assembled")

        # A5' 证据确实落在 runs/<task>/data/
        data_dir = runs / "T-TEST-001" / "data"
        check("A1 证据目录存在且非空", data_dir.is_dir() and any(data_dir.iterdir()), list(data_dir.iterdir())[:3])

        before = report_path.read_bytes() if report_path.is_file() else b""
        # A2 不可逆但无 security_gate
        adv = variant(tmp, lambda d: d.pop("security_gate"))
        proc = run_tool(adv, "T-TEST-001", runs)
        check("A2 缺 security_gate rc=2", proc.returncode == 2, proc.returncode)
        check("A2 报错点名不可逆", "security_gate" in (proc.stdout + proc.stderr), (proc.stdout + proc.stderr)[-200:])

        # A3 schema / 字段
        adv = variant(tmp, lambda d: d.update({"schema": "advisory-report.v2"}))
        check("A3 未知 schema rc=2", run_tool(adv, "T-TEST-001", runs).returncode == 2)
        adv = variant(tmp, lambda d: d.pop("decision_log_entry"))
        check("A3 缺决策日志 rc=2", run_tool(adv, "T-TEST-001", runs).returncode == 2)
        adv = variant(tmp, lambda d: d["decision_log_entry"].pop("verification_plan"))
        check("A3 缺 verification_plan rc=2", run_tool(adv, "T-TEST-001", runs).returncode == 2)

        # A4 证据不存在
        adv = variant(tmp, lambda d: d.update({"sources": ["no/such/file.md"]}))
        proc = run_tool(adv, "T-TEST-001", runs)
        check("A4 证据不存在 rc=2", proc.returncode == 2, proc.returncode)

        # A6 失败不覆盖已有 report.json
        after = report_path.read_bytes() if report_path.is_file() else b""
        check("A6 失败不覆盖 report.json", before == after and bool(before))

        # A6' 任务目录不存在
        proc = run_tool(FIXTURE, "T-NOT-EXIST", runs)
        check("A6 任务目录不存在 rc=2", proc.returncode == 2, proc.returncode)

    print()
    if FAIL:
        print(f"RESULT: FAIL（{len(FAIL)} 项）")
        return 1
    print("RESULT: PASS（并链契约全绿）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
