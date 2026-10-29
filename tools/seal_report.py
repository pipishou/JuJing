#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""封存 report.json：写 artifact_sha256（手改后失配，validate/finalize 拒收）。

用法：
  python tools/seal_report.py runs/T-xxx/report.json --note "post-G2.5 repair"
先跑交付门（real 报告必须过 gates.real_report_errors），再封存；demo 报告允许封存用于管线验证。
"""
import argparse
import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, str(ROOT / rel))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("report")
    ap.add_argument("--note", default="")
    ap.add_argument("--tool", default="seal_report.py")
    ap.add_argument("--force", action="store_true", help="demo 报告跳过交付门")
    args = ap.parse_args()
    path = Path(args.report)
    report = json.loads(path.read_text(encoding="utf-8-sig"))
    from core import gates, artifact as art
    is_demo = report.get("is_demo") is True
    if not is_demo and not args.force:
        errs = gates.real_report_errors(report, ROOT)
        if errs:
            print("[seal] 拒绝：交付门未过：\n  - " + "\n  - ".join(errs), file=sys.stderr)
            return 1
    import os as _os
    sha = art.seal(report, args.tool, args.note,
                   by=_os.environ.get("USERNAME") or _os.environ.get("USER") or "unknown",
                   command=" ".join(sys.argv))
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"[seal] ok {path} artifact_sha256={sha[:16]}… note={args.note}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
