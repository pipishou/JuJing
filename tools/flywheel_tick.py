#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""一次飞轮 tick：采集 → 候选 → 周报 → WAL 检查 → 记忆固化。

设计原则（对齐 m10375「把飞轮转起来」）：
- 每一步都是既有的独立工具，本文件只做**编排**，不复制业务逻辑；
- 单步失败不阻断后续步骤，失败原因写进结果与事件（fail-soft）；
- 只读 Tobs 库（由 tobs_collect 保证 `?mode=ro`），只写我方目录；
- 每次 tick 落一条 `bot_team_flywheel_tick` 事件，形成可回放的时间线。

用法：
    python -B tools/flywheel_tick.py                 # 立刻跑一次（手工）
    python -B tools/flywheel_tick.py --json          # 机器可读
    python -B tools/flywheel_tick.py --due           # 只在 11:00/17:00 窗口内跑（给计划任务用）
    python -B tools/flywheel_tick.py --at 11:00,17:00 --loop   # 常驻，到点才跑
    python -B tools/flywheel_tick.py --skip memory,weekly      # 跳过指定步骤
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
sys.path.insert(0, str(ROOT))

from core.timeslots import (  # noqa: E402 - 依赖 sys.path 先就位
    DEFAULT_SLOTS, in_window, next_slot, parse_slots, seconds_until, slot_key,
)

STATE_PATH = ROOT / "runtime" / "flywheel_tick.json"

STEPS = ("collect", "candidates", "weekly", "wal", "memory")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _run(step: str, args: list[str], timeout: int = 300) -> dict:
    """跑一个子工具，返回 {step, ok, rc, summary, error}；任何异常都吞掉。"""
    cmd = [sys.executable, "-B", str(TOOLS / f"{_script(step)}")] + args
    started = time.time()
    try:
        proc = subprocess.run(
            cmd, cwd=str(ROOT), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout,
        )
        out = (proc.stdout or "").strip()
        payload = _parse(out)
        return {
            "step": step,
            "ok": proc.returncode == 0,
            "rc": proc.returncode,
            "elapsed_ms": int((time.time() - started) * 1000),
            "summary": _summary(step, payload, out),
            "error": None if proc.returncode == 0 else _tail(proc.stderr, out),
        }
    except Exception as exc:  # noqa: BLE001 - 单步失败不应打断整条飞轮
        return {
            "step": step, "ok": False, "rc": -1,
            "elapsed_ms": int((time.time() - started) * 1000),
            "summary": {}, "error": f"{type(exc).__name__}: {exc}",
        }


def _script(step: str) -> str:
    return {
        "collect": "tobs_collect.py",
        "candidates": "task_candidates.py",
        "weekly": "flywheel_weekly.py",
        "wal": "flywheel_promote.py",
        "memory": "memory_commit.py",
    }[step]


def _args(step: str, days: int) -> list[str]:
    return {
        "collect": ["--once", "--json"],
        "candidates": ["--build", "--json"],
        "weekly": ["--days", str(days), "--write", "--json"],
        "wal": ["check", "--json"],
        "memory": ["--snapshot", "--json"],
    }[step]


def _parse(text: str):
    if not text:
        return {}
    try:
        return json.loads(text)
    except Exception:  # noqa: BLE001
        pass
    # memory_commit 等工具用 print(dict) 输出 Python 字面量（单引号），用 literal_eval 兜底
    try:
        import ast  # noqa: PLC0415

        value = ast.literal_eval(text.strip())
        if isinstance(value, dict):
            return value
    except Exception:  # noqa: BLE001
        pass
    # 有的工具会在 JSON 前面打印一行人读信息，取最后一行 JSON
    for line in reversed(text.splitlines()):
        line = line.strip()
        if line.startswith("{") or line.startswith("["):
            try:
                return json.loads(line)
            except Exception:  # noqa: BLE001
                continue
    return {"_raw": text[:400]}


def _tail(err: str | None, out: str) -> str:
    text = (err or "").strip() or (out or "").strip()
    return text.splitlines()[-1][:300] if text else "unknown"


def _summary(step: str, payload, out: str) -> dict:
    """把各工具的输出压成统一字段，避免 tick 结果越攒越大。"""
    if not isinstance(payload, dict):
        return {"raw": str(payload)[:200]}
    pick = {
        "collect": ("emitted", "skipped", "errors"),
        "candidates": ("built", "count", "pending"),
        "weekly": ("events_in_window", "issued", "signed", "outcomes", "wal_signals"),
        "wal": ("candidates", "promoted", "retired", "signals"),
        "memory": ("committed", "files", "skipped", "mirrored", "denied"),
    }[step]
    got = {k: payload[k] for k in pick if k in payload}
    if not got:
        got = {"keys": sorted(payload.keys())[:8], "raw": out[:200]}
    return got


def tick(days: int = 7, skip: set[str] | None = None) -> dict:
    skip = skip or set()
    started = _now()
    results = []
    for step in STEPS:
        if step in skip:
            results.append({"step": step, "ok": True, "rc": 0, "skipped": True, "summary": {}, "error": None})
            continue
        results.append(_run(step, _args(step, days)))
    ok = all(r.get("ok") for r in results)
    tick_result = {
        "ok": ok,
        "started_at": started,
        "finished_at": _now(),
        "steps": results,
        "counts": {r["step"]: r.get("summary", {}) for r in results},
    }
    _emit(tick_result)
    return tick_result


def _emit(result: dict) -> None:
    """落一条 tick 事件（失败不抛）。"""
    try:
        from core import eventlog  # noqa: PLC0415 - 允许在无 core 的环境降级

        eventlog.emit(
            "bot_team_flywheel_tick",
            actor="flywheel",
            payload={
                "ok": result["ok"],
                "duration_s": _duration(result),
                "steps": {r["step"]: r.get("ok") for r in result["steps"]},
                "counts": {k: v for k, v in result["counts"].items() if v},
            },
        )
    except Exception:  # noqa: BLE001
        pass


def _duration(result: dict) -> float:
    total = 0
    for r in result["steps"]:
        total += int(r.get("elapsed_ms") or 0)
    return round(total / 1000.0, 2)


def _print(result: dict) -> None:
    mark = "OK " if result["ok"] else "FAIL"
    print(f"[{mark}] flywheel tick  {result['started_at']}  ({_duration(result)}s)")
    for r in result["steps"]:
        if r.get("skipped"):
            print(f"  - {r['step']:<10} skipped")
            continue
        flag = "ok  " if r.get("ok") else "FAIL"
        detail = json.dumps(r.get("summary") or {}, ensure_ascii=False)
        print(f"  {flag} {r['step']:<10} {r.get('elapsed_ms', 0):>6}ms  {detail}")
        if r.get("error"):
            print(f"       error: {r['error']}")


def _slots(args) -> list[tuple[int, int]]:
    raw = (args.at or "").strip()
    if raw:
        return parse_slots(raw)
    env = (os.environ.get("BOT_FLYWHEEL_AT") or "").strip()
    return parse_slots(env or DEFAULT_SLOTS)


def _load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - 坏状态当作没跑过
        return {}


def _save_state(state: dict) -> None:
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="飞轮 tick（采集→候选→周报→WAL→记忆）")
    parser.add_argument("--days", type=int, default=7, help="周报窗口天数")
    parser.add_argument("--skip", default="", help="跳过的步骤，逗号分隔：collect,candidates,weekly,wal,memory")
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    parser.add_argument("--at", default="", help=f"每天的时间槽，逗号分隔（默认 env BOT_FLYWHEEL_AT 或 {DEFAULT_SLOTS}）")
    parser.add_argument("--loop", action="store_true", help="常驻：睡到下一个时间槽再跑（配合 --at）")
    parser.add_argument("--due", action="store_true", help="单次：只在当前时间槽的宽限窗口内且今天没跑过时才跑（给计划任务）")
    parser.add_argument("--grace", type=int, default=20, help="--due 的宽限分钟数（默认 20）")
    parser.add_argument("--max-rounds", type=int, default=0, help="常驻模式最多跑几轮（0=不限）")
    args = parser.parse_args(argv)

    skip = {s.strip() for s in args.skip.split(",") if s.strip()}
    bad = skip - set(STEPS)
    if bad:
        print(f"未知步骤：{','.join(sorted(bad))}", file=sys.stderr)
        return 2

    slots = _slots(args)

    if args.due:
        now = datetime.now()
        if not in_window(slots, now, args.grace):
            print(json.dumps({"ok": True, "skipped": True, "reason": "not_in_window",
                              "slots": [f"{h:02d}:{m:02d}" for h, m in slots]}, ensure_ascii=False))
            return 0
        state = _load_state()
        key = slot_key(now)
        if state.get("last_slot") == key:
            print(json.dumps({"ok": True, "skipped": True, "reason": "already_ran", "slot": key}, ensure_ascii=False))
            return 0
        result = tick(days=args.days, skip=skip)
        _save_state({"last_slot": key, "last_ok": result["ok"], "finished_at": result["finished_at"]})
        print(json.dumps(result, ensure_ascii=False) if args.json else "") if args.json else _print(result)
        return 0 if result["ok"] else 1

    if args.loop:
        rounds = 0
        while True:
            wait = seconds_until(slots, datetime.now())
            print(f"[flywheel] 下一次 {next_slot(slots, datetime.now()).strftime('%Y-%m-%d %H:%M')}"
                  f"（{int(wait)}s 后）", flush=True)
            try:
                time.sleep(wait)
            except KeyboardInterrupt:
                break
            result = tick(days=args.days, skip=skip)
            if args.json:
                print(json.dumps(result, ensure_ascii=False), flush=True)
            else:
                _print(result)
            rounds += 1
            if args.max_rounds and rounds >= args.max_rounds:
                break
        return 0

    result = tick(days=args.days, skip=skip)
    if args.json:
        print(json.dumps(result, ensure_ascii=False))
    else:
        _print(result)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
