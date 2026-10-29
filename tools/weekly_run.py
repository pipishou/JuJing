#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""周汇总一键：算窗口 → 建周卡单 → 并链出报告 → 封存 → 等人签字。

默认窗口＝上周一~昨天；固定节奏见 server/install_weekly_task.ps1（周一 15:00）。
产物：runs/<task_id>/report.json（card_kind=weekly，已封存待签）+ loop/ 四卡。
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def default_window(today: date | None = None) -> tuple[str, str]:
    today = today or date.today()
    monday = today - timedelta(days=today.weekday())
    start = monday - timedelta(days=7)
    end = monday - timedelta(days=1)
    return start.isoformat(), end.isoformat()


def post(server: str, path: str, payload: dict, timeout: float = 30) -> dict:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        server.rstrip("/") + path, data=body, method="POST",
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"[ERROR] POST {path} → HTTP {exc.code}: {exc.read().decode('utf-8', 'replace')[:300]}")


def run(cmd: list[str]) -> None:
    proc = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        tail = "\n".join((proc.stdout or "").splitlines()[-6:] + (proc.stderr or "").splitlines()[-4:])
        raise SystemExit(f"[ERROR] {Path(cmd[1]).name} 失败：\n{tail}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="周汇总一键出卡（待签字）")
    ap.add_argument("--server", default="http://127.0.0.1:8787")
    ap.add_argument("--events", default=str(ROOT / "runtime" / "events.jsonl"))
    ap.add_argument("--workitems", default=str(ROOT / "runtime" / "workitems" / "current.json"))
    ap.add_argument("--runs-root", default=str(ROOT / "runs"))
    ap.add_argument("--out", default=str(ROOT / "runtime" / "weekly" / "advisory.json"))
    ap.add_argument("--start", default="")
    ap.add_argument("--end", default="")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-seal", action="store_true")
    args = ap.parse_args(argv)

    d_start, d_end = default_window()
    start, end = args.start or d_start, args.end or d_end
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)

    run([sys.executable, "-B", str(ROOT / "tools" / "weekly_digest.py"),
         "--out", str(out), "--events", args.events, "--workitems", args.workitems,
         "--start", start, "--end", end])
    advisory = json.loads(out.read_text(encoding="utf-8"))
    summary = {"period": advisory.get("period"), "metrics": advisory.get("metrics"), "advisory": str(out)}
    if args.dry_run:
        print(json.dumps({"ok": True, "dry_run": True, **summary}, ensure_ascii=False))
        return 0

    created = post(args.server, "/api/tasks", {
        "goal": advisory.get("goal") or f"周汇总 {start}~{end}",
        "card_kind": "weekly",
        "source": "weekly-digest",
        "runner": "external",
        "scene_id": "scene-workitems-weekly",
        "metric_id": "overdue_rate",
        "period": advisory.get("period"),
    })
    task_id = created.get("task_id") or created.get("id")
    if not task_id:
        raise SystemExit(f"[ERROR] 建单未返回 task_id：{json.dumps(created, ensure_ascii=False)[:200]}")

    run([sys.executable, "-B", str(ROOT / "tools" / "advisory_to_task.py"),
         "--advisory", str(out), "--task-id", task_id, "--runs-root", args.runs_root])

    runs_root = Path(args.runs_root).resolve()
    if not args.no_seal:
        run([sys.executable, "-B", str(ROOT / "tools" / "seal_report.py"),
             str(runs_root / task_id / "report.json"), "--base", str(runs_root.parent),
             "--note", f"周汇总 {start}~{end}"])

    print(json.dumps({
        "ok": True, "task_id": task_id, "server": args.server.rstrip("/"),
        "period": summary["period"], "metrics": summary["metrics"],
        "report": str(runs_root / task_id / "report.json"),
        "next": f"签字：POST /api/tasks/{task_id}/sign ；终态：/finalize",
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
