#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""日常工作接单器 —— 把「一句工作问题」变成可签字的任务单。

用法（最省事的一条）：
    python tools/daily_intake.py --text "B线补料率这周 7.2%，怀疑模具磨损" --data 补料率.csv

它会：抽字段（产线/指标/窗口/嫌疑因素）→ 建单（POST /api/tasks）→ 数据随单上传
（base64 内联，走既有 /api/tasks 上传契约）→ 打印 task_id 与下一步。

设计纪律：
- **不猜数**：抓不到基线就不填，写进 assumptions，不编造 baseline_value。
- **不猜口径**：指标只能映射到 pack 里登记过的 metric id，映射不到就留空并提示。
- **不静默放宽**：--strict 下缺产线/指标直接拒单（rc=2）。
- 零依赖：只用标准库（urllib），与仓库零新依赖红线一致。
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import json
import os
import pathlib
import re
import sys
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent

MAX_UPLOAD_FILE = 8 * 1024 * 1024      # 与服务端 MAX_UPLOAD_FILE 同口径
MAX_UPLOAD_TOTAL = 24 * 1024 * 1024    # 与服务端 MAX_UPLOAD_TOTAL 同口径
TABLE_SUFFIXES = (".csv", ".xlsx", ".xls")  # 表格走 /api/tasks/<id>/data，不内联

# 中文口径 → pack 登记的 metric id（packs/manufacturing/pack.json#metrics）
METRIC_MAP = {
    "补料率": ("refill_rate", "%"),
    "停机": ("downtime", "小时/周"),
    "停机时长": ("downtime", "小时/周"),
    "人均产量": ("output_per_capita", "件/人·班"),
    "产量": ("output_per_capita", "件/人·班"),
    "废品率": ("scrap_rate", "%"),
    "不良率": ("scrap_rate", "%"),
}

SUSPECT_MAP = {
    "模具": "模具状态",
    "保养": "保养记录",
    "原料": "原料批次",
    "批次": "原料批次",
    "换人": "信息员/操作员变更",
    "换班": "班组变更",
    "培训": "培训与熟练度",
    "参数": "工艺参数",
    "温度": "工艺温度",
    "保养到期": "保养到期日",
}

LINE_RE = re.compile(r"([A-Da-d])\s*线|([一二三四1-4])\s*线")
PCT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*%")
CN_NUM = {"一": "1", "二": "2", "三": "3", "四": "4"}


def _win(days: int, end: dt.date | None = None, shift: int = 0) -> dict:
    end = end or dt.date.today()
    end = end - dt.timedelta(days=shift)
    start = end - dt.timedelta(days=days - 1)
    return {"start": start.isoformat(), "end": end.isoformat(), "days": days}


def parse_text(text: str) -> dict:
    """从一句话里抽线索。抽不到就空着——这里只提线索，不做判断。"""
    hints: dict = {"assumptions": []}

    m = LINE_RE.search(text)
    if m:
        raw = m.group(1) or m.group(2)
        hints["line"] = CN_NUM.get(raw, raw).upper()

    for zh, (metric_id, unit) in METRIC_MAP.items():
        if zh in text:
            hints["metric"] = zh
            hints["metric_id"] = metric_id
            hints["metric_unit"] = unit
            break

    pct = PCT_RE.search(text)
    if pct:
        hints["reported_value"] = float(pct.group(1))
        hints["assumptions"].append(
            f"文中出现的 {pct.group(1)}% 记为「用户口述现值」，不是统计口径；不入 baseline_value"
        )

    suspects = [label for key, label in SUSPECT_MAP.items() if key in text]
    if suspects:
        hints["known_confounders"] = sorted(set(suspects))

    if "line" not in hints:
        hints["assumptions"].append("未识别产线（可用 --line 指定）")
    if "metric" not in hints:
        hints["assumptions"].append("未识别指标（可用 --metric 指定；须是补料率/停机/人均产量/废品率）")
    return hints


def build_intake(text: str, args: argparse.Namespace) -> dict:
    hints = parse_text(text)
    line = args.line or hints.get("line")
    metric = args.metric or hints.get("metric")
    metric_id = args.metric_id or hints.get("metric_id")
    unit = hints.get("metric_unit", "")
    if metric and not metric_id:
        metric_id = METRIC_MAP.get(metric, (None, ""))[0]

    window = _win(args.days, shift=1) if not args.start else {
        "start": args.start,
        "end": args.end or args.start,
        "days": 1,
    }
    if args.start and args.end:
        d0 = dt.date.fromisoformat(args.start)
        d1 = dt.date.fromisoformat(args.end)
        window["days"] = (d1 - d0).days + 1

    baseline = _win(args.baseline_days, end=dt.date.fromisoformat(window["start"]) - dt.timedelta(days=1))

    intake = {
        "plant": args.plant,
        "line": line,
        "metric": metric,
        "metric_id": metric_id,
        "metric_unit": unit,
        "window": window,
        "baseline_window": baseline,
        "target": args.target
        or (f"T+7 复查：{metric or '指标'} 周均值回到基线以下，且本周建议全部可回溯" ),
        "data_owner": args.data_owner,
        "line_owner": args.line_owner,
        "known_confounders": args.confounder or hints.get("known_confounders", []),
        "gate": "G0",
        "runner": args.runner,
        "source": "daily-intake",
        "intake": {
            "raw_text": text,
            "reported_value": hints.get("reported_value"),
            "assumptions": hints.get("assumptions", []),
            "captured_at": dt.datetime.now().isoformat(timespec="seconds"),
            "tool": "tools/daily_intake.py",
        },
        "notes": args.notes or "日常工作接单：字段由一句话抽取，缺失项见 intake.assumptions；不可逆动作只呈请不执行。",
    }
    if args.baseline_value is not None:
        intake["baseline_value"] = args.baseline_value

    files = []
    tables = []
    total = 0
    for path in args.data or []:
        p = pathlib.Path(path)
        if not p.is_file():
            raise SystemExit(f"[FAIL] 数据文件不存在：{p}")
        size = p.stat().st_size
        if size > MAX_UPLOAD_FILE:
            raise SystemExit(f"[FAIL] 单文件超过 8MB 上限：{p.name}（{size} bytes）——服务端也会拒")
        total += size
        if total > MAX_UPLOAD_TOTAL:
            raise SystemExit(f"[FAIL] 数据文件总量超过 24MB 上限（{total} bytes）")
        if p.suffix.lower() in TABLE_SUFFIXES:
            # 表格走 /api/tasks/<id>/data（转 T/T30，带口径校验）；建单后再传
            tables.append(p)
            continue
        # JSON 数据直接内联（服务端会校验 UTF-8 JSON，不合格立刻拒）
        files.append(
            {
                "name": p.name,
                "content": base64.b64encode(p.read_bytes()).decode("ascii"),
                "encoding": "base64",
            }
        )
    if files:
        intake["data_files"] = files
    if tables:
        intake["intake"]["pending_tables"] = [p.name for p in tables]
    return intake, tables


def post_task(server: str, intake: dict, timeout: float = 30.0) -> dict:
    body = json.dumps(intake, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        server.rstrip("/") + "/api/tasks",
        data=body,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def upload_table(server: str, task_id: str, path: pathlib.Path, intake: dict,
                 timeout: float = 120.0, field_map: str | None = None) -> dict:
    """把表格按 /api/tasks/<id>/data 的表模式上传（服务端转 T/T30 并做口径校验）。

    注意：POST /api/tasks 的 data_files 只认 UTF-8 JSON，CSV/XLSX 必须走这里。
    """
    window = intake.get("window") or {}
    baseline = intake.get("baseline_window") or {}
    body = {
        "table": {
            "name": path.name,
            "content": base64.b64encode(path.read_bytes()).decode("ascii"),
            "encoding": "base64",
        },
        "line": intake.get("line") or "",
        "t_start": window.get("start"),
        "t_end": window.get("end"),
        "base_start": baseline.get("start"),
        "base_end": baseline.get("end"),
    }
    if field_map:
        body["field_map"] = field_map
    req = urllib.request.Request(
        f"{server.rstrip('/')}/api/tasks/{task_id}/data",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def strict_errors(intake: dict) -> list[str]:
    errs = []
    if not intake.get("line"):
        errs.append("缺产线 line")
    if not intake.get("metric"):
        errs.append("缺指标 metric")
    elif not intake.get("metric_id"):
        errs.append(f"指标「{intake['metric']}」不在 pack 登记口径内（补料率/停机/人均产量/废品率）")
    if not intake.get("window", {}).get("start"):
        errs.append("缺统计窗口 window")
    return errs


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="把一句工作问题变成任务单")
    ap.add_argument("--text", help="原话，例如「B线补料率这周 7.2%，怀疑模具磨损」")
    ap.add_argument("--text-file", help="从文件读原话（长日报）")
    ap.add_argument("--plant", default="塑胶厂")
    ap.add_argument("--line")
    ap.add_argument("--metric")
    ap.add_argument("--metric-id")
    ap.add_argument("--start")
    ap.add_argument("--end")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--baseline-days", type=int, default=30)
    ap.add_argument("--baseline-value", type=float)
    ap.add_argument("--target")
    ap.add_argument("--data", nargs="*", help="随单上传的数据文件（csv/xlsx/json）")
    ap.add_argument("--field-map", help="列名映射 YAML（用户自己的表头 → pack 必需列），随表格一起上传")
    ap.add_argument("--data-owner", default="数据负责人（待填）")
    ap.add_argument("--line-owner", default="产线负责人（待填）")
    ap.add_argument("--confounder", nargs="*", help="已知混杂因素")
    ap.add_argument("--runner", default="external", choices=["external", "dsh", "fixture"])
    ap.add_argument("--notes")
    ap.add_argument("--server", default=os.environ.get("BOT_SERVER") or "http://127.0.0.1:8787")
    ap.add_argument("--strict", action="store_true", help="缺产线/指标即拒（rc=2）")
    ap.add_argument("--dry-run", action="store_true", help="只打印 intake JSON，不建单")
    args = ap.parse_args(argv)

    text = args.text or ""
    if args.text_file:
        text = (text + "\n" + pathlib.Path(args.text_file).read_text(encoding="utf-8")).strip()
    if not text:
        ap.error("要 --text 或 --text-file（原话是这张单的源头）")

    intake, tables = build_intake(text, args)

    if args.strict:
        errs = strict_errors(intake)
        if errs:
            print(json.dumps({"ok": False, "errors": errs}, ensure_ascii=False))
            return 2

    if args.dry_run:
        print(json.dumps(intake, ensure_ascii=False, indent=2))
        return 0

    try:
        created = post_task(args.server, intake)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:600]
        print(json.dumps({"ok": False, "http": exc.code, "detail": detail}, ensure_ascii=False))
        return 3
    except urllib.error.URLError as exc:
        print(json.dumps({"ok": False, "reachable": False, "error": str(exc.reason)}, ensure_ascii=False))
        return 4

    task_id = created.get("task_id") or ""

    table_results = []
    for path in tables:
        fm_text = None
        if args.field_map:
            fm_text = pathlib.Path(args.field_map).read_text(encoding="utf-8")
        try:
            got = upload_table(args.server, task_id, path, intake, field_map=fm_text)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:600]
            out = {"ok": False, "task_id": task_id, "stage": "upload_table",
                   "file": path.name, "http": exc.code, "detail": detail}
            if "缺少必需列" in detail:
                out["hint"] = ("表头要能被 pack 认出来：默认必需 date/production_line/refill_rate/output_volume；"
                               "你的表头不一样就写一个 field-map.yaml（例如 refill_rate: 补料率）用 --field-map 传上来")
                out["required_columns"] = ["date", "production_line", "refill_rate", "output_volume"]
            print(json.dumps(out, ensure_ascii=False))
            return 5
        except urllib.error.URLError as exc:
            print(json.dumps({"ok": False, "task_id": task_id, "stage": "upload_table",
                              "file": path.name, "reachable": False, "error": str(exc.reason)},
                             ensure_ascii=False))
            return 5
        table_results.append({
            "file": path.name,
            "rows": (got or {}).get("rows"),
            "data_files": (got or {}).get("data_files", []),
            "data_ready": (got or {}).get("data_ready"),
        })

    print(json.dumps({"ok": True, "task_id": task_id, "line": created.get("line"),
                      "data_files": created.get("data_files", []),
                      "tables": table_results,
                      "assumptions": intake["intake"]["assumptions"]}, ensure_ascii=False, indent=2))
    print()
    print("下一步：")
    print(f"  1) 打开工作台： {args.server}/ui/index.html")
    print(f"  2) 看这张单：   {args.server}/api/tasks/{task_id}")
    print(f"  3) 跑分析：     填 runs/{task_id}/ 的分析产物 → python tools/advisory_to_task.py --advisory <report.json> --task-id {task_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
