#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""参谋团产物 → bot-team 任务报告（甲方案 A「并链」的桥）。

问题：参谋团（Octop 专家市场）跑完只落 `.octop/agents/<id>/generated/*.md`——
不进 `runs/`、不签字、不回看，等于另一条链路。

本工具把参谋团产出的 `advisory-report.v1` JSON 变成标准 `report.json`：
证据拷进 `runs/<task>/data/` 并算 sha256（provenance）、评分卡走
`core.report_contract`（0–5 或 100 分制都吃）、不可逆建议必须有 securityGate
（缺了就 fail closed）、决策日志发号后落账，最后用 `validate_report.py
--require-real` 自检；过不了就不写盘。

用法：
    python tools/advisory_to_task.py --advisory <v1.json> [--task-id T-xxx] [--dry-run]
    python tools/advisory_to_task.py --advisory x.json --task-id T-x --records-dir ../records
参数缺省时 task 目录必须已由 `POST /api/tasks` 创建（编号由服务发，避免撞号）。

advisory-report.v1 必填：goal / finding / cannot_attribute / scorecard /
recommendation / decision_log_entry{context,advice,expected_result,verification_plan}；
可选：crew_id / sources[{path}] / data_refs / security_gate / attribution_method。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent          # JuJing/bot-team
REQUIRED = ("goal", "finding", "cannot_attribute", "scorecard", "recommendation", "decision_log_entry")
DL_REQUIRED = ("context", "advice", "expected_result", "verification_plan")
SIX = ("data_completeness", "timeliness", "significance", "exclusivity", "history", "human_ai_consistency")

sys.path.insert(0, str(ROOT))
from core import gates, ids, report_contract  # noqa: E402
from core import pack as pack_mod  # noqa: E402


class BridgeError(RuntimeError):
    """输入不满足契约（fail closed，不写盘）。"""


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def resolve(path_like: str) -> Path:
    p = Path(path_like)
    return p if p.is_absolute() else (ROOT / p)


def check_input(doc: dict) -> None:
    if str(doc.get("schema") or "") != "advisory-report.v1":
        raise BridgeError(f"schema 必须是 advisory-report.v1，实际 {doc.get('schema')!r}")
    missing = [k for k in REQUIRED if not doc.get(k)]
    if missing:
        raise BridgeError(f"缺必填字段：{', '.join(missing)}")
    dl = doc.get("decision_log_entry") or {}
    missing_dl = [k for k in DL_REQUIRED if not dl.get(k)]
    if missing_dl:
        raise BridgeError(f"decision_log_entry 缺：{', '.join(missing_dl)}")
    sc = doc.get("scorecard") or {}
    missing_sc = [k for k in SIX if sc.get(k) is None]
    if missing_sc:
        raise BridgeError(f"scorecard 缺维度：{', '.join(missing_sc)}")
    irreversible = [r.get("action") for r in doc["recommendation"] if r.get("irreversibility") == "irreversible"]
    if irreversible and not doc.get("security_gate"):
        raise BridgeError(
            "含不可逆建议但没有 security_gate（必须由人工确认后填入）：" + "；".join(str(a)[:40] for a in irreversible)
        )


def stage_evidence(task_dir: Path, sources: list[str], base: Path) -> list[dict]:
    """证据拷进 runs/<task>/data/ 并计算 sha256（refs 路径相对 base，供 provenance 校验）。"""
    data_dir = task_dir / "data"
    refs = []
    for item in sources:
        src = resolve(item)
        if not src.is_file():
            raise BridgeError(f"证据文件不存在：{item}")
        data_dir.mkdir(parents=True, exist_ok=True)
        dst = data_dir / src.name
        if not (dst.is_file() and sha256_file(dst) == sha256_file(src)):
            shutil.copy2(src, dst)
        try:
            rel = dst.relative_to(base).as_posix()
        except ValueError:
            rel = dst.as_posix()
        refs.append(
            {
                "path": rel,
                "sha256": sha256_file(dst),
                "size": dst.stat().st_size,
            }
        )
    return refs


def build_report(doc: dict, task_id: str, task_dir: Path, pack: dict, refs: list[dict]) -> dict:
    holder = {"scorecard": dict(doc["scorecard"])}
    report_contract.apply(holder, pack=pack)   # 原地补 total/verdict/label/guardrails/points
    scorecard = holder["scorecard"]
    dl_raw = dict(doc["decision_log_entry"])
    dl_id = ids.allocate(task_dir, "report", task_id)
    decision_log = {
        "id": dl_id,
        "date": datetime.now().strftime("%Y-%m-%d"),
        "actor": doc.get("crew_id") or doc.get("actor") or "manufacturing-advisory-board",
        "task_id": task_id,
        "context": dl_raw["context"],
        "advice": dl_raw["advice"],
        "expected_result": dl_raw["expected_result"],
        "verification_plan": dl_raw["verification_plan"],
        "attribution_method": doc.get("attribution_method") or "MANUAL",
        "data_refs": refs,   # 与顶层同结构（path/sha256/size）：两处都过 provenance 校验
        "security_gate": doc.get("security_gate") or {},
    }
    report = {
        "schema": "report.v1",
        "generated_at": now_iso(),
        "is_demo": False,
        "task_id": task_id,
        "goal": doc["goal"],
        "pack": pack.get("pack_id") or pack.get("id") or "manufacturing",
        "roi_anchors": (pack.get("roi_anchors") or {}),
        "finding": doc["finding"],
        "cannot_attribute": doc["cannot_attribute"],
        "data_refs": refs,
        "scorecard": scorecard,
        "recommendation": doc["recommendation"],
        "security_gate_requested": any(r.get("irreversibility") == "irreversible" for r in doc["recommendation"]),
        "decision_log_entry": decision_log,
        "role_coverage": doc.get("role_coverage") or ["karpathy", "manufacturing-ops-expert", "content-operator", "host"],
        "source": doc.get("source") or "advisory-report.v1",
    }
    if report["security_gate_requested"]:
        report["security_gate"] = doc.get("security_gate") or {}
    return report


def write_outputs(task_dir: Path, report: dict, task_id: str) -> None:
    import os

    target = task_dir / "report.json"
    tmp = target.with_name("report.json.tmp")
    tmp.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, target)
    status_path = task_dir / "status.json"
    status = {}
    if status_path.is_file():
        status = json.loads(status_path.read_text(encoding="utf-8-sig") or "{}")
    status.update({"state": "assembled", "step": "assembled", "updated_at": now_iso(),
                   "updated_by": "advisory_to_task"})
    tmp = status_path.with_name("status.json.tmp")
    tmp.write_text(json.dumps(status, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, status_path)
    with (task_dir / "run.log").open("a", encoding="utf-8") as fh:
        fh.write(f"{now_iso()} phase=assemble role=host kind=status line=参谋团产物已并链（advisory_to_task）\n")


def selfcheck(task_dir: Path, base: Path) -> tuple[bool, str]:
    proc = subprocess.run(
        [sys.executable, "-B", str(ROOT / "tests" / "validate_report.py"),
         str(task_dir / "report.json"), "--require-real", "--base", str(base)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    tail = [ln for ln in (proc.stdout or "").splitlines() if ln.startswith(("[FAIL]", "RESULT:"))]
    return proc.returncode == 0, "\n".join(tail[-8:])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="参谋团产物 → 标准报告（并链）")
    ap.add_argument("--advisory", required=True, help="advisory-report.v1 JSON")
    ap.add_argument("--task-id", help="已存在的任务 id（缺省取 JSON 里的 task_id）")
    ap.add_argument("--runs-root", default=str(ROOT / "runs"), help="runs 目录（测试用）")
    ap.add_argument("--skip-sources", action="store_true", help="不拷证据文件，只用 JSON 里的 data_refs")
    ap.add_argument("--dry-run", action="store_true", help="只打印将写入的报告，不落盘")
    args = ap.parse_args(argv)

    adv_path = Path(args.advisory)
    doc = json.loads(adv_path.read_text(encoding="utf-8-sig"))
    task_id = args.task_id or doc.get("task_id")
    if not task_id:
        print("[ERROR] 缺 --task-id（或 advisory JSON 里的 task_id）；任务必须由 POST /api/tasks 先建", file=sys.stderr)
        return 2
    task_dir = Path(args.runs_root) / task_id
    if not task_dir.is_dir():
        print(f"[ERROR] 任务目录不存在：{task_dir}（先用 POST /api/tasks 建任务）", file=sys.stderr)
        return 2

    try:
        check_input(doc)
        pack = pack_mod.load()
        runs_root = Path(args.runs_root).resolve()
        base = runs_root.parent if runs_root.parent != ROOT else ROOT
        refs = []
        if not args.skip_sources:
            refs = stage_evidence(task_dir, list(doc.get("sources") or []), base)
        report = build_report(doc, task_id, task_dir, pack, refs)
        errs = gates.real_report_errors(report, base)
        if errs:
            raise BridgeError("可信门未过：" + "；".join(str(e) for e in errs))
    except BridgeError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 2

    if args.dry_run:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        print("\n[dry-run] 未写盘")
        return 0

    write_outputs(task_dir, report, task_id)
    ok, detail = selfcheck(task_dir, base)
    print(f"[OK] 已写 {task_dir / 'report.json'}")
    print(f"[{'OK' if ok else 'FAIL'}] validate_report --require-real")
    if detail:
        print(detail)
    if not ok:
        return 1
    print(f"[NEXT] 签字：POST /api/tasks/{task_id}/sign ；终态：POST /api/tasks/{task_id}/finalize")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
