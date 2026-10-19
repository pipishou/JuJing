#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""host 收口：把 team-workflow 的 {snapshot,factorTable,attribution,advice}
组装成一份「可交付报告」——补 scorecard 100 分制结论、decision_log_entry、
ROI 三笔账、数据溯源；然后在真实模式下过「可信门」再原子落盘。

用法（旧 CLI 保持兼容）：
  python orchestration/assemble_report.py runs/demo-result-2026-10-19.json runs/demo-report.json
  python orchestration/assemble_report.py result.json report.json --real --task-id T-... \
      --context "..." --data-refs "runs/x/T.json,runs/x/T30.json" --expected "..." --records-dir runs/x

M1 新增：
  --pack-path 指定行业包（默认 packs/manufacturing/pack.json，env BOT_PACK 可覆盖）
  real 模式：先过 core.gates 可信门（占位/缺证据/data_refs 不存在或 sha 不符/total 与 verdict 不一致）
             再原子写（tempfile + os.replace），失败不落半成品
  data_refs 在 real 模式记录为 [{path,sha256,size}]
  scorecard 注入 total/verdict/label/guardrails/points（100 分制，全 5=100.0）
  ROI 缺失/占位时按行业包 roi_anchors 兜底（证明行业包被真正消费）
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import gates, pack as pack_mod, provenance, report_contract  # noqa: E402

SIX = report_contract.SIX_DIMS
DEFAULT_DATA_REFS = ["demo/material_refill_T.json", "demo/material_refill_T30.json"]


def log(msg):
    print(f"[assemble] {msg}")


def fail(msg, code=2):
    print(f"[assemble][FAIL] {msg}", file=sys.stderr)
    return code


def atomic_write_json(path, doc):
    """原子写：同目录 temp + fsync + os.replace；失败不留半成品。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="." + path.name + ".", suffix=".tmp", dir=str(path.parent))
    tmp_path = Path(tmp)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, path)
        tmp_path = None
    finally:
        if tmp_path is not None and tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass


def split_csv(text):
    return [part.strip() for part in (text or "").split(",") if part.strip()]


def pick_report_id(out_path):
    """报告编号：按 out 所在任务目录分配 DL-YYYYMMDD-###（锁 + 跳过已占用）。"""
    from core import ids as ids_mod

    out_path = Path(out_path)
    day = date.today()
    root = out_path.parent
    try:
        return ids_mod.allocate(root, "report")
    except Exception:  # noqa: BLE001 —— 编号失败不阻断报告（仍可人工编号）
        return f"DL-{day:%Y%m%d}-001"


def normalize_real_scorecard(attribution, base):
    """real 模式：先拒绝占位/缺证据，再规整成数值六维；否则抛 ValueError。"""
    raw = attribution.get("scorecard") or {}
    if not isinstance(raw, dict):
        raise ValueError("real 模式 attribution.scorecard 必须是对象")
    errors = []
    for dim in SIX:
        if dim not in raw:
            errors.append(f"scorecard 缺维度 {dim}")
            continue
        val = raw[dim]
        if isinstance(val, dict):
            if gates.is_placeholder_evidence(val.get("evidence")):
                errors.append(f"scorecard.{dim}.evidence 是占位/缺证据")
            try:
                report_contract.to_score(val.get("score"), strict=False)
            except ValueError as exc:
                errors.append(f"scorecard.{dim}.score 非法：{exc}")
        elif isinstance(val, str):
            if gates.is_placeholder_evidence(val):
                errors.append(f"scorecard.{dim} 是占位文本")
            try:
                report_contract.to_score(val, strict=False)
            except ValueError as exc:
                errors.append(f"scorecard.{dim} 非法：{exc}")
    if errors:
        raise ValueError("；".join(errors))
    return report_contract.normalize(raw, strict=False)


def demo_scorecard(attribution):
    """demo 模式：缺维补占位（显式标注不得当真），其余原样保留。"""
    raw = attribution.get("scorecard") or {}
    out = {}
    for dim in SIX:
        if dim in raw:
            out[dim] = raw[dim]
        else:
            out[dim] = {"score": 3, "evidence": "demo 占位：归因专家未提供该维（不得当真实结论）"}
    return out


def apply_pack_roi(pack_doc, items):
    """ROI 三笔账统一按行业包 roi_anchors 定价（缺省/占位时兜底）。

    这是「行业包被真正消费」的关键：修改 pack.roi_anchors 后，新报告的 ROI 数值随之变化。
    anchor 关键字匹配不上的动作保留原有 ROI（不臆造）。
    """
    anchors = pack_doc.get("roi_anchors") or {}
    pid = pack_doc.get("pack_id", "")
    filled = 0
    for item in items or []:
        if not isinstance(item, dict):
            continue
        roi = item.get("roi")
        if not isinstance(roi, dict):
            roi = {}
        action = item.get("action", "")
        key = pack_mod.anchor_key_for(action)
        if key and key in anchors:
            value = anchors[key]
            roi["cost"] = f"¥{value}"
            if not roi.get("avoided") or gates.is_placeholder_evidence(str(roi.get("avoided"))):
                roi["avoided"] = f"¥{value}"
            roi["anchor"] = f"{key}（pack:{pid}）"
            roi["roi_source"] = f"pack:{pid}"
            item["roi"] = roi
            filled += 1
            continue
        need = [k for k in ("cost", "avoided", "anchor") if not roi.get(k) or gates.is_placeholder_evidence(str(roi.get(k)))]
        if not need:
            item["roi"] = roi
            continue
        base_roi = pack_mod.roi_for(pack_doc, action)
        for k in need:
            roi[k] = base_roi[k]
        roi["roi_source"] = f"pack:{pid}"
        item["roi"] = roi
        filled += 1
    return filled


def needs_security_gate(pack_doc, items):
    for item in items or []:
        if not isinstance(item, dict):
            continue
        if item.get("irreversibility") == "irreversible":
            return True
        if pack_mod.is_irreversible(pack_doc, item.get("action", "")):
            return True
    return False


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass

    ap = argparse.ArgumentParser()
    ap.add_argument("src", help="team-workflow result JSON")
    ap.add_argument("out", help="report JSON to write")
    ap.add_argument("--real", action="store_true")
    ap.add_argument("--task-id", default="")
    ap.add_argument("--context", default="")
    ap.add_argument("--data-refs", default="", help="comma separated data files")
    ap.add_argument("--expected", default="")
    ap.add_argument("--goal", default="")
    ap.add_argument("--records-dir", default="")
    ap.add_argument("--records-task-id", default="", help="records dir task id (loop bundle)")
    ap.add_argument("--approver", default="")
    ap.add_argument("--date", default="")
    ap.add_argument("--analysis-json", default="")
    ap.add_argument("--goal-json", default="")
    ap.add_argument("--cost-json", default="")
    ap.add_argument("--pack-path", default="", help="industry pack json (default from core.pack)")
    ap.add_argument("--pack-id", default=pack_mod.DEFAULT_PACK_ID)
    args = ap.parse_args()

    # 0) 日期先校验：非法日期必须在写任何文件之前失败
    run_day = date.today()
    if args.date:
        try:
            run_day = date.fromisoformat(args.date)
        except ValueError:
            return fail(f"--date 不是 ISO 日期（YYYY-MM-DD）：{args.date!r}")

    # 1) 行业包（阈值/ROI/不可逆动作的唯一来源）
    try:
        pack_doc = pack_mod.load_file(args.pack_path) if args.pack_path else pack_mod.load(args.pack_id)
    except pack_mod.PackError as exc:
        return fail(str(exc))
    th = pack_doc["thresholds"]
    baseline = th["refill_baseline_pct"]
    target = th["refill_target_pct"]

    # 2) 读 workflow result
    src = Path(args.src)
    try:
        doc = json.loads(src.read_text(encoding="utf-8-sig"))
    except Exception as exc:  # noqa: BLE001
        return fail(f"读不到 workflow 结果 {src}: {exc}")
    attribution = doc.get("attribution") or {}
    advice = doc.get("advice") or {}
    finding = attribution.get("finding") or []
    cannot = attribution.get("cannot_attribute") or []
    rec = advice.get("recommendation") or advice.get("recommendations") or []

    real = bool(args.real)
    task_id = (args.task_id or "").strip() or None
    if real:
        missing = [name for name, val in (
            ("--task-id", task_id), ("--context", args.context),
            ("--data-refs", args.data_refs), ("--expected", args.expected),
        ) if not val]
        if missing:
            return fail(f"real 模式必须提供 {', '.join(missing)}")

    # 3) 数据溯源（real 记录 sha256；demo 保留裸路径）
    refs = split_csv(args.data_refs) or list(DEFAULT_DATA_REFS)
    if real:
        try:
            data_refs = provenance.refs_record(refs, ROOT)
        except provenance.ProvenanceError as exc:
            return fail(f"data_refs 溯源失败：{exc}")
    else:
        data_refs = refs

    # 4) scorecard → 100 分制（real 先过占位/缺证据门）
    if real:
        try:
            dims = normalize_real_scorecard(attribution, ROOT)
        except ValueError as exc:
            return fail(f"real 模式 scorecard 不可信：{exc}")
        scorecard = {d: dims[d] for d in SIX}
    else:
        scorecard = demo_scorecard(attribution)
    try:
        scored = report_contract.compute(scorecard)
    except ValueError as exc:
        return fail(f"scorecard 无法换算：{exc}")
    scorecard["total"] = scored["total"]
    scorecard["verdict"] = scored["verdict"]
    scorecard["label"] = scored["label"]
    scorecard["guardrails"] = scored["guardrails"]
    scorecard["points"] = scored["points"]

    # 5) ROI 三笔账：统一按行业包 roi_anchors 定价（缺省/占位兜底）
    roi_filled = apply_pack_roi(pack_doc, rec)

    # 6) 安全门：不可逆动作必须呈请
    gate_needed = needs_security_gate(pack_doc, rec) or bool(advice.get("security_gate_requested"))
    gate = {
        "requested_at": run_day.isoformat(),
        "confirmed_by": None,
        "status": "pending_user_confirmation",
    } if gate_needed else None

    # 7) 文本字段缺省值（行业包阈值参与，不再硬编码 5.5%/7.2%）
    context = args.context or (
        f"{pack_doc.get('industry', '制造业')}·B产线补料率7日爬升(5.1%→{target}%) 归因（demo 数据）"
    )
    expected = args.expected or f"T+7 补料率回落至 ≤{baseline}%（demo 占位预期，仅演示结构）"
    goal = args.goal or doc.get("goal") or (
        f"判断{context.split(' ')[0]}是否由模具磨损/保养不足引起并给出可逆处置"
    )
    report_id = pick_report_id(args.out)

    report = {
        "schema": "manufacturing-ops-report/0.1",
        "generated_at": run_day.isoformat(),
        "is_demo": not real,
        "task_id": task_id,
        "goal": goal,
        "pack": {
            "pack_id": pack_doc.get("pack_id"),
            "version": pack_doc.get("version"),
            "industry": pack_doc.get("industry"),
            "crew_id": pack_doc.get("crew_id"),
            "thresholds": dict(th),
        },
        "roi_anchors": dict(pack_doc.get("roi_anchors") or {}),
        "finding": finding,
        "cannot_attribute": cannot,
        "data_refs": data_refs,
        "scorecard": scorecard,
        "recommendation": rec,
        "security_gate_requested": gate_needed,
        "security_gate": gate,
        "decision_log_entry": {
            "id": report_id,
            "date": run_day.isoformat(),
            "actor": "manufacturing-ops-team-dsh",
            "task_id": task_id,
            "context": context,
            "advice": advice.get("direction_check") or advice.get("advice")
            or "按 exclusion 排除法结论执行；无归因时先补数据再重跑",
            "options": [item.get("action", "") for item in rec if isinstance(item, dict)],
            "expected_result": expected,
            "verification_plan": f"T+{th.get('t30_window_days', 30) // 4 or 7} 天复查 7 日补料率均值；未回落则按 cannot_attribute 扩数据重跑",
            "data_refs": data_refs,
            "irreversibility": "irreversible" if gate_needed else "reversible",
            "security_gate": gate,
            "roi_source": f"pack:{pack_doc.get('pack_id')}",
        },
        "role_coverage": {
            "host": "s5",
            "consulting-analyst": "s1",
            "ai-attribution-expert": "s3",
            "manufacturing-ops-expert": ["s2", "s4"],
        },
        "source": {"workflow_result": "team-workflow.mjs", "pack": pack_doc.get("_pack_path", "")},
    }

    # 8) 真实模式可信门：不通过就不写文件
    if real:
        errors = gates.real_report_errors(report, ROOT)
        if errors:
            return fail("real 报告可信门未通过：\n  - " + "\n  - ".join(errors))

    # 9) 原子落盘
    try:
        atomic_write_json(args.out, report)
    except OSError as exc:
        return fail(f"写报告失败：{exc}")
    log(f"wrote {args.out} (is_demo={report['is_demo']}, total={scorecard['total']}, verdict={scorecard['verdict']}, roi_filled={roi_filled})")

    # 10) 四卡（analysis/goal/evidence/decision）落盘
    if args.records_dir:
        cmd = [
            sys.executable, str(ROOT / "tools" / "write_loop_bundle.py"),
            "--report", str(args.out), "--outdir", str(args.records_dir),
        ]
        if args.records_task_id:
            cmd += ["--task-id", args.records_task_id]
        for flag, val in (
            ("--analysis-json", args.analysis_json),
            ("--goal-json", args.goal_json),
            ("--cost-json", args.cost_json),
            ("--approver", args.approver),
        ):
            if val:
                cmd += [flag, str(val)]
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
        if proc.returncode != 0:
            print(proc.stdout or "", end="")
            print(proc.stderr or "", file=sys.stderr, end="")
            return fail(f"四卡落盘失败（write_loop_bundle exit={proc.returncode}）")
        log(f"loop bundle written: {args.records_dir}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
