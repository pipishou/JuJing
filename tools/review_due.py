#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""T+7 回看到期扫描（GitHub Issues / 本机）。

扫描任务目录里的 loop/closure.json + decision.json：
  - verdict=unverified 且 due <= today → 列入回看清单
  - --create-issue 用 gh 创建一条汇总 issue（无到期则不创建）
  - 默认 dry-run 只打印 markdown

用法：
  python tools/review_due.py --root runs
  python tools/review_due.py --root runs --create-issue
"""
import argparse
import json
import subprocess
import sys
from datetime import date
from pathlib import Path


def readj(p):
    return json.loads(Path(p).read_text(encoding="utf-8-sig"))


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="runs")
    ap.add_argument("--today", default=date.today().isoformat())
    ap.add_argument("--create-issue", action="store_true")
    ap.add_argument("--out", default="", help="写本地回看清单 Markdown（本机计划任务用）")
    ap.add_argument("--repo", default="")
    args = ap.parse_args()
    today = date.fromisoformat(args.today)
    root = Path(args.root)
    due = []
    legacy = []
    seen = set()
    for clo in root.rglob("closure.json"):
        d = clo.parent
        dec = d / "decision.json"
        if not dec.is_file():
            continue
        try:
            c, de = readj(clo), readj(dec)
        except Exception:
            continue
        # F11：demo 报告不进真实回看队列
        rep = d.parent / "report.json"
        try:
            _rj = readj(rep) if rep.is_file() else {}
        except Exception:
            _rj = {}
        # F22: demo never enters real queue; legacy/test runs lacking contract fields are quarantined
        if _rj.get("is_demo") is not False:
            legacy.append(de.get("id") or str(d))
            continue
        if not all(k in _rj for k in ("verdict_one_line", "sign_scope", "evidence_tier")):
            legacy.append(de.get("id") or str(d))
            continue
        if c.get("verdict") != "unverified":
            continue
        # F11：同一决策多份 loop 只计一次
        if de.get("id") in seen:
            continue
        seen.add(de.get("id"))
        try:
            due_date = date.fromisoformat(c.get("due") or "")
        except ValueError:
            continue
        if due_date <= today:
            due.append({
                "decision_id": de.get("id"), "question": de.get("question"),
                "due": c.get("due"), "expected": de.get("expected_outcome"),
                "task_dir": str(d), "review_days": 7,
            })
    _legacy_u = list(dict.fromkeys(legacy))
    if _legacy_u:
        print(f"[review_due] quarantined {len(_legacy_u)} legacy/test run(s) missing contract fields: "
              + ", ".join(map(str, _legacy_u[:8])))
    due.sort(key=lambda x: (x.get("due") or "", x.get("decision_id") or ""))
    if not due:
        print(json.dumps({"ok": True, "due": 0, "message": "no review due"}, ensure_ascii=False))
        return 0
    lines = ["# T+7 回看到期", "", f"> 截至 {today.isoformat()}，共 {len(due)} 条待回填。", "",
             "| 决策 | 问题 | 到期 | 预期 |", "|---|---|---|---|"]
    for x in due:
        lines.append(f"| {x['decision_id']} | {x['question']} | {x['due']} | {x['expected']} |")
    lines += ["", "回填：`closure.json` 的 actual_outcome/metric_result/verdict/lessons/action；verdict∈hit/miss/partial。"]
    body = "\n".join(lines)
    print(body)
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(body + "\n", encoding="utf-8")
        print(f"[OK] local review list: {out}")
    if args.create_issue:
        cmd = ["gh", "issue", "create", "--title", f"T+7 回看到期（{len(due)} 条）", "--body", body]
        if args.repo:
            cmd += ["--repo", args.repo]
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
        except FileNotFoundError:
            print("[ERROR] 本机未安装 gh；请用 --out 生成本地回看清单，或安装 gh", file=sys.stderr)
            return 2
        print(p.stdout.strip())
        if p.returncode != 0:
            print(p.stderr, file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
