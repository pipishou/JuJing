#!/usr/bin/env python3
"""人机一致性抽样校准（单签 + 抽样）。

外挂式打分拿不到真值，唯一能测「准不准」的办法：定期抽一张卡，让本人独立打分，
与系统分逐维比。偏差超过阈值 → 判不一致，人机一致性维度标红、卡片强制复核。

只写 runs/<task>/consistency.json（旁证，不改已签报告字节）+ 事件流。

用法：
    python -B tools/consistency_sample.py --list
    python -B tools/consistency_sample.py --propose --task-id T-20261104-001
    python -B tools/consistency_sample.py --record --task-id T-20261104-001 --scores "3,2,1,2,3,0" --by 老板
    python -B tools/consistency_sample.py --status
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import eventlog  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SAMPLE_SCHEMA = "consistency.sample.v1"
DIMS = ["data_completeness", "timeliness", "significance",
        "exclusivity", "history", "human_ai_consistency"]
DEV_LIMIT = 1.5          # 平均绝对偏差超过此值 → 判不一致
MAX_HISTORY = 20


class SampleError(Exception):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def runs_root(runs: Path | None = None) -> Path:
    if runs:
        return Path(runs)
    env = os.environ.get("BOT_RUNS_ROOT")
    return Path(env) if env else ROOT / "runs"


def _load_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def candidates(runs: Path | None = None) -> list[dict]:
    base = runs_root(runs)
    out = []
    if not base.is_dir():
        return out
    for d in sorted(base.iterdir()):
        if not d.is_dir() or not d.name.startswith("T-"):
            continue
        rep = _load_json(d / "report.json")
        if not isinstance(rep, dict) or rep.get("is_demo") is not False:
            continue
        sc = rep.get("scorecard") or {}
        if not all(k in sc for k in DIMS):
            continue
        out.append({
            "task_id": d.name,
            "total": sc.get("total"),
            "verdict": sc.get("verdict"),
            "dims": {k: sc.get(k) for k in DIMS},
            "sampled": (d / "consistency.json").exists(),
        })
    return out


def propose(task_id: str, runs: Path | None = None) -> dict:
    tdir = runs_root(runs) / task_id
    rep = _load_json(tdir / "report.json")
    if not isinstance(rep, dict):
        raise SampleError(f"找不到报告：{tdir / 'report.json'}")
    sc = rep.get("scorecard") or {}
    return {
        "ok": True, "task_id": task_id, "goal": rep.get("goal"),
        "total": sc.get("total"), "verdict": sc.get("verdict"),
        "dims": [{"dim": k, "system": sc.get(k)} for k in DIMS],
        "ask": "请按 0-5 给这六维独立打分（顺序同上），只凭你自己的判断，不看系统分。",
    }


def record(task_id: str, scores: list[int], *, by: str = "老板", runs: Path | None = None,
           events: Path | None = None, limit: float = DEV_LIMIT) -> dict:
    tdir = runs_root(runs) / task_id
    rep = _load_json(tdir / "report.json")
    if not isinstance(rep, dict):
        raise SampleError(f"找不到报告：{tdir / 'report.json'}")
    if len(scores) != len(DIMS):
        raise SampleError(f"scores 必须 {len(DIMS)} 个，收到 {len(scores)}")
    for v in scores:
        if not isinstance(v, int) or not 0 <= v <= 5:
            raise SampleError(f"每维必须是 0-5 的整数，收到 {v!r}")
    sc = rep.get("scorecard") or {}
    dev = []
    for dim, human in zip(DIMS, scores):
        system = sc.get(dim)
        try:
            d = round(abs(float(human) - float(system)), 2)
        except (TypeError, ValueError):
            d = None
        dev.append({"dim": dim, "system": system, "human": human, "deviation": d})
    nums = [x["deviation"] for x in dev if x["deviation"] is not None]
    mean_abs = round(sum(nums) / len(nums), 3) if nums else None
    flagged = mean_abs is not None and mean_abs > limit
    entry = {
        "schema": SAMPLE_SCHEMA, "task_id": task_id, "by": by, "at": _now(),
        "scores": scores, "dims": dev,
        "mean_abs_deviation": mean_abs, "limit": limit, "flagged": flagged,
    }
    path = tdir / "consistency.json"
    doc = _load_json(path) or {"schema": SAMPLE_SCHEMA, "task_id": task_id, "history": []}
    history = [h for h in (doc.get("history") or []) if isinstance(h, dict)]
    history.append(entry)
    doc = {"schema": SAMPLE_SCHEMA, "task_id": task_id, "updated_at": _now(),
           "latest": entry, "count": len(history),
           "mean_abs_deviation": mean_abs, "flagged": flagged,
           "history": history[-MAX_HISTORY:]}
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    ev = Path(events) if events else (ROOT / "runtime" / "events.jsonl")
    eventlog.emit("bot_team_consistency_sampled", actor=by, path=ev,
                  refs=[task_id],
                  payload={"task_id": task_id, "mean_abs_deviation": mean_abs,
                           "flagged": flagged, "by": by})
    return {"ok": True, **entry}


def status(runs: Path | None = None) -> dict:
    rows = candidates(runs)
    sampled, devs, flagged = 0, [], 0
    for c in rows:
        doc = _load_json(runs_root(runs) / c["task_id"] / "consistency.json")
        if not isinstance(doc, dict) or not doc.get("count"):
            continue
        sampled += 1
        if isinstance(doc.get("mean_abs_deviation"), (int, float)):
            devs.append(doc["mean_abs_deviation"])
        if doc.get("flagged"):
            flagged += 1
    return {"ok": True, "candidates": len(rows), "sampled": sampled,
            "unsampled": len(rows) - sampled,
            "mean_abs_deviation": round(sum(devs) / len(devs), 3) if devs else None,
            "flagged": flagged,
            "note": "样本 <3 时该均值不作为结论" if sampled < 3 else ""}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="人机一致性抽样")
    ap.add_argument("--task-id", default="")
    ap.add_argument("--scores", default="")
    ap.add_argument("--by", default="老板")
    ap.add_argument("--runs", default="")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--propose", action="store_true")
    ap.add_argument("--record", action="store_true")
    ap.add_argument("--status", action="store_true")
    args = ap.parse_args(argv)
    runs = Path(args.runs) if args.runs else None

    try:
        if args.status:
            out = status(runs)
        elif args.list:
            out = {"ok": True, "candidates": candidates(runs)}
        elif args.propose:
            out = propose(args.task_id, runs)
        elif args.record:
            parts = [p.strip() for p in args.scores.split(",") if p.strip()]
            out = record(args.task_id, [int(p) for p in parts], by=args.by, runs=runs)
        else:
            ap.print_help()
            return 2
    except (SampleError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 2

    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        if "candidates" in out:
            for c in out["candidates"]:
                print(f"{c['task_id']}  total={c['total']} verdict={c['verdict']} sampled={c['sampled']}")
        elif "dims" in out and "ask" in out:
            print(f"请给 {out['task_id']} 打六维分（0-5）：")
            for d in out["dims"]:
                print(f"  {d['dim']:<22} 系统={d['system']}")
        elif "mean_abs_deviation" in out:
            print(f"[OK] {out['task_id']} 平均偏差 {out['mean_abs_deviation']} "
                  f"{'（不一致，强制复核）' if out['flagged'] else '（在阈值内）'}")
        else:
            print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
