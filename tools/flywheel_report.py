# -*- coding: utf-8 -*-
"""飞轮聚合器：把 .telemetry/flywheel.jsonl 变成可决策的指标。

设计原则：
  1. 只算能支撑商业决策的承重指标（命中率/采纳率/介入率/端到端/单轮成本/角色失败率），
     不产事件计数堆砌。
  2. 每个指标都写明分子分母口径，口径改了必须同步本文件，否则报告不可复算。
  3. 缺字段不猜：直接列进「数据缺口」，缺口本身就是待补的埋点。

用法：
  python tools/flywheel_report.py                       # 控制台 + Markdown
  python tools/flywheel_report.py --json                # 机器可读
  python tools/flywheel_report.py --out runs/flywheel-2026-10-27.md
  python tools/flywheel_report.py --since 2026-10-20    # 只看该日期之后
"""
import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.telemetry import emit, read  # noqa: E402

LETTERS = {"a": "采纳", "b": "转人工", "c": "转分析"}
STEP_LABELS = {"s1": "取数快照", "s2": "因子表", "s3": "排除", "s4": "建议",
               "s5": "报告", "s6": "交付"}
VERDICTS = ("hit", "miss", "partial")   # closure.json 的判定值；partial 不算命中


def _readj(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except Exception:  # noqa: BLE001
        return {}


def ingest_reviews(root=None):
    """把人工回填的 closure.json 判定转成 review 事件（幂等：同一任务只记一次）。

    为什么要采集：回看结论是人工写进 JSON 的，没有代码路径会触发埋点；
    而命中率是这套产品唯一能证明「建议真的管用」的指标，不能靠人手抄。
    """
    base = Path(root) if root else ROOT
    done = {r.get("task_id") for r in read(root) if r.get("name") == "review"}
    seen, emitted = set(), []
    for clo in sorted((base / "runs").rglob("closure.json")):
        doc = _readj(clo)
        verdict = str(doc.get("verdict") or "").strip().lower()
        if verdict not in VERDICTS:
            continue
        tdir = clo.parent.parent if clo.parent.name == "loop" else clo.parent
        task_id = tdir.name
        if task_id in done or task_id in seen:
            continue
        seen.add(task_id)
        props = {"task_id": task_id, "hit": verdict == "hit"}
        actual = str(doc.get("actual_outcome") or "").strip()
        if actual:
            props["actual"] = actual[:200]
        try:
            mtime = datetime.fromtimestamp(clo.stat().st_mtime).astimezone()
            props["reviewed_at"] = mtime.isoformat(timespec="seconds")
            created = _dt(_readj(tdir / "task.json").get("created_at"))
            if created and created.tzinfo is None:
                created = created.astimezone()
            if created:
                props["days_after"] = max(0, (mtime - created).days)
        except OSError:
            pass
        try:
            emit("review", props, root=root)
            emitted.append(task_id)
        except Exception:  # noqa: BLE001 — 采集失败不改主流程
            continue
    return emitted


def _dt(value):
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def _pct(num, den):
    return round(100.0 * num / den, 1) if den else None


def _pctile(values, q):
    if not values:
        return None
    ordered = sorted(values)
    idx = min(len(ordered) - 1, int(round((len(ordered) - 1) * q)))
    return ordered[idx]


def _num(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def load(root=None, since=None):
    rows = read(root)
    if since:
        cut = _dt(since)
        if cut:
            rows = [r for r in rows if (_dt(r.get("ts")) or cut) >= cut]
    return rows


def analyze(rows):
    # 作废先算：append-only 下不删历史行，靠 void 事件把整单排除出所有指标
    total_in = len(rows)
    voided = {r.get("task_id") for r in rows
              if r.get("name") == "void" and r.get("task_id")}
    rows = [r for r in rows
            if r.get("name") != "void" and r.get("task_id") not in voided]
    by_task = defaultdict(lambda: defaultdict(list))
    changes = []
    gaps = defaultdict(int)
    for row in rows:
        name = row.get("name")
        if not name:
            gaps["未带 event name 的行"] += 1
            continue
        task_id = row.get("task_id")
        by_task[task_id][name].append(row)
        if name == "role_change":
            changes.append(row)
        if name == "review" and row.get("hit") is None:
            gaps["review.hit 缺失"] += 1
        if name == "reply" and row.get("letter") not in LETTERS:
            gaps["reply.letter 非法或缺失"] += 1

    # ---- 任务级 ----
    created = report = signed = 0
    e2e, cost_per_task, findings = [], [], []
    reply_total = defaultdict(int)
    review_rows = []
    for task_id, evs in by_task.items():
        if not task_id:
            continue
        if evs.get("task_created"):
            created += 1
        if evs.get("report_built"):
            report += 1
            findings.append(_num(evs["report_built"][-1].get("finding_count")))
        if evs.get("sign"):
            signed += 1
        started = _dt((evs.get("task_created") or [{}])[0].get("ts"))
        finished = _dt((evs.get("report_built") or [{}])[-1].get("ts"))
        if started and finished and finished >= started:
            e2e.append((finished - started).total_seconds())
        # 成本只在真有 cost_cny 时才计入：没有成本埋点的任务记 0 会把均值做假（空值不是 0）
        costed = [r for r in evs.get("step_run", []) if r.get("cost_cny") is not None]
        if costed:
            cost_per_task.append(sum(_num(r.get("cost_cny")) for r in costed))
        for r in evs.get("reply", []):
            reply_total[r.get("letter")] += 1
        review_rows.extend(evs.get("review", []))

    # ---- 角色级 ----
    steps_by_role = defaultdict(lambda: {"ok": 0, "bad": 0, "ms": [], "cost": 0.0,
                                          "costed": 0})
    for evs in by_task.values():
        for r in evs.get("step_run", []):
            slot = steps_by_role[r.get("role") or "?"]
            if str(r.get("status")) == "ok":
                slot["ok"] += 1
            else:
                slot["bad"] += 1
            slot["ms"].append(_num(r.get("duration_ms")))
            if r.get("cost_cny") is not None:
                slot["cost"] += _num(r.get("cost_cny"))
                slot["costed"] += 1

    # ---- 版本对比（飞轮齿：每次改动前后各指标）----
    version_blocks = []
    for row in sorted(changes, key=lambda r: str(r.get("ts"))):
        cut = _dt(row.get("ts"))
        if not cut:
            continue
        before, after = [], []
        for other in rows:
            t = _dt(other.get("ts"))
            if not t:
                continue
            (before if t < cut else after).append(other)

        def _hit_rate(rs):
            rel = [r for r in rs if r.get("name") == "review"]
            hits = sum(1 for r in rel if r.get("hit") is True)
            return _pct(hits, len(rel))

        def _adopt(rs):
            rel = [r for r in rs if r.get("name") == "reply"]
            a = sum(1 for r in rel if r.get("letter") == "a")
            return _pct(a, len(rel))

        version_blocks.append({
            "ts": row.get("ts"), "agent_id": row.get("agent_id"),
            "field": row.get("field"), "before_hash": row.get("before_hash"),
            "after_hash": row.get("after_hash"), "reason": row.get("reason"),
            "hit_rate_before": _hit_rate(before), "hit_rate_after": _hit_rate(after),
            "adopt_before": _adopt(before), "adopt_after": _adopt(after),
        })

    replies = sum(reply_total.values())
    hits = sum(1 for r in review_rows if r.get("hit") is True)
    return {
        "funnel": {"created": created, "report": report, "signed": signed,
                   "reported_rate": _pct(report, created),
                   "signed_rate": _pct(signed, created)},
        "quality": {
            "finding_avg": round(sum(findings) / len(findings), 2) if findings else None,
            "finding_zero_tasks": sum(1 for f in findings if f == 0),
        },
        "adoption": {
            "reply_total": replies,
            "by_letter": {k: reply_total.get(k, 0) for k in ("a", "b", "c")},
            "adopt_rate": _pct(reply_total.get("a", 0), replies),
            "intervention_rate": _pct(reply_total.get("b", 0) + reply_total.get("c", 0), replies),
        },
        "review": {
            "total": len(review_rows), "hit": hits,
            "hit_rate": _pct(hits, len(review_rows)),
        },
        "e2e_seconds": {
            "n": len(e2e), "p50": _pctile(e2e, 0.5), "p90": _pctile(e2e, 0.9),
        },
        "cost": {
            "tasks": len(cost_per_task),
            "per_task_avg": round(sum(cost_per_task) / len(cost_per_task), 4) if cost_per_task else None,
            "total": round(sum(cost_per_task), 4) if cost_per_task else None,
        },
        "roles": {
            role: {"ok": s["ok"], "bad": s["bad"],
                   "fail_rate": _pct(s["bad"], s["ok"] + s["bad"]),
                   "p50_ms": _pctile(s["ms"], 0.5),
                   "cost": round(s["cost"], 4) if s["costed"] else None}
            for role, s in sorted(steps_by_role.items())
        },
        "versions": version_blocks,
        "gaps": dict(gaps),
        "voided": {"tasks": sorted(voided), "rows_excluded": total_in - len(rows)},
        "events_total": total_in,
    }


def render_md(result, source):
    def fmt(v, suffix=""):
        return "—" if v is None else "%s%s" % (v, suffix)

    lines = ["# 飞轮指标报告", "",
             "> 数据源：`%s` ｜ 事件 %d 条" % (source, result["events_total"]), ""]
    v = result.get("voided") or {}
    if v.get("tasks"):
        lines += ["> 已作废任务 %d 个（%s），排除事件 %d 条；历史行保留未删（append-only）。"
                  % (len(v["tasks"]), "、".join(v["tasks"]), v["rows_excluded"]), ""]
    f = result["funnel"]
    lines += ["## 1 漏斗", "",
              "| 阶段 | 数量 | 转化率 |", "|---|---|---|",
              "| 建任务 | %d | — |" % f["created"],
              "| 产出报告 | %d | %s |" % (f["report"], fmt(f["reported_rate"], "%")),
              "| 签字收口 | %d | %s |" % (f["signed"], fmt(f["signed_rate"], "%")), ""]
    a, c, r = result["adoption"], result["cost"], result["review"]
    e = result["e2e_seconds"]
    lines += ["## 2 承重指标", "",
              "| 指标 | 值 | 口径 |", "|---|---|---|",
              "| 报告采纳率 | %s | letter=a ÷ 全部回信 |" % fmt(a["adopt_rate"], "%"),
              "| 人工介入率 | %s | letter∈{b,c} ÷ 全部回信 |" % fmt(a["intervention_rate"], "%"),
              "| 发现命中率 | %s | review.hit=true ÷ review 总数 |" % fmt(r["hit_rate"], "%"),
              "| 端到端耗时 p50 | %s | 建任务→报告 ts 差 |" % fmt(e["p50"], "s"),
              "| 端到端耗时 p90 | %s | 同上 |" % fmt(e["p90"], "s"),
              "| 单任务成本均值 | %s | Σstep_run.cost_cny ÷ 任务数 |" % fmt(c["per_task_avg"], " 元"),
              "| 平均有效发现数 | %s | report_built.finding_count 均值 |" % fmt(result["quality"]["finding_avg"]), ""]
    if e["n"] == 0 or r["total"] == 0 or a["reply_total"] == 0:
        lines += ["> 有指标为空：分母为 0，说明该阶段还没埋点或还没跑过真实任务。空值不是 0。", ""]
    lines += ["## 3 角色执行", "",
              "| 角色 | 成功 | 失败 | 失败率 | p50 耗时 | 成本 |", "|---|---|---|---|---|---|"]
    for role, s in result["roles"].items():
        lines.append("| %s | %d | %d | %s | %s | %s |"
                     % (role, s["ok"], s["bad"], fmt(s["fail_rate"], "%"),
                        fmt(s["p50_ms"], "ms"), fmt(s["cost"], " 元")))
    if not result["roles"]:
        lines.append("| — | 0 | 0 | — | — | — |")
    lines += ["", "## 4 版本对比（飞轮齿）", ""]
    if result["versions"]:
        lines += ["| 时间 | agent | 字段 | 命中率 前→后 | 采纳率 前→后 | 原因 |",
                  "|---|---|---|---|---|---|"]
        for v in result["versions"]:
            lines.append("| %s | %s | %s | %s → %s | %s → %s | %s |"
                         % (v["ts"], v["agent_id"], v["field"],
                            fmt(v["hit_rate_before"], "%"), fmt(v["hit_rate_after"], "%"),
                            fmt(v["adopt_before"], "%"), fmt(v["adopt_after"], "%"),
                            v.get("reason") or "—"))
        lines += ["", "> 改动前样本 <10 单时，前后对比只作线索，不进决策。", ""]
    else:
        lines += ["尚无 role_change 事件：飞轮没有分组键，任何指标变化都无法归因到具体改动。", ""]
    if result["gaps"]:
        lines += ["## 5 数据缺口", ""]
        for k, v in sorted(result["gaps"].items()):
            lines.append("- %s：%d 条" % (k, v))
        lines.append("")
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description="飞轮指标聚合")
    ap.add_argument("--root", default=None, help="bot-team 根目录（默认本仓库）")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--out", default=None, help="把 Markdown 写到指定文件")
    ap.add_argument("--since", default=None, help="只统计该日期(ISO)之后的事件")
    ap.add_argument("--ingest-review", action="store_true",
                    help="先把 closure.json 的人工判定采集成 review 事件（幂等）")
    args = ap.parse_args(argv)

    if args.ingest_review:
        got = ingest_reviews(args.root)
        if got:
            print("[ingest-review] 新增 review 事件：%s" % ", ".join(got), file=sys.stderr)

    rows = load(args.root, args.since)
    result = analyze(rows)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    md = render_md(result, "bot-team/.telemetry/flywheel.jsonl")
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(md, encoding="utf-8")
        print("written:", out)
    else:
        print(md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
