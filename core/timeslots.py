#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""时间槽工具：按「每天 11:00、17:00 各一次」这类窗口驱动任务。

为什么单列一个文件：`tools/flywheel_tick.py`（命令行常驻）与 `server/app.py`
（服务后台线程）都要算「下一次该几点跑」，两处各写一遍必然漂移——公理「口径单一来源」。
"""

from __future__ import annotations

from datetime import datetime, timedelta

DEFAULT_SLOTS = "11:00,17:00"


def parse_slots(text: str) -> list[tuple[int, int]]:
    """把 `"11:00,17:00"` 解析成 `[(11,0),(17,0)]`；重复去重、按时间升序。

    非法格式（缺冒号、时/分越界、空串）抛 ValueError——配置错了要当场炸，
    不能静默退化成「每分钟跑一次」。
    """
    slots: list[tuple[int, int]] = []
    for raw in (text or "").split(","):
        item = raw.strip()
        if not item:
            continue
        if ":" not in item:
            raise ValueError(f"时间槽格式应为 HH:MM：{item!r}")
        hh, _, mm = item.partition(":")
        try:
            hour, minute = int(hh), int(mm)
        except ValueError as exc:
            raise ValueError(f"时间槽不是数字：{item!r}") from exc
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError(f"时间槽越界：{item!r}")
        slot = (hour, minute)
        if slot not in slots:
            slots.append(slot)
    if not slots:
        raise ValueError("时间槽为空（例：--at 11:00,17:00）")
    return sorted(slots)


def slots_from_env(getter, key: str = "BOT_FLYWHEEL_AT") -> list[tuple[int, int]]:
    """从环境读取时间槽；缺失或非法回落默认（11:00,17:00）。"""
    raw = (getter(key) or "").strip()
    try:
        return parse_slots(raw or DEFAULT_SLOTS)
    except ValueError:
        return parse_slots(DEFAULT_SLOTS)


def next_slot(slots: list[tuple[int, int]], now: datetime) -> datetime:
    """返回严格晚于 now 的下一个时间点（当天未到的槽，否则次日第一个槽）。"""
    for hour, minute in slots:
        candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate > now:
            return candidate
    hour, minute = slots[0]
    tomorrow = (now + timedelta(days=1)).replace(
        hour=hour, minute=minute, second=0, microsecond=0)
    return tomorrow


def seconds_until(slots: list[tuple[int, int]], now: datetime) -> float:
    """距离下一个时间点还有多少秒（>=1）。"""
    return max(1.0, (next_slot(slots, now) - now).total_seconds())


def slot_key(now: datetime) -> str:
    """窗口标识，用于「今天这一槽跑过没有」的幂等判断：`2026-11-04@11:00`。"""
    return f"{now.strftime('%Y-%m-%d')}@{now.hour:02d}:{now.minute:02d}"


def in_window(slots: list[tuple[int, int]], now: datetime, grace_minutes: int = 20) -> bool:
    """当前是否落在某个槽的宽限窗口内（供 --due 模式：计划任务每 10 分钟喊一次，
    只有命中窗口才真跑，避免计划任务精度问题漏跑）。"""
    for hour, minute in slots:
        start = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if start <= now < start + timedelta(minutes=max(1, grace_minutes)):
            return True
    return False
