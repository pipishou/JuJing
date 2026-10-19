#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""签字收口：过「可信门」后把 decision.json 更新为 approved（A3）。

M1 修复（审计 P0-1：任意签名可 approved）：
  - 不再只看 report_sha256：必须同时满足 core.gates.check_finalize 全部判据
     · demo 报告不得走真实交付
     · decision.status 必须是 proposed（rejected/approved 不得再收口）
     · decision.task_id 必须等于 report.task_id（跨任务混用拒绝）
     · signature.signer 非空（只有 report_sha256、没有签字人 = 伪签名）
     · 签字人白名单 fail closed：--allow-signers > env BOT_SIGNERS > 旧名 BOT_SIGNER_ALLOWLIST；
    三者都没有 → 拒绝（未配置 signer 白名单）
     · securityGate 必须 confirmed（pending_user_confirmation 拒绝）
  - 任一条不满足 → 非 0 退出且**不修改** decision.json
  - 原子写回；approval.by == 传入 signer

用法：
  python tools/finalize_delivery.py --report runs/task-B/report.json \
      --loop-dir runs/task-B/loop --signature runs/task-B/report.signature.json
"""
import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import fsutil, gates  # noqa: E402


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
    except Exception:  # noqa: BLE001
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", required=True)
    ap.add_argument("--loop-dir", required=True)
    ap.add_argument("--signature", required=True)
    ap.add_argument("--allow-signers", default="", help="逗号分隔的合法签字人白名单（优先级高于 env BOT_SIGNERS）")
    ap.add_argument("--sha256", default="",
                    help="兼容参数：仅与现算哈希做一致性校验，不再允许覆写（防绕过）")
    args = ap.parse_args()

    try:
        report = json.loads(Path(args.report).read_text(encoding="utf-8-sig"))
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"ok": False, "error": f"读不到报告: {exc}"}, ensure_ascii=False, indent=2))
        return 2
    try:
        sig = json.loads(Path(args.signature).read_text(encoding="utf-8-sig"))
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"ok": False, "error": f"读不到签名文件: {exc}"}, ensure_ascii=False, indent=2))
        return 2
    dpath = Path(args.loop_dir) / "decision.json"
    if not dpath.is_file():
        print("[ERROR] decision.json not found", file=sys.stderr)
        return 2
    dec = json.loads(dpath.read_text(encoding="utf-8-sig"))

    actual = sha256_file(args.report)
    if args.sha256 and args.sha256 != actual:
        print(json.dumps({"ok": False, "error": "--sha256 与报告整文件哈希不一致（不允许覆写哈希）",
                          "expected": actual, "given": args.sha256},
                         ensure_ascii=False, indent=2))
        return 2
    if not sig.get("report_sha256"):
        print(json.dumps({"ok": False, "error": "签名缺少 report_sha256"}, ensure_ascii=False, indent=2))
        return 2
    if actual != sig.get("report_sha256"):
        print(json.dumps({"ok": False, "error": "signature hash mismatch",
                          "expected": sig.get("report_sha256"), "actual": actual},
                         ensure_ascii=False, indent=2))
        return 2

    # A3②：signer 白名单 fail closed —— 未显式配置就拒绝（伪 signer 不得蒙混过关）
    raw_allow = (args.allow_signers or os.environ.get("BOT_SIGNERS")
                 or os.environ.get("BOT_SIGNER_ALLOWLIST") or "")
    allowlist = [x.strip() for x in raw_allow.split(",") if x.strip()]
    if not allowlist:
        print(json.dumps({"ok": False, "status": "rejected",
                          "reasons": ["未配置 signer 白名单：请用 --allow-signers 或 env BOT_SIGNERS 指定合法签字人（fail closed）"],
                          "decision": dec.get("id")}, ensure_ascii=False, indent=2))
        return 2
    report = dict(report)
    report["_sha256"] = actual
    reasons = gates.check_finalize(dec, report, sig, allowlist=allowlist)
    if reasons:
        print(json.dumps({"ok": False, "status": "rejected", "reasons": reasons,
                          "decision": dec.get("id")}, ensure_ascii=False, indent=2))
        return 2

    dec["status"] = "approved"
    dec["approval"] = {"by": sig.get("signer"), "at": sig.get("signed_at"), "mode": "explicit"}
    dec["signature_sha256"] = actual
    dec["signature_file"] = str(args.signature)
    fsutil.atomic_write_json(dpath, dec)
    print(json.dumps({"ok": True, "decision": dec.get("id"), "status": "approved",
                      "signer": sig.get("signer"), "signature_sha256": actual},
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
