# -*- coding: utf-8 -*-
"""事件流（M1.5 §2.2）：runs/<task_id>/run.log 的追加与读取。

契约：
  append_event(run_log, ev) -> int
      追加一行 UTF-8 JSON；自动补 seq（单调 +1）与 ts；返回本次 seq。
      并发安全：O_EXCL 锁文件（跨进程互斥）+ O_APPEND 单次写入（行不交错）。
  iter_events(run_log, since=0, errors=None) -> list[dict]
      读全部事件（按文件顺序，过滤 seq > since）；坏行跳过并计入 errors（若给了列表）。

事件字段（写入方最少给 phase/role/line，其余自动补）：
  seq:int  ts:str(ISO, 本地时区)  phase:str  role:str  agent:str  kind:str  line:str  meta?:dict
  phase 建议 s1..s5 / gate / intake / data / status；kind ∈ user|speak|tool|think|status|artifact。
兼容 M1 旧 run.log：只有 {ts, phase, role, line} 的行也能读——补 kind="status"、agent=角色真名、
seq 按行号回填（读侧不重写文件）。
只依赖 stdlib。
"""
import json
import os
import time
from datetime import datetime
from pathlib import Path

EVENT_PHASES = ("s1", "s2", "s3", "s4", "s5", "gate", "intake", "data", "status")
EVENT_KINDS = ("user", "speak", "tool", "think", "status", "artifact")
EVENT_ROLES = ("host", "analyst", "attr", "biz", "quality", "data")

# 角色短码 → 员工真名（与 server AGENT_BY_ROLE 保持一致；旧 run.log 只有 role 时用它补 agent）
DEFAULT_AGENT_BY_ROLE = {
    "host": "工作助理",
    "analyst": "咨询分析师",
    "attr": "AI 归因专家",
    "biz": "制造业业务专家",
    "quality": "合规质检",
    "data": "取数员",
}

# 员工长 id → 事件 role 短码（run_task/server 可能传长 id）
ROLE_ALIASES = {
    "host": "host",
    "consulting-analyst": "analyst",
    "manufacuring-analyst": "analyst",
    "ai-attribution-expert": "attr",
    "manufacturing-ops-expert": "biz",
    "compliance-reviewer": "quality",
    "report-recorder": "quality",
    "data-fetcher": "data",
}

_LOCK_TIMEOUT = 10.0          # 单次 append 最多等锁 10s
_LOCK_STALE = 60.0            # 锁文件超过 60s 视为持有者已崩，可抢占


def now_iso():
    """本地时区 ISO 时间（秒级），与 M1 旧格式一致。"""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _role_of(value):
    text = str(value or "").strip()
    if not text:
        return "host"
    return ROLE_ALIASES.get(text, text)


def _normalize(ev, seq):
    if not isinstance(ev, dict):
        raise TypeError(f"append_event 事件必须是 dict，收到 {type(ev).__name__}")
    phase = str(ev.get("phase") or "status").strip() or "status"
    role = _role_of(ev.get("role"))
    kind = str(ev.get("kind") or "status").strip() or "status"
    line = ev.get("line")
    out = {
        "seq": int(seq),
        "ts": str(ev.get("ts") or now_iso()),
        "phase": phase,
        "role": role,
        "agent": str(ev.get("agent") or DEFAULT_AGENT_BY_ROLE.get(role, role)),
        "kind": kind,
        "line": "" if line is None else str(line),
    }
    meta = ev.get("meta")
    if isinstance(meta, dict) and meta:
        out["meta"] = meta
    # 允许调用方附加业务字段（task_id/step/...），但 seq/ts 不能被覆盖
    for key, value in ev.items():
        if key not in out and key != "meta":
            out[key] = value
    return out


def _scan(run_log):
    """读文件 → (max_seq, line_count, records)。坏行忽略。"""
    path = Path(run_log)
    max_seq = 0
    count = 0
    if not path.is_file():
        return max_seq, count, []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return max_seq, count, []
    for raw in text.splitlines():
        if not raw.strip():
            continue
        count += 1
        try:
            obj = json.loads(raw)
        except ValueError:
            continue
        seq = obj.get("seq") if isinstance(obj, dict) else None
        try:
            seq = int(seq)
        except (TypeError, ValueError):
            continue
        if seq > max_seq:
            max_seq = seq
    return max_seq, count, []


def _next_seq(run_log):
    max_seq, line_count, _ = _scan(run_log)
    # 旧 run.log 没有 seq：读侧按行号回填 1..N，故写入侧从 max(max_seq, 行数)+1 起算，避免撞号
    return max(max_seq, line_count) + 1


def _acquire_lock(lock_path):
    """O_CREAT|O_EXCL 文件锁；超时/陈锁处理。返回加锁的文件描述符。"""
    deadline = time.monotonic() + _LOCK_TIMEOUT
    while True:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            try:
                os.write(fd, f"{os.getpid()}".encode("ascii"))
            except OSError:
                pass
            return fd
        except FileExistsError:
            try:
                age = time.time() - os.path.getmtime(lock_path)
            except OSError:
                age = 0.0
            if age > _LOCK_STALE:
                try:
                    os.unlink(lock_path)
                except OSError:
                    pass
                continue
            if time.monotonic() > deadline:
                raise TimeoutError(f"等待事件日志锁超时（{_LOCK_TIMEOUT}s）: {lock_path}")
            time.sleep(0.005)


def _release_lock(fd, lock_path):
    try:
        os.close(fd)
    except OSError:
        pass
    try:
        os.unlink(lock_path)
    except OSError:
        pass


def append_event(run_log, ev):
    """追加一条事件，返回 seq。ev 非 dict → TypeError；写盘失败 OSError 上抛。"""
    if not isinstance(ev, dict):
        raise TypeError(f"append_event 事件必须是 dict，收到 {type(ev).__name__}")
    path = Path(run_log)
    path.parent.mkdir(parents=True, exist_ok=True)
    if ev.get("seq") is not None:
        # 允许调用方指定 seq（重放/导入），但仍需单调安全由锁保证
        seq = int(ev["seq"])
    else:
        seq = 0
    lock_path = str(path) + ".lock"
    fd = _acquire_lock(lock_path)
    try:
        if not seq:
            seq = _next_seq(path)
        record = _normalize(ev, seq)
        data = (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")
        out_fd = os.open(str(path), os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o644)
        try:
            os.write(out_fd, data)
            try:
                os.fsync(out_fd)
            except OSError:
                pass
        finally:
            os.close(out_fd)
        return int(seq)
    finally:
        _release_lock(fd, lock_path)


def iter_events(run_log, since=0, errors=None):
    """读取事件列表（seq > since）；坏行跳过，可选把错误记入 errors 列表。"""
    try:
        since = int(since or 0)
    except (TypeError, ValueError):
        since = 0
    path = Path(run_log)
    out = []
    if not path.is_file():
        return out
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        if isinstance(errors, list):
            errors.append({"line": 0, "error": f"读取失败: {exc}"})
        return out
    seq_fallback = 0
    for line_no, raw in enumerate(text.splitlines(), start=1):
        if not raw.strip():
            continue
        seq_fallback += 1
        try:
            obj = json.loads(raw)
        except ValueError as exc:
            if isinstance(errors, list):
                errors.append({"line": line_no, "error": f"JSON 解析失败: {exc}"})
            continue
        if not isinstance(obj, dict):
            if isinstance(errors, list):
                errors.append({"line": line_no, "error": "事件不是 JSON 对象"})
            continue
        try:
            seq = int(obj.get("seq"))
        except (TypeError, ValueError):
            seq = seq_fallback  # 旧 run.log 兼容：按行号回填
        if seq <= since:
            continue
        record = {
            "seq": seq,
            "ts": str(obj.get("ts") or ""),
            "phase": str(obj.get("phase") or "status"),
            "role": _role_of(obj.get("role")),
            "agent": str(obj.get("agent") or ""),
            "kind": str(obj.get("kind") or "status"),
            "line": str(obj.get("line") or ""),
        }
        if not record["agent"]:
            record["agent"] = DEFAULT_AGENT_BY_ROLE.get(record["role"], record["role"])
        if "ts" not in obj:
            record["ts"] = ""
        meta = obj.get("meta")
        if isinstance(meta, dict):
            record["meta"] = meta
        for key, value in obj.items():
            if key not in record and key != "meta":
                record[key] = value
        out.append(record)
    return out


def last_seq(run_log):
    """当前最大 seq（无文件/空文件 → 0）。server 用它判断事件流是否前进。"""
    return _scan(run_log)[0]


if __name__ == "__main__":
    import sys
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        log = Path(tmp) / "run.log"
        print("seq1 =", append_event(str(log), {"phase": "s1", "role": "host", "line": "hello"}))
        print("seq2 =", append_event(str(log), {"phase": "s2", "role": "data-fetcher", "kind": "tool", "line": "取数"}))
        print(json.dumps(iter_events(str(log)), ensure_ascii=False, indent=2))
        log.write_text('{"ts": "old", "phase": "s5", "role": "host", "line": "legacy"}\nbroken\n', encoding="utf-8")
        errs = []
        print(json.dumps(iter_events(str(log), errors=errs), ensure_ascii=False, indent=2))
        print("errors =", errs)
    sys.exit(0)
