#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""交付物版式机检（tools/delivery_lint.py）的离线断言。

判据来自 2026-11-05 老板反馈：「输出物都不能直接使用，结论不在核心位置，沉浸在细节里」。
根因取证：docs/product/DELIVERY_QUALITY_ROOTCAUSE_2026-11-05.md
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import delivery_lint as L  # noqa: E402

PASS = 0
FAIL = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name} {extra}")


GOOD = """# 结论：可以做，报价 15,000，11 月 20 日交付

一句话说完。

## 一、做什么
给销售的询价应答副驾。

## 二、三行数字
- 样本 30 条实测，覆盖率 92%

## 附录 A · 证据
原始记录见 runs/x/data/。
"""

META_FIRST = """# 可行性评估

> 版本 v1.4.1：共五处修订，取代此前各版

## 一、背景
正文。
"""

LONG_BODY = "# 结论：可以做\n\n" + "细节\n" * 200 + "\n## 附录 A\n证据\n"
HEAVY_APPENDIX = "# 结论：可以做\n\n正文一句。\n\n## 附录 A\n" + "证据\n" * 300
PROCESS = "# 结论：可以做\n\n今天这一轮八个错里有六个是同一类。\n\n## 一、做法\n做。\n"
LONG_ONE_SENT = "# 结论：可以做\n\n一句话定义：给销售用的、能把客户问的型号配色材料价格从飞书表里查出第几行并生成草稿的副驾系统。\n"
PCT_NO_SCOPE = "# 结论：可以做\n\n覆盖率 92%。\n"


def main() -> int:
    r = L.lint(GOOD)
    check("好稿通过", r["ok"] is True, str(r["errors"]))
    check("统计正文行数", r["stats"]["body_lines"] > 0)
    check("好稿无附录占比告警", r["stats"]["appendix_ratio"] <= 0.4)

    r2 = L.lint(META_FIRST)
    rules = {e["rule"] for e in r2["errors"]}
    check("修订记录在前 → 红", "conclusion_first" in rules, str(rules))

    r3 = L.lint(LONG_BODY)
    check("正文过长 → 红", any(e["rule"] == "body_length" for e in r3["errors"]), str(r3["errors"]))
    r3b = L.lint(LONG_BODY, {"body_length_max": 500})
    check("阈值可配置", not any(e["rule"] == "body_length" for e in r3b["errors"]))

    r4 = L.lint(HEAVY_APPENDIX)
    check("附录压过正文 → 红", any(e["rule"] == "appendix_ratio" for e in r4["errors"]))

    r5 = L.lint(PROCESS)
    check("过程反思进正文 → 红", any(e["rule"] == "no_process_talk" for e in r5["errors"]))

    r6 = L.lint(LONG_ONE_SENT)
    check("一句话定义过长 → 红", any(e["rule"] == "one_sentence" for e in r6["errors"]))

    r7 = L.lint(PCT_NO_SCOPE)
    check("百分比无口径 → 告警", any(w["rule"] == "numbers_have_scope" for w in r7["warnings"]))

    r8 = L.lint("")
    check("空稿 → 红", r8["ok"] is False)

    check("fixes 可读", any("第" in f for f in L.required_fixes(r2)))

    # 真产物回归（存在才跑，不因缺文件变红）
    real = Path("D:/work/产品类/个人产品/企微+飞书/deliverables/可行性评估-询价应答副驾.md")
    brief = Path("D:/work/产品类/个人产品/企微+飞书/deliverables/决策简报.md")
    if real.is_file():
        rr = L.lint(real.read_text(encoding="utf-8", errors="replace"))
        check("真实 66KB 产物被判红（结论不在前/附录压过）", rr["ok"] is False, str(rr["stats"]))
        check("真实产物触发附录占比", any(e["rule"] == "appendix_ratio" for e in rr["errors"]))
    if brief.is_file():
        rb = L.lint(brief.read_text(encoding="utf-8", errors="replace"))
        check("同批决策简报通过（说明是契约问题不是能力问题）", rb["ok"] is True, str(rb["errors"]))

    print(f"\nRESULT: {'PASS' if FAIL == 0 else 'FAIL'}（PASS {PASS} / FAIL {FAIL}）")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
