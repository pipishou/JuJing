#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""签字收口：校验 signature.json 与报告 hash，把 decision.json 更新为 approved。

用法：
  python tools/finalize_delivery.py --report runs/task-B/report.json \
      --loop-dir runs/task-B/loop --signature runs/task-B/report.signature.json
"""
import argparse
import hashlib
import json
import sys
from pathlib import Path


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
    ap.add_argument("--report", required=True)
    ap.add_argument("--loop-dir", required=True)
    ap.add_argument("--signature", required=True)
    args = ap.parse_args()

    sig = json.loads(Path(args.signature).read_text(encoding="utf-8-sig"))
    actual = sha256_file(args.report)
    if actual != sig.get("report_sha256"):
        print(json.dumps({"ok": False, "error": "signature hash mismatch", "expected": sig.get("report_sha256"), "actual": actual}, ensure_ascii=False, indent=2))
        return 2
    dpath = Path(args.loop_dir) / "decision.json"
    if not dpath.is_file():
        print("[ERROR] decision.json not found", file=sys.stderr)
        return 2
    dec = json.loads(dpath.read_text(encoding="utf-8-sig"))
    dec["status"] = "approved"
    dec["approval"] = {"by": sig.get("signer"), "at": sig.get("signed_at"), "mode": "explicit"}
    dec["signature_sha256"] = actual
    dec["signature_file"] = str(args.signature)
    dpath.write_text(json.dumps(dec, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"ok": True, "decision": dec.get("id"), "status": "approved",
                      "signer": sig.get("signer"), "signature_sha256": actual}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
