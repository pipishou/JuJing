#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""B8 批次一：ingest_task_data.py 去 pandas 读取层回归。

覆盖：
  1. CSV（utf-8-sig + 中文字段映射）：T/T30 行数、字段、日期归一、空单元格 null、--line 精确过滤、窗口含首尾
  2. 输出的 T/T30 能通过 check_data_contract.py --strict
  3. 缺列 / 窗口空 / 文件不存在 / 不支持后缀 → rc=2 + 单行 JSON + 无 traceback
  4. openpyxl 生成的 .xlsx 同链路（含 Excel 真日期单元格）
  5. 伪造 .xls → rc=2 且提示「请另存为 .xlsx」

运行：python tests/test_ingest_csv.py
"""
import csv
import datetime as dt
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable
TOOL = ROOT / "tools" / "ingest_task_data.py"
TMP = Path(tempfile.mkdtemp(prefix="bot-ingest-"))

# 与 demo/material_refill_T.json 的 field_contract 一致的 canonical 顺序
COLS = ["date", "production_line", "refill_rate", "output_volume", "mold_status",
        "maintenance_records", "shift_team", "material_batch", "ambient_env",
        "mold_change_records", "downtime_records", "reporter"]
SRC_COLS = ["日期", "产线", "补料率", "产量", "模具状态", "保养记录", "班组",
            "原料批次", "环境温湿度", "换模记录", "停机记录", "信息员"]
FIELD_MAP = str(ROOT / "templates" / "field-map.example.yaml")

FAILS = []


def check(name, ok, detail=""):
    print(("[OK]   " if ok else "[FAIL] ") + name + ("" if ok else f" :: {detail}"))
    if not ok:
        FAILS.append(name)


def make_rows():
    """37 行合成数据：i=10/11 用日期变体，i=12 空 ambient，另加一条产线 A 干扰行。"""
    start = dt.date(2026, 9, 5)
    rows = []
    for i in range(37):
        d = start + dt.timedelta(days=i)
        rows.append([d.isoformat(), "B", round(5.0 + 0.06 * i, 2), 1000 + i,
                     "{}", "[]", "甲班", "RM-2026", "{}", "[]", "[]", "李强"])
    rows[10][0] = "2026/9/15"
    rows[11][0] = "20260916"
    rows[12][8] = ""
    rows.append(["2026-10-05", "A", 5.5, 999, "{}", "[]", "乙班", "RM-X", "{}", "[]", "[]", "干扰"])
    return rows


def write_csv(path, rows, columns):
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(columns)
        writer.writerows(rows)


def run_ingest(*args):
    p = subprocess.run([PY, "-B", str(TOOL), *args], cwd=str(ROOT),
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    return p.returncode, p.stdout, p.stderr


def base_args(src, outdir):
    return ["--input", str(src), "--line", "B",
            "--t-start", "2026-10-05", "--t-end", "2026-10-11",
            "--base-start", "2026-09-05", "--base-end", "2026-10-04",
            "--outdir", str(outdir), "--field-map", FIELD_MAP]


def load_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def assert_ok_payload(name, rc, stdout, stderr, outdir):
    ok = rc == 0 and stderr.strip() == ""
    try:
        payload = json.loads(stdout)
    except Exception as exc:  # noqa: BLE001
        payload = {}
        ok = False
        check(name + "：stdout 是 JSON", False, f"{exc}: {stdout[:200]}")
    check(name + "：rc=0 且无 stderr", ok, f"rc={rc} stderr={stderr[:300]}")
    check(name + "：ok=true 且落两个文件", bool(payload.get("ok")) and
          (outdir / "material_refill_T.json").is_file() and
          (outdir / "material_refill_T30.json").is_file(),
          f"payload={payload} outdir={outdir}")
    return payload


def assert_ok_json_core(tag, t_doc, t30_doc, t_records, t30_records):
    check(f"{tag}：T 记录数={t_records}", len(t_doc["records"]) == t_records, len(t_doc["records"]))
    check(f"{tag}：T30 记录数={t30_records}", len(t30_doc["records"]) == t30_records, len(t30_doc["records"]))
    meta = t_doc["meta"]
    check(f"{tag}：meta.field_contract == demo 12 列", meta.get("field_contract") == COLS, meta.get("field_contract"))
    check(f"{tag}：meta.null_rate_declared is None", meta.get("null_rate_declared") is None, meta.get("null_rate_declared"))
    check(f"{tag}：meta.planted_signal == {{}}", meta.get("planted_signal") == {}, meta.get("planted_signal"))
    check(f"{tag}：meta.dataset/purpose", meta.get("dataset") == "material_refill_T" and meta.get("purpose") == "T", meta)
    check(f"{tag}：T30 dataset/purpose", t30_doc["meta"].get("dataset") == "material_refill_T30"
          and t30_doc["meta"].get("purpose") == "T30", t30_doc["meta"])
    dates = [r.get("date") for r in t_doc["records"]]
    check(f"{tag}：T 窗口含首尾 2026-10-05..2026-10-11", dates == [f"2026-10-{d:02d}" for d in range(5, 12)], dates)
    check(f"{tag}：--line 精确过滤（无产线 A）", all(r.get("production_line") == "B" for r in t_doc["records"]),
          [r.get("production_line") for r in t_doc["records"]])
    check(f"{tag}：日期归一 2026-09-15 / 2026-09-16",
          any(r.get("date") == "2026-09-15" for r in t30_doc["records"]) and
          any(r.get("date") == "2026-09-16" for r in t30_doc["records"]),
          [r.get("date") for r in t30_doc["records"]][:15])
    ambient = next((r.get("ambient_env") for r in t30_doc["records"] if r.get("date") == "2026-09-17"), "missing")
    check(f"{tag}：空单元格 → null", ambient is None, repr(ambient))
    check(f"{tag}：数值字段保持数字", isinstance(t_doc["records"][0].get("refill_rate"), (int, float))
          and isinstance(t_doc["records"][0].get("output_volume"), int), t_doc["records"][0])


def case_csv_and_contract():
    rows = make_rows()
    src = TMP / "task.csv"
    write_csv(src, rows, SRC_COLS)
    outdir = TMP / "out-csv"
    rc, out, err = run_ingest(*base_args(src, outdir))
    payload = assert_ok_payload("CSV 全链路", rc, out, err, outdir)
    t_doc = load_json(outdir / "material_refill_T.json")
    t30_doc = load_json(outdir / "material_refill_T30.json")
    assert_ok_json_core("CSV", t_doc, t30_doc, 7, 30)
    applied = payload.get("field_map_applied") or {}
    check("CSV：字段映射生效（日期→date）", applied.get("日期") == "date", applied)
    check("CSV：summary t_records/t30_records", payload.get("t_records") == 7 and payload.get("t30_records") == 30, payload)
    # 输出的 T/T30 必须能过数据合同（含 T30）
    p = subprocess.run([PY, "-B", str(ROOT / "tools" / "check_data_contract.py"),
                        "--t", str(outdir / "material_refill_T.json"),
                        "--t30", str(outdir / "material_refill_T30.json"), "--strict"],
                       cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", errors="replace")
    check("CSV：输出通过 check_data_contract --strict", p.returncode == 0,
          (p.stdout or p.stderr)[-500:])


def case_errors():
    rows = make_rows()
    # 缺列
    src = TMP / "missing.csv"
    cols = [c for c in SRC_COLS if c != "产量"]
    write_csv(src, [[v for col, v in zip(SRC_COLS, row) if col != "产量"] for row in rows], cols)
    outdir = TMP / "out-missing"
    rc, out, err = run_ingest(*base_args(src, outdir))
    payload = {}
    try:
        payload = json.loads(out)
    except Exception:  # noqa: BLE001
        pass
    check("缺列：rc=2", rc == 2, rc)
    check("缺列：单行 JSON 错误", bool(payload.get("ok") is False) and "缺少必需列" in str(payload.get("error")), out[:300])
    check("缺列：无 traceback（stderr 空）", err.strip() == "", err[:300])
    check("缺列：不落半成品文件", not (outdir / "material_refill_T.json").exists(), outdir)

    # 窗口空（合法文件 + 字段映射，窗口在数据之外）
    rc, out, err = run_ingest("--input", str(TMP / "task.csv"), "--line", "B",
                              "--t-start", "2027-01-01", "--t-end", "2027-01-07",
                              "--base-start", "2026-09-05", "--base-end", "2026-10-04",
                              "--outdir", str(TMP / "out-empty"), "--field-map", FIELD_MAP)
    payload = {}
    try:
        payload = json.loads(out)
    except Exception:  # noqa: BLE001
        pass
    check("窗口空：rc=2 + 窗口内无数据", rc == 2 and "窗口内无数据" in str(payload.get("error")), out[:300])
    check("窗口空：无 traceback", err.strip() == "", err[:300])

    # 文件不存在
    rc, out, err = run_ingest(*base_args(TMP / "nope.csv", TMP / "out-nofile"))
    payload = {}
    try:
        payload = json.loads(out)
    except Exception:  # noqa: BLE001
        pass
    check("文件不存在：rc=2 + 文件不存在", rc == 2 and "文件不存在" in str(payload.get("error")), out[:300])
    check("文件不存在：无 traceback", err.strip() == "", err[:300])

    # 不支持后缀
    bad_suffix = TMP / "task.txt"
    bad_suffix.write_text("x", encoding="utf-8")
    rc, out, err = run_ingest(*base_args(bad_suffix, TMP / "out-suffix"))
    payload = {}
    try:
        payload = json.loads(out)
    except Exception:  # noqa: BLE001
        pass
    check("不支持后缀：rc=2 + 明确错误", rc == 2 and "不支持的数据文件类型" in str(payload.get("error")), out[:300])


def case_xlsx():
    try:
        import openpyxl  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        print(f"[SKIP] .xlsx 用例：openpyxl 不可用（{exc}）")
        return
    rows = make_rows()
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.append(SRC_COLS)
    for i, row in enumerate(rows):
        values = list(row)
        if i in (10, 11):
            # Excel 真日期单元格（而不是文本），验证 datetime → YYYY-MM-DD
            values[0] = dt.datetime.fromisoformat("2026-09-15" if i == 10 else "2026-09-16")
        if i == 12:
            values[8] = None  # 空单元格
        sheet.append(values)
    xlsx = TMP / "task.xlsx"
    workbook.save(xlsx)
    workbook.close()
    outdir = TMP / "out-xlsx"
    rc, out, err = run_ingest(*base_args(xlsx, outdir))
    payload = assert_ok_payload("XLSX 全链路", rc, out, err, outdir)
    t_doc = load_json(outdir / "material_refill_T.json")
    t30_doc = load_json(outdir / "material_refill_T30.json")
    assert_ok_json_core("XLSX", t_doc, t30_doc, 7, 30)
    check("XLSX：summary 行数", payload.get("t_records") == 7 and payload.get("t30_records") == 30, payload)


def case_fake_xls():
    src = TMP / "fake.xls"
    write_csv(src, make_rows(), SRC_COLS)
    rc, out, err = run_ingest(*base_args(src, TMP / "out-xls"))
    payload = {}
    try:
        payload = json.loads(out)
    except Exception:  # noqa: BLE001
        pass
    check("伪 .xls：rc=2 + 提示另存为 .xlsx", rc == 2 and "请另存为 .xlsx" in str(payload.get("error")), out[:300])
    check("伪 .xls：无 traceback", err.strip() == "", err[:300])


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    print(f"tmp = {TMP}")
    case_csv_and_contract()
    case_errors()
    case_xlsx()
    case_fake_xls()
    print()
    if FAILS:
        print(f"RESULT: FAIL ({len(FAILS)} 项) :: {FAILS[:5]}")
        return 1
    print("RESULT: PASS（ingest 去 pandas 读取层全绿）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
