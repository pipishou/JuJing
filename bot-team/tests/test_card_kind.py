#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""C-1 交付卡三卡型（card_kind）契约测试：六维评分卡只对 attribution 强制。

只依赖 stdlib；所有子进程 python 带 -B；临时文件落 %TEMP%。

判据：
  1. card_kind 缺省 = attribution（旧报告向后兼容）
  2. content 卡无 scorecard：real_report_errors 通过 + validate_report --require-real PASS
  3. content 卡强制 content_plan 六键（angle/structure/titles/channel/review_metrics/labels）
     缺键/空值/标题不足 5 条 → 拒绝；5 与 10 条为合法边界
  4. review 卡强制非空 observed_outcome（review.* 或顶层 outcome.*）
  5. 未知 card_kind → real_report_errors / report_shape_errors / apply / validate_report 全拒（不静默）
  6. attribution 回归不变（total/verdict 与 scorecard 原值、独立 compute 一致）
  7. apply 对 content 无六维 → {"skipped": True, "card_kind": "content"} 且不改写 scorecard
  8. attribution 缺白名单/不可逆门等 M1 行为不受卡型改造影响（抽查 real_report_errors 未回归）
"""
from __future__ import annotations

import copy
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASE_REPORT = ROOT / "runs" / "T-20261028-054" / "report.json"

sys.path.insert(0, str(ROOT))
from core import gates, report_contract  # noqa: E402

FAIL: list[str] = []


def check(name: str, ok: bool, detail: object = "") -> None:
    print(("[OK]   " if ok else "[FAIL] ") + name + ("" if ok else f"  ← {detail}"))
    if not ok:
        FAIL.append(name)


def load_base() -> dict:
    return json.loads(BASE_REPORT.read_text(encoding="utf-8-sig"))


def content_plan() -> dict:
    return {
        "angle": "补料率 9.2% 是否值得停线换模：先算停机损失再谈动作（选题判断：现场素材齐全、有明确数字口径）",
        "structure": [
            "第 1 段 3 秒：抛结论——补料率不是越高越好，先看停机与换模成本",
            "第 2 段：还原 B 线本周补料率走势与两次异常点",
            "第 3 段：给换模/停线的成本口径与不可逆提醒",
            "第 4 段：收口——本周先补数据重跑，换模走人工确认",
        ],
        "titles": [
            "补料率暴涨，先别急着换模",
            "停线 40 分钟，这笔账怎么算",
            "三张单看懂 B 线补料率异常",
            "补料率不是越高越好：先算停机损失",
            "B 线补料率异常复盘：哪些坑不能踩",
        ],
        "channel": "主渠道视频号（老板与同行社交链），副渠道公众号；首帧用「补料率 9.2%」大字 + 停线 40 分钟倒计时",
        "review_metrics": [
            "48h 前 3 秒留存 ≥ 35%（低于则改首帧）",
            "48h 私信/转发 ≥ 8 次（低于则改标题与结尾动作）",
        ],
        "labels": ["制造业经营", "补料率", "停机损失", "换模"],
    }


def make_content(base: dict, plan: dict | None = None) -> dict:
    doc = copy.deepcopy(base)
    doc["card_kind"] = "content"
    doc.pop("scorecard", None)
    doc["content_plan"] = content_plan() if plan is None else plan
    return doc


def make_review(base: dict) -> dict:
    doc = copy.deepcopy(base)
    doc["card_kind"] = "review"
    doc.pop("scorecard", None)
    doc["review"] = {
        "observed_outcome": "发布 48h 后前 3 秒留存 37%、私信 11 次，高于复盘阈值；换模建议已转人工确认",
        "reviewed_at": "2026-10-30",
    }
    return doc


def run_validate(path: Path, require_real: bool = True) -> tuple[int, str]:
    cmd = [sys.executable, "-B", str(ROOT / "tests" / "validate_report.py"), str(path)]
    if require_real:
        cmd += ["--require-real", "--base", str(ROOT)]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def case_defaults(tmp: Path) -> None:
    check("默认 card_kind=attribution", report_contract.card_kind({}) == "attribution", report_contract.card_kind({}))
    base = load_base()
    check("旧报告（无 card_kind）判为 attribution", report_contract.card_kind(base) == "attribution")
    errs = gates.real_report_errors(base, ROOT)
    check("attribution 基线报告可信门通过", not errs, "；".join(errs)[:300])


def case_content_ok(tmp: Path) -> None:
    base = load_base()
    doc = make_content(base)
    errs = gates.real_report_errors(doc, ROOT)
    check("content 无 scorecard 可信门通过", not errs, "；".join(errs)[:300])
    p = tmp / "content_ok.json"
    p.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    rc, out = run_validate(p)
    check("content 卡 validate_report --require-real PASS", rc == 0 and "RESULT: PASS" in out, out[-400:])


def case_content_plan_negatives(tmp: Path) -> None:
    base = load_base()

    def errors_for(plan):
        return gates.content_plan_errors(plan)

    doc = make_content(base)
    doc["content_plan"].pop("titles")
    errs = gates.real_report_errors(doc, ROOT)
    check("content 缺 content_plan.titles 拒", any("titles" in e for e in errs), errs[:3])

    errs = errors_for({**content_plan(), "titles": []})
    check("content_plan 空 titles 拒", any("5–10" in e or "titles" in e for e in errs), errs[:3])

    errs = errors_for({**content_plan(), "titles": ["a", "b", "c"]})
    check("titles 只有 3 条拒（要求 5–10）", any("5–10" in e for e in errs), errs[:3])

    for key in ("angle", "structure", "channel", "review_metrics", "labels"):
        plan = content_plan()
        plan.pop(key)
        errs = errors_for(plan)
        check(f"content_plan 缺 {key} 拒", any(key in e for e in errs), errs[:2])

    # 空值
    plan = content_plan()
    plan["angle"] = "   "
    check("content_plan.angle 空白拒", bool(errors_for(plan)), errors_for(plan)[:2])

    # 5 条与 10 条为合法边界
    plan = content_plan()
    plan["titles"] = [f"标题候选 {i}" for i in range(1, 6)]
    check("titles 5 条：合法边界通过", not errors_for(plan), errors_for(plan)[:2])
    plan["titles"] = [f"标题候选 {i}" for i in range(1, 11)]
    check("titles 10 条：合法边界通过", not errors_for(plan), errors_for(plan)[:2])
    plan["titles"] = [f"标题候选 {i}" for i in range(1, 12)]
    check("titles 11 条：拒绝", bool(errors_for(plan)))

    # 落盘后 validate_report 也必须 FAIL
    bad = make_content(base, {**content_plan(), "titles": ["只有一条"]})
    p = tmp / "content_bad.json"
    p.write_text(json.dumps(bad, ensure_ascii=False, indent=2), encoding="utf-8")
    rc, out = run_validate(p)
    check("content 标题不足 → validate_report FAIL", rc != 0 and "RESULT: FAIL" in out, out[-300:])


def case_review(tmp: Path) -> None:
    base = load_base()
    ok = make_review(base)
    errs = gates.real_report_errors(ok, ROOT)
    check("review 有 observed_outcome 通过", not errs, "；".join(errs)[:300])
    p = tmp / "review_ok.json"
    p.write_text(json.dumps(ok, ensure_ascii=False, indent=2), encoding="utf-8")
    rc, out = run_validate(p)
    check("review 卡 validate_report --require-real PASS", rc == 0 and "RESULT: PASS" in out, out[-400:])

    bad = make_review(base)
    bad["review"] = {"reviewed_at": "2026-10-30"}
    errs = gates.real_report_errors(bad, ROOT)
    check("review 无 observed_outcome 拒", any("observed_outcome" in e for e in errs), errs[:3])

    # 顶层 outcome.observed_outcome 是合法等价路径
    alt = make_review(base)
    alt.pop("review", None)
    alt["outcome"] = {"observed_outcome": "48h 留存 36%，私信 9 次"}
    errs = gates.real_report_errors(alt, ROOT)
    check("review 顶层 outcome.observed_outcome 通过", not errs, "；".join(errs)[:300])

    p = tmp / "review_bad.json"
    p.write_text(json.dumps(bad, ensure_ascii=False, indent=2), encoding="utf-8")
    rc, out = run_validate(p)
    check("review 缺实测 → validate_report FAIL", rc != 0 and "RESULT: FAIL" in out, out[-300:])


def case_unknown_kind(tmp: Path) -> None:
    base = load_base()
    doc = copy.deepcopy(base)
    doc["card_kind"] = "unknown-kind"
    errs = gates.real_report_errors(doc, ROOT)
    check("未知 card_kind → real_report_errors 拒", any("card_kind" in e for e in errs), errs[:3])
    shape = gates.report_shape_errors(doc)
    check("未知 card_kind → report_shape_errors 拒", bool(shape), shape[:2])
    try:
        report_contract.apply(copy.deepcopy(doc))
        raised = False
    except ValueError:
        raised = True
    check("未知 card_kind → apply 抛 ValueError", raised)
    p = tmp / "unknown.json"
    p.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    rc, out = run_validate(p)
    check("未知 card_kind → validate_report FAIL", rc != 0 and "RESULT: FAIL" in out, out[-300:])

    # 非字符串同样拒
    doc2 = copy.deepcopy(base)
    doc2["card_kind"] = 123
    try:
        report_contract.card_kind(doc2)
        raised = False
    except ValueError:
        raised = True
    check("card_kind 非字符串 → ValueError", raised)


def case_attribution_regression() -> None:
    base = load_base()
    sc_before = copy.deepcopy(base["scorecard"])
    doc = copy.deepcopy(base)
    result = report_contract.apply(doc)
    check("attribution apply 返回 card_kind=attribution", result.get("card_kind") == "attribution", result.get("card_kind"))
    check("attribution total 与改造前一致",
          doc["scorecard"]["total"] == sc_before["total"],
          f"{doc['scorecard']['total']} vs {sc_before['total']}")
    check("attribution verdict 与改造前一致",
          doc["scorecard"]["verdict"] == sc_before["verdict"],
          f"{doc['scorecard']['verdict']} vs {sc_before['verdict']}")
    computed = report_contract.compute(copy.deepcopy(sc_before))
    check("attribution total 与独立 compute 一致", computed["total"] == sc_before["total"], computed["total"])
    check("attribution 六维未被动过",
          all(doc["scorecard"][d] == sc_before[d] for d in report_contract.SIX_DIMS))


def case_apply_skip() -> None:
    base = load_base()
    doc = make_content(base)
    before = json.dumps(doc, ensure_ascii=False, sort_keys=True)
    result = report_contract.apply(doc)
    check("content 无 scorecard → apply skip",
          result == {"skipped": True, "card_kind": "content"}, result)
    check("content apply skip 不新增 scorecard", "scorecard" not in doc)
    check("content apply skip 不改写报告", json.dumps(doc, ensure_ascii=False, sort_keys=True) == before)

    partial = make_content(base)
    partial["scorecard"] = {"data_completeness": 4}
    snap = copy.deepcopy(partial["scorecard"])
    result = report_contract.apply(partial)
    check("content 部分六维 → apply skip", result == {"skipped": True, "card_kind": "content"}, result)
    check("content 部分六维不被改写", partial["scorecard"] == snap, partial["scorecard"])

    # 给了完整六维仍照常计算（允许内容任务自评）
    full = make_content(base)
    full["scorecard"] = copy.deepcopy(base["scorecard"])
    result = report_contract.apply(full)
    check("content 完整六维 → 照常计算",
          isinstance(result, dict) and not result.get("skipped") and result.get("card_kind") == "content", result)
    check("content 自评 total 落地", "total" in full["scorecard"] and "verdict" in full["scorecard"])


def case_review_apply_skip() -> None:
    base = load_base()
    doc = make_review(base)
    result = report_contract.apply(doc)
    check("review 无 scorecard → apply skip",
          result == {"skipped": True, "card_kind": "review"}, result)


def main() -> int:
    if not BASE_REPORT.is_file():
        print(f"[FAIL] 基线报告不存在：{BASE_REPORT}")
        print("RESULT: FAIL")
        return 1
    with tempfile.TemporaryDirectory(prefix="card-kind-") as tmpdir:
        tmp = Path(tmpdir)
        case_defaults(tmp)
        case_content_ok(tmp)
        case_content_plan_negatives(tmp)
        case_review(tmp)
        case_unknown_kind(tmp)
        case_attribution_regression()
        case_apply_skip()
        case_review_apply_skip()
    total = 0
    print()
    if FAIL:
        print(f"RESULT: FAIL（{len(FAIL)} 项失败）")
        return 1
    print("RESULT: PASS（card_kind 契约全绿）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
