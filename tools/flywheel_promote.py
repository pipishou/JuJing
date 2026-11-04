#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""F4 反哺：走晋升/退役机制，而不是「有产物就回流」。

依据：聚鲸工作流 §四 自优化四齿——信号 WAL → 积累（同类达 promote_min 即候选）
→ 落地（棘轮，只能升不能降）→ 控制（人类确认点）。退役同样要有依据
（证伪或长期空转），不是靠感觉删。

本工具的边界（故意做窄）：
  * 只写 `runtime/wal.jsonl`（append-only，不删改历史行）
  * **不自动改 SOUL / pack / 提醒策略**——promote 只产出「提案 + 落点」，由人确认后才有人去改
  * 候选 ≠ 同类：`promote-check` 必须打印同类性三问，`promote` 必须带 --cohesion 留痕

零依赖，仅 stdlib。
"""
import argparse
import json
import os
import sys
import time

WAL_SCHEMA = "flywheel.wal.v1"
DEFAULT_MIN = 3            # 同类达 N 条才成候选（工作流 promote_min 口径，项目侧可调）
TARGETS = ("soul", "pack_threshold", "reminder_policy", "model_route")
COHESION_QUESTIONS = (
    "这 N 条是不是同一个原因（不是同一个现象）？",
    "换掉其中任意一条，结论还成立吗？",
    "如果照此改，下次出现同类问题时判据会不会误伤别的场景？",
)


class WalError(RuntimeError):
    pass


def _wal_path(path=None):
    if path:
        return os.path.abspath(path)
    env = os.environ.get("BOT_WAL_FILE")
    if env:
        return os.path.abspath(env)
    runtime = os.environ.get("BOT_RUNTIME_DIR")
    base = os.path.abspath(runtime) if runtime else os.path.join(os.getcwd(), "runtime")
    return os.path.join(base, "wal.jsonl")


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def load(path=None):
    """读 WAL。坏行跳过（append-only 文件被并发追加时可能留半行）。"""
    p = _wal_path(path)
    rows = []
    if not os.path.exists(p):
        return rows
    with open(p, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict) and row.get("schema") == WAL_SCHEMA:
                rows.append(row)
    return rows


def _append(row, path=None):
    p = _wal_path(path)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    return row


def add(kind, clause, detail, evidence=None, project="bot-team", path=None):
    """记一条信号。kind ∈ fix|falsify|feedback|artifact|incident。"""
    if not kind or not clause:
        raise WalError("kind 与 clause 都必填（clause 是锚点，后续靠它聚类）")
    if not str(detail).strip():
        raise WalError("detail 不能为空——没有事实的信号不叫信号")
    row = {
        "schema": WAL_SCHEMA,
        "ts": _now(),
        "project": project,
        "kind": kind,
        "clause": clause,
        "detail": str(detail)[:800],
        "evidence": list(evidence or []),
        "state": "open",
    }
    return _append(row, path)


def promote_check(rows, min_count=DEFAULT_MIN):
    """按 (kind, clause) 聚类，达阈值的才是候选；打印同类性三问所需的样本。"""
    groups = {}
    for r in rows:
        if r.get("state") != "open":
            continue
        key = (r.get("kind"), r.get("clause"))
        groups.setdefault(key, []).append(r)
    out = []
    for (kind, clause), items in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        if len(items) < int(min_count):
            continue
        out.append({
            "kind": kind,
            "clause": clause,
            "count": len(items),
            "samples": [{"ts": i.get("ts"), "detail": i.get("detail")} for i in items[:5]],
            "cohesion_questions": list(COHESION_QUESTIONS),
            "proposed_targets": list(TARGETS),
        })
    return out


def promote(clause, kind, cohesion, target, note, rows=None, min_count=DEFAULT_MIN, path=None):
    """晋升：写入一条 promotion 记录（棘轮 = 只增不改），并把被采纳的 open 信号标记 promoted。

    target 必须是 TARGETS 之一；cohesion 必须逐条回答三问（用 | 分隔三段）。
    返回提案对象——**不修改任何 SOUL/pack 文件**，落地由人按 target 执行。
    """
    if target not in TARGETS:
        raise WalError(f"未知落点：{target}（合法：{', '.join(TARGETS)}）")
    parts = [p.strip() for p in str(cohesion).split("|") if p.strip()]
    if len(parts) < 3:
        raise WalError("cohesion 必须逐条回答同类性三问（三段，用 | 分隔）")
    rows = rows if rows is not None else load(path)
    hits = [r for r in rows if r.get("state") == "open"
            and r.get("kind") == kind and r.get("clause") == clause]
    if len(hits) < int(min_count):
        raise WalError(f"同类信号只有 {len(hits)} 条，未达晋升阈值 {min_count}——不晋升")
    proposal = {
        "schema": WAL_SCHEMA,
        "ts": _now(),
        "kind": "promotion",
        "clause": clause,
        "source_kind": kind,
        "count": len(hits),
        "target": target,
        "cohesion": parts,
        "note": str(note)[:400],
        "state": "promoted",
        "applies_to": [],   # 由人填：具体改了哪个文件哪一行
    }
    _append(proposal, path)
    return proposal


def retire(clause, reason, evidence=None, rows=None, path=None):
    """退役：必须带理由（falsified / idle）+ 证据，append 一条 retirement 记录。"""
    if reason not in ("falsified", "idle", "superseded"):
        raise WalError("reason ∈ falsified|idle|superseded")
    if not evidence:
        raise WalError("退役必须带证据（证伪的实验号 / 空转的观测区间）")
    rows = rows if rows is not None else load(path)
    live = [r for r in rows if r.get("clause") == clause and r.get("kind") == "promotion"
            and r.get("state") == "promoted"]
    if not live:
        raise WalError(f"clause={clause} 没有已晋升的条款，无从退役")
    row = {
        "schema": WAL_SCHEMA,
        "ts": _now(),
        "kind": "retirement",
        "clause": clause,
        "reason": reason,
        "evidence": list(evidence),
        "state": "retired",
    }
    _append(row, path)
    return row


def summary(rows):
    kinds, states = {}, {}
    for r in rows:
        kinds[r.get("kind")] = kinds.get(r.get("kind"), 0) + 1
        states[r.get("state")] = states.get(r.get("state"), 0) + 1
    promoted = [r for r in rows if r.get("state") == "promoted"]
    return {
        "total": len(rows),
        "by_kind": kinds,
        "by_state": states,
        "promotions": len(promoted),
        "retirements": len([r for r in rows if r.get("state") == "retired"]),
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description="F4 反哺：WAL → 候选 → 晋升/退役（只提案，不改产品文件）")
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("add", help="记一条信号")
    p.add_argument("--kind", required=True)
    p.add_argument("--clause", required=True)
    p.add_argument("--detail", required=True)
    p.add_argument("--evidence", action="append", default=[])
    p.add_argument("--project", default="bot-team")

    p = sub.add_parser("check", help="列候选（达阈值）")
    p.add_argument("--min", type=int, default=DEFAULT_MIN)
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("promote", help="晋升一条（需三问 + 落点）")
    p.add_argument("--clause", required=True)
    p.add_argument("--kind", required=True)
    p.add_argument("--cohesion", required=True)
    p.add_argument("--target", required=True, choices=list(TARGETS))
    p.add_argument("--note", default="")
    p.add_argument("--min", type=int, default=DEFAULT_MIN)

    p = sub.add_parser("retire", help="退役（需理由 + 证据）")
    p.add_argument("--clause", required=True)
    p.add_argument("--reason", required=True, choices=["falsified", "idle", "superseded"])
    p.add_argument("--evidence", action="append", required=True)

    p = sub.add_parser("summary", help="统计")
    p.add_argument("--json", action="store_true")

    ap.add_argument("--wal", default=None)
    args = ap.parse_args(argv)
    path = args.wal

    try:
        if args.cmd == "add":
            row = add(args.kind, args.clause, args.detail, args.evidence, args.project, path)
            print(json.dumps({"ok": True, "ts": row["ts"], "clause": row["clause"]}, ensure_ascii=False))
        elif args.cmd == "check":
            cands = promote_check(load(path), args.min)
            if args.json:
                print(json.dumps({"ok": True, "candidates": cands}, ensure_ascii=False, indent=2))
            else:
                if not cands:
                    print(f"候选 0 条（阈值 {args.min}）")
                for c in cands:
                    print(f"[候选] {c['kind']}/{c['clause']} × {c['count']}")
                    for q in c["cohesion_questions"]:
                        print(f"   ? {q}")
        elif args.cmd == "promote":
            pr = promote(args.clause, args.kind, args.cohesion, args.target, args.note,
                         load(path), args.min, path)
            print(json.dumps({"ok": True, "proposal": pr}, ensure_ascii=False, indent=2))
        elif args.cmd == "retire":
            row = retire(args.clause, args.reason, args.evidence, load(path), path)
            print(json.dumps({"ok": True, "retirement": row}, ensure_ascii=False, indent=2))
        elif args.cmd == "summary":
            s = summary(load(path))
            print(json.dumps({"ok": True, **s}, ensure_ascii=False, indent=2) if args.json
                  else f"信号 {s['total']} 条 · 晋升 {s['promotions']} · 退役 {s['retirements']} · {s['by_kind']}")
        else:
            ap.print_help()
            return 2
    except WalError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
