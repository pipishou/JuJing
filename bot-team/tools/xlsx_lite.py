#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""零依赖 .xlsx 读取器（stdlib：zipfile + ElementTree）。

为什么存在：openpyxl 在 WPS / 某些国产 Excel 导出的文件上会抛
`TypeError: Fill() takes no arguments`（styles.xml 里的 fill 结构它不认），
而这类文件恰恰是「企微文档导出」的真实形态——读不了就等于整条链路死在入口。
本模块只读单元格值，忽略样式（只在需要判断「这是不是日期」时读 numFmt），
因此对样式畸形免疫。

用法：
    from xlsx_lite import read_sheets
    sheets = read_sheets("清单.xlsx")            # {"工作表名": [[str, ...], ...]}
    sheets = read_sheets("清单.xlsx", dates=True)  # 日期列转 ISO 字符串

约定：
- 单元格统一转成字符串（数字去掉多余的 .0，布尔转 TRUE/FALSE，空转 ""）
- 日期列（numFmt 命中日期格式，或单元格是 t="d"）默认输出 ISO `YYYY-MM-DD`；
  时间带时分则输出 `YYYY-MM-DD HH:MM`
- 合并单元格不展开，只在左上角给值（与 Excel 复制粘贴的直觉一致）
- 读不了的文件抛 XlsxError，不静默返回空表
"""
import re
import zipfile
from datetime import datetime, timedelta
from xml.etree import ElementTree as ET

NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
REL_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
PKG_REL_NS = "{http://schemas.openxmlformats.org/package/2006/relationships}"

# Excel 内置的日期/时间 numFmt（14–22 日期时间，45–47 时间）
BUILTIN_DATE_FMTS = set(range(14, 23)) | {45, 46, 47}
SERIAL_EPOCH = datetime(1899, 12, 30)


class XlsxError(Exception):
    """.xlsx 打不开 / 结构不认识 —— 调用方按 rc=2 处理。"""


def _text_of(node):
    """拼接 <si>/<is> 下所有 <t>（含富文本分段）。"""
    return "".join(t.text or "" for t in node.iter(NS + "t"))


def _col_index(ref):
    """'BC12' → 54（0 基）。"""
    m = re.match(r"([A-Za-z]+)", ref or "")
    if not m:
        return 0
    n = 0
    for ch in m.group(1).upper():
        n = n * 26 + (ord(ch) - 64)
    return n - 1


def _sheet_paths(zf):
    """workbook.xml + rels → [(工作表名, zip 内路径)]，保持工作簿顺序。"""
    try:
        wb = ET.fromstring(zf.read("xl/workbook.xml"))
    except KeyError:
        raise XlsxError("不是有效的 .xlsx（缺 xl/workbook.xml）")
    rels = {}
    try:
        rel_root = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
        for rel in rel_root:
            rels[rel.get("Id")] = rel.get("Target")
    except KeyError:
        pass
    out = []
    for sheet in wb.iter(NS + "sheet"):
        name = sheet.get("name") or "Sheet"
        rid = sheet.get(REL_NS + "id")
        target = rels.get(rid, "")
        if target.startswith("/"):
            path = target.lstrip("/")
        elif target:
            path = "xl/" + target.lstrip("./")
        else:
            path = "xl/worksheets/sheet%d.xml" % (len(out) + 1)
        out.append((name, path))
    return out


def _date_styles(zf):
    """cellXfs 索引 → 是否日期列（看 numFmtId + 自定义格式串）。"""
    try:
        root = ET.fromstring(zf.read("xl/styles.xml"))
    except KeyError:
        return set()
    custom = {}
    for fmt in root.iter(NS + "numFmt"):
        try:
            custom[int(fmt.get("numFmtId"))] = fmt.get("formatCode") or ""
        except (TypeError, ValueError):
            continue
    out = set()
    cell_xfs = root.find(NS + "cellXfs")
    if cell_xfs is None:
        return out
    for idx, xf in enumerate(cell_xfs.findall(NS + "xf")):
        try:
            fmt_id = int(xf.get("numFmtId") or 0)
        except ValueError:
            fmt_id = 0
        code = (custom.get(fmt_id) or "").lower()
        if fmt_id in BUILTIN_DATE_FMTS:
            out.add(idx)
        elif code and re.search(r"[ymd]", code) and not re.search(r"(^|;)[^\"]*[a-z]*\"", code):
            out.add(idx)
        elif code and re.search(r"y+m+d+", code):
            out.add(idx)
    return out


def _serial_to_text(value):
    try:
        num = float(value)
    except (TypeError, ValueError):
        return str(value)
    try:
        dt = SERIAL_EPOCH + timedelta(days=num)
    except (OverflowError, ValueError):
        return str(value)
    if abs(num - int(num)) < 1e-9:
        return dt.strftime("%Y-%m-%d")
    return dt.strftime("%Y-%m-%d %H:%M")


def _num_text(value):
    s = str(value).strip()
    try:
        f = float(s)
    except ValueError:
        return s
    if f.is_integer():
        return str(int(f))
    return ("%f" % f).rstrip("0").rstrip(".")


def read_sheets(path, dates=True, max_rows=None, sheets=None):
    """读 .xlsx → {工作表名: 行列表}。dates=True 时日期列转成 ISO 字符串。"""
    try:
        zf = zipfile.ZipFile(str(path))
    except (zipfile.BadZipFile, FileNotFoundError, OSError) as exc:
        raise XlsxError("打不开 %s：%s" % (path, exc))
    with zf:
        names = set(zf.namelist())
        shared = []
        if "xl/sharedStrings.xml" in names:
            root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
            shared = [_text_of(si) for si in root.findall(NS + "si")]
        date_styles = _date_styles(zf) if dates else set()
        result = {}
        for sheet_name, sheet_path in _sheet_paths(zf):
            if sheets is not None and sheet_name not in sheets:
                continue
            if sheet_path not in names:
                continue
            try:
                root = ET.fromstring(zf.read(sheet_path))
            except ET.ParseError as exc:
                raise XlsxError("工作表 %s 解析失败：%s" % (sheet_name, exc))
            rows = []
            for row in root.iter(NS + "row"):
                cells = {}
                for cell in row.findall(NS + "c"):
                    kind = cell.get("t")
                    v = cell.find(NS + "v")
                    inline = cell.find(NS + "is")
                    raw = ""
                    if kind == "s" and v is not None:
                        try:
                            raw = shared[int(v.text)]
                        except (ValueError, IndexError):
                            raw = v.text or ""
                    elif kind == "inlineStr" and inline is not None:
                        raw = _text_of(inline)
                    elif kind == "d" and v is not None:
                        raw = (v.text or "").replace("T", " ")[:16].rstrip(":00").strip()
                    elif v is not None:
                        raw = v.text or ""
                    elif kind == "str" and inline is not None:
                        raw = _text_of(inline)
                    if raw == "":
                        continue
                    style = cell.get("s")
                    if dates and kind not in ("s", "inlineStr", "str") and style is not None:
                        try:
                            if int(style) in date_styles:
                                raw = _serial_to_text(raw)
                        except ValueError:
                            pass
                    elif kind not in ("s", "inlineStr", "str") and re.match(r"^-?\d+(\.\d+)?$", str(raw)):
                        raw = _num_text(raw)
                    cells[_col_index(cell.get("r"))] = str(raw).replace("\r\n", "\n").strip()
                if cells:
                    width = max(cells) + 1
                    rows.append([cells.get(i, "") for i in range(width)])
                else:
                    rows.append([])
                if max_rows and len(rows) >= max_rows:
                    break
            result[sheet_name] = rows
        if not result:
            raise XlsxError("%s 里没读到任何工作表" % path)
        return result


def first_sheet(path, **kwargs):
    """最常用的一步到位：返回第一张工作表的行。"""
    data = read_sheets(path, **kwargs)
    return next(iter(data.values()))


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("用法: python -B tools/xlsx_lite.py <文件.xlsx> [工作表名]")
        raise SystemExit(2)
    target = sys.argv[1]
    only = [sys.argv[2]] if len(sys.argv) > 2 else None
    data = read_sheets(target, sheets=only)
    for name, rows in data.items():
        print("=== %s（%d 行）" % (name, len(rows)))
        for i, row in enumerate(rows[:5]):
            print("  r%d: %s" % (i + 1, " | ".join(c[:18] for c in row[:10])))
