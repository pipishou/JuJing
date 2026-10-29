#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""报告校验：demo 报告 or 真实报告是否满足产出契约。

判据（任一 FAIL → 非零退出）：
  1. finding：0–2 条，每条 factor/confidence/evidence 齐，evidence 非空
  2. cannot_attribute：非空
  3. card_kind（C-1）：缺省 attribution；attribution 要求 scorecard 六维键齐；
     review/content 各自强制面复用 core.gates 辅助函数（content_plan_errors /
     review_errors / scorecard_errors），不在本文件复制第二份规则
  4. recommendation：非空，每条 action/roi(cost,avoided,anchor)/irreversibility 齐
  5. decision_log_entry：id/actor/context/advice/expected_result/verification_plan 齐
  6. 若 any irreversibility=irreversible → security_gate 必须非空
  7. --require-real（M1 可信门，A2）：
     is_demo=false、task_id 非空、data_refs 不含 demo/、
     core.gates.real_report_errors 全绿（占位/缺证据/refs 不存在或 sha 不符/
     scorecard.total 与 verdict 缺失或与独立计算不一致；content/review 卡强制
     对应 content_plan / observed_outcome）
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import gates as _gates  # noqa: E402
from core import report_contract as _rc  # noqa: E402

SIX = ["data_completeness", "timeliness", "significance", "exclusivity", "history", "human_ai_consistency"]
FAIL = []


def check(name, ok, detail=""):
    print(("[OK]   " if ok else "[FAIL] ") + name + ("" if ok else "  ← " + detail))
    if not ok:
        FAIL.append(name)


def main():
    raw = sys.argv[1:]
    require_real = "--require-real" in raw
    base = ROOT
    if "--base" in raw:
        idx = raw.index("--base")
        if idx + 1 >= len(raw):
            print("usage: validate_report.py <report.json> [--require-real] [--base <dir>]")
            return 2
        base = Path(raw[idx + 1]).resolve()
    drop = {"--require-real", "--base"}
    args = [a for i, a in enumerate(raw) if a not in drop and not (i > 0 and raw[i - 1] == "--base")]
    if len(args) != 1:
        print("usage: validate_report.py <report.json> [--require-real] [--base <dir>]")
        return 2
    p = Path(args[0])
    if not p.is_file():
        print(f"report not found: {p}")
        return 2
    r = json.loads(p.read_text(encoding="utf-8-sig"))

    # C-1：卡型缺省 attribution；未知值 → FAIL（不静默）
    try:
        kind = _rc.card_kind(r)
        kind_error = None
    except ValueError as exc:
        kind = None
        kind_error = str(exc)

    finding = r.get("finding") or []
    check("finding 0-2 条", 0 <= len(finding) <= 2, str(len(finding)))
    if len(finding) == 0:
        check("no_finding 需 cannot_attribute 非空", isinstance(r.get("cannot_attribute"), list) and len(r["cannot_attribute"]) > 0)
    for i, f in enumerate(finding):
        check(f"finding[{i}] factor", bool(f.get("factor")))
        check(f"finding[{i}] confidence", f.get("confidence") not in (None, ""))
        ev = f.get("evidence") or []
        check(f"finding[{i}] evidence 非空", isinstance(ev, list) and len(ev) > 0)

    ca = r.get("cannot_attribute") or []
    check("cannot_attribute 非空", isinstance(ca, list) and len(ca) > 0)

    sc = r.get("scorecard") or {}
    if kind == "attribution":
        for k in SIX:
            check(f"scorecard {k}", k in sc)
    elif kind is None:
        check("card_kind 合法", False, kind_error or "")
    # F1/F5：交付层三字段（demo 报告也验；老报告无字段时跳过）
    if any(k in r for k in ("verdict_one_line", "sign_scope", "evidence_tier")):
        check("verdict_one_line 非空且≤30字",
              isinstance(r.get("verdict_one_line"), str) and 0 < len(r.get("verdict_one_line") or "") <= 30,
              str(r.get("verdict_one_line")))
        check("sign_scope 合法",
              r.get("sign_scope") in ("approve", "no_stop", "approve_B1", "approve_B2", "other"),
              str(r.get("sign_scope")))
        check("evidence_tier 合法",
              r.get("evidence_tier") in ("measured", "mixed", "inference_only"),
              str(r.get("evidence_tier")))

    # F10/K-07：封存校验（有字段必验；real 报告必须有）
    if "artifact_sha256" in r or (require_real and r.get("is_demo") is not True):
        try:
            import importlib.util as _iu
            _spec = _iu.spec_from_file_location("artifact_mod", str(ROOT / "core" / "artifact.py"))
            _art = _iu.module_from_spec(_spec); _spec.loader.exec_module(_art)
            _ok_seal, _seal_why = _art.verify(r)
        except Exception as _exc:  # noqa: BLE001
            _ok_seal, _seal_why = False, f"封存校验异常: {_exc}"
        check("artifact_sha256 与内容一致", _ok_seal, _seal_why)

    # F9：证据合同——单源推演不得记 measured；url 必须可解析
    _tier = r.get("evidence_tier")
    for _i, _e in enumerate(r.get("evidence") or []):
        if not isinstance(_e, dict):
            continue
        if _tier == "inference_only" and _e.get("tier") == "measured":
            check(f"evidence[{_i}] tier 与 evidence_tier 冲突", False, "inference_only 报告不得含 measured 证据")
        _u = _e.get("url")
        if _u and not str(_u).startswith(("http://", "https://")):
            check(f"evidence[{_i}] url 不可解析", False, str(_u))

    if require_real:
        check("real card_kind 合法", kind is not None, kind_error or "")
        # C-1：六维/total/verdict 只在 attribution 强制；非 attribution 卡给了完整
        # 六维才做分值校验（允许自评）。判定统一复用 core.gates.scorecard_errors。
        sc_errors = []
        if kind == "attribution" or (isinstance(r.get("scorecard"), dict)
                                     and all(k in r["scorecard"] for k in SIX)):
            sc_errors = _gates.scorecard_errors(r.get("scorecard") or {},
                                               require_total=(kind == "attribution"))
        check(f"real scorecard 六维/total/verdict（card_kind={kind}）",
              not sc_errors, "；".join(str(e) for e in sc_errors)[:300])
        if kind == "content":
            cp_errors = _gates.content_plan_errors(r.get("content_plan"))
            check("real content_plan 齐全（angle/structure/titles/channel/review_metrics/labels）",
                  not cp_errors, "；".join(cp_errors)[:300])
        elif kind == "review":
            rv_errors = _gates.review_errors(r)
            check("real review.observed_outcome 非空", not rv_errors, "；".join(rv_errors)[:300])
        check("real is_demo=false", r.get("is_demo") is False)
        check("real task_id", bool(r.get("task_id")))
        # M1.5 §3 B4 硬化：data_refs 可能在顶层与 decision_log_entry 两处，两处都要查 demo/
        top_refs = r.get("data_refs")
        dl_refs = (r.get("decision_log_entry") or {}).get("data_refs")
        for where, refs in (("data_refs", top_refs), ("decision_log_entry.data_refs", dl_refs)):
            if refs is None:
                continue
            check(f"real {where} 无 demo/",
                  isinstance(refs, list) and all("demo/" not in str(x) for x in refs), str(refs))
        try:
            errs = _gates.real_report_errors(r, base)
        except Exception as exc:  # noqa: BLE001
            errs = [f"可信门执行异常：{exc}"]
        check("real 可信门（占位/证据/溯源/total-verdict/卡型）", not errs, "；".join(str(e) for e in errs)[:500])

    rec = r.get("recommendation") or []
    check("recommendation 非空", len(rec) > 0)
    irreversible = False
    for i, x in enumerate(rec):
        check(f"rec[{i}] action", bool(x.get("action")))
        roi = x.get("roi") or {}
        for k in ("cost", "avoided", "anchor"):
            # 0 是有效值（¥0 复核/对照试跑真存在），不能用 bool() —— 与 core.report_contract.is_empty 同口径
            check(f"rec[{i}] roi.{k}", not _rc.is_empty(roi.get(k)))
        irr = x.get("irreversibility")
        check(f"rec[{i}] irreversibility", irr in ("reversible", "irreversible"), str(irr))
        irreversible = irreversible or irr == "irreversible"

    dl = r.get("decision_log_entry") or {}
    for k in ("id", "actor", "context", "advice", "expected_result", "verification_plan"):
        check(f"decision_log {k}", bool(dl.get(k)))
    if irreversible:
        check("irreversible 需 security_gate", dl.get("security_gate") not in (None, {}, ""))

    print()
    if FAIL:
        print(f"RESULT: FAIL ({len(FAIL)} 项)")
        return 1
    print("RESULT: PASS（报告契约全绿）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
