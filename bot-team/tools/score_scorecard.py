#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""六维评分卡计算器（方法论 v1.3 §4 的规则化）。

输入：六个维度的 0-5 分（报告里的 "4/5" 字符串也兼容，但 --strict 拒绝叙述）
输出：100 分制总分、硬护栏、80/60/40 判定；供 assemble_report/validate_report/交付卡/run_task 使用。

修复（M1）：
  - 100 分制精确换算：六维全 5 → 100.0（不是 6×16.67=100.02）
  - NaN/Inf/bool 一律拒收
  - stdout 不再带 UTF-8 BOM（可直接 json.loads）
  - 提供可被 run_task/其他工具 import 的 compute() / compute_from_report()

用法：
  python tools/score_scorecard.py --scores '{"data_completeness":4,...}'
  python tools/score_scorecard.py --from-report runs/demo-report.json --strict
  python tools/score_scorecard.py --scores-file scores.json
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import report_contract  # noqa: E402

DIMS = report_contract.SIX_DIMS


def compute(raw, strict=False):
    """把六个 0-5 分换算成 100 分制结果（可被 run_task 直接 import）。"""
    out = report_contract.compute(raw, strict=strict)
    return {
        "ok": True,
        "dimensions_0_5": out["dimensions_0_5"],
        "points": out["points"],
        "total": out["total"],
        "guardrails": out["guardrails"],
        "verdict": out["verdict"],
        "label": out["label"],
        "calibration": report_contract.CALIBRATION,
        "strict": strict,
    }


def compute_from_report(path, strict=False):
    """读 report.json 的 scorecard 并计算（可被 run_task 直接 import）。"""
    doc = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    sc = doc.get("scorecard") or {}
    return compute(sc, strict=strict)


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--scores", default="", help="JSON: six dimensions")
    ap.add_argument("--scores-file", default="", help="JSON file with six dimensions")
    ap.add_argument("--from-report", default="", help="read report.scorecard")
    ap.add_argument("--strict", action="store_true", help="reject narrative scores")
    args = ap.parse_args()

    try:
        if args.scores_file:
            raw = json.loads(Path(args.scores_file).read_text(encoding="utf-8-sig"))
        elif args.scores:
            raw = json.loads(args.scores)
        elif args.from_report:
            raw = json.loads(Path(args.from_report).read_text(encoding="utf-8-sig")).get("scorecard") or {}
        else:
            print("need --scores or --scores-file or --from-report", file=sys.stderr)
            return 2
    except (json.JSONDecodeError, OSError) as exc:
        print(json.dumps({"ok": False, "errors": [f"输入 JSON 解析失败：{exc}"]}, ensure_ascii=False, indent=2))
        return 2

    try:
        out = compute(raw, args.strict)
    except ValueError as exc:
        print(json.dumps({"ok": False, "errors": [str(exc)]}, ensure_ascii=False, indent=2))
        return 2
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
