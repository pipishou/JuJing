#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""G4 T+7 催办：扫描 runs/<id>/closure.json（verdict=unverified 且 due<=today）
→ 4 合 1 网关推 mo_p2p 催办卡；防重＝runs/<id>/.nudged-<due> 标记文件。

判据与 tools/review_due.py 对齐（verdict=unverified ∧ due<=today），
但只扫 runs/*/ 一层（review_due.py 的 rglob 会重复命中 loop/ 子目录）。
verdict 在回填后变 hit/miss/partial，判据自然排除已回填任务。

用法：
  python tools/review_nudge.py --root runs --dry          # 只打印不推送
  python tools/review_nudge.py --root runs                # 真扫真推
计划任务：tools/install_review_task.ps1（每日 09:00 注册本脚本）
"""
import argparse
import json
import os
import subprocess
import sys
from datetime import date
from pathlib import Path

MARK_PREFIX = ".nudged-"


def _log(message):
    try:
        print("[review-nudge] " + str(message), file=sys.stderr, flush=True)
    except Exception:  # noqa: BLE001
        pass


def find_gateway():
    """lark_send.py 定位：env LARK_SEND 优先，默认 bot-2/tools/lark_send.py。"""
    env_path = os.environ.get("LARK_SEND")
    candidates = [Path(env_path)] if env_path else []
    # 本脚本 = bot-team/tools/review_nudge.py → parents[3] = bot-2
    candidates.append(Path(__file__).resolve().parents[3] / "tools" / "lark_send.py")
    for cand in candidates:
        if cand.is_file():
            return cand
    return None


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    ap = argparse.ArgumentParser(description="T+7 回看到期催办（飞书）")
    # 默认 root＝脚本上一级的 runs/（绝对路径，对计划任务 cwd=system32 免疫）
    ap.add_argument("--root", default=str(Path(__file__).resolve().parents[1] / "runs"))
    ap.add_argument("--today", default=date.today().isoformat())
    ap.add_argument("--dry", action="store_true", help="只扫描打印，不推送不落标记")
    args = ap.parse_args()
    today = date.fromisoformat(args.today)
    root = Path(args.root)
    due_list = []
    for clo in sorted(root.glob("*/closure.json")):
        tdir = clo.parent
        try:
            closure = json.loads(clo.read_text(encoding="utf-8-sig"))
        except Exception:  # noqa: BLE001
            continue
        if closure.get("verdict") != "unverified":
            continue
        try:
            due = date.fromisoformat(str(closure.get("due") or ""))
        except ValueError:
            continue
        if due > today:
            continue
        due_list.append((tdir, closure, due))
    if not due_list:
        print(json.dumps({"ok": True, "due": 0, "message": "no review due"},
                         ensure_ascii=False))
        return 0
    gateway = None if args.dry else find_gateway()
    if not args.dry and gateway is None:
        _log("网关不存在（设 LARK_SEND 指定 lark_send.py），放弃推送")
        return 2
    for tdir, closure, due in due_list:
        task_id = tdir.name
        due_iso = due.isoformat()
        mark = tdir / (MARK_PREFIX + due_iso)
        if mark.exists():
            _log(f"{task_id} 已催办过（{due_iso}），跳过")
            continue
        decision = {}
        for dpath in (tdir / "decision.json", tdir / "loop" / "decision.json"):
            try:
                decision = json.loads(dpath.read_text(encoding="utf-8-sig"))
                break
            except Exception:  # noqa: BLE001
                continue
        question = str(decision.get("question")
                       or (closure.get("metric_result") or {}).get("metric") or "").strip()
        lines = [
            "**T+7 回看催办**",
            f"任务：{task_id} ｜ 回看到期：{due_iso}（verdict 仍为 unverified，请回填）",
        ]
        if question:
            lines.append(f"议题：{question[:80]}")
        lines.append("回填：UI 任务详情 outcome 表单，或 "
                     f"POST /api/tasks/{task_id}/outcome（verdict ∈ hit/miss/partial）")
        if args.dry:
            print(f"[dry] {task_id} due={due_iso} question={question[:40]}")
            continue
        cmd = [sys.executable, "-B", str(gateway), "--purpose", "mo_p2p",
               "--markdown", "\n".join(lines), "--send",
               "--idempotency-key", f"nudge-{task_id}-{due_iso}"]
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=60)
        if proc.returncode == 0:
            mark.write_text(json.dumps({"nudged_at": today.isoformat(),
                                        "gateway": str(gateway)}, ensure_ascii=False),
                            encoding="utf-8")
            _log(f"{task_id} 催办已推送（due={due_iso}）")
        else:
            _log(f"{task_id} 推送失败 rc={proc.returncode}: "
                 f"{((proc.stdout or '') + (proc.stderr or ''))[:200]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
