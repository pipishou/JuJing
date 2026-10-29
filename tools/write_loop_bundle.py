#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把已收口的报告写成四段闭环 bundle（s5 收口用）。

自动写：evidence.jsonl / decision.json / closure.json
可选拷贝：--analysis-json/--goal-json/--cost-json（分析卡由 s3 产出，目标/成本由业务输入）
不伪造：没有 --analysis-json/--goal-json/--cost-json 就不写，validate_loop --require-complete 会报缺。

M1 修复：
  - 编号走 core.ids.allocate（D/A/E/C 同日不再全部 -001 撞号）
  - decision 带 task_id（与 report.task_id 一致，finalize 跨任务门可校验）
  - decision.analysis_id 与拷贝进来的 analysis.id 对齐（不再悬空）
  - 所有 JSON 原子写
"""
import argparse
import json
import re
import shutil
import sys
from datetime import datetime, date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import fsutil, ids  # noqa: E402

_WINDOW_RE = re.compile(r"^\d{1,4}d$")


def _ids_root(outdir):
    """runs/<task_id>/ids.json：loop/ 目录下时回到任务根目录。"""
    outdir = Path(outdir)
    if outdir.name == "loop" and (outdir.parent / "task.json").exists():
        return outdir.parent
    return outdir


def _alloc(root, kind, task_id):
    try:
        return ids.allocate(root, kind, task_id=task_id)
    except Exception:  # noqa: BLE001 —— 编号失败不阻断四卡
        return ""


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", required=True)
    ap.add_argument("--outdir", required=True)
    ap.add_argument("--task-id", default="")
    ap.add_argument("--analysis-json", "--analysis", dest="analysis", default="")
    ap.add_argument("--goal-json", "--goal", dest="goal", default="")
    ap.add_argument("--cost-json", "--cost", dest="cost", default="")
    ap.add_argument("--approver", default="")
    ap.add_argument("--review-days", type=int, default=7)
    args = ap.parse_args()

    report = json.loads(Path(args.report).read_text(encoding="utf-8-sig"))
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    # generated_at 常带时分与 +08:00（真实报告），直接 date.fromisoformat 会炸。
    # 统一归一成 YYYY-MM-DD：四卡里的 day/compact/review_due 都只用日期。
    raw_day = str(report.get("generated_at") or date.today().isoformat())
    try:
        day = datetime.fromisoformat(raw_day.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        day = date.today().isoformat()
    compact = day.replace("-", "")
    task_id = (args.task_id or report.get("task_id") or None) or None
    id_root = _ids_root(outdir)
    task_key = task_id if id_root.name != task_id else None

    # 先拷贝可选卡（保证 decision.analysis_id 与拷贝进来的 analysis.id 对齐）
    copied = []
    analysis_doc = None
    for key, src in (("analysis", args.analysis), ("goal", args.goal), ("cost", args.cost)):
        if not src:
            continue
        src_path = Path(src)
        if not src_path.is_file():
            print(f"[write_loop_bundle][FAIL] 找不到 {key} 文件: {src}", file=sys.stderr)
            return 2
        if key == "analysis":
            try:
                analysis_doc = json.loads(src_path.read_text(encoding="utf-8-sig"))
            except Exception:  # noqa: BLE001
                analysis_doc = None
        shutil.copyfile(src_path, outdir / f"{key}.json")
        copied.append(key)

    if analysis_doc and analysis_doc.get("id"):
        aid = str(analysis_doc["id"])
    else:
        aid = _alloc(id_root, "analysis", task_key)
    did = _alloc(id_root, "decision", task_key)

    refs = (report.get("decision_log_entry") or {}).get("data_refs") or []
    refs = report.get("data_refs") or refs
    findings = report.get("finding") or []
    _ref = refs[0] if refs else None
    _url = ("file://" + str(_ref.get("path")) + "#sha256=" + str(_ref.get("sha256"))
            if isinstance(_ref, dict) else "file://report")
    _shas = {str((x or {}).get("sha256") or "") for x in refs} if refs else set()
    _single = len(_shas) <= 1
    _grade = "C" if _single else "B"
    _dq = "inference_only" if report.get("evidence_tier") == "inference_only" else "measured"
    evidence = []
    for fi, f in enumerate(findings):
        for ei, quote in enumerate(f.get("evidence") or []):
            eid = _alloc(id_root, "evidence", task_key) or f"E-{compact}-{fi+1:02d}{ei+1:02d}"
            evidence.append({
                "id": eid,
                "query": f"报告证据：{f.get('factor')}",
                "source": {"url": _url, "title": f.get("factor", "data evidence")},
                "quote": str(quote)[:200],
                "score": 1.0,
                "grade": _grade,
                "data_quality_grade": _dq,
                "freshness_days": 30,
                "retrieved_at": day,
                "stance": "supports",
                "used_for": [aid, did],
            })
    fsutil.atomic_write_text(
        outdir / "evidence.jsonl",
        "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in evidence),
    )

    rec = (report.get("recommendation") or [{}])[0] if (report.get("recommendation") or []) else {}
    entry = report.get("decision_log_entry") if isinstance(report.get("decision_log_entry"), dict) else {}
    roi = rec.get("roi") if isinstance(rec.get("roi"), dict) else {}
    no_finding = len(findings) == 0
    options = [f.get("factor") for f in findings if f.get("factor")] + ["不做"]
    chosen = options[0] if options else "不做"
    if no_finding:
        chosen = "不做"
    status = "proposed"  # 四卡生成时只能 proposed；人工签字后由 finalize_delivery 置 approved
    # B17：DEC 扩展字段（设计文档 §3.3）；全部可选、有值才写，不写 null 占位（lead 指令 #2）。
    method = str(entry.get("attribution_method") or "").strip()
    if method not in ("exclusion", "counterfactual", "rule_based"):
        method = "exclusion"
    outcome_window = f"{args.review_days}d"
    if not _WINDOW_RE.match(outcome_window):
        outcome_window = "7d"
    action_cost = entry.get("action_cost")
    if action_cost in (None, "", [], {}):
        action_cost = roi.get("cost")
    increment_value = entry.get("increment_value")
    if increment_value in (None, "", [], {}):
        increment_value = roi.get("avoided")
    decision = {
        "id": did,
        "task_id": task_id,
        "question": report.get("goal") or "经营异常归因",
        "analysis_id": aid,
        "evidence_ids": [e["id"] for e in evidence],
        "options": options,
        "chosen": chosen,
        "rejected": [{"option": o, "reason": "证据/收益排序落后"} for o in options[1:]],
        "confidence": "LOW" if no_finding else "MED",
        "reversibility": rec.get("irreversibility", "reversible"),
        "approval": {"by": "pending", "at": day, "mode": "explicit" if args.approver else "default"},
        "reversal_conditions": [entry.get("expected_result") or "T+7 未达预期"],
        "expected_outcome": entry.get("expected_result") or "待填",
        "review_due": (date.fromisoformat(day) + timedelta(days=args.review_days)).isoformat(),
        "status": status,
        "no_finding": no_finding,
        "entity_id": (task_id or "task"),
        "metric": report.get("goal") or "",
        "context": entry.get("context") or "",
        "recommendation": rec.get("action") or "",
        "attribution_method": method,
        "outcome_window": outcome_window,
        "calibration_version": "cold-start-16.67",
        "notes": "由 write_loop_bundle 自动生成；approval.pending 表示尚未人工签字",
    }
    for key, value in (("actual_action", entry.get("actual_action")),
                       ("override_reason", entry.get("override_reason")),
                       ("observed_outcome", entry.get("observed_outcome")),
                       ("action_cost", action_cost),
                       ("increment_value", increment_value)):
        if value not in (None, "", [], {}):
            decision[key] = value
    fsutil.atomic_write_json(outdir / "decision.json", decision)

    cid = _alloc(id_root, "closure", task_key) or f"C-{compact}-001"
    closure = {
        "id": cid,
        "decision_id": did,
        "due": decision["review_due"],
        "actual_outcome": "待回填",
        "metric_result": {"metric": decision["metric"], "expected": decision["expected_outcome"], "actual": "", "delta": ""},
        "evidence_ids": [e["id"] for e in evidence],
        "verdict": "unverified",
        "lessons": ["待回填"],
        "action": "keep",
        "increment_value": decision.get("increment_value", ""),
        "attribution_method": method,
        "outcome_window": outcome_window,
        "scorecard_snapshot": report.get("scorecard") or {},
        "failure_mode": "none",
        "calibration_version": "cold-start-16.67",
        "wal_ref": "",
    }
    fsutil.atomic_write_json(outdir / "closure.json", closure)

    summary = {"ok": True, "outdir": str(outdir), "task_id": task_id,
               "decision_id": did, "analysis_id": aid, "closure_id": cid,
               "evidence": len(evidence), "copied": copied,
               "missing": [k for k in ("analysis", "goal", "cost") if k not in copied]}
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
