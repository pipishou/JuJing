#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""员工记忆检索消费者（关系层）。

按 entity + employee 取记忆，过滤过期/隐私，最多 N 条；只作上下文，不覆盖证据。

用法：
  python tools/memory_retrieve.py --file team/memory/example-memory.json \
      --employee analyst --entity "塑胶厂-B线" --limit 5
  python tools/memory_retrieve.py --file ... --employee analyst --privacy internal --as-of 2026-12-01
"""
import argparse
import json
import sys
from datetime import date
from pathlib import Path


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", required=True)
    ap.add_argument("--employee", required=True)
    ap.add_argument("--entity", default="")
    ap.add_argument("--limit", type=int, default=5)
    ap.add_argument("--privacy", choices=["public", "internal", "customer_sensitive"], default="internal",
                    help="允许注入的最高隐私级（internal 不含 customer_sensitive）")
    ap.add_argument("--as-of", default=date.today().isoformat())
    args = ap.parse_args()

    allowed = {"public": 0, "internal": 1, "customer_sensitive": 2}
    rows = json.loads(Path(args.file).read_text(encoding="utf-8-sig"))
    asof = date.fromisoformat(args.as_of)
    picked = []
    for m in rows:
        if m.get("employee_id") != args.employee:
            continue
        if args.entity and m.get("entity") and m["entity"] != args.entity:
            continue
        if allowed.get(m.get("privacy"), 9) > allowed[args.privacy]:
            continue
        exp = m.get("expires_at")
        if exp:
            try:
                if date.fromisoformat(exp) < asof:
                    continue
            except ValueError:
                pass
        picked.append(m)

    prio = {"outcome": 0, "decision": 1, "data_caveat": 2, "preference": 3, "profile": 4}
    picked.sort(key=lambda m: (prio.get(m.get("kind"), 9), m.get("created_at", "")))
    picked = picked[: args.limit]
    out = {
        "ok": True,
        "employee": args.employee,
        "entity": args.entity,
        "count": len(picked),
        "memories": [
            {"memory_id": m.get("memory_id"), "kind": m.get("kind"), "text": m.get("text"),
             "source_ref": m.get("source_ref"), "confidence": m.get("confidence"),
             "privacy": m.get("privacy"), "expires_at": m.get("expires_at")}
            for m in picked
        ],
        "rule": "记忆只作上下文；与当前数据冲突时以数据为准",
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
