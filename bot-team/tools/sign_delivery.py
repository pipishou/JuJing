#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""交付卡签字（持久化，不是 UI toast）。

真实交付必须能证明"谁在何时签了哪份报告"：
  - 报告 SHA256（内容不可抵赖）
  - 签字人 / 时间 / decision_id / 是否 demo
  - 默认拒绝给 demo 报告签字（必须 --allow-demo）

用法：
  python tools/sign_delivery.py runs/task-B/report.json --signer "张伟" --out runs/task-B/signature.json
  python tools/sign_delivery.py --verify runs/task-B/signature.json --report runs/task-B/report.json
"""
import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone, timedelta


def sha256_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("report", nargs="?", default="")
    ap.add_argument("--report", dest="report_opt", default="")
    ap.add_argument("--signer", default=os.environ.get("BOT_SIGNER", "老板"), help="签字人（默认 老板，可用 env BOT_SIGNER 覆盖）")
    ap.add_argument("--out", default="")
    ap.add_argument("--allow-demo", action="store_true")
    ap.add_argument("--verify", default="")
    args = ap.parse_args()

    if args.verify:
        sig = json.loads(open(args.verify, encoding="utf-8-sig").read())
        report = args.report_opt or args.report
        if not report:
            print("verify 需要 --report", file=sys.stderr)
            return 2
        actual = sha256_file(report)
        ok = actual == sig.get("report_sha256")
        print(json.dumps({"ok": ok, "expected": sig.get("report_sha256"), "actual": actual,
                          "signer": sig.get("signer"), "signed_at": sig.get("signed_at")}, ensure_ascii=False, indent=2))
        return 0 if ok else 1

    report = args.report_opt or args.report
    if not report or not args.signer:
        print("usage: sign_delivery.py <report.json> --signer NAME [--out signature.json]", file=sys.stderr)
        return 2
    doc = json.loads(open(report, encoding="utf-8-sig").read())
    if doc.get("is_demo") and not args.allow_demo:
        print("[ERROR] 拒绝给 demo 报告签字（防 demo 冒充真实）；确需演练请加 --allow-demo", file=sys.stderr)
        return 2
    tz = timezone(timedelta(hours=8))
    sig = {
        "report": report,
        "report_sha256": sha256_file(report),
        "signer": args.signer,
        "signed_at": datetime.now(tz).isoformat(timespec="seconds"),
        "decision_id": (doc.get("decision_log_entry") or {}).get("id"),
        "task_id": doc.get("task_id"),
        "is_demo": doc.get("is_demo"),
        "goal": doc.get("goal"),
    }
    out = args.out or (report.rsplit(".", 1)[0] + ".signature.json")
    open(out, "w", encoding="utf-8").write(json.dumps(sig, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"ok": True, "signature": out, "sha256": sig["report_sha256"], "signer": args.signer}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

