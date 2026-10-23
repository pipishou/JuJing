#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""巡检数据聚合：事件级缺陷台账 xlsx → 业务日级 T/基线 JSON（T0 真实单数据入口）。

只做「搬运 + 窗口切分 + 日聚合 + 空值披露」，不做补数/插值/猜测；
行级空值原样进 meta.null_disclosure，聚合记录的每个因素键恒有值（0 是有效值）。

读取层复用 ingest_task_data（标准库 csv + openpyxl 只读，不依赖 pandas）。

用法：
  python tools/ingest_inspection_data.py \
      --input "D:\\...\\巡检数据源.xlsx" \
      --base-start 2026-09-17 --base-end 2026-10-11 \
      --t-start 2026-10-12 --t-end 2026-10-19 \
      --outdir runs/T-20261023-01/data
"""
import argparse
import hashlib
import json
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.ingest_task_data import IngestError, normalize_date, read_table  # noqa: E402

# 中文源列 → 规范键（缺必需列即报错，不猜）
COLUMN_MAP = {
    "业务日": "date",
    "IPQC人员": "inspector",
    "机台号": "machine",
    "产品型号": "product",
    "产品名称": "product_name",
    "缺陷描述": "defect_desc",
    "问题类型": "problem_type",
    "责任人": "owner",
    "是否退货": "is_return_raw",
    "照片数": "photo_count",
}
REQUIRED_SRC_COLS = list(COLUMN_MAP.keys())
FACTORS = ["inspection_count", "return_count", "return_rate", "machine_active_count",
           "product_active_count", "defect_type_count", "top_machine_share",
           "top_defect_type_share", "photo_evidence_rate", "inspector_count"]


def blank(value):
    return value is None or str(value).strip() == ""


def is_return(value):
    """退货判定：值非空且（含「退」字或为「是」）。

    源数据 426 行全量核实值域 = {空 300, 否 123, 是 3}；「是」不含量词「退」，
    单用含「退」判定会漏报（2026-10-14 两起外观不良退货即填「是」）。
    """
    if blank(value):
        return False
    text = str(value).strip()
    return "退" in text or text == "是"


def to_detail(rows):
    """原始行 → 规范 detail 记录（空值保留 None，不补数）。"""
    details = []
    for idx, r in enumerate(rows, start=2):
        raw_date = r.get("业务日")
        date = normalize_date(raw_date, where=f"第 {idx} 行 业务日")
        if not date:
            raise IngestError(f"第 {idx} 行 业务日 为空，无法归日")
        rec = {"date": date}
        for src, key in COLUMN_MAP.items():
            if key == "date":
                continue
            rec[key] = r.get(src)
        details.append(rec)
    return details


def aggregate(details):
    """按业务日聚合出 10 因素日级记录（键恒有值；0 是有效值）。"""
    by_day = {}
    for d in details:
        by_day.setdefault(d["date"], []).append(d)
    records = []
    for day in sorted(by_day):
        rows = by_day[day]
        n = len(rows)
        machines = [str(r["machine"]).strip() for r in rows if not blank(r["machine"])]
        products = [str(r["product"]).strip() for r in rows if not blank(r["product"])]
        ptypes = [str(r["problem_type"]).strip() for r in rows if not blank(r["problem_type"])]
        returns = sum(1 for r in rows if is_return(r.get("is_return_raw")))
        photos = sum(1 for r in rows if isinstance(r.get("photo_count"), (int, float))
                     and r["photo_count"] > 0)
        mc, pc, tc = Counter(machines), Counter(products), Counter(ptypes)
        records.append({
            "date": day,
            "inspection_count": n,
            "return_count": returns,
            "return_rate": round(returns / n, 4) if n else 0.0,
            "machine_active_count": len(mc),
            "product_active_count": len(pc),
            "defect_type_count": len(tc),
            "top_machine_share": round(mc.most_common(1)[0][1] / n, 4) if mc else 0.0,
            "top_defect_type_share": round(tc.most_common(1)[0][1] / n, 4) if tc else 0.0,
            "photo_evidence_rate": round(photos / n, 4) if n else 0.0,
            "inspector_count": len({str(r["inspector"]).strip() for r in rows
                                    if not blank(r["inspector"])}),
        })
    return records


def null_disclosure(details):
    """行级空值披露（按规范键；聚合记录不体现的缺口在这里如实报出）。"""
    cols = [k for k in COLUMN_MAP.values() if k != "date"]
    per = {}
    for c in cols:
        miss = sum(1 for d in details if blank(d.get(c)))
        if miss:
            per[c] = {"missing": miss, "rate": round(miss / len(details), 4) if details else 0.0}
    overall_cells = len(details) * len(cols)
    overall_miss = sum(v["missing"] for v in per.values())
    return {
        "per_column": per,
        "null_rate_cell_overall": round(overall_miss / overall_cells, 4) if overall_cells else 0.0,
        "note": "行级空值未补数；「是否退货」缺失 70.4% 使退货类因素仅可做描述性对比",
    }


def sha256_of(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def window(details, start, end):
    return [d for d in details if start <= d["date"] <= end]


def build_doc(src, details, start, end, purpose):
    sub = window(details, start, end)
    if not sub:
        raise IngestError(f"{purpose} 窗口 {start}~{end} 内无数据"
                          f"（数据业务日范围 {details[0]['date']}~{details[-1]['date']}）")
    return {
        "meta": {
            "dataset": f"quality_inspection_{purpose}",
            "purpose": purpose,
            "source_file": str(src),
            "source_sha256": sha256_of(src),
            "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "grain": "business_day(08:00 切日)",
            "window": {"start": start, "end": end, "business_days": len(aggregate(sub))},
            "field_contract": FACTORS,
            "null_disclosure": null_disclosure(sub),
        },
        "records": aggregate(sub),
    }


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    ap = argparse.ArgumentParser(description="巡检 xlsx → 业务日级 T/T30 JSON（T0 真实单数据入口）")
    ap.add_argument("--input", required=True)
    ap.add_argument("--outdir", required=True)
    ap.add_argument("--t-start", required=True)
    ap.add_argument("--t-end", required=True)
    ap.add_argument("--base-start", required=True)
    ap.add_argument("--base-end", required=True)
    args = ap.parse_args()

    src = Path(args.input)
    if not src.is_file():
        raise IngestError(f"文件不存在：{src}")
    header, rows = read_table(src)
    missing = [c for c in REQUIRED_SRC_COLS if c not in header]
    if missing:
        raise IngestError(f"缺少必需列: {missing}（实际列: {header}）")

    details = to_detail(rows)
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    outputs = []
    for purpose, (start, end), name in (
        ("T", (args.t_start, args.t_end), "inspection_T.json"),
        ("T30", (args.base_start, args.base_end), "inspection_T30.json"),
    ):
        doc = build_doc(src, details, start, end, purpose)
        (outdir / name).write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n",
                                   encoding="utf-8")
        outputs.append(name)
        # 明细（供机台/类型聚集归因）
        sub = window(details, start, end)
        detail_name = name.replace("inspection_", "inspection_detail_")
        (outdir / detail_name).write_text(
            json.dumps({"meta": {"source_file": str(src), "purpose": purpose,
                                 "window": {"start": start, "end": end},
                                 "rows": len(sub)},
                        "records": sub},
                       ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        outputs.append(detail_name)

    t_days = len(aggregate(window(details, args.t_start, args.t_end)))
    b_days = len(aggregate(window(details, args.base_start, args.base_end)))
    print(json.dumps({
        "ok": True, "outdir": str(outdir), "files": outputs,
        "t_business_days": t_days, "baseline_business_days": b_days,
        "rows_total": len(details),
        "note": "未补数；行级空值见 meta.null_disclosure，请跑 check_data_contract.py --factors-yaml assets/factors_quality.yaml",
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except IngestError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        raise SystemExit(2)
