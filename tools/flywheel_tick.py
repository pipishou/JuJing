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
    # 计划任务下 stdout 被重定向到文件，Windows 默认按 GBK 编码 →
    # 工具里一旦打印 `↔`/中文符号就 UnicodeEncodeError（2026-11-04 17:01 实测踩到）。
    env = dict(os.environ)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONUTF8", "1")
    try:
        proc = subprocess.run(
            cmd, cwd=str(ROOT), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout, env=env,
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


# ---------------------------------------------------------------- 失败通知
# 用户 m10487：「失败可以发飞书」。出站沿用 bot-2/tools/lark_send.py（已验证 1.6s），
# 只允许 purpose=mo_p2p（该脚本的白名单只有这一项）。
LARK_SEND_DEFAULT = "D:/work/产品类/个人产品/bot-2/tools/lark_send.py"
NOTIFY_PURPOSE = "mo_p2p"


def notify_enabled() -> bool:
    """BOT_FLYWHEEL_NOTIFY=0 可关；默认开。"""
    return (os.environ.get("BOT_FLYWHEEL_NOTIFY") or "1").strip() != "0"


def lark_send_path() -> Path | None:
    raw = (os.environ.get("BOT_LARK_SEND") or LARK_SEND_DEFAULT).strip()
    p = Path(raw)
    return p if p.is_file() else None


def compose_failure_text(result: dict, slot: str = "", attempts: int = 1,
                         max_attempts: int = 3) -> str:
    bad = [s for s in result.get("steps", []) if not s.get("ok") and not s.get("skipped")]
    head = "飞轮失败"
    if slot:
        head += f" {slot}"
    if max_attempts > 1:
        head += f"（第 {attempts}/{max_attempts} 次）"
    lines = [f"**{head}**", ""]
    for s in bad:
        err = (s.get("error") or "").replace("\n", " ").strip()
        lines.append(f"- `{s.get('step')}` : {err[:240] or '（无错误文本）'}")
    if not bad:
        lines.append("- 有步骤失败，但未取到错误文本（看 runtime/logs/flywheel-due.log）")
    okc = sum(1 for s in result.get("steps", []) if s.get("ok"))
    lines += ["", f"通过 {okc}/{len(result.get('steps', []))} 步，耗时 "
                  f"{result.get('duration_s', 0)}s，finish {result.get('finished_at', '')}"]
    lines.append("处理：看日志 → 修配置/代码 → 手工 `python -B tools/flywheel_tick.py` 复跑")
    return "\n".join(lines)


def notify_failure(result: dict, slot: str = "", attempts: int = 1,
                   max_attempts: int = 3, dry_run: bool = False) -> dict:
    """发一条飞书告警；任何异常都吞掉（告警本身不能把 tick 搞失败）。"""
    if not notify_enabled():
        return {"sent": False, "reason": "disabled_by_env"}
    script = lark_send_path()
    if script is None:
        return {"sent": False, "reason": "lark_send_not_found"}
    text = compose_failure_text(result, slot=slot, attempts=attempts, max_attempts=max_attempts)
    cmd = [sys.executable, "-B", str(script), "--purpose", NOTIFY_PURPOSE,
           "--markdown", text, "--auth", "tenant"]
    if not dry_run:
        cmd.append("--send")
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=60, env=env, cwd=str(ROOT))
    except Exception as exc:  # noqa: BLE001
        return {"sent": False, "reason": f"exception: {exc}"}
    out = (proc.stdout or "").strip().replace("\n", " ")[:300]
    err = (proc.stderr or "").strip().replace("\n", " ")[:200]
    return {"sent": proc.returncode == 0 and not dry_run,
            "dry_run": dry_run, "rc": proc.returncode, "out": out, "err": err}


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
        # 同槽失败可重试（计划任务每 10 分钟一次，窗口 20 分钟 → 至少 2 次机会），
        # 超过 3 次才认输，避免把坏配置变成无限重跑。
        attempts = int((state.get("attempts") or {}).get(key, 0))
        if attempts >= 3:
            print(json.dumps({"ok": False, "skipped": True, "reason": "attempts_exhausted",
                              "slot": key, "attempts": attempts}, ensure_ascii=False))
            return 1
        result = tick(days=args.days, skip=skip)
        attempts_map = dict(state.get("attempts") or {})
        attempts_map[key] = attempts + 1
        if result["ok"]:
            _save_state({"last_slot": key, "last_ok": True, "finished_at": result["finished_at"],
                         "attempts": {}})
        else:
            _save_state({"last_slot": state.get("last_slot"), "last_ok": False,
                         "finished_at": result["finished_at"], "attempts": attempts_map})
            result["notify"] = notify_failure(result, slot=key, attempts=attempts + 1,
                                              max_attempts=3)
        if args.json:
            print(json.dumps(result, ensure_ascii=False))
        else:
            _print(result)
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
    if not result["ok"]:
        result["notify"] = notify_failure(result, max_attempts=0)
    if args.json:
        print(json.dumps(result, ensure_ascii=False))
    else:
        _print(result)
        nf = result.get("notify")
        if nf:
            print(f"  飞书告警：{json.dumps(nf, ensure_ascii=False)}")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
