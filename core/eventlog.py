"""全局事件流（MVP E1）：append-only JSONL，bot_team_* 命名。

为什么另建一个而不是复用 core/events.py：
  现有 `core/events.py` 是**按任务**写 `runs/<task>/run.log` 的，`seq` 也是任务内序号。
  MVP 需要的是**跨任务、跨对话**的一条主线（对话→事项→提醒→卡片→回填都在上面投影），
  所以另建 `runtime/events.jsonl`，只复用「seq + O_APPEND + 单行 JSON」这个模式。

口径（K-07 可复算）：
  - 每次 emit 一行，含 `seq`（单调递增）+ `event_id`（uuid4）+ `ts`（ISO 带时区）
  - 只 append，不修改历史；错账用新事件勘误
  - 坏行读时跳过并计数（不静默吞掉：`iter_events(strict=True)` 会抛）
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import pathlib
import re
import threading
import uuid

ROOT = pathlib.Path(__file__).resolve().parents[1]
EVENT_PREFIX = "bot_team_"
EVENT_RE = re.compile(r"^bot_team_[a-z0-9_]+$")

_lock = threading.Lock()


class EventError(ValueError):
    """事件不合法。"""


def events_path() -> pathlib.Path:
    env = os.environ.get("BOT_EVENTS_FILE")
    return pathlib.Path(env) if env else ROOT / "runtime" / "events.jsonl"


def now_iso() -> str:
    return _dt.datetime.now().astimezone().isoformat(timespec="seconds")


def _read_lines(path: pathlib.Path) -> list[str]:
    if not path.is_file():
        return []
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        return [ln for ln in fh.read().splitlines() if ln.strip()]


def iter_events(path: pathlib.Path | None = None, *, strict: bool = False) -> list[dict]:
    p = pathlib.Path(path) if path else events_path()
    out, bad = [], 0
    for line in _read_lines(p):
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            bad += 1
            if strict:
                raise EventError(f"坏行（第 {bad} 条）：{line[:80]}")
            continue
        if isinstance(rec, dict):
            out.append(rec)
        else:
            bad += 1
    return out


def last_seq(path: pathlib.Path | None = None) -> int:
    seqs = [int(e.get("seq") or 0) for e in iter_events(path)]
    return max(seqs) if seqs else 0


def validate(record: dict) -> None:
    name = record.get("event") or ""
    if not EVENT_RE.match(str(name)):
        raise EventError(f"事件名必须以 {EVENT_PREFIX} 开头且小写下划线：{name!r}")
    for key in ("seq", "ts", "event", "event_id"):
        if not record.get(key):
            raise EventError(f"缺字段 {key}")
    if not isinstance(record.get("payload", {}), dict):
        raise EventError("payload 必须是对象")


def emit(event: str, *, actor: str = "system", dialog_id: str | None = None,
         refs: dict | None = None, payload: dict | None = None,
         path: pathlib.Path | None = None, ts: str | None = None) -> dict:
    """追加一条事件并返回它。并发安全（进程内锁 + O_APPEND 单次写）。"""
    p = pathlib.Path(path) if path else events_path()
    if not str(event).startswith(EVENT_PREFIX):
        event = f"{EVENT_PREFIX}{event}"
    with _lock:
        p.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "seq": last_seq(p) + 1,
            "event_id": uuid.uuid4().hex,
            "ts": ts or now_iso(),
            "event": event,
            "actor": actor,
        }
        if dialog_id:
            record["dialog_id"] = dialog_id
        if refs:
            record["refs"] = refs
        record["payload"] = payload or {}
        validate(record)
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
        fd = os.open(str(p), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        try:
            os.write(fd, line.encode("utf-8"))
        finally:
            os.close(fd)
        return record


def stats(path: pathlib.Path | None = None) -> dict:
    rows = iter_events(path)
    by: dict[str, int] = {}
    for r in rows:
        by[str(r.get("event"))] = by.get(str(r.get("event")), 0) + 1
    return {
        "path": str(pathlib.Path(path) if path else events_path()),
        "count": len(rows),
        "last_seq": max((int(r.get("seq") or 0) for r in rows), default=0),
        "first_ts": rows[0].get("ts") if rows else None,
        "last_ts": rows[-1].get("ts") if rows else None,
        "by_event": dict(sorted(by.items())),
        "dialogs": len({r.get("dialog_id") for r in rows if r.get("dialog_id")}),
    }


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description="bot-team 全局事件流")
    ap.add_argument("--emit", help="事件名（可省略 bot_team_ 前缀）")
    ap.add_argument("--actor", default="system")
    ap.add_argument("--dialog", help="dialog_id")
    ap.add_argument("--payload", help="payload JSON 字符串")
    ap.add_argument("--tail", type=int, help="打印最后 N 条")
    ap.add_argument("--stats", action="store_true")
    ap.add_argument("--file", help="事件文件（默认 env BOT_EVENTS_FILE 或 runtime/events.jsonl）")
    args = ap.parse_args(argv)
    path = pathlib.Path(args.file) if args.file else None

    if args.emit:
        payload = json.loads(args.payload) if args.payload else {}
        rec = emit(args.emit, actor=args.actor, dialog_id=args.dialog, payload=payload, path=path)
        print(json.dumps(rec, ensure_ascii=False, indent=2))
        return 0
    if args.tail:
        rows = iter_events(path)[-args.tail:]
        for r in rows:
            print(json.dumps(r, ensure_ascii=False))
        return 0
    print(json.dumps(stats(path), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
