#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""M1 冒烟（B1 自测）：A1–A5 全在 %TEMP% 沙盒里跑，不写仓库 runs/。

覆盖：
  A1 同日连建 2 任务 → task_id 不同、各自 runs/<id>/、历史不被覆盖
  A2 real 报告的占位证据 / 不存在 data_refs → assemble 与 validate_report 均 FAIL
  A3 pending securityGate / rejected decision / 伪 signer → finalize 非 0；
     合法链（proposed + 白名单内 signer）→ approved 且 approval.by == signer
  A4 六维全 5 → total=100.0、verdict 命中「系统建议」；run_task report 的
     scorecard 与 score_scorecard 独立调用一致
  A5 改 pack.roi_anchors → 新报告 ROI 数值随之变化（行业包真被消费）

用法：& $env:PYTHONIOENCODING="utf-8"; python -B tests/m1_smoke.py
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable
ENV = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1")
SIX = ["data_completeness", "timeliness", "significance", "exclusivity", "history", "human_ai_consistency"]
FAILS = []


def run(args, cwd=ROOT, env=None):
    e = dict(ENV)
    if env:
        e.update(env)
    p = subprocess.run([PY, "-B"] + [str(a) for a in args], cwd=str(cwd),
                       capture_output=True, text=True, encoding="utf-8", errors="replace", env=e)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def check(name, ok, detail=""):
    print(("[OK]   " if ok else "[FAIL] ") + name + ((" :: " + str(detail)[:400]) if not ok and detail else ""))
    if not ok:
        FAILS.append(name)
    return ok


def load(p):
    return json.loads(Path(p).read_text(encoding="utf-8-sig"))


def dump(p, doc):
    Path(p).write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def make_result(tmp):
    """demo workflow 结果的变体（可在测试里改 scorecard/recommendation）。"""
    return load(ROOT / "runs" / "demo-result-2026-10-19.json")


def make_data(tmp):
    data = {"meta": {"dataset": "material_refill_T", "production_line": "B"},
            "records": [{"date": "2026-10-05", "production_line": "B", "refill_rate": 7.1, "output_volume": 1200}]}
    dump(tmp / "data_T.json", data)
    return tmp / "data_T.json"


def assemble(tmp, result_doc, name, real=True, extra=()):
    res = tmp / f"result-{name}.json"
    dump(res, result_doc)
    out = tmp / f"report-{name}.json"
    args = [ROOT / "orchestration" / "assemble_report.py", res, out]
    if real:
        args += ["--real", "--task-id", f"T-{name}", "--context", "m1 smoke",
                 "--data-refs", tmp / "data_T.json", "--expected", "T+7 复查"]
    args += list(extra)
    rc, log = run(args)
    return rc, out, log


def bundle(tmp, report, name):
    loop = tmp / f"loop-{name}"
    rc, log = run([ROOT / "tools" / "write_loop_bundle.py", "--report", report, "--outdir", loop,
                   "--analysis-json", ROOT / "team/loop/example-analysis.json",
                   "--goal-json", ROOT / "team/goals/example-goal.json",
                   "--cost-json", ROOT / "team/cost/example-cost.json"])
    return rc, loop / "decision.json", log


def finalize(report, loop, sig, tmp, allow=None, env=None):
    args = [ROOT / "tools" / "finalize_delivery.py", "--report", report,
            "--loop-dir", loop, "--signature", sig]
    if allow is not None:
        args += ["--allow-signers", allow]
    return run(args, env=env)


def case_a1(tmp):
    roots = tmp / "runs"
    base = dict(BOT_RUNS_ROOT=str(roots))
    ids = []
    for _ in range(2):
        rc, log = run([ROOT / "tools" / "run_task.py", "--demo",
                       "--workflow-result", ROOT / "runs" / "demo-result-2026-10-19.json",
                       "--expected", "T+7 复查 ≤5.40%"], env=base)
        check("A1 run_task rc0", rc == 0, log[-600:])
        if rc == 0:
            dirs = sorted(p.name for p in roots.iterdir() if p.is_dir())
            ids = dirs
    check("A1 连建任务 id 不同", len(ids) == 2 and ids[0] != ids[1], ids)
    check("A1 各自 runs/<id>/ 且都有 task.json",
          all((roots / i / "task.json").is_file() for i in ids) and len(ids) == 2, ids)
    check("A1 重跑不覆盖历史（两次目录都在）", len(list(roots.glob("T-*"))) == 2,
          list(roots.glob("T-*")))


def case_a2(tmp):
    base = make_result(tmp)
    base["attribution"]["scorecard"] = {"data_completeness": {"score": 3, "evidence": "demo 占位"}}
    rc, out, log = assemble(tmp, base, "a2-placeholder")
    check("A2 占位证据 assemble 非 0", rc != 0, log[-500:])
    check("A2 占位证据不落半成品", not out.exists())
    rc, out, log = assemble(tmp, make_result(tmp), "a2-badref",
                            extra=["--data-refs", tmp / "no_such_T.json"])
    check("A2 不存在 data_refs assemble 非 0", rc != 0, log[-500:])
    # validate_report --require-real：占位证据
    rc, out, log = assemble(tmp, make_result(tmp), "a2-ok")
    report = load(out)
    report["finding"][0]["evidence"] = ["demo 占位：未提供"]
    bad = tmp / "a2-bad-report.json"
    dump(bad, report)
    rc, log = run([ROOT / "tests" / "validate_report.py", bad, "--require-real"])
    check("A2 validate_report real 拒占位", rc != 0, log[-500:])


def case_a3(tmp):
    rc, report, log = assemble(tmp, make_result(tmp), "a3")
    check("A3 assemble rc0", rc == 0, log[-400:])
    rc, dec, log = bundle(tmp, report, "a3")
    d = load(dec)
    check("A3 四卡 decision.status==proposed", d.get("status") == "proposed", d.get("status"))
    # 伪 signer：有正确 sha256、无 signer 白名单
    sig = tmp / "sig-fake.json"
    run([ROOT / "tools" / "sign_delivery.py", report, "--signer", "fake-signer-zzz-9000", "--out", sig])
    rc, log = finalize(report, dec.parent, sig, tmp)
    check("A3 未配置白名单 finalize 非 0", rc != 0 and "未配置 signer 白名单" in log, log[-400:])
    rc, log = finalize(report, dec.parent, sig, tmp, allow="老板,boss")
    check("A3 伪 signer 不在白名单 finalize 非 0", rc != 0, log[-400:])
    check("A3 拒绝后 decision 仍是 proposed", load(dec).get("status") == "proposed")
    # 伪签名：只有 sha256 没有 signer
    import hashlib
    nosig = tmp / "sig-nosigner.json"
    dump(nosig, {"report_sha256": hashlib.sha256(Path(report).read_bytes()).hexdigest()})
    rc, log = finalize(report, dec.parent, nosig, tmp, allow="老板")
    check("A3 无 signer 伪签名 finalize 非 0", rc != 0, log[-400:])
    # 合法链：老板签 + 白名单（flag 或 env BOT_SIGNERS 均可）；动作可逆 → 无 securityGate 阻塞
    rev = make_result(tmp)
    rev["advice"]["security_gate_requested"] = False
    rev["advice"]["recommendation"] = [{"action": "优化保养排程", "irreversibility": "reversible",
                                        "roi": {"cost": "¥500", "avoided": "¥500", "anchor": "a"}}]
    rc, report_g, log = assemble(tmp, rev, "a3-legit")
    rc, dec_g, log = bundle(tmp, report_g, "a3-legit")
    good = tmp / "sig-boss.json"
    run([ROOT / "tools" / "sign_delivery.py", report_g, "--signer", "老板", "--out", good])
    rc, log = finalize(report_g, dec_g.parent, good, tmp, allow="老板,boss")
    check("A3 合法链 approved", rc == 0 and load(dec_g).get("status") == "approved", log[-400:])
    check("A3 approval.by==传入 signer", load(dec_g).get("approval", {}).get("by") == "老板",
          load(dec_g).get("approval"))
    # env 回退 BOT_SIGNERS
    rc, report2, _ = assemble(tmp, rev, "a3-legit2")
    rc, dec2, _ = bundle(tmp, report2, "a3-legit2")
    good2 = tmp / "sig-boss2.json"
    run([ROOT / "tools" / "sign_delivery.py", report2, "--signer", "老板", "--out", good2])
    rc, log = finalize(report2, dec2.parent, good2, tmp, env={"BOT_SIGNERS": "老板"})
    check("A3 env BOT_SIGNERS 回退 approved", rc == 0, log[-400:])
    # pending securityGate：不可逆动作 + gate 未确认
    doc = make_result(tmp)
    doc["advice"]["recommendation"] = [{"action": "停线检修", "irreversibility": "irreversible",
                                        "roi": {"cost": "¥1200", "avoided": "¥1200", "anchor": "dict"}}]
    rc, report3, log = assemble(tmp, doc, "a3-gate")
    rc, dec3, _ = bundle(tmp, report3, "a3-gate")
    good3 = tmp / "sig-boss3.json"
    run([ROOT / "tools" / "sign_delivery.py", report3, "--signer", "老板", "--out", good3])
    rc, log = finalize(report3, dec3.parent, good3, tmp, allow="老板")
    check("A3 pending securityGate finalize 非 0", rc != 0 and "securityGate" in log, log[-400:])


def case_a4(tmp):
    doc = make_result(tmp)
    doc["attribution"]["scorecard"] = {k: 5 for k in SIX}
    rc, report, log = assemble(tmp, doc, "a4")
    check("A4 assemble rc0", rc == 0, log[-400:])
    sc = load(report)["scorecard"]
    check("A4 全 5 → total=100.0", sc.get("total") == 100.0, sc.get("total"))
    check("A4 verdict=output/系统建议", sc.get("verdict") == "output" and sc.get("label") == "系统建议",
          (sc.get("verdict"), sc.get("label")))
    rc, log = run([ROOT / "tests" / "validate_report.py", report, "--require-real"])
    check("A4 100 分报告 validate_report --require-real PASS", rc == 0, log[-400:])
    rc, log = run([ROOT / "tools" / "run_task.py", "--demo", "--outdir", tmp / "a4-task",
                   "--workflow-result", ROOT / "runs" / "demo-result-2026-10-19.json",
                   "--expected", "T+7 复查"])
    check("A4 run_task rc0", rc == 0, log[-400:])
    r = load(tmp / "a4-task" / "report.json")
    check("A4 run_task report 含 total/verdict", "total" in r["scorecard"] and "verdict" in r["scorecard"])
    rc, log = run([ROOT / "tools" / "score_scorecard.py", "--from-report", tmp / "a4-task" / "report.json"])
    ind = json.loads(log[log.index("{"):]) if "{" in log else {}
    check("A4 run_task 与 score_scorecard 独立调用一致",
          ind.get("total") == r["scorecard"].get("total") and ind.get("verdict") == r["scorecard"].get("verdict"),
          (ind.get("total"), r["scorecard"].get("total")))
    # B16：100 分制新口径 —— 每维给 100（百分制）也必须精确 100.0 / output
    pct = {k: 100 for k in SIX}
    rc, log = run([ROOT / "tools" / "score_scorecard.py", "--scores",
                   json.dumps(pct, ensure_ascii=False)])
    obj = json.loads(log[log.index("{"):]) if "{" in log else {}
    check("A4 百分制输入全 100 → total=100.0/output",
          obj.get("total") == 100.0 and obj.get("verdict") == "output",
          (obj.get("total"), obj.get("verdict")))
    # B16：60-79 档位 = force_manual_review（不得直接输出）
    mid = {k: 70 for k in SIX}
    rc, log = run([ROOT / "tools" / "score_scorecard.py", "--scores",
                   json.dumps(mid, ensure_ascii=False)])
    obj = json.loads(log[log.index("{"):]) if "{" in log else {}
    check("A4 60-79 档位 force_manual_review",
          obj.get("total", 0) >= 60 and obj.get("total", 0) < 80
          and obj.get("verdict") == "force_manual_review",
          (obj.get("total"), obj.get("verdict")))
    # B16：硬护栏阈值来自 pack.json（人机一致性 0 → human_ai_conflict）
    guarded = dict(pct, human_ai_consistency=0)
    rc, log = run([ROOT / "tools" / "score_scorecard.py", "--scores",
                   json.dumps(guarded, ensure_ascii=False)])
    obj = json.loads(log[log.index("{"):]) if "{" in log else {}
    check("A4 人机一致性=0 → 硬护栏 force_manual_review",
          "human_ai_conflict" in (obj.get("guardrails") or [])
          and obj.get("verdict") == "force_manual_review",
          (obj.get("guardrails"), obj.get("verdict")))


def case_a5(tmp):
    pack = load(ROOT / "packs" / "manufacturing" / "pack.json")
    doc = make_result(tmp)
    doc["advice"]["recommendation"] = [{"action": "更换模具 M-03", "irreversibility": "reversible"}]
    vals = []
    for v in (777, 999):
        pack["roi_anchors"]["mold_change"] = v
        pp = tmp / f"pack-{v}.json"
        dump(pp, pack)
        rc, report, log = assemble(tmp, doc, f"a5-{v}", extra=["--pack-path", pp])
        check(f"A5 assemble rc0 (mold_change={v})", rc == 0, log[-400:])
        r = load(report)
        vals.append(r["recommendation"][0]["roi"]["cost"])
        check(f"A5 report.roi_anchors 反映 pack ({v})",
              r.get("roi_anchors", {}).get("mold_change") == v, r.get("roi_anchors"))
    check("A5 pack 改锚点 → 报告 ROI 随之变化", vals == ["¥777", "¥999"], vals)


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    tmp = Path(tempfile.mkdtemp(prefix="m1-smoke-"))
    print(json.dumps({"ok": True, "tmp": str(tmp)}, ensure_ascii=False))
    make_data(tmp)
    for name, fn in (("A1", case_a1), ("A2", case_a2), ("A3", case_a3), ("A4", case_a4), ("A5", case_a5)):
        print(f"== {name} ==")
        try:
            fn(tmp)
        except Exception as exc:  # noqa: BLE001
            check(f"{name} 未抛异常", False, f"{type(exc).__name__}: {exc}")
    ok = not FAILS
    print("RESULT:", "PASS（A1–A5 全绿）" if ok else f"FAIL ({len(FAILS)} 项: {FAILS})")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
