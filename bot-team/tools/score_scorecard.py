#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""六维评分卡计算器（方法论 v1.3 §4 的规则化）。

输入：六个维度的 0-5 分（旧口径）或 0-100 分（B16 百分制；报告里的 "4/5" 字符串也兼容，
      但 --strict 拒绝叙述）。两种口径都归一为百分制，输出 dimensions_0_5 与 dimensions_100。
输出：100 分制总分、硬护栏（阈值只从 pack.json 的 scorecard_hard_guardrails 读）、
      80/60/40 档位判定；供 assemble_report/validate_report/交付卡/run_task 使用。

修复（M1）：
  - 100 分制精确换算：六维全 5 → 100.0（不是 6×16.67=100.02）
  - NaN/Inf/bool 一律拒收
  - stdout 不再带 UTF-8 BOM（可直接 json.loads）
  - 提供可被 run_task/其他工具 import 的 compute() / compute_from_report()

用法：
  python tools/score_scorecard.py --scores '{"data_completeness":4,...}'
  python tools/score_scorecard.py --from-report runs/demo-report.json --strict
  python tools/score_scorecard.py --scores-file scores.json --pack-path packs/manufacturing/pack.json
  python tools/score_scorecard.py --scores '{"data_completeness":100,...}'   # B16 百分制输入
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import pack as pack_mod  # noqa: E402
from core import report_contract  # noqa: E402

DIMS = report_contract.SIX_DIMS


def load_pack(path=None):
    """读取行业包（B16：硬护栏阈值唯一事实源）；--pack-path 优先，其次 BOT_PACK/默认包。"""
    return pack_mod.load_file(path) if path else pack_mod.load()


def compute(raw, strict=False, pack=None):
    """把六维分数换算成 100 分制结果（可被 run_task 直接 import）。

    raw 支持旧 0-5 与 B16 新 0-100 百分制两种输入（见 core.report_contract.to_score）。
    pack：行业包 dict（缺省按 core.pack.load() 读取，硬护栏阈值只从 pack 读）。
    """
    pack_doc = pack if isinstance(pack, dict) and pack.get("pack_id") else load_pack()
    out = report_contract.compute(raw, strict=strict, pack=pack_doc)
    return {
        "ok": True,
        "dimensions_0_5": out["dimensions_0_5"],
        "dimensions_100": out["dimensions_100"],
        "points": out["points"],
        "total": out["total"],
        "guardrails": out["guardrails"],
        "guardrail_details": out["guardrail_details"],
        "verdict": out["verdict"],
        "label": out["label"],
        "pack_id": pack_doc.get("pack_id"),
        "calibration": report_contract.CALIBRATION,
        "strict": strict,
    }


def compute_from_report(path, strict=False, pack=None):
    """读 report.json 的 scorecard 并计算（可被 run_task 直接 import）。"""
    doc = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    sc = doc.get("scorecard") or {}
    return compute(sc, strict=strict, pack=pack)


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
    ap.add_argument("--pack-path", default="", help="pack.json 路径（默认 BOT_PACK/内置制造业包）")
    ap.add_argument("--strict", action="store_true", help="reject narrative scores")
    args = ap.parse_args()

    try:
        pack_doc = load_pack(args.pack_path or None)
    except pack_mod.PackError as exc:
        print(json.dumps({"ok": False, "errors": [f"行业包加载失败：{exc}"]}, ensure_ascii=False, indent=2))
        return 2

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
        out = compute(raw, args.strict, pack=pack_doc)
    except ValueError as exc:
        print(json.dumps({"ok": False, "errors": [str(exc)]}, ensure_ascii=False, indent=2))
        return 2
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
