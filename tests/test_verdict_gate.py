#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""终审门方向锁：只有 output 档可 approved。

历史缺陷（2026-11-02 真实案例实测发现，属于最严重的一类）：
  `core/gates.py` 的 F7 原本只拦 `force_manual_review`，于是
    - `no_output`(33.33 分) 与 `reference_only`(43.33 分) **能签字定案**；
    - `force_manual_review`(60–79 分) 反而被拒、且没有人工复核入口。
  门是反的：最差的能过，该复核的过不了 → 决策卡不可信。

现在改为白名单：只有 `output` 可签，未知/缺失一律 fail-closed。
"""

from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import core.gates as gates  # noqa: E402

FAILS: list[str] = []
CHECKED = 0


def check(name: str, cond: bool) -> None:
    global CHECKED
    CHECKED += 1
    if cond:
        print(f"[ ok ] {name}")
    else:
        FAILS.append(name)
        print(f"[FAIL] {name}")


def main() -> int:
    # 1) 白名单判定本身
    check("output 可签", gates.signable_verdict("output") is True)
    for v in ("force_manual_review", "reference_only", "no_output", "", None, "OUTPUT"):
        check(f"{v!r} 不可签", gates.signable_verdict(v) is False)

    # 2) F7 必须走这个白名单（防止有人改回「只拦 force_manual_review」）
    src = (ROOT / "core" / "gates.py").read_text(encoding="utf-8")
    check("F7 使用 signable_verdict()", "not signable_verdict(_sc.get(\"verdict\"))" in src)
    check("F7 不再只拦 force_manual_review",
          '_sc.get("verdict") == "force_manual_review"' not in src)

    print(f"\n{CHECKED - len(FAILS)}/{CHECKED} 通过")
    if FAILS:
        print("RESULT: FAIL")
        return 1
    print("RESULT: PASS（终审门方向正确：仅 output 可签）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
