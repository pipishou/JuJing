#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""R2-B 任务候选器：把「连续、够长、带动作」的真人消息聚成待确认任务候选。

口径（可复算）：
  - 只看 `bot_team_msg_human`；`bot_team_msg_bot` 不参与成候选（仅作上下文留存）。
  - 聚合：同一 dialog_id（无则同一 refs.channel）+ 相邻间隔 ≤ 30 分钟的连续 human 消息为一段。
  - 成候选三条件全满足：合并文本 ≥ min_chars 字；命中动作关键词；不是纯寒暄。
  - 幂等：同一 source_event_ids 集合已有候选跳过；被 reject 过的 key 不得复活。
  - 落盘：runtime/candidates/<id>.json（tmp + os.replace 原子写）。
    根目录取 env BOT_RUNTIME_DIR，缺省 <repo>/runtime。

只用 stdlib + 仓内 core/eventlog.py，零第三方依赖。

用法：
    python -B tools/task_candidates.py --build --json
    python -B tools/task_candidates.py --list
    python -B tools/task_candidates.py --confirm C-20261104-001 --by 老板 --task-id T-1
    python -B tools/task_candidates.py --reject C-20261104-002 --by 老板 --reason 闲聊
退出码：0 正常；2 参数/候选状态错误。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import eventlog  # noqa: E402

SCHEMA = "task.candidate.v1"
HUMAN_EVENT = "bot_team_msg_human"
BOT_EVENT = "bot_team_msg_bot"
MERGE_GAP = timedelta(minutes=30)
CANDIDATES_DIRNAME = "candidates"
REJECTED_FILENAME = "rejected_keys.json"

# 动作关键词（命中任一即视为「有活要干」）
KEYWORDS = (
    "要", "做", "查", "评估", "改", "定", "方案", "是否",
    "为什么", "怎么做", "帮我看", "复盘", "风险",
)

# 纯寒暄集合：去掉标点与这些词后为空 → 不是任务
GREETINGS = frozenset({
    "你好", "您好", "大家好", "在吗", "在么", "在不在", "早", "早安", "早上好",
    "中午好", "下午好", "晚上好", "晚安", "哈喽", "hello", "hi", "嗨",
    "谢谢", "多谢", "感谢", "辛苦了", "辛苦", "好的", "好", "收到", "嗯",
    "哦", "ok", "okay", "拜拜", "再见", "没事", "没关系", "没问题", "客气",
})

_PUNCT_RE = re.compile(r"[\s，。！？、；：,.!?;:~～…—\-（）()\[\]【】\"'“”‘’]+")
_SENT_RE = re.compile(r"[。！？!?；;\n]+")


class CandidateError(ValueError):
    """候选不存在 / 状态不允许该动作。"""


# --------------------------------------------------------------------------- #
# 路径 / 读写
# --------------------------------------------------------------------------- #
def runtime_dir() -> Path:
    """运行根目录：env BOT_RUNTIME_DIR 优先，否则 <repo>/runtime。"""
    env = os.environ.get("BOT_RUNTIME_DIR")
    return Path(env) if env else ROOT / "runtime"


def candidates_dir(root=None) -> Path:
    """候选目录：root 为 runtime 根；缺省用 runtime_dir()/candidates。"""
    base = Path(root) if root else runtime_dir()
    return base / CANDIDATES_DIRNAME


def _read_json(path: Path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None


def _write_json(path: Path, obj) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _load_all(root=None) -> list[dict]:
    cdir = candidates_dir(root)
    if not cdir.is_dir():
        return []
    out = []
    for path in sorted(cdir.glob("*.json")):
        if path.name == REJECTED_FILENAME:
            continue
        rec = _read_json(path)
        if isinstance(rec, dict) and rec.get("id"):
            out.append(rec)
    return out


def _load_rejected(root=None) -> list[dict]:
    doc = _read_json(candidates_dir(root) / REJECTED_FILENAME)
    rows = []
    if isinstance(doc, dict):
        raw = doc.get("keys")
        if not isinstance(raw, list):
            raw = []
        rows = raw
    elif isinstance(doc, list):
        rows = doc
    out = []
    for row in rows:
        if isinstance(row, str) and row:
            out.append({"key": row})
        elif isinstance(row, dict) and row.get("key"):
            out.append(dict(row))
    return out


def _key(source_event_ids) -> str:
    ids = "|".join(sorted(str(x) for x in source_event_ids if x))
    return hashlib.sha256(ids.encode("utf-8")).hexdigest()[:16]


# --------------------------------------------------------------------------- #
# 事件解析
# --------------------------------------------------------------------------- #
def _aware(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.astimezone()
    return dt


def _parse_ts(value):
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return _aware(datetime.fromisoformat(text))
    except ValueError:
        return None


def _body(ev: dict) -> str:
    payload = ev.get("payload")
    if not isinstance(payload, dict):
        return ""
    for key in ("body", "text", "content"):
        val = payload.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return ""


def _channel(ev: dict) -> str:
    refs = ev.get("refs")
    if isinstance(refs, dict):
        for key in ("channel", "channel_id"):
            val = refs.get(key)
            if val:
                return str(val)
    payload = ev.get("payload")
    if isinstance(payload, dict) and payload.get("channel_id"):
        return str(payload["channel_id"])
    if isinstance(refs, (list, tuple)) and len(refs) >= 2 and refs[1]:
        return str(refs[1])
    return ""


def _event_id(ev: dict, index: int) -> str:
    return str(ev.get("event_id") or ev.get("id") or f"idx-{index}")


def _next_seq(root, date_str: str) -> int:
    prefix = f"C-{date_str}-"
    seqs = []
    for cand in _load_all(root):
        cid = str(cand.get("id") or "")
        if cid.startswith(prefix):
            try:
                seqs.append(int(cid[len(prefix):]))
            except ValueError:
                continue
    return (max(seqs) if seqs else 0) + 1


# --------------------------------------------------------------------------- #
# 判据
# --------------------------------------------------------------------------- #
def _normalize_for_greeting(text: str) -> str:
    return _PUNCT_RE.sub("", text or "")


def is_greeting(text: str) -> bool:
    """整段只由寒暄词组成（允许重复、标点）→ True。"""
    rest = _normalize_for_greeting(text)
    if not rest:
        return True
    tokens = sorted(
        {_normalize_for_greeting(w) for w in GREETINGS} - {""},
        key=len, reverse=True,
    )
    while rest:
        # 每次从最长候选重新匹配，避免「好的」被更短的「好」抢先吃掉
        token = next((t for t in tokens if rest.startswith(t)), None)
        if token is None:
            return False
        rest = rest[len(token):]
    return True


def _title(text: str, limit: int = 30) -> str:
    first = _SENT_RE.split(text, 1)[0].strip()
    if not first:
        first = text.strip()
    first = " ".join(first.split())
    return first[:limit]


def _segments(humans: list[dict]) -> list[list[dict]]:
    groups: dict[str, list[dict]] = {}
    for ev in humans:
        dialog = str(ev.get("_dialog") or "")
        channel = ev.get("_channel") or ""
        key = f"dialog:{dialog}" if dialog else (f"channel:{channel}" if channel else f"event:{ev['_id']}")
        groups.setdefault(key, []).append(ev)
    out = []
    for items in groups.values():
        items.sort(key=lambda e: (e["_ts_epoch"], e.get("_seq", 0)))
        seg = [items[0]]
        for prev, cur in zip(items, items[1:]):
            gap = cur["_ts"] - prev["_ts"] if cur["_ts"] and prev["_ts"] else None
            if gap is not None and gap <= MERGE_GAP:
                seg.append(cur)
            else:
                out.append(seg)
                seg = [cur]
        out.append(seg)
    out.sort(key=lambda s: (s[0]["_ts_epoch"], s[0].get("_seq", 0)))
    return out


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def build(events, min_chars: int = 40, now=None, root=None, per_message: bool = False) -> list[dict]:
    """从事件流生成候选（不落盘）。同集合/已 reject 的段跳过。"""
    if now is None:
        now_dt = datetime.now().astimezone()
    elif isinstance(now, datetime):
        now_dt = _aware(now)
    else:
        now_dt = _parse_ts(now) or datetime.now().astimezone()
    date_str = now_dt.strftime("%Y%m%d")
    created_at = now_dt.isoformat(timespec="seconds")

    humans = []
    for index, ev in enumerate(events or []):
        if not isinstance(ev, dict) or ev.get("event") != HUMAN_EVENT:
            continue
        body = _body(ev)
        if not body:
            continue
        ts = _parse_ts(ev.get("ts"))
        humans.append({
            "_id": _event_id(ev, index),
            "_body": body,
            "_ts": ts,
            "_ts_epoch": ts.timestamp() if ts else 0.0,
            "_seq": int(ev.get("seq") or 0),
            "_dialog": str(ev.get("dialog_id") or ""),
            "_channel": _channel(ev),
            "_raw_ts": str(ev.get("ts") or ""),
        })

    existing_sets = {
        frozenset(str(x) for x in (cand.get("source_event_ids") or []))
        for cand in _load_all(root)
    }
    rejected = {row["key"] for row in _load_rejected(root)}
    seq = _next_seq(root, date_str)
    out = []

    segments = [[e] for e in humans] if per_message else _segments(humans)
    for seg in segments:
        text = "\n".join(e["_body"] for e in seg).strip()
        chars = len(text)
        if chars < int(min_chars):
            continue
        if not any(word in text for word in KEYWORDS):
            continue
        if is_greeting(text):
            continue
        ids = [e["_id"] for e in seg]
        idset = frozenset(ids)
        key = _key(ids)
        if key in rejected:
            continue
        if idset in existing_sets:
            continue
        channel = next((e["_channel"] for e in seg if e["_channel"]), None)
        out.append({
            "id": f"C-{date_str}-{seq:03d}",
            "schema": SCHEMA,
            "title": _title(text),
            "conclusion_hint": text[:200],
            "source_event_ids": ids,
            "channel": channel,
            "first_ts": seg[0]["_raw_ts"] or None,
            "last_ts": seg[-1]["_raw_ts"] or None,
            "chars": chars,
            "status": "pending",
            "is_demo": False,
            "created_at": created_at,
        })
        seq += 1
        existing_sets.add(idset)
    return out


def save(cands, root=None) -> list[str]:
    """原子写候选文件，返回写入路径。"""
    cdir = candidates_dir(root)
    cdir.mkdir(parents=True, exist_ok=True)
    written = []
    for cand in cands or []:
        if not isinstance(cand, dict) or not str(cand.get("id") or "").strip():
            raise CandidateError("候选缺 id，拒绝落盘")
        path = cdir / f"{cand['id']}.json"
        _write_json(path, cand)
        written.append(str(path))
    return written


def list_candidates(root=None, status: str | None = "pending") -> list[dict]:
    rows = _load_all(root)
    if status and str(status).lower() != "all":
        rows = [c for c in rows if str(c.get("status") or "pending") == str(status)]
    rows.sort(key=lambda c: (str(c.get("created_at") or ""), str(c.get("id") or "")))
    return rows


def list_pending(root=None) -> list[dict]:
    return list_candidates(root, "pending")


def _load_one(root, cand_id: str) -> dict:
    cand_id = str(cand_id or "").strip()
    if not cand_id or "/" in cand_id or "\\" in cand_id or cand_id.startswith("."):
        raise CandidateError(f"非法候选 id：{cand_id!r}")
    rec = _read_json(candidates_dir(root) / f"{cand_id}.json")
    if not isinstance(rec, dict) or not rec.get("id"):
        raise CandidateError(f"候选不存在：{cand_id}")
    return rec


def confirm(root, cand_id: str, by: str = "", task_id: str = "") -> dict:
    """确认候选；重复 confirm 返回既有并带 already=True。"""
    cand = _load_one(root, cand_id)
    if cand.get("status") == "confirmed":
        out = dict(cand)
        out["already"] = True
        return out
    if cand.get("status") == "rejected":
        raise CandidateError(f"候选已 reject，不能 confirm：{cand_id}")
    stamp = eventlog.now_iso()
    cand.update({
        "status": "confirmed",
        "confirmed_by": str(by or ""),
        "task_id": str(task_id or ""),
        "confirmed_at": stamp,
        "updated_at": stamp,
    })
    _write_json(candidates_dir(root) / f"{cand['id']}.json", cand)
    return dict(cand)


def reject(root, cand_id: str, by: str = "", reason: str = "") -> dict:
    """拒绝候选，并登记 key 防止再次成候选。重复 reject 幂等。"""
    cand = _load_one(root, cand_id)
    if cand.get("status") == "rejected":
        out = dict(cand)
        out["already"] = True
        return out
    if cand.get("status") == "confirmed":
        raise CandidateError(f"候选已 confirm，不能 reject：{cand_id}")
    stamp = eventlog.now_iso()
    key = _key(cand.get("source_event_ids") or [cand["id"]])
    cand.update({
        "status": "rejected",
        "rejected_by": str(by or ""),
        "reject_reason": str(reason or ""),
        "rejected_at": stamp,
        "updated_at": stamp,
        "rejected_key": key,
    })
    _write_json(candidates_dir(root) / f"{cand['id']}.json", cand)
    rows = _load_rejected(root)
    if not any(row.get("key") == key for row in rows):
        rows.append({"key": key, "candidate_id": cand["id"], "by": str(by or ""),
                     "reason": str(reason or ""), "at": stamp})
        _write_json(candidates_dir(root) / REJECTED_FILENAME, {"keys": rows})
    return dict(cand)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _out(obj, as_json: bool) -> None:
    if as_json:
        print(json.dumps(obj, ensure_ascii=False))
    else:
        print(json.dumps(obj, ensure_ascii=False, indent=2))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="任务候选器（R2-B）")
    ap.add_argument("--build", action="store_true", help="从事件流生成候选并落盘")
    ap.add_argument("--confirm", metavar="ID", help="确认候选")
    ap.add_argument("--reject", metavar="ID", help="拒绝候选")
    ap.add_argument("--list", action="store_true", help="列候选")
    ap.add_argument("--json", action="store_true", help="单行 JSON 输出")
    ap.add_argument("--root", default=None, help="runtime 根目录（默认 env BOT_RUNTIME_DIR 或 runtime/）")
    ap.add_argument("--by", default="老板")
    ap.add_argument("--task-id", default="")
    ap.add_argument("--reason", default="")
    ap.add_argument("--status", default="pending", help="--list 过滤：pending/confirmed/rejected/all")
    ap.add_argument("--merge", action="store_true", help="按 dialog/频道合并（旧行为）")
    ap.add_argument("--min-chars", type=int, default=40)
    ap.add_argument("--events", default=None, help="覆盖事件流文件（测试用）")
    args = ap.parse_args(argv)
    root = Path(args.root) if args.root else None
    try:
        if args.build:
            path = Path(args.events) if args.events else None
            events = eventlog.iter_events(path)
            built = build(events, args.min_chars, root=root, per_message=not args.merge)
            save(built, root)
            _out({"ok": True, "built": len(built), "ids": [c["id"] for c in built]}, args.json)
            return 0
        if args.confirm:
            cand = confirm(root, args.confirm, args.by, args.task_id)
            _out({"ok": True, "candidate": cand}, args.json)
            return 0
        if args.reject:
            cand = reject(root, args.reject, args.by, args.reason)
            _out({"ok": True, "candidate": cand}, args.json)
            return 0
        if args.list:
            _out({"ok": True, "items": list_candidates(root, args.status)}, args.json)
            return 0
    except CandidateError as exc:
        _out({"ok": False, "error": str(exc)}, args.json)
        return 2
    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
