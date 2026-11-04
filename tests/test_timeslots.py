#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""core/timeslots.py 单测：时间槽解析与「下一次几点」的边界。

跑法：python -B tests/test_timeslots.py
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.timeslots import (  # noqa: E402
    DEFAULT_SLOTS, in_window, next_slot, parse_slots, seconds_until, slot_key,
    slots_from_env,
)

PASS = 0
FAIL = []


def check(name: str, cond: bool, extra: str = "") -> None:
    global PASS
    if cond:
        PASS += 1
    else:
        FAIL.append(f"{name} {extra}".strip())


def expect_error(name: str, fn) -> None:
    try:
        fn()
    except ValueError:
        check(name, True)
        return
    except Exception as exc:  # noqa: BLE001
        check(name, False, f"抛了别的异常：{type(exc).__name__}")
        return
    check(name, False, "应当抛 ValueError 却没抛")


def main() -> int:
    # 解析
    check("解析两槽", parse_slots("11:00,17:00") == [(11, 0), (17, 0)])
    check("去空格", parse_slots(" 9:30 , 18:05 ") == [(9, 30), (18, 5)])
    check("去重", parse_slots("11:00,11:00") == [(11, 0)])
    check("乱序排序", parse_slots("17:00,11:00") == [(11, 0), (17, 0)])
    check("单槽", parse_slots("08:00") == [(8, 0)])
    check("默认值常量", DEFAULT_SLOTS == "11:00,17:00")
    expect_error("无冒号报错", lambda: parse_slots("1100"))
    expect_error("非数字报错", lambda: parse_slots("aa:00"))
    expect_error("小时越界", lambda: parse_slots("24:00"))
    expect_error("分钟越界", lambda: parse_slots("11:60"))
    expect_error("空串报错", lambda: parse_slots("  "))

    # env 回落
    check("env 命中", slots_from_env(lambda k: "09:00,15:30") == [(9, 0), (15, 30)])
    check("env 缺失回落默认", slots_from_env(lambda k: None) == [(11, 0), (17, 0)])
    check("env 空串回落默认", slots_from_env(lambda k: "  ") == [(11, 0), (17, 0)])
    check("env 非法回落默认", slots_from_env(lambda k: "oops") == [(11, 0), (17, 0)])

    # next_slot
    slots = [(11, 0), (17, 0)]
    check("当天第一槽前", next_slot(slots, datetime(2026, 11, 4, 9, 0)) == datetime(2026, 11, 4, 11, 0))
    check("两槽之间", next_slot(slots, datetime(2026, 11, 4, 12, 0)) == datetime(2026, 11, 4, 17, 0))
    check("过最后一槽转次日",
          next_slot(slots, datetime(2026, 11, 4, 17, 1)) == datetime(2026, 11, 5, 11, 0))
    check("恰在槽点算下一槽",
          next_slot(slots, datetime(2026, 11, 4, 11, 0)) == datetime(2026, 11, 4, 17, 0))
    check("跨月边界",
          next_slot(slots, datetime(2026, 11, 30, 18, 0)) == datetime(2026, 12, 1, 11, 0))

    # seconds_until
    wait = seconds_until(slots, datetime(2026, 11, 4, 10, 59, 30))
    check("剩余 30 秒", 29 <= wait <= 31, f"got {wait}")
    check("剩余永远为正", seconds_until(slots, datetime(2026, 11, 4, 17, 0)) > 0)

    # in_window
    check("窗口内 11:00", in_window(slots, datetime(2026, 11, 4, 11, 0), 20))
    check("窗口内 11:19", in_window(slots, datetime(2026, 11, 4, 11, 19), 20))
    check("窗口外 11:25", not in_window(slots, datetime(2026, 11, 4, 11, 25), 20))
    check("窗口外 09:00", not in_window(slots, datetime(2026, 11, 4, 9, 0), 20))
    check("17:00 也命中", in_window(slots, datetime(2026, 11, 4, 17, 5), 20))
    check("宽限 0 按 1 分钟算", in_window(slots, datetime(2026, 11, 4, 11, 0), 0))

    # slot_key
    check("slot_key 格式", slot_key(datetime(2026, 11, 4, 11, 0)) == "2026-11-04@11:00")
    check("slot_key 补零", slot_key(datetime(2026, 11, 4, 9, 5)) == "2026-11-04@09:05")

    total = PASS + len(FAIL)
    for item in FAIL:
        print(f"FAIL {item}")
    print(f"RESULT: {'PASS' if not FAIL else 'FAIL'}（PASS {PASS} / FAIL {len(FAIL)} / 共 {total}）")
    return 0 if not FAIL else 1


if __name__ == "__main__":
    raise SystemExit(main())
