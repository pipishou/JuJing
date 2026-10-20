#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""真实任务数据入口：CSV/Excel → T/T30 JSON（demo 结构）。

只做"搬运 + 窗口切分 + 字段声明"，不做补数/插值/猜测；缺列就报错、空单元格写 null 让数据合同检查器报。
M1.5 批次一（去 pandas）：读取层改为标准库 csv + openpyxl(只读) + xlrd，不依赖 pandas。
- .csv   → csv.DictReader（utf-8-sig，失败回退 gbk）
- .xlsx  → openpyxl read_only
- .xls   → 优先 xlrd；解析失败给出「请另存为 .xlsx」

用法：
  python tools/ingest_task_data.py --input task.csv --line B \
      --t-start 2026-10-05 --t-end 2026-10-11 \
      --base-start 2026-09-05 --base-end 2026-10-04 \
      --outdir runs/task-20261019-B
"""
import argparse
import csv
import datetime as dt
import io
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REQUIRED = ["date", "production_line", "refill_rate", "output_volume"]
ALLOWED_SUFFIX = (".csv", ".xlsx", ".xls")
INT_RE = re.compile(r"^[+-]?\d+$")
FLOAT_RE = re.compile(r"^[+-]?(?:\d+\.\d*|\.\d+|\d+[eE][+-]?\d+|\d+\.\d*[eE][+-]?\d+)$")
DATE_FORMATS = (
    "%Y-%m-%d", "%Y/%m/%d", "%Y%m%d", "%Y.%m.%d",
    "%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S",
    "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M",
)


class IngestError(RuntimeError):
    """取数失败（可读原因，CLI 转成单行 JSON + rc=2，不打印 traceback）。"""


# ---------------------------------------------------------------- 读取层（无 pandas）
def _decode_text(raw, path):
    for enc in ("utf-8-sig", "gbk"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    raise IngestError(f"无法识别 CSV 编码（已尝试 utf-8-sig / gbk）：{path}")


def read_csv(path):
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise IngestError(f"文件不存在或不可读：{path}（{exc}）") from exc
    text = _decode_text(raw, path)
    rdr = csv.DictReader(io.StringIO(text, newline=""))
    header = [str(c).strip() for c in (rdr.fieldnames or [])]
    rdr.fieldnames = header  # 覆盖原始表头，保证列名无首尾空白
    rows = []
    for raw_row in rdr:
        row = {k: v for k, v in raw_row.items() if k not in (None, "")}
        if any(str(v).strip() for v in row.values() if v is not None):
            rows.append(row)
    return [c for c in header if c], rows


def read_xlsx(path):
    try:
        import openpyxl  # noqa: PLC0415 —— 懒导入：JSON 路径不装 Excel 依赖也能活
    except Exception as exc:  # noqa: BLE001
        raise IngestError(
            f"读取 .xlsx 需要 openpyxl（本机应已安装 openpyxl 3.1.5）：{exc}"
        ) from exc
    try:
        wb = openpyxl.load_workbook(filename=str(path), read_only=True, data_only=True)
    except OSError as exc:
        raise IngestError(f"文件不存在或不可读：{path}（{exc}）") from exc
    except Exception as exc:  # noqa: BLE001
        raise IngestError(f"无法读取 Excel：{path.name}（{exc}）") from exc
    try:
        ws = wb.active
        it = ws.iter_rows(values_only=True)
        header = next(it, None)
        if header is None:
            raise IngestError(f"Excel 首个工作表为空：{path.name}")
        header = [str(c).strip() if c is not None else "" for c in header]
        rows = []
        for values in it:
            row = {h: v for h, v in zip(header, values) if h}
            if any(v is not None and str(v).strip() != "" for v in row.values()):
                rows.append(row)
        return [h for h in header if h], rows
    finally:
        try:
            wb.close()
        except Exception:  # noqa: BLE001
            pass


def read_xls(path):
    try:
        import xlrd  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        raise IngestError(f"读取 .xls 需要 xlrd；请另存为 .xlsx 后重试（{exc}）") from exc
    try:
        book = xlrd.open_workbook(str(path))
    except OSError as exc:
        raise IngestError(f"文件不存在或不可读：{path}（{exc}）") from exc
    except Exception as exc:  # noqa: BLE001
        raise IngestError(f".xls 解析失败：{exc}；请另存为 .xlsx 后重试") from exc
    try:
        sheet = book.sheet_by_index(0)
    except Exception as exc:  # noqa: BLE001
        raise IngestError(f".xls 没有可读工作表：{exc}；请另存为 .xlsx 后重试") from exc
    if sheet.nrows == 0:
        raise IngestError(f"Excel 首个工作表为空：{path.name}")
    header = [str(sheet.cell_value(0, c)).strip() for c in range(sheet.ncols)]
    rows = []
    for r in range(1, sheet.nrows):
        row = {}
        for c, h in enumerate(header):
            if not h:
                continue
            cell = sheet.cell(r, c)
            if cell.ctype == xlrd.XL_CELL_DATE:
                try:
                    row[h] = xlrd.xldate_as_datetime(cell.value, book.datemode)
                except Exception:  # noqa: BLE001
                    row[h] = cell.value
            elif cell.ctype in (xlrd.XL_CELL_EMPTY, xlrd.XL_CELL_BLANK):
                row[h] = None
            else:
                row[h] = cell.value
        if any(v is not None and str(v).strip() != "" for v in row.values()):
            rows.append(row)
    return [h for h in header if h], rows


def read_table(path):
    suffix = path.suffix.lower()
    if suffix == ".csv":
        return read_csv(path)
    if suffix == ".xlsx":
        return read_xlsx(path)
    if suffix == ".xls":
        return read_xls(path)
    raise IngestError(f"不支持的数据文件类型：{path.name}（仅支持 {' / '.join(ALLOWED_SUFFIX)}）")


# ---------------------------------------------------------------- 值归一
def normalize_date(value, where=""):
    """统一成 YYYY-MM-DD；接受 2026/10/5、20261005、带时间、Excel 日期/序列号；空 → None。"""
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        return value.date().isoformat()
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, bool):
        raise IngestError(f"日期列出现布尔值：{value!r}（{where}）")
    if isinstance(value, (int, float)):
        num = int(value) if float(value).is_integer() else value
        if isinstance(num, int):
            if 19000101 <= num <= 21001231:
                try:
                    return dt.date(num // 10000, (num // 100) % 100, num % 100).isoformat()
                except ValueError as exc:
                    raise IngestError(f"日期无法解析：{value!r}（{where}）") from exc
            if 20000 <= num <= 80000:  # Excel 序列日期（1899-12-30 起）
                return (dt.date(1899, 12, 30) + dt.timedelta(days=num)).isoformat()
        raise IngestError(f"日期无法解析：{value!r}（{where}）")
    text = str(value).strip()
    if not text:
        return None
    text = text.replace("T", " ").strip() if re.match(r"^\d{4}-\d{2}-\d{2}T", text) else text
    for fmt in DATE_FORMATS:
        try:
            return dt.datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    try:
        return dt.datetime.fromisoformat(str(value).strip()).date().isoformat()
    except ValueError as exc:
        raise IngestError(f"日期无法解析：{value!r}（{where}）") from exc


def coerce_value(value):
    """空单元格 → None；数字串转 int/float（与旧 pandas 读取行为一致）；其余原样。"""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if INT_RE.match(text):
            try:
                return int(text)
            except ValueError:
                return value
        if FLOAT_RE.match(text):
            try:
                return float(text)
            except ValueError:
                return text
        return text
    return value


# ---------------------------------------------------------------- 字段映射 / 切片
def apply_field_map(header, rows, field_map_path):
    if not field_map_path:
        return header, rows, {}
    try:
        import yaml  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        raise IngestError(f"--field-map 需要 pyyaml：{exc}") from exc
    try:
        doc = yaml.safe_load(Path(field_map_path).read_text(encoding="utf-8")) or {}
    except OSError as exc:
        raise IngestError(f"字段映射文件不可读：{field_map_path}（{exc}）") from exc
    mapping = doc.get("source_columns") or {}
    if not isinstance(mapping, dict):
        raise IngestError(f"字段映射格式错误（source_columns 应为映射）：{field_map_path}")
    rename = {str(src): str(canonical) for canonical, src in mapping.items() if src in header}
    if not rename:
        return header, rows, {}
    new_header = []
    for col in header:
        canonical = rename.get(col, col)
        if canonical in new_header:
            raise IngestError(
                f"字段映射冲突：多列映射到同一字段 {canonical!r}（源列 {col!r}）；请检查 --field-map"
            )
        new_header.append(canonical)
    renamed_rows = [{rename.get(k, k): v for k, v in row.items()} for row in rows]
    return new_header, renamed_rows, rename


def normalize_rows(header, rows):
    """逐行归一：日期 → YYYY-MM-DD；空 → None；数字串 → 数字。"""
    out = []
    for idx, row in enumerate(rows, start=2):  # Excel/CSV 第 2 行起是数据
        rec = {}
        for col in header:
            value = row.get(col)
            if col == "date":
                rec[col] = normalize_date(value, where=f"第 {idx} 行 date")
            else:
                rec[col] = coerce_value(value)
        out.append(rec)
    return out


def window_slice(records, start, end):
    return [r for r in records if r.get("date") and start <= r["date"] <= end]


# ---------------------------------------------------------------- 主流程
def run(args):
    src = Path(args.input)
    if not src.is_file():
        raise IngestError(f"文件不存在：{src}")
    if src.suffix.lower() not in ALLOWED_SUFFIX:
        raise IngestError(f"不支持的数据文件类型：{src.name}（仅支持 {' / '.join(ALLOWED_SUFFIX)}）")
    for flag, value in (("--t-start", args.t_start), ("--t-end", args.t_end),
                        ("--base-start", args.base_start), ("--base-end", args.base_end)):
        try:
            dt.date.fromisoformat(str(value))
        except ValueError as exc:
            raise IngestError(f"{flag} 需要 YYYY-MM-DD：{value!r}") from exc

    header, rows = read_table(src)
    header, rows, field_map_applied = apply_field_map(header, rows, args.field_map)
    missing = [c for c in REQUIRED if c not in header]
    if missing:
        raise IngestError(f"缺少必需列: {missing}（实际列: {header}）")
    records = normalize_rows(header, rows)
    if args.line:
        records = [r for r in records if str(r.get("production_line")) == str(args.line)]
    if not records:
        raise IngestError(f"窗口内无数据（产线={args.line or 'ALL'}，数据行数={len(rows)}）")

    template = json.loads((ROOT / "demo/material_refill_T.json").read_text(encoding="utf-8"))
    base_meta = template.get("meta") or {}
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    def build(start, end, purpose):
        sub = window_slice(records, start, end)
        if not sub:
            raise IngestError(
                f"窗口内无数据：{purpose} {start}~{end}（产线={args.line or 'ALL'}，"
                f"数据日期范围 {records[0].get('date')}~{records[-1].get('date')}）"
            )
        doc = {
            "meta": {
                **base_meta,
                "dataset": "material_refill_T" if purpose == "T" else "material_refill_T30",
                "purpose": purpose,
                "production_line": args.line or "ALL",
                "field_contract": list(header),
                "null_rate_declared": None,
                "planted_signal": {},
            },
            "records": sub,
        }
        name = "material_refill_T.json" if purpose == "T" else "material_refill_T30.json"
        (outdir / name).write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n",
                                   encoding="utf-8")
        return len(sub), name

    nt, fn = build(args.t_start, args.t_end, "T")
    nb, fn2 = build(args.base_start, args.base_end, "T30")
    summary = {
        "ok": True,
        "outdir": str(outdir),
        "t_records": nt,
        "t30_records": nb,
        "files": [fn, fn2],
        "field_map_applied": field_map_applied,
        "note": "未补数；缺列写 null，请跑 check_data_contract.py",
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def main(argv=None):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    ap = argparse.ArgumentParser(description="CSV/Excel → T/T30 JSON（标准库读取层，不依赖 pandas）")
    ap.add_argument("--input", required=True)
    ap.add_argument("--line", default="")
    ap.add_argument("--t-start", required=True)
    ap.add_argument("--t-end", required=True)
    ap.add_argument("--base-start", required=True)
    ap.add_argument("--base-end", required=True)
    ap.add_argument("--outdir", required=True)
    ap.add_argument("--field-map", default="", help="YAML: canonical -> source column")
    args = ap.parse_args(argv)
    try:
        return run(args)
    except IngestError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 2
    except Exception as exc:  # noqa: BLE001 —— 任何异常都收敛成单行 JSON，不打印 traceback
        print(json.dumps({"ok": False, "error": f"取数失败：{exc}"}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
