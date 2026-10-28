#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""主动发现 · 存量异常复检（detect → create → push）。

职责边界（别把这里当分析器）：
  本脚本只做三件事——用行业包阈值判定「存量数据里有没有越界」、命中后建一个
  runner=external 的任务单（数据已就位）、推一张飞书发现卡。
  它不跑 run_task：归因要 DSH/LLM（server 里 runner=dsh 是 501），用 demo 结果
  冒充真实分析是造假，宁可把 task 停在 created 等外部执行。

判据全部来自行业包 packs/manufacturing/pack.json#thresholds，代码里不写业务数字：
  - 基线 μ/σ 由同目录 T30 数据实算；上限 = μ + 2σ（scene 的 control_line.method
    = mean_2sigma，2 是这个方法的定义，不是业务阈值）
  - 命中 = T 均值 > μ+2σ 且 T 均值 > pack.thresholds.refill_baseline_pct
  - 逐日越界天数按 max(μ+2σ, 包阈值) 计，作为卡片证据

幂等：状态文件 <root>/.telemetry/recheck-state.json，键 = 源任务 + T/T30 双 sha。
  - 同签名只建一个任务；
  - 推送失败不算完成，下次续推同一个任务，不重复建单；
  - 数据变了（sha 变）= 新发现，允许再建一单。

用法：
  python tools/discover_recheck.py                 # 默认 dry：只打印发现
  python tools/discover_recheck.py --apply         # 建单 + 推卡
  python tools/discover_recheck.py --apply --no-push
计划任务：tools/install_discovery_task.ps1（每日 08:30）
"""
import argparse
import hashlib
import json
import os
import shutil
import statistics
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]          # bot-team
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import ids, telemetry  # noqa: E402

T_FILE = "material_refill_T.json"
T30_FILE = "material_refill_T30.json"
SCENE_ID = "scene-refill-rate-first"
PACK_KEYS = ("refill_baseline_pct", "refill_target_pct", "t30_window_days")
SIGMA = 2.0          # mean_2sigma 方法定义，不是业务阈值
MARK_FILE = ".lark-discovered.json"


def now_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _log(msg):
    try:
        print("[discover] " + str(msg), file=sys.stderr, flush=True)
    except Exception:  # noqa: BLE001
        pass


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def write_json(path, doc):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def file_sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def load_pack(pack_path):
    """读行业包并校验阈值键在位；缺键直接失败——阈值是判据，不允许兜底默认值。"""
    doc = read_json(pack_path)
    thr = doc.get("thresholds") or {}
    missing = [k for k in PACK_KEYS if thr.get(k) is None]
    if missing:
        raise SystemExit("[discover][FAIL] 行业包缺阈值键 %s（%s）" % (missing, pack_path))
    return doc, thr


def _rates(doc, path):
    recs = [r for r in (doc.get("records") or [])
            if isinstance(r, dict) and isinstance(r.get("refill_rate"), (int, float))]
    if len(recs) != len(doc.get("records") or []):
        _log("%s 有非数值 refill_rate 记录，已跳过" % Path(path).name)
    return recs


def dataset_stats(t_path, t30_path, thr):
    """算一份数据的越界判据；数据不足返回 None（不猜）。"""
    try:
        t_doc = read_json(t_path)
        b_doc = read_json(t30_path)
    except Exception as exc:  # noqa: BLE001
        _log("读数据失败 %s: %s" % (t_path, exc))
        return None
    t_recs = _rates(t_doc, t_path)
    b_recs = _rates(b_doc, t30_path)
    if not t_recs or len(b_recs) < 2:
        return None
    trates = [float(r["refill_rate"]) for r in t_recs]
    brates = [float(r["refill_rate"]) for r in b_recs]
    t_dates = sorted(str(r.get("date") or "") for r in t_recs if r.get("date"))
    b_dates = sorted(str(r.get("date") or "") for r in b_recs if r.get("date"))
    if not t_dates or not b_dates:
        return None
    base_mean = statistics.mean(brates)
    base_sd = statistics.pstdev(brates)
    upper = base_mean + SIGMA * base_sd
    pack_base = float(thr["refill_baseline_pct"])
    gate = max(upper, pack_base)
    line = str((t_doc.get("meta") or {}).get("production_line")
               or t_recs[0].get("production_line") or "")
    return {
        "t_path": str(t_path),
        "t30_path": str(t30_path),
        "line": line,
        "window": {"start": t_dates[0], "end": t_dates[-1]},
        "baseline_window": {"start": b_dates[0], "end": b_dates[-1]},
        "t_days": len(trates),
        "t_mean": round(statistics.mean(trates), 3),
        "t_max": round(max(trates), 3),
        "base_mean": round(base_mean, 3),
        "base_sd": round(base_sd, 3),
        "upper_2sigma": round(upper, 3),
        "pack_baseline": round(pack_base, 3),
        "gate": round(gate, 3),
        "days_above": sum(1 for v in trates if v > gate),
        "hit": (statistics.mean(trates) > upper) and (statistics.mean(trates) > pack_base),
        "t_records": t_recs,
        "b_records": b_recs,
    }


def scan(runs):
    """扫 runs/*/data 与 runs/* 两处的一级 T 文件；同目录 T30 是基线来源。"""
    seen = set()
    for pattern in ("*/data/" + T_FILE, "*/" + T_FILE):
        for t_path in sorted(Path(runs).glob(pattern)):
            key = t_path.resolve()
            if key in seen:
                continue
            seen.add(key)
            t30 = t_path.with_name(T30_FILE)
            yield t_path, (t30 if t30.is_file() else None)


def series_hash(records):
    """指标序列指纹：只认 (date, refill_rate)，同值不同排版视为同一发现。

    用整文件 sha 会把「同一份数据换个任务目录/换行符」当成新发现，重复建单；
    只对指标序列取指纹，既去重又不漏「数值变了」。
    """
    canon = [[str(r.get("date") or ""), round(float(r["refill_rate"]), 6)] for r in records]
    return hashlib.sha256(json.dumps(canon, ensure_ascii=False).encode("utf-8")).hexdigest()


def signature(st, t_records):
    """发现指纹 = 产线 + T 窗口 + T 指标序列。

    不含基线序列：基线变了但判据仍命中时，用户要看到的是同一条「这个窗口越界」，
    重复推两张卡只是噪音；基线变化已由 hit 判定本身消费（不命中就进不了 hits）。
    """
    return "|".join([st["line"] or "-", st["window"]["start"], st["window"]["end"],
                     series_hash(t_records)[:16]])


def load_state(path):
    try:
        doc = read_json(path)
    except Exception:  # noqa: BLE001
        return {"version": 1, "entries": {}}
    if not isinstance(doc.get("entries"), dict):
        doc["entries"] = {}
    doc.setdefault("version", 1)
    return doc


def save_state(path, state):
    write_json(path, state)


def create_task(runs, st, thr, sig, pack_doc):
    """建 runner=external 的任务单：数据拷进去、状态 created、四卡里的 decision 先立。"""
    task_id = ids.allocate(runs, "task")
    tdir = Path(runs) / task_id
    (tdir / "data").mkdir(parents=True, exist_ok=False)
    shutil.copy2(st["t_path"], tdir / "data" / T_FILE)
    shutil.copy2(st["t30_path"], tdir / "data" / T30_FILE)
    decision_id = ids.allocate(tdir, "decision", task_id=task_id)
    created = now_iso()
    context = ("%s 线 %s~%s 补料率均值 %.2f，基线 μ %s σ %s → 2σ 上限 %.2f，"
               "行业包阈值 %.2f；%d/%d 天高于门槛"
               % (st["line"] or "-", st["window"]["start"], st["window"]["end"],
                  st["t_mean"], st["base_mean"], st["base_sd"], st["upper_2sigma"],
                  st["pack_baseline"], st["days_above"], st["t_days"]))
    task = {
        "task_id": task_id,
        "runner": "external",
        "line": st["line"],
        "scene_id": SCENE_ID,
        "metric": "refill_rate",
        "goal": "复核 %s 线补料率越界（存量数据复检），定位根因并给出可执行建议"
                % (st["line"] or "-"),
        "context": context,
        "target": "补料率回落到基线 2σ 带上限 %.2f 以内" % st["upper_2sigma"],
        "expected": "T+7 复查补料率是否回落",
        "window": st["window"],
        "baseline_window": st["baseline_window"],
        "data_files": [T_FILE, T30_FILE,
                       str(Path("runs") / task_id / "data" / T_FILE),
                       str(Path("runs") / task_id / "data" / T30_FILE)],
        "data_manifest": [
            {"name": T_FILE,
             "path": str(Path("runs") / task_id / "data" / T_FILE),
             "sha256": file_sha(tdir / "data" / T_FILE),
             "size": (tdir / "data" / T_FILE).stat().st_size},
            {"name": T30_FILE,
             "path": str(Path("runs") / task_id / "data" / T30_FILE),
             "sha256": file_sha(tdir / "data" / T30_FILE),
             "size": (tdir / "data" / T30_FILE).stat().st_size},
        ],
        "pack": {"pack_id": pack_doc.get("pack_id"), "version": pack_doc.get("version")},
        "source": {"kind": "discovered", "from_task": st["source_task"],
                   "rule": "mean_2sigma", "signature": sig},
        "created_at": created,
        "updated_at": created,
    }
    write_json(tdir / "task.json", task)
    write_json(tdir / "status.json", {"state": "created", "step": "intake",
                                      "task_id": task_id, "created_at": created,
                                      "updated_at": created,
                                      # 数据是拷进来的，UI 不应显示「数据未就位」
                                      "data_ready": True,
                                      "data_files": [T_FILE, T30_FILE]})
    write_json(tdir / "discovery.json", {
        "signature": sig, "detected_at": created,
        "stats": {k: st[k] for k in ("line", "window", "baseline_window", "t_mean",
                                     "base_mean", "base_sd", "upper_2sigma",
                                     "pack_baseline", "gate", "days_above", "t_days")},
        "source": {"task": st["source_task"], "t": st["t_path"], "t30": st["t30_path"]},
        "thresholds": {k: thr[k] for k in PACK_KEYS},
    })
    write_json(tdir / "loop" / "decision.json", {
        "id": decision_id,
        "task_id": task_id,
        "status": "pending",
        "question": "%s 线补料率 %.2f 已越界，是否立项归因？"
                    % (st["line"] or "-", st["t_mean"]),
        "expected_outcome": "补料率回落到 2σ 带上限 %.2f 内" % st["upper_2sigma"],
        "review_days": 7,
        "created_at": created,
    })
    return task_id, tdir


def build_card(task_id, st, thr):
    """发现卡：与交付回执同一套 a/b/c 按钮（复用既有回调消费者，不新造协议）。"""
    def btn(text, letter, kind="default"):
        return {"tag": "button", "text": {"tag": "plain_text", "content": text},
                "type": kind,
                "behaviors": [{"type": "callback",
                               "value": {"letter": letter, "task_id": task_id}}]}

    return {
        "schema": "2.0",
        "config": {"update_multi": True, "width_mode": "fill"},
        "header": {
            "title": {"tag": "plain_text", "content": "异常复检 · 主动发现"},
            "subtitle": {"tag": "plain_text", "content": task_id},
            "template": "orange",
        },
        "body": {"elements": [
            {"tag": "markdown",
             "content": "**产线**  %s ｜ **指标**  补料率(%%)" % (st["line"] or "-")},
            {"tag": "markdown",
             "content": "**窗口**  %s ~ %s ｜ **均值**  %.2f"
                        % (st["window"]["start"], st["window"]["end"], st["t_mean"])},
            {"tag": "markdown",
             "content": "**基线**  T30 μ %.2f σ %.2f → 2σ 上限 %.2f ｜ **包阈值** %.2f"
                        % (st["base_mean"], st["base_sd"], st["upper_2sigma"],
                           st["pack_baseline"])},
            {"tag": "markdown",
             "content": "**越界**  %d/%d 天高于 %.2f；窗口峰值 %.2f"
                        % (st["days_above"], st["t_days"], st["gate"], st["t_max"])},
            {"tag": "hr"},
            {"tag": "markdown",
             "content": "数据已就位（任务 %s，待归因执行）。是否立项？点按钮或回 **a** / **b** / **c**。"
                        % task_id},
            {"tag": "column_set", "flex_mode": "bisect", "columns": [
                {"tag": "column", "width": "weighted", "weight": 1,
                 "elements": [btn("采纳归档", "a", "primary")]},
                {"tag": "column", "width": "weighted", "weight": 1,
                 "elements": [btn("转人工", "b")]},
                {"tag": "column", "width": "weighted", "weight": 1,
                 "elements": [btn("转补充分析", "c")]},
            ]},
        ]},
    }


def find_gateway(root):
    env_path = os.environ.get("LARK_SEND")
    candidates = [Path(env_path)] if env_path else []
    candidates.append(Path(root).parents[1] / "tools" / "lark_send.py")
    for cand in candidates:
        if cand.is_file():
            return cand
    return None


def _agent_id(root):
    """推送归属：不编造；查不到就记 unbound（与 notify_lark 同口径）。"""
    try:
        sys.path.insert(0, str(Path(root) / "server"))
        import notify_lark  # noqa: E402
        return notify_lark.channel_agent_id(SimpleNamespace(root=str(root))) or "unbound"
    except Exception:  # noqa: BLE001
        return "unbound"


def push_card(root, task_id, card):
    """推发现卡并返回 {ok,...}；任何异常都收敛成 ok=false，不抛。"""
    gateway = find_gateway(root)
    agent_id = _agent_id(root)
    if gateway is None:
        return {"ok": False, "agent_id": agent_id, "error_kind": "no_gateway"}
    started = time.perf_counter()
    try:
        proc = subprocess.run(
            [sys.executable, "-B", str(gateway), "--purpose", "mo_p2p",
             "--card", json.dumps(card, ensure_ascii=False), "--send",
             "--auth", "tenant", "--idempotency-key", "discover-%s" % task_id],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
    except Exception as exc:  # noqa: BLE001
        _log("推送异常 %s: %s" % (task_id, exc))
        return {"ok": False, "agent_id": agent_id, "error_kind": "exception"}
    latency_ms = int((time.perf_counter() - started) * 1000)
    out = (proc.stdout or "") + (proc.stderr or "")
    payload = {"ok": proc.returncode == 0, "agent_id": agent_id, "latency_ms": latency_ms}
    if proc.returncode == 0:
        import re
        m = re.search(r'"message_id"\s*:\s*"([^"]+)"', out)
        if m:
            payload["message_id"] = m.group(1)
    else:
        payload["error_kind"] = "gateway_rc"
        _log("推送失败 %s rc=%s: %s" % (task_id, proc.returncode, out[:200]))
    return payload


def emit(name, props, root):
    """埋点不得中断业务。"""
    try:
        telemetry.emit(name, props, root=str(root))
    except Exception:  # noqa: BLE001
        pass


def main(argv=None):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    ap = argparse.ArgumentParser(description="存量异常复检（主动发现）")
    ap.add_argument("--root", default=str(ROOT))
    ap.add_argument("--runs", default="", help="缺省 <root>/runs（绝对路径，对计划任务 cwd 免疫）")
    ap.add_argument("--pack", default="", help="缺省 <root>/packs/manufacturing/pack.json")
    ap.add_argument("--state", default="", help="缺省 <root>/.telemetry/recheck-state.json")
    ap.add_argument("--apply", action="store_true", help="真建单（缺省 dry：只打印发现）")
    ap.add_argument("--no-push", action="store_true", help="只建单不推飞书")
    ap.add_argument("--max-new", type=int, default=1, help="单次最多新建任务数（防故障爆量）")
    args = ap.parse_args(argv)

    root = Path(args.root).resolve()
    runs = Path(args.runs).resolve() if args.runs else root / "runs"
    pack_path = Path(args.pack).resolve() if args.pack else root / "packs" / "manufacturing" / "pack.json"
    state_path = Path(args.state).resolve() if args.state else root / ".telemetry" / "recheck-state.json"
    ledger_root = os.environ.get("BOT_LEDGER_ROOT") or str(root)

    pack_doc, thr = load_pack(pack_path)
    state = load_state(state_path)
    entries = state["entries"]

    hits, skipped, scanned = [], 0, 0
    for t_path, t30_path in scan(runs):
        scanned += 1
        if t30_path is None:
            skipped += 1
            continue
        st = dataset_stats(t_path, t30_path, thr)
        if st is None:
            skipped += 1
            continue
        st["source_task"] = t_path.parent.name if t_path.parent.name != "data" else t_path.parent.parent.name
        st["signature"] = signature(st, st["t_records"])
        if st["hit"]:
            hits.append(st)

    summary = {"ok": True, "apply": bool(args.apply), "scanned": scanned,
               "skipped": skipped, "hits": [], "created": [], "pushed": [], "skipped_seen": 0}
    if not hits:
        print(json.dumps(dict(summary, hits=[], message="no anomaly"), ensure_ascii=False))
        return 0

    new_created = 0
    for st in hits:
        sig = st["signature"]
        row = {"source_task": st["source_task"], "line": st["line"],
               "t_mean": st["t_mean"], "gate": st["gate"], "days_above": st["days_above"],
               "signature": sig}
        prev = entries.get(sig)
        row["task_id"] = (prev or {}).get("task_id")
        summary["hits"].append(row)
        if not args.apply:
            continue
        if prev and prev.get("push_ok"):
            summary["skipped_seen"] += 1
            continue
        task_id = (prev or {}).get("task_id")
        if not task_id:
            if new_created >= max(1, args.max_new):
                _log("达到 --max-new=%d，本轮不再新建" % args.max_new)
                continue
            task_id, _tdir = create_task(runs, st, thr, sig, pack_doc)
            new_created += 1
            entries[sig] = {"task_id": task_id, "created_at": now_iso(),
                            "push_ok": False, "from_task": st["source_task"]}
            save_state(state_path, state)
            summary["created"].append(task_id)
            emit("task_created", {"task_id": task_id, "scene_id": SCENE_ID,
                                  "source": "discovered", "line": st["line"] or "",
                                  "window_start": st["window"]["start"],
                                  "window_end": st["window"]["end"]},
                 ledger_root)
        if args.no_push:
            continue
        payload = push_card(root, task_id, build_card(task_id, st, thr))
        emit("push_sent", {"task_id": task_id,
                           "agent_id": payload.get("agent_id") or "unbound",
                           "ok": bool(payload.get("ok")),
                           **({"message_id": payload["message_id"]}
                              if payload.get("message_id") else {}),
                           **({"latency_ms": payload["latency_ms"]}
                              if payload.get("latency_ms") is not None else {}),
                           **({"error_kind": payload["error_kind"]}
                              if payload.get("error_kind") else {})},
             ledger_root)
        if payload.get("ok"):
            entries[sig].update({"push_ok": True, "pushed_at": now_iso(),
                                 "message_id": payload.get("message_id")})
            save_state(state_path, state)
            tdir = runs / task_id
            try:
                write_json(tdir / MARK_FILE,
                           {"sent_at": now_iso(), "message_id": payload.get("message_id"),
                            "gateway": str(find_gateway(root) or "")})
            except Exception:  # noqa: BLE001
                pass
            summary["pushed"].append(task_id)
        else:
            summary["ok"] = False

    print(json.dumps(summary, ensure_ascii=False))
    return 0 if summary["ok"] else 3


if __name__ == "__main__":
    raise SystemExit(main())
