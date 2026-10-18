#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""六维评分卡计算器（方法论 v1.3 §4 的规则化）。

输入：六个维度的 0-5 分（报告里的 "4/5" 字符串也兼容，但 --strict 拒绝叙述）
输出：100 分制总分、硬护栏、80/60/40 判定；供 assemble_report/validate_report/交付卡使用。

用法：
  python tools/score_scorecard.py --scores '{"data_completeness":4,"timeliness":3,"significance":5,"exclusivity":3,"history":4,"human_ai_consistency":0}'
  python tools/score_scorecard.py --from-report runs/demo-report.json --strict
"""
import argparse
import json
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
DIMS = ["data_completeness", "timeliness", "significance", "exclusivity", "history", "human_ai_consistency"]


def to_score(v, strict=False):
    if isinstance(v, bool):
        raise ValueError("boolean is not a score")
    if isinstance(v, (int, float)):
        s = float(v)
    elif isinstance(v, dict):
        return to_score(v.get("score"), strict)
    elif isinstance(v, str):
        if strict:
            raise ValueError(f"strict 模式要求数值分数，收到字符串：{v[:40]}")
        m = re.match(r"^\s*(\d+(?:\.\d+)?)\s*/\s*5", v)
        if m:
            s = float(m.group(1))
        else:
            if strict:
                raise ValueError(f"strict 模式要求数值分数，收到叙述：{v[:40]}")
            m = re.match(r"^\s*(\d+(?:\.\d+)?)", v)
            if not m:
                raise ValueError(f"无法解析分数：{v[:40]}")
            s = float(m.group(1))
    else:
        raise ValueError(f"unsupported score type: {type(v)}")
    if s < 0 or s > 5:
        raise ValueError(f"score out of range 0-5: {s}")
    return s


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8-sig")
        sys.stderr.reconfigure(encoding="utf-8-sig")
    except Exception:
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--scores", default="", help="JSON: six dimensions")
    ap.add_argument("--scores-file", default="", help="JSON file with six dimensions")
    ap.add_argument("--from-report", default="", help="read report.scorecard")
    ap.add_argument("--strict", action="store_true", help="reject narrative scores")
    args = ap.parse_args()

    if args.scores_file:
        raw = json.loads(Path(args.scores_file).read_text(encoding="utf-8-sig"))
    elif args.scores:
        raw = json.loads(args.scores)
    elif args.from_report:
        raw = json.loads(Path(args.from_report).read_text(encoding="utf-8-sig")).get("scorecard") or {}
    else:
        print("need --scores or --from-report", file=sys.stderr)
        return 2

    cfg = yaml.safe_load((ROOT / "assets/scorecard_six_dim.yaml").read_text(encoding="utf-8-sig"))
    dims = {}
    errors = []
    for d in DIMS:
        try:
            dims[d] = to_score(raw.get(d), args.strict)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{d}: {exc}")
    if errors:
        print(json.dumps({"ok": False, "errors": errors}, ensure_ascii=False, indent=2))
        return 2

    # 0-5 -> 16.67 分/维；六维合计 100
    points = {d: round(dims[d] / 5 * 16.67, 2) for d in DIMS}
    total = round(sum(points.values()), 2)
    guards = []
    if dims["human_ai_consistency"] <= 0:
        guards.append("human_ai_conflict")
    if points["exclusivity"] <= 10:
        guards.append("too_many_hypotheses")
    if points["data_completeness"] <= 15:
        guards.append("data_gap")

    thresholds = cfg.get("thresholds") or []
    verdict = "no_output"
    for t in thresholds:
        lo = t.get("min_score", 0)
        hi = t.get("max_score", 100)
        if lo <= total <= hi:
            verdict = t.get("action", "no_output")
            label = t.get("label", "")
            break
    else:
        label = ""
    if guards:
        verdict = "force_manual_review"
        label = "建议人工复核（硬护栏触发）"

    out = {
        "ok": True,
        "dimensions_0_5": dims,
        "points": points,
        "total": total,
        "guardrails": guards,
        "verdict": verdict,
        "label": label,
        "calibration": cfg.get("calibration", {}),
        "strict": args.strict,
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

