#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""真实任务数据入口：CSV/Excel → T/T30 JSON（demo 结构）。

只做"搬运 + 窗口切分 + 字段声明"，不做补数/插值/猜测；缺列就写 null 并让数据合同检查器报。
复用 pandas（与方法论 Excel 降级方案同栈），不引入新依赖。

用法：
  python tools/ingest_task_data.py --input task.csv --line B \
      --t-start 2026-10-05 --t-end 2026-10-11 \
      --base-start 2026-09-05 --base-end 2026-10-04 \
      --outdir runs/task-20261019-B
"""
import argparse
import json
import sys
from pathlib import Path

import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parent.parent
REQUIRED = ["date", "production_line", "refill_rate", "output_volume"]


def read_table(p: Path) -> pd.DataFrame:
    if p.suffix.lower() in (".xlsx", ".xls"):
        return pd.read_excel(p)
    return pd.read_csv(p)


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    ap.add_argument("--line", default="")
    ap.add_argument("--t-start", required=True)
    ap.add_argument("--t-end", required=True)
    ap.add_argument("--base-start", required=True)
    ap.add_argument("--base-end", required=True)
    ap.add_argument("--outdir", required=True)
    ap.add_argument("--field-map", default="", help="YAML: canonical -> source column")
    args = ap.parse_args()

    df = read_table(Path(args.input))
    df.columns = [str(c).strip() for c in df.columns]
    if args.field_map:
        fmap = (yaml.safe_load(Path(args.field_map).read_text(encoding="utf-8")) or {}).get("source_columns") or {}
        rename = {src: canonical for canonical, src in fmap.items() if src in df.columns}
        df = df.rename(columns=rename)
        print(json.dumps({"field_map_applied": rename}, ensure_ascii=False))
    missing = [c for c in REQUIRED if c not in df.columns]
    if missing:
        print(json.dumps({"ok": False, "error": f"缺少必需列: {missing}", "columns": list(df.columns)}, ensure_ascii=False, indent=2))
        return 2
    df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
    if args.line:
        df = df[df["production_line"].astype(str) == args.line]
    if df.empty:
        print(json.dumps({"ok": False, "error": "窗口内无数据"}, ensure_ascii=False))
        return 2

    template = json.loads((ROOT / "demo/material_refill_T.json").read_text(encoding="utf-8"))
    meta = template.get("meta") or {}
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    def build(start, end, purpose):
        sub = df[(df["date"] >= start) & (df["date"] <= end)].copy()
        records = []
        for _, row in sub.iterrows():
            rec = {str(k): (None if pd.isna(v) else (v.item() if hasattr(v, "item") else v)) for k, v in row.items()}
            records.append(rec)
        doc = {
            "meta": {
                **meta,
                "dataset": "material_refill_T" if purpose == "T" else "material_refill_T30",
                "purpose": purpose,
                "production_line": args.line or "ALL",
                "field_contract": list(sub.columns),
                "null_rate_declared": None,
                "planted_signal": {},
            },
            "records": records,
        }
        name = "material_refill_T.json" if purpose == "T" else "material_refill_T30.json"
        (outdir / name).write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return len(records), name

    nt, fn = build(args.t_start, args.t_end, "T")
    nb, fn2 = build(args.base_start, args.base_end, "T30")
    print(json.dumps({"ok": True, "outdir": str(outdir), "t_records": nt, "t30_records": nb,
                      "files": [fn, fn2], "note": "未补数；缺列写 null，请跑 check_data_contract.py"}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
