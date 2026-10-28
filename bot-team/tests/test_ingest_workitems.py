#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tools/ingest_workitems.py 回归：清单 → workitems.v1 快照。

跑法：python -B tests/test_ingest_workitems.py   （全绿打印 PASS，失败非 0 退出）
"""
import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import ingest_workitems as iw  # noqa: E402

FAILS = []
COUNT = 0


def check(name, cond, detail=""):
    global COUNT
    COUNT += 1
    if cond:
        print("  PASS " + name)
    else:
        FAILS.append(name)
        print("  FAIL " + name + ("  <<< " + str(detail) if detail else ""))


CSV_CN = """工作事项,负责人,截止日,状态,优先级,预估工时,阻塞原因,完成日
补料率复盘材料,本人,2026-10-20,进行中,高,4,,
M-03 换模方案,本人,2026-10-26,阻塞,高,8,等模具厂家报价,
周会纪要整理,本人,2026-10-30,待办,中,1,,
旧产线数据迁移,本人,2026-09-30,已完成,低,16,,2026-09-29
供应商走访,他人,2026-10-25,进行中,中,6,,
"""


def run_csv(text, **kw):
    tmp = Path(tempfile.mkdtemp(prefix="wi-"))
    path = tmp / "items.csv"
    path.write_text(text, encoding="utf-8-sig")
    return iw.build_snapshot(path, **kw)


def main():
    print("== 统计口径 ==")
    doc = run_csv(CSV_CN, as_of="2026-10-28")
    s = doc["summary"]
    check("schema", doc["schema"] == "workitems.v1", doc["schema"])
    check("总行数 5（含他人）", s["total_items"] == 5, s)
    check("owner 过滤后 4", run_csv(CSV_CN, as_of="2026-10-28", owner="本人")["summary"]["total_items"] == 4)
    check("在办 4（含他人）", s["open_items"] == 4, s)
    check("逾期 3（10-20 / 10-25 / 10-26；已完成不计）", s["overdue_items"] == 3, s)
    check("本周到期 1（10-30 在 7 天内）", s["due_this_week"] == 1, s)
    check("阻塞 1", s["blocked_items"] == 1, s)
    check("逾期率 = 3/4 = 75.0", s["overdue_rate"] == 75.0, s["overdue_rate"])
    check("完成 1 条且按期 → closure 100", s["closure_rate"] == 100.0, s["closure_rate"])
    check("缺负责人/缺截止日计数为 0", s["data_gaps"] == {"no_owner": 0, "no_due": 0}, s["data_gaps"])
    check("worst_three 取逾期最久 3 条", len(s["worst_three"]) == 3 and s["worst_three"][0]["overdue_days"] == 8, s["worst_three"])

    print("== 完成/阻塞/未知状态映射 ==")
    doc2 = run_csv(CSV_CN, as_of="2026-10-28", owner="本人")
    kinds = {i["title"]: i["status"] for i in doc2["items"]}
    check("进行中→open", kinds["补料率复盘材料"] == "open", kinds)
    check("阻塞→blocked", kinds["M-03 换模方案"] == "blocked", kinds)
    check("待办→open", kinds["周会纪要整理"] == "open", kinds)
    check("已完成→done", kinds["旧产线数据迁移"] == "done", kinds)

    print("== 日期与负数边界 ==")
    doc3 = run_csv("事项,负责人,截止日,状态\nA,我,2026/10/20,进行中\nB,我,20261021,进行中\nC,我,45000,进行中\n",
                   as_of="2026-10-28")
    dues = [i["due"] for i in doc3["items"]]
    check("斜杠日期", dues[0] == "2026-10-20", dues)
    check("紧凑日期", dues[1] == "2026-10-21", dues)
    check("Excel 序列号", dues[2] is not None and dues[2].startswith("2023-03"), dues)

    print("== 分母为 0 不冒充成绩 ==")
    all_done = run_csv("事项,负责人,截止日,状态,完成日\nA,我,2026-10-20,已完成,2026-10-19\n", as_of="2026-10-28")
    check("无在办时 overdue_rate=None", all_done["summary"]["overdue_rate"] is None, all_done["summary"])

    print("== 溯源 ==")
    doc4 = run_csv(CSV_CN, as_of="2026-10-28")
    raw = (Path(doc4["source"]["path"])).read_bytes()
    check("sha256 与文件一致", doc4["source"]["sha256"] == hashlib.sha256(raw).hexdigest())
    check("rows 与 items 数一致", doc4["source"]["rows"] == len(doc4["items"]))

    print("== 报错路径（rc=2，不静默补默认值）==")
    cases = [
        ("缺 title 列", "负责人,截止日,状态\n我,2026-10-20,进行中\n"),
        ("空表", ""),
        ("只有表头", "事项,负责人,截止日,状态\n"),
        ("日期非法", "事项,负责人,截止日,状态\nA,我,下周三,进行中\n"),
    ]
    for name, text in cases:
        tmp = Path(tempfile.mkdtemp(prefix="wi-bad-"))
        path = tmp / "x.csv"
        path.write_text(text, encoding="utf-8-sig")
        try:
            iw.build_snapshot(path, as_of="2026-10-28")
            check("拒绝：" + name, False, "未抛错")
        except iw.IngestError as exc:
            check("拒绝：" + name, True, str(exc))

    print("== field-map 覆盖表头 ==")
    tmp = Path(tempfile.mkdtemp(prefix="wi-fm-"))
    csv_path = tmp / "odd.csv"
    csv_path.write_text("事项名称,执行人,计划完成,进展\nX,我,2026-10-25,进行中\n", encoding="utf-8-sig")
    fm = {"source_columns": {"title": "事项名称", "owner": "执行人", "due": "计划完成", "status": "进展"}}
    doc5 = iw.build_snapshot(csv_path, as_of="2026-10-28", field_map=fm)
    check("field-map 生效", doc5["items"][0]["title"] == "X", doc5["items"][0])
    bad_fm = {"source_columns": {"title": "不存在的列"}}
    try:
        iw.build_snapshot(csv_path, as_of="2026-10-28", field_map=bad_fm)
        check("field-map 指定列缺失要报错", False)
    except iw.IngestError as exc:
        check("field-map 指定列缺失要报错", "找不到" in str(exc), exc)

    print("== xlsx 通道 ==")
    try:
        from openpyxl import Workbook  # noqa: PLC0415
        tmp = Path(tempfile.mkdtemp(prefix="wi-xlsx-"))
        xlsx = tmp / "items.xlsx"
        wb = Workbook()
        ws = wb.active
        ws.append(["工作事项", "负责人", "截止日", "状态"])
        ws.append(["表格事项", "本人", "2026-10-22", "进行中"])
        wb.save(xlsx)
        doc6 = iw.build_snapshot(xlsx, as_of="2026-10-28")
        check("xlsx 读到 1 条", doc6["summary"]["total_items"] == 1, doc6["summary"])
        check("xlsx sheets 记录表名", doc6["source"]["sheets"] == ["Sheet"])
    except ImportError:
        print("  SKIP openpyxl 不可用")

    print("== CLI ==")
    tmp = Path(tempfile.mkdtemp(prefix="wi-cli-"))
    csv_path = tmp / "c.csv"
    csv_path.write_text(CSV_CN, encoding="utf-8-sig")
    out = tmp / "out.json"
    rc = subprocess.run([sys.executable, "-B", str(ROOT / "tools" / "ingest_workitems.py"),
                         str(csv_path), "--as-of", "2026-10-28", "-o", str(out)],
                        capture_output=True, text=True, encoding="utf-8", errors="replace")
    check("CLI rc=0", rc.returncode == 0, rc.stderr[-300:])
    check("CLI 落盘可解析", out.is_file() and json.loads(out.read_text(encoding="utf-8"))["schema"] == "workitems.v1")
    bad = tmp / "bad.csv"
    bad.write_text("负责人,截止日,状态\n我,2026-10-20,进行中\n", encoding="utf-8-sig")
    rc2 = subprocess.run([sys.executable, "-B", str(ROOT / "tools" / "ingest_workitems.py"), str(bad)],
                         capture_output=True, text=True, encoding="utf-8", errors="replace")
    check("CLI 缺列 rc=2", rc2.returncode == 2, rc2.returncode)
    check("CLI 错误是单行 JSON", json.loads(rc2.stdout.strip().splitlines()[0])["ok"] is False, rc2.stdout[:200])

    print()
    if FAILS:
        print("FAILED %d / %d: %s" % (len(FAILS), COUNT, ", ".join(FAILS)))
        return 1
    print("PASS 全绿（%d 项）" % COUNT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
