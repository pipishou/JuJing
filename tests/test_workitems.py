#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""工作事项账本 + 零依赖 xlsx 读取器测试（stdlib only）。

覆盖三件在真实数据上踩过的事：
1. WPS/企微导出的 xlsx 会让 openpyxl 抛 `TypeError: Fill() takes no arguments`，
   所以 xlsx_lite 只读值、不碰样式 —— 这里用畸形 styles.xml 造一个这样的文件。
2. 真实清单的分组标题行在**表头上面一行**（`▎改革试点（1项）`），漏了就会整批落进「未分组」。
3. 重复导入不能翻倍；进度越界必须报错而不是静默截断。
"""
import io
import json
import os
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
sys.path.insert(0, str(TOOLS))

import workitems_store as ws  # noqa: E402
import import_workitems as imp  # noqa: E402
import xlsx_lite  # noqa: E402

CHECK = []


def check(name, cond, extra=""):
    CHECK.append((name, bool(cond), extra))
    if not cond:
        print("FAIL %s %s" % (name, extra))


NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"


def build_hostile_xlsx(path):
    """造一个样式畸形但数据正常的 xlsx（模拟 WPS 导出）。"""
    shared = ["项", "甲", "进行中", "2026-10-01"]
    sheet_rows = []
    # r1: 标题；r2: 空；r3: 分组；r4: 表头；r5: 一条；r6: 日期用序列号 + 日期样式
    rows = [
        [("A1", "s", 0)],
        [],
        [("A3", "inline", "▎试点（1项）")],
        [("A4", "inline", "序号"), ("B4", "inline", "问题点描述"), ("C4", "inline", "责任人"),
         ("D4", "inline", "计划完成时间"), ("E4", "inline", "状态")],
        [("A5", "n", "1"), ("B5", "s", 1), ("C5", "s", 1), ("D5", "s", 3), ("E5", "s", 2)],
        [("A6", "n", "2"), ("B6", "inline", "第二条"), ("C6", "inline", "乙"),
         ("D6", "n", "46275"), ("E6", "inline", "待开始")],
    ]
    for i, cells in enumerate(rows, start=1):
        if not cells:
            continue
        parts = []
        for ref, kind, val in cells:
            if kind == "s":
                parts.append('<c r="%s" t="s"><v>%s</v></c>' % (ref, val))
            elif kind == "inline":
                parts.append('<c r="%s" t="inlineStr"><is><t>%s</t></is></c>' % (ref, val))
            elif kind == "n" and ref.startswith("D") and val == "46275":
                parts.append('<c r="%s" s="1"><v>%s</v></c>' % (ref, val))
            else:
                parts.append('<c r="%s"><v>%s</v></c>' % (ref, val))
        sheet_rows.append('<row r="%d">%s</row>' % (i, "".join(parts)))
    sheet_xml = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                 '<worksheet xmlns="%s"><sheetData>%s</sheetData></worksheet>'
                 % (NS, "".join(sheet_rows)))
    shared_xml = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                  '<sst xmlns="%s" count="%d" uniqueCount="%d">%s</sst>'
                  % (NS, len(shared), len(shared), "".join("<si><t>%s</t></si>" % s for s in shared)))
    # 关键：styles.xml 里塞一个带属性的 <fill>（openpyxl 会在解析期抛 TypeError）
    styles_xml = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                  '<styleSheet xmlns="%s">'
                  '<numFmts count="1"><numFmt numFmtId="164" formatCode="yyyy\\-mm\\-dd"/></numFmts>'
                  '<fonts count="1"><font><sz val="11"/></font></fonts>'
                  '<fills count="2"><fill color="FFFF0000"/><fill><patternFill patternType="none"/></fill></fills>'
                  '<borders count="1"><border/></borders>'
                  '<cellStyleXfs count="1"><xf numFmtId="0"/></cellStyleXfs>'
                  '<cellXfs count="2"><xf numFmtId="0"/><xf numFmtId="14" applyNumberFormat="1"/></cellXfs>'
                  '</styleSheet>' % NS)
    workbook_xml = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                    '<workbook xmlns="%s" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
                    '<sheets><sheet name="工作事项清单" sheetId="1" r:id="rId1"/></sheets></workbook>' % NS)
    wb_rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
               '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
               '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
               '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/sharedStrings" Target="sharedStrings.xml"/>'
               '<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
               '</Relationships>')
    root_rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                 '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                 '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
                 '</Relationships>')
    content_types = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                     '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                     '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                     '<Default Extension="xml" ContentType="application/xml"/>'
                     '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
                     '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
                     '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
                     '<Override PartName="/xl/sharedStrings.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/>'
                     '</Types>')
    with zipfile.ZipFile(str(path), "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("_rels/.rels", root_rels)
        zf.writestr("xl/workbook.xml", workbook_xml)
        zf.writestr("xl/_rels/workbook.xml.rels", wb_rels)
        zf.writestr("xl/styles.xml", styles_xml)
        zf.writestr("xl/sharedStrings.xml", shared_xml)
        zf.writestr("xl/worksheets/sheet1.xml", sheet_xml)
    return path


def sample_rows():
    """真实清单的形状：分组行在表头上一行 + 表中再出现一次分组。"""
    return [
        ["企业管理部 · 工作事项（2026-10-24）"],
        [],
        ["优先级：紧急且重要：I级……"],
        ["▎改革试点（1项）"],
        ["序号", "问题类型", "问题点描述", "优先级", "影响程度", "涉及部门",
         "解决方案", "责任人", "计划完成时间", "状态", "备注"],
        ["1", "改革试点", "研发中心：晋升体系改革试点", "II", "影响梯队", "研发中心 \n人力资源中心",
         "待绩效经理梳理", "赵蓉", "", "待开始", "·"],
        ["2", "改革试点", "塑胶厂：模房管理汇报", "II", "影响积极性", "工模部",
         "8月13日汇报", "刘秀平", "2026-09-10", "进行中", ""],
        ["▎流程管理（2项）"],
        ["1", "采购单价", "采购单价流程", "I", "影响成本", "成本控制部",
         "已签批", "刘秀平", "持续", "暂停", ""],
        ["2", "变更管理", "工程更改规范", "III", "·", "体系组", "修订中", "常贺平", "2026-11-01", "已完成", "备注一"],
    ]


def main():
    tmp = Path(tempfile.mkdtemp(prefix="wi-test-"))

    # --- xlsx_lite：畸形样式不会挡住读值 -------------------------------
    xlsx = build_hostile_xlsx(tmp / "hostile.xlsx")
    data = xlsx_lite.read_sheets(xlsx)
    check("xlsx 读到工作表", "工作事项清单" in data, str(list(data)))
    rows = data.get("工作事项清单") or []
    check("xlsx 行数（空行被跳过）", len(rows) == 5, str(len(rows)))
    check("xlsx 共享字符串", rows[3][1] == "甲", repr(rows[3][:3]))
    check("xlsx 日期序列号转换（样式在 numFmt）", rows[4][3] == "2026-09-10", repr(rows[4][3]))
    check("xlsx 日期原值不带时间", rows[3][3] == "2026-10-01", repr(rows[3][3]))
    check("xlsx 内联字符串", rows[1][0] == "▎试点（1项）", repr(rows[1][:1]))
    # 记住：openpyxl 对同类文件会抛 TypeError（真实 WPS 文件实测），这里不依赖它
    try:
        import openpyxl
        try:
            openpyxl.load_workbook(str(xlsx), read_only=True, data_only=True)
            check("openpyxl 兼容性（信息项）", True, "本次畸形样式它也能读，不影响结论")
        except Exception:
            check("openpyxl 兼容性（信息项）", True, "openpyxl 拒读，xlsx_lite 仍可读")
    except ImportError:
        pass

    # --- parse_sheet：分组 / 状态 / 日期 / 空占位 ----------------------
    items = ws.parse_sheet(sample_rows())
    check("解析条数", len(items) == 4, str(len(items)))
    check("表头前分组生效", items[0]["section"] == "改革试点", items[0]["section"])
    check("表中分组切换", items[2]["section"] == "流程管理", items[2]["section"])
    check("责任人", items[0]["owner"] == "赵蓉", items[0]["owner"])
    check("状态归一 进行中", items[1]["status"] == "doing", items[1]["status"])
    check("状态归一 暂停", items[2]["status"] == "paused", items[2]["status"])
    check("状态归一 已完成", items[3]["status"] == "done", items[3]["status"])
    check("进度按状态默认", items[3]["progress"] == 100, str(items[3]["progress"]))
    check("日期原样保留 due_raw", items[2]["due_raw"] == "持续" and items[2]["due"] == "", repr(items[2]["due_raw"]))
    check("换行归一成空格", "\n" not in items[0]["depts"], repr(items[0]["depts"]))
    check("· 当空值", items[3]["impact"] == "", repr(items[3]["impact"]))
    check("优先级归一", [i["priority"] for i in items] == ["II", "II", "I", "III"],
          str([i["priority"] for i in items]))

    # --- 账本：合并幂等 / 更新 / 越界 / 汇总 ---------------------------
    store = ws.empty_store()
    store, stats = ws.merge_items(store, items, by="导入")
    check("首次合并新增", stats == {"added": 4, "updated": 0, "total": 4}, str(stats))
    store, stats2 = ws.merge_items(store, items, by="导入")
    check("重复导入不翻倍", stats2 == {"added": 0, "updated": 4, "total": 4}, str(stats2))
    ids = [it["id"] for it in store["items"]]
    check("id 稳定", ids == ["WI-0001", "WI-0002", "WI-0003", "WI-0004"], str(ids))

    store, item = ws.update_item(store, "WI-0001", {"status": "进行中", "progress": 40}, by="老板")
    check("更新状态", item["status"] == "doing", item["status"])
    check("更新进度", item["progress"] == 40, str(item["progress"]))
    check("更新人留痕", item["updated_by"] == "老板", item["updated_by"])
    try:
        ws.update_item(store, "WI-0001", {"progress": 150})
        check("进度越界报错", False, "150 被接受了")
    except ws.StoreError as exc:
        check("进度越界报错", "0–100" in str(exc), str(exc))
    try:
        ws.update_item(store, "WI-0001", {"hack": 1})
        check("未知字段拒改", False, "hack 被接受了")
    except ws.StoreError:
        check("未知字段拒改", True)
    try:
        ws.update_item(store, "WI-9999", {"status": "done"})
        check("不存在的 id 报错", False)
    except ws.StoreError:
        check("不存在的 id 报错", True)

    from datetime import date as _d
    store, new_item = ws.add_item(store, {"title": "新增事项", "owner": "莫光伟", "due": "2026-10-01"}, by="老板")
    check("新增自增 id", new_item["id"] == "WI-0005", new_item["id"])
    summ = ws.summary(store, _d(2026, 10, 28))
    check("汇总总数", summ["total"] == 5, str(summ["total"]))
    check("逾期只算未完成且有日期", summ["overdue_count"] == 2, str(summ["overdue_count"]))
    check("逾期率=逾期/在办", summ["overdue_rate"] == 50.0, str(summ["overdue_rate"]))
    check("闭环率", summ["closure_rate"] == 20.0, str(summ["closure_rate"]))
    check("分母 0 → None",
          ws.summary(ws.empty_store(), _d(2026, 1, 1))["overdue_rate"] is None)

    # --- 落盘 + 导出 + CLI -------------------------------------------
    path = tmp / "store.json"
    ws.save(store, path)
    again = ws.load(path)
    check("落盘可回读", again["revision"] == store["revision"], str(again["revision"]))
    check("revision 自增", again["revision"] == 1, str(again["revision"]))
    csv_path = ws.write_csv(again, tmp / "out.csv")
    text = csv_path.read_text(encoding="utf-8-sig")
    check("CSV 有表头", text.splitlines()[0].startswith("序号,分组,问题类型,问题点描述"), text.splitlines()[0][:30])
    check("CSV 行数", len(text.splitlines()) == 6, str(len(text.splitlines())))

    bad = tmp / "bad.json"
    bad.write_text("{}", encoding="utf-8")
    try:
        ws.load(bad)
        check("坏账本拒绝加载", False)
    except ws.StoreError:
        check("坏账本拒绝加载", True)

    env = dict(os.environ, PYTHONIOENCODING="utf-8", BOT_WORKITEMS_FILE=str(path))
    r = subprocess.run([sys.executable, "-B", str(TOOLS / "workitems_store.py"), "set", "WI-0001",
                        "--status", "done", "--progress", "100"], capture_output=True, env=env)
    check("CLI set rc=0", r.returncode == 0, r.stderr.decode("utf-8", "replace")[:120])
    check("CLI set 生效", "done" in r.stdout.decode("utf-8", "replace"), "")
    r2 = subprocess.run([sys.executable, "-B", str(TOOLS / "workitems_store.py"), "set", "WI-0001",
                         "--progress", "300"], capture_output=True, env=env)
    check("CLI 越界 rc=2", r2.returncode == 2, str(r2.returncode))
    r3 = subprocess.run([sys.executable, "-B", str(TOOLS / "import_workitems.py"),
                         str(tmp / "nope.xlsx")], capture_output=True, env=env)
    check("CLI 不存在的文件 rc=2", r3.returncode == 2, str(r3.returncode))
    r4 = subprocess.run([sys.executable, "-B", str(TOOLS / "import_workitems.py"), str(xlsx)],
                        capture_output=True, env=dict(env, BOT_WORKITEMS_FILE=str(tmp / "store2.json")))
    check("CLI 导入畸形 xlsx rc=0", r4.returncode == 0, r4.stdout.decode("utf-8", "replace")[:200])

    failed = [c for c in CHECK if not c[1]]
    print("共 %d 项，通过 %d 项" % (len(CHECK), len(CHECK) - len(failed)))
    if failed:
        for name, _, extra in failed:
            print("  ✗", name, extra)
        print("RESULT: FAIL")
        return 1
    print("RESULT: PASS（工作事项账本 + xlsx 读取全绿）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
