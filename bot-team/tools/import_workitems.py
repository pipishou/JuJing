#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把「工作事项清单」表（xlsx/csv）导入唯一账本。

为什么不用 openpyxl：企微/WPS 导出的真实文件常让 openpyxl 抛
`TypeError: Fill() takes no arguments`，链路会死在入口。这里走
`tools/xlsx_lite.py`（stdlib zipfile 只读值），对样式畸形免疫。

用法：
  python -B tools/import_workitems.py 清单.xlsx            # 导入并打印汇总
  python -B tools/import_workitems.py 清单.xlsx --dry-run   # 只看解析结果，不写账本
  python -B tools/import_workitems.py 清单.xlsx --sheet 工作事项清单 --by 导入
  python -B tools/import_workitems.py 清单.csv

幂等：同一份表重复导入不会翻倍（按 分组+序号+标题 匹配）；改了标题算新事项。
"""
import argparse
import csv
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import workitems_store as ws  # noqa: E402
import xlsx_lite  # noqa: E402


def read_rows(path, sheet=None):
    """xlsx → 行；csv → 行（utf-8-sig / gbk 都试）。"""
    p = Path(path)
    suffix = p.suffix.lower()
    if suffix in (".xlsx", ".xlsm"):
        data = xlsx_lite.read_sheets(p, sheets=[sheet] if sheet else None)
        name = sheet or next(iter(data))
        if name not in data:
            raise ws.StoreError("工作表 %r 不存在，实际有：%s" % (name, "、".join(data)))
        return data[name], name
    if suffix == ".csv":
        for enc in ("utf-8-sig", "gbk", "utf-8"):
            try:
                with open(p, encoding=enc, newline="") as fh:
                    return [row for row in csv.reader(fh)], "csv"
            except UnicodeDecodeError:
                continue
        raise ws.StoreError("CSV 编码识别不了（试过 utf-8-sig/gbk/utf-8）")
    if suffix == ".xls":
        raise ws.StoreError("老 .xls 请先另存为 .xlsx（本项目不引 xlrd）")
    raise ws.StoreError("不支持的格式：%s（要 .xlsx 或 .csv）" % suffix)


def main(argv=None):
    ap = argparse.ArgumentParser(description="工作事项清单 → 唯一账本")
    ap.add_argument("source", help="xlsx 或 csv 路径")
    ap.add_argument("--sheet", help="工作表名（默认第一张）")
    ap.add_argument("--file", help="账本路径（默认 runtime/workitems/current.json）")
    ap.add_argument("--by", default="导入")
    ap.add_argument("--as-of", help="汇总基准日 YYYY-MM-DD")
    ap.add_argument("--dry-run", action="store_true", help="只解析不写账本")
    ap.add_argument("--no-save-source", action="store_true", help="不在账本里记来源文件")
    args = ap.parse_args(argv)

    try:
        rows, sheet_name = read_rows(args.source, args.sheet)
        items = ws.parse_sheet(rows)
        if not items:
            raise ws.StoreError("表里没解析出任何事项（检查表头是否含 %s）"
                                % "、".join(ws.REQUIRED_HEADERS))
        if args.dry_run:
            print(json.dumps({"ok": True, "dry_run": True, "sheet": sheet_name,
                              "parsed": len(items), "sample": items[:2]},
                             ensure_ascii=False, indent=2))
            return 0
        store = ws.load(args.file)
        source = None
        if not args.no_save_source:
            src_path = Path(args.source)
            source = {
                "kind": "import",
                "file": str(src_path),
                "sheet": sheet_name,
                "sha256": ws.sha256_file(src_path),
                "size": src_path.stat().st_size,
                "imported_at": ws._now(),
                "imported_by": args.by,
            }
        store, stats = ws.merge_items(store, items, by=args.by, source=source)
        ws.save(store, args.file)
        as_of = date.fromisoformat(args.as_of) if args.as_of else None
        print(json.dumps({
            "ok": True, "sheet": sheet_name, "revision": store.get("revision"),
            "stats": stats, "summary": ws.summary(store, as_of),
            "path": str(args.file or ws.store_path()),
        }, ensure_ascii=False, indent=2))
        return 0
    except ws.StoreError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 2
    except xlsx_lite.XlsxError as exc:
        print(json.dumps({"ok": False, "error": "xlsx 读取失败：%s" % exc}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    sys.exit(main())
