#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""F4 反哺（WAL→候选→晋升/退役）+ 证据空白项误拦截修复 的负测试。

跑法：python -B tests/test_promote_and_evidence.py
"""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

from core import gates                     # noqa: E402
import flywheel_promote as fp              # noqa: E402

PASS = 0
FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        print(f"[FAIL] {name} {extra}")


def tmp_wal():
    fd, path = tempfile.mkstemp(suffix=".jsonl")
    os.close(fd)
    os.unlink(path)
    return path


def main():
    base = tempfile.mkdtemp(prefix="gates-base-")
    # ---------- 一、证据空白项：不再误判，但全空仍拒 ----------
    base = {
        "card_kind": "attribution",
        "goal": "补料率异常归因",
        "is_demo": False,
        "task_id": "T-20261104-001",
        "scorecard": {"total": 80.0, "verdict": "output"},
        "cannot_attribute": ["原料批次同一天首次出现"],
        "recommendation": [{"action": "reprioritize", "roi": {"cost": 0, "avoided": 0, "anchor": "pack.roi_anchors.reprioritize"}}],
        "decision_log_entry": {"id": "DL-1", "actor": "lead", "context": "x", "advice": "y",
                               "expected_result": "z", "verification_plan": "T+7 复盘"},
    }

    def findings(ev):
        d = dict(base)
        d["finding"] = [{"factor": "模具磨损", "confidence": 0.35, "evidence": ev}]
        return d

    ok_real = findings(["10-07 起 M-03 模具状态从 normal 变为 slight_wear",
                        "同窗口补料率 5.1% → 7.2%（口径：单线周度）"])
    errs = gates.real_report_errors(ok_real, base)
    check("真实中文证据（含空格）不再被判空", not any("含占位/空证据" in e for e in errs), errs[:2])

    with_blank = findings(["  ", "10-07 起 M-03 模具状态从 normal 变为 slight_wear"])
    errs = gates.real_report_errors(with_blank, base)
    check("空白条目被过滤，不判红", not any("含占位/空证据" in e for e in errs), errs[:2])

    all_blank = findings(["  ", "\n", "\t"])
    errs = gates.real_report_errors(all_blank, base)
    check("全空白仍拒（缺有效证据）", any("缺有效证据" in e for e in errs), errs[:3])

    placeholder = findings(["待补充"])
    errs = gates.real_report_errors(placeholder, base)
    check("真占位词仍红", any("含占位" in e for e in errs), errs[:2])

    # ---------- 二、WAL：记账与幂等 ----------
    wal = tmp_wal()
    fp.add("fix", "gates.evidence.blank", "把空白条目当占位误拦", ["54cbf8e"], path=wal)
    fp.add("fix", "gates.evidence.blank", "中文证据带空格被判空", [], path=wal)
    rows = fp.load(wal)
    check("两条信号落盘", len(rows) == 2, len(rows))
    check("schema 正确", all(r["schema"] == fp.WAL_SCHEMA for r in rows))
    check("state=open", all(r["state"] == "open" for r in rows))
    check("空 detail 拒", _raises(lambda: fp.add("fix", "x", "   ", path=wal), fp.WalError))
    check("缺 clause 拒", _raises(lambda: fp.add("fix", "", "y", path=wal), fp.WalError))

    # ---------- 三、候选：阈值与聚类 ----------
    cands = fp.promote_check(rows, min_count=2)
    check("达阈值成候选", len(cands) == 1 and cands[0]["count"] == 2, cands)
    check("候选带三问", len(cands[0]["cohesion_questions"]) == 3)
    check("未达阈值不成候选", fp.promote_check(rows, min_count=3) == [])
    check("不同 clause 不合并", len(fp.promote_check(
        rows + [{"schema": fp.WAL_SCHEMA, "kind": "fix", "clause": "other", "detail": "d", "state": "open"}],
        min_count=2)) == 1)

    # ---------- 四、晋升：必须三问 + 合法落点 + 达阈值 ----------
    check("落点非法拒", _raises(lambda: fp.promote("gates.evidence.blank", "fix", "a|b|c", "soul2", "", rows, 2, wal), fp.WalError))
    check("三问不足拒", _raises(lambda: fp.promote("gates.evidence.blank", "fix", "a|b", "soul", "", rows, 2, wal), fp.WalError))
    check("未达阈值拒", _raises(lambda: fp.promote("gates.evidence.blank", "fix", "a|b|c", "soul", "", rows, 3, wal), fp.WalError))
    pr = fp.promote("gates.evidence.blank", "fix", "同一原因：判定面过宽|换掉任一条仍成立|误伤风险：真实证据含空格",
                    "soul", "写入 jobs.md 记忆纪律第 3 条", rows, 2, wal)
    check("晋升记录 state=promoted", pr["state"] == "promoted")
    check("晋升记录 blocked 不改产品文件", pr["applies_to"] == [])
    check("晋升后 WAL 三行", len(fp.load(wal)) == 3)

    # ---------- 五、退役：需理由与证据 ----------
    check("退役需已晋升", _raises(lambda: fp.retire("nope", "idle", ["x"], fp.load(wal), wal), fp.WalError))
    check("退役理由受限", _raises(lambda: fp.retire("gates.evidence.blank", "bored", ["x"], fp.load(wal), wal), fp.WalError))
    check("退役需证据", _raises(lambda: fp.retire("gates.evidence.blank", "idle", [], fp.load(wal), wal), fp.WalError))
    rt = fp.retire("gates.evidence.blank", "falsified", ["实测 2026-11-04 三条真实证据仍通过"], fp.load(wal), wal)
    check("退役记录 state=retired", rt["state"] == "retired")

    # ---------- 六、统计 ----------
    s = fp.summary(fp.load(wal))
    check("统计 total=4", s["total"] == 4, s)
    check("统计 promotions=1", s["promotions"] == 1, s)
    check("统计 retirements=1", s["retirements"] == 1, s)
    os.unlink(wal)

    print(f"RESULT: {'PASS' if FAIL == 0 else 'FAIL'}（PASS {PASS} / FAIL {FAIL}）")
    return 0 if FAIL == 0 else 1


def _raises(fn, exc_type):
    try:
        fn()
    except exc_type:
        return True
    except Exception:
        return False
    return False


if __name__ == "__main__":
    sys.exit(main())
