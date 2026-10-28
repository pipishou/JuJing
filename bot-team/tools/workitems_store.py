#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""工作事项账本（唯一写入入口，stdlib only）。

问题：同一批工作事项有多个副本（企微在线文档、导出 xlsx ×12、周检报告），
谁都能改，谁都改不全 → 进度永远对不上。解法不是再做一个文档，而是定一个
**机器可读的唯一账本**，所有入口（前端表格 / 导入 xlsx / CLI）都写它，
导出物只是镜像。

账本文件：`runtime/workitems/current.json`（env `BOT_WORKITEMS_FILE` 可改）
  schema  workitems.store.v1
  revision 自增，每次写入 +1（并发写靠它发现冲突）
  items    事项数组，id 稳定（WI-0001…），外部用 id 引用，不用行号

一条事项：
  id / section（分组，如「流程管理」）/ seq（原表序号）/ category（问题类型）
  title / priority（I/II/III）/ impact / depts / solution / owner
  due（ISO 或 ""）/ due_raw（原样，如「持续」）/ status（见 STATUS）
  progress（0–100 整数）/ note / updated_at / updated_by

用法（CLI）：
  python -B tools/workitems_store.py list [--status doing]
  python -B tools/workitems_store.py summary [--as-of 2026-10-26]
  python -B tools/workitems_store.py set WI-0003 --status done --progress 100 --by 老板
  python -B tools/workitems_store.py export --format csv -o 清单.csv
"""
import argparse
import csv
import hashlib
import json
import os
import re
import sys
import tempfile
from datetime import date, datetime
from pathlib import Path

SCHEMA = "workitems.store.v1"
ROOT = Path(__file__).resolve().parents[1]

# 状态词 → 机器值（前端只认这 6 个，导入时把五花八门的中文折进来）
STATUS = {
    "todo": ("待开始", "未开始", "待办", "未启动", "计划中", "todo", "open", "new"),
    "doing": ("进行中", "在办", "办理中", "推进中", "处理中", "doing", "in_progress", "wip"),
    "paused": ("暂停", "挂起", "搁置", "暂缓", "paused", "hold", "on_hold"),
    "blocked": ("阻塞", "卡住", "受阻", "blocked", "waiting"),
    "done": ("已完成", "完成", "已关闭", "关闭", "已办结", "done", "closed", "finished"),
    "cancelled": ("已取消", "取消", "作废", "cancelled", "canceled", "dropped"),
}
STATUS_LABEL = {k: v[0] for k, v in STATUS.items()}
STATUS_ORDER = ("todo", "doing", "blocked", "paused", "done", "cancelled")

# 表头识别（真实表：序号|问题类型|问题点描述|优先级|影响程度|涉及部门|解决方案|责任人|计划完成时间|状态|备注）
HEADER_ALIASES = {
    "seq": ("序号", "编号", "no", "seq"),
    "category": ("问题类型", "类别", "分类", "类型", "category"),
    "title": ("问题点描述", "工作事项", "事项", "任务", "事项名称", "工作内容", "title"),
    "priority": ("优先级", "重要度", "priority"),
    "impact": ("影响程度", "影响", "impact"),
    "depts": ("涉及部门", "部门", "协同部门", "dept", "depts"),
    "solution": ("解决方案", "措施", "方案", "solution", "action"),
    "owner": ("责任人", "负责人", "执行人", "owner", "assignee"),
    "due": ("计划完成时间", "计划完成", "截止日", "截止日期", "完成时间", "due", "deadline"),
    "status": ("状态", "进展", "当前状态", "status", "state"),
    "note": ("备注", "说明", "note", "remark"),
}
REQUIRED_HEADERS = ("title", "owner", "due", "status")
SECTION_RE = re.compile(r"^\s*(?:▎|■|◆|●|【)\s*(.+?)\s*[)】]?\s*$")


class StoreError(Exception):
    """账本不可用（坏 JSON / 版本不符 / 找不到事项），CLI 一律 rc=2。"""


# ---------------------------------------------------------------- 路径与读写

def store_path():
    env = os.environ.get("BOT_WORKITEMS_FILE")
    if env:
        return Path(env)
    return ROOT / "runtime" / "workitems" / "current.json"


def _now():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def empty_store():
    return {"schema": SCHEMA, "revision": 0, "updated_at": _now(),
            "source": {"kind": "empty"}, "items": []}


def load(path=None):
    p = Path(path) if path else store_path()
    if not p.exists():
        return empty_store()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        raise StoreError("账本读不了：%s（%s）" % (p, exc))
    if data.get("schema") != SCHEMA:
        raise StoreError("账本版本不符：%r（期望 %s）" % (data.get("schema"), SCHEMA))
    data.setdefault("items", [])
    data.setdefault("revision", 0)
    return data


def save(store, path=None, bump=True):
    """原子写：先写同目录临时文件再 replace，避免半个 JSON。"""
    p = Path(path) if path else store_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    if bump:
        store["revision"] = int(store.get("revision") or 0) + 1
    store["updated_at"] = _now()
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".wi-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(store, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        os.replace(tmp, str(p))
    finally:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass
    return store


# ---------------------------------------------------------------- 归一化

def norm_status(value):
    raw = str(value or "").strip()
    if not raw:
        return "todo"
    low = raw.lower()
    for key, words in STATUS.items():
        for w in words:
            if low == w.lower() or low.startswith(w.lower()):
                return key
    # 「进行中 60%」这类带进度的写法
    if "进行" in raw or "推进" in raw:
        return "doing"
    if "完" in raw:
        return "done"
    return "todo"


def norm_priority(value):
    raw = str(value or "").strip().upper()
    if not raw:
        return ""
    m = re.match(r"^(III|II|I)", raw)
    if m:
        return m.group(1)
    digits = re.findall(r"\d", raw)
    if digits:
        return {"1": "I", "2": "II", "3": "III"}.get(digits[0], "")
    if "紧急" in raw and "重要" in raw:
        return "I"
    return raw[:3]


def norm_progress(value, status):
    """进度：显式数字优先，没有就按状态给默认值（不编造精细度）。"""
    m = re.search(r"(\d{1,3})\s*%", str(value or ""))
    if m:
        return max(0, min(100, int(m.group(1))))
    if isinstance(value, (int, float)):
        return max(0, min(100, int(value)))
    return {"todo": 0, "doing": 50, "paused": 50, "blocked": 30,
            "done": 100, "cancelled": 0}.get(status, 0)


def mask_none(text):
    """"·" 与空白是表里的空占位，不能当内容；单元格内换行/制表归一成空格。"""
    s = re.sub(r"\s+", " ", str(text or "")).strip()
    if s in ("·", "-", "—", "/", "无", "None"):
        return ""
    return s


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------- 表格解析

def _find_header(rows):
    """找表头行：命中 title 且命中 owner/status 的那一行。"""
    for idx, row in enumerate(rows):
        hits = {}
        for cell_i, cell in enumerate(row):
            text = str(cell or "").strip()
            if not text:
                continue
            for field, aliases in HEADER_ALIASES.items():
                if field in hits:
                    continue
                if any(text == a or (len(a) > 1 and a in text) for a in aliases):
                    hits[field] = cell_i
        if all(f in hits for f in REQUIRED_HEADERS):
            return idx, hits
    return None, {}


def _section_of(row):
    """分组标题行：只有一格有字，且形如「▎流程管理（15项）」。"""
    filled = [str(c).strip() for c in row if str(c or "").strip()]
    if len(filled) != 1:
        return ""
    text = filled[0]
    if text[0] in "▎■◆●【" or "（" in text and text.endswith("）") and len(text) <= 24:
        m = SECTION_RE.match(text)
        if m:
            return re.sub(r"[（(]\s*\d+\s*项\s*[)）]", "", m.group(1)).strip()
    return ""


def parse_sheet(rows, default_section="未分组"):
    """把一个工作表（xlsx_lite 的行）解析成事项数组（不含 id）。"""
    header_idx, cols = _find_header(rows)
    if header_idx is None:
        raise StoreError("找不到表头：需要同时具备 %s 的列" % "、".join(REQUIRED_HEADERS))
    items = []
    # 表头之前的最后一个分组标题，是表头下方第一批事项所属分组
    section = default_section
    for row in reversed(rows[:header_idx]):
        got = _section_of(row)
        if got:
            section = got
            break
    for row in rows[header_idx + 1:]:
        if not any(str(c or "").strip() for c in row):
            continue
        got = _section_of(row)
        if got:
            section = got
            continue
        def cell(field):
            i = cols.get(field)
            if i is None or i >= len(row):
                return ""
            return mask_none(row[i])
        title = cell("title")
        if not title:
            continue
        status = norm_status(cell("status"))
        raw_due = cell("due")
        items.append({
            "section": section,
            "seq": cell("seq"),
            "category": cell("category"),
            "title": title,
            "priority": norm_priority(cell("priority")),
            "impact": cell("impact"),
            "depts": cell("depts"),
            "solution": cell("solution"),
            "owner": cell("owner"),
            "due": normalize_due(raw_due),
            "due_raw": raw_due,
            "status": status,
            "progress": norm_progress(raw_due, status),
            "note": cell("note"),
        })
    return items


def normalize_due(raw):
    """'持续' / '46275' / '2026-10-26' → ('持续' 保留在 due_raw) ISO 或 ''。"""
    text = mask_none(raw)
    if not text:
        return ""
    m = re.match(r"^(\d{4})[-/.年](\d{1,2})[-/.月](\d{1,2})", text)
    if m:
        return "%04d-%02d-%02d" % (int(m.group(1)), int(m.group(2)), int(m.group(3)))
    if re.match(r"^\d{5}(\.\d+)?$", text):
        try:
            from datetime import timedelta
            dt = datetime(1899, 12, 30) + timedelta(days=float(text))
            return dt.strftime("%Y-%m-%d")
        except (ValueError, OverflowError):
            return ""
    return ""


# ---------------------------------------------------------------- 合并与写入

def item_key(item):
    return (item.get("section", ""), str(item.get("seq", "")), item.get("title", ""))


def next_id(items):
    best = 0
    for it in items:
        m = re.match(r"WI-(\d+)$", str(it.get("id") or ""))
        if m:
            best = max(best, int(m.group(1)))
    return "WI-%04d" % (best + 1)


def merge_items(store, new_items, source_kind="import", by="import", source=None,
                match_by_id=False):
    """把解析出的事项并入账本。

    幂等规则：有 id 按 id 覆盖；没有 id 时按 (分组, 序号, 标题) 匹配——
    同一份表重复导入不会翻倍，改了标题会当成新事项（保守，不猜）。
    返回 (store, stats)。
    """
    items = [dict(it) for it in store.get("items", [])]
    index_id = {it.get("id"): i for i, it in enumerate(items)}
    index_key = {item_key(it): i for i, it in enumerate(items)}
    now = _now()
    added = updated = 0
    for raw in new_items:
        it = dict(raw)
        it.setdefault("progress", norm_progress("", it.get("status")))
        it["updated_at"] = now
        it["updated_by"] = by
        target = None
        if match_by_id and it.get("id") in index_id:
            target = index_id[it["id"]]
        elif item_key(it) in index_key:
            target = index_key[item_key(it)]
        if target is None:
            it["id"] = it.get("id") or next_id(items)
            items.append(it)
            index_id[it["id"]] = len(items) - 1
            index_key[item_key(it)] = len(items) - 1
            added += 1
        else:
            old = items[target]
            keep_id = old.get("id")
            merged = dict(old)
            merged.update({k: v for k, v in it.items() if k != "id"})
            merged["id"] = keep_id
            items[target] = merged
            index_id[keep_id] = target
            index_key[item_key(merged)] = target
            updated += 1
    store["items"] = items
    if source:
        store["source"] = source
    elif source_kind:
        store.setdefault("source", {})
    return store, {"added": added, "updated": updated, "total": len(items)}


def find_item(store, item_id):
    for i, it in enumerate(store.get("items", [])):
        if it.get("id") == item_id:
            return i, it
    raise StoreError("找不到事项 %s" % item_id)


def update_item(store, item_id, patch, by="老板"):
    idx, item = find_item(store, item_id)
    allowed = {"title", "owner", "due", "status", "progress", "note", "priority",
               "impact", "depts", "solution", "category", "section", "seq"}
    for key, value in (patch or {}).items():
        if key not in allowed:
            raise StoreError("不可改的字段：%s" % key)
        if key == "status":
            value = norm_status(value)
        elif key == "progress":
            try:
                value = int(value)
            except (TypeError, ValueError):
                raise StoreError("progress 必须是 0–100 的整数")
            if not 0 <= value <= 100:
                raise StoreError("progress 必须在 0–100 之间（收到 %s）" % value)
        elif key == "due":
            value = normalize_due(value) or str(value or "")
        else:
            value = str(value or "").strip()
        item[key] = value
    item["updated_at"] = _now()
    item["updated_by"] = str(by or "老板")
    store["items"][idx] = item
    return store, item


def add_item(store, data, by="老板"):
    item = {
        "section": str(data.get("section") or "未分组").strip(),
        "seq": str(data.get("seq") or "").strip(),
        "category": str(data.get("category") or "").strip(),
        "title": str(data.get("title") or "").strip(),
        "priority": norm_priority(data.get("priority")),
        "impact": str(data.get("impact") or "").strip(),
        "depts": str(data.get("depts") or "").strip(),
        "solution": str(data.get("solution") or "").strip(),
        "owner": str(data.get("owner") or "").strip(),
        "due": normalize_due(data.get("due")),
        "due_raw": str(data.get("due") or "").strip(),
        "status": norm_status(data.get("status")),
        "note": str(data.get("note") or "").strip(),
    }
    if not item["title"]:
        raise StoreError("title 必填")
    item["progress"] = norm_progress(data.get("progress"), item["status"])
    item["updated_at"] = _now()
    item["updated_by"] = str(by or "老板")
    item["id"] = next_id(store.get("items", []))
    store.setdefault("items", []).append(item)
    return store, item


def delete_item(store, item_id):
    idx, item = find_item(store, item_id)
    store["items"].pop(idx)
    return store, item


# ---------------------------------------------------------------- 视图

def is_overdue(item, as_of=None):
    if item.get("status") in ("done", "cancelled"):
        return False
    due = item.get("due") or ""
    if not due:
        return False
    try:
        return date.fromisoformat(due) < (as_of or date.today())
    except ValueError:
        return False


def summary(store, as_of=None):
    items = store.get("items", [])
    as_of = as_of or date.today()
    counts = {k: 0 for k in STATUS_ORDER}
    by_priority = {}
    by_section = {}
    overdue = []
    for it in items:
        counts[it.get("status") or "todo"] = counts.get(it.get("status") or "todo", 0) + 1
        pri = it.get("priority") or "未标"
        by_priority[pri] = by_priority.get(pri, 0) + 1
        sec = it.get("section") or "未分组"
        by_section[sec] = by_section.get(sec, 0) + 1
        if is_overdue(it, as_of):
            overdue.append({"id": it.get("id"), "title": it.get("title"),
                            "owner": it.get("owner"), "due": it.get("due")})
    active = counts["todo"] + counts["doing"] + counts["blocked"] + counts["paused"]
    done = counts["done"]
    return {
        "total": len(items),
        "active": active,
        "done": done,
        "overdue_count": len(overdue),
        "overdue_rate": (round(len(overdue) / active * 100, 2) if active else None),
        "closure_rate": (round(done / len(items) * 100, 2) if items else None),
        "by_status": counts,
        "by_priority": by_priority,
        "by_section": by_section,
        "overdue": sorted(overdue, key=lambda x: x.get("due") or "")[:10],
        "as_of": str(as_of),
    }


def export_rows(store):
    header = ["序号", "分组", "问题类型", "问题点描述", "优先级", "影响程度", "涉及部门",
              "解决方案", "责任人", "计划完成时间", "状态", "进度%", "备注", "事项ID"]
    rows = [header]
    for it in store.get("items", []):
        rows.append([
            it.get("seq", ""), it.get("section", ""), it.get("category", ""),
            it.get("title", ""), it.get("priority", ""), it.get("impact", ""),
            it.get("depts", ""), it.get("solution", ""), it.get("owner", ""),
            it.get("due") or it.get("due_raw", ""),
            STATUS_LABEL.get(it.get("status"), it.get("status", "")),
            str(it.get("progress", "")), it.get("note", ""), it.get("id", ""),
        ])
    return rows


def write_csv(store, path):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8-sig", newline="") as fh:
        csv.writer(fh).writerows(export_rows(store))
    return p


# ---------------------------------------------------------------- CLI

def _print(obj):
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def main(argv=None):
    ap = argparse.ArgumentParser(description="工作事项账本（唯一写入入口）")
    ap.add_argument("--file", help="账本路径（默认 runtime/workitems/current.json）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list", help="列出事项")
    p_list.add_argument("--status", help="按状态过滤：todo/doing/blocked/paused/done/cancelled")
    p_list.add_argument("--owner")
    p_list.add_argument("--section")

    p_sum = sub.add_parser("summary", help="汇总（逾期率/闭环率/分布）")
    p_sum.add_argument("--as-of")

    p_set = sub.add_parser("set", help="更新一条事项")
    p_set.add_argument("item_id")
    p_set.add_argument("--status")
    p_set.add_argument("--progress", type=int)
    p_set.add_argument("--owner")
    p_set.add_argument("--due")
    p_set.add_argument("--note")
    p_set.add_argument("--title")
    p_set.add_argument("--by", default="老板")

    p_add = sub.add_parser("add", help="新增一条事项")
    p_add.add_argument("--title", required=True)
    p_add.add_argument("--section", default="未分组")
    p_add.add_argument("--category")
    p_add.add_argument("--priority")
    p_add.add_argument("--impact")
    p_add.add_argument("--depts")
    p_add.add_argument("--solution")
    p_add.add_argument("--owner")
    p_add.add_argument("--due")
    p_add.add_argument("--status")
    p_add.add_argument("--note")
    p_add.add_argument("--by", default="老板")

    p_exp = sub.add_parser("export", help="导出 CSV（可回填表格）")
    p_exp.add_argument("--format", default="csv", choices=["csv"])
    p_exp.add_argument("-o", "--out", required=True)

    args = ap.parse_args(argv)
    path = args.file
    try:
        store = load(path)
        if args.cmd == "list":
            items = store.get("items", [])
            if args.status:
                want = norm_status(args.status)
                items = [it for it in items if it.get("status") == want]
            if args.owner:
                items = [it for it in items if args.owner in (it.get("owner") or "")]
            if args.section:
                items = [it for it in items if args.section in (it.get("section") or "")]
            _print({"ok": True, "count": len(items), "revision": store.get("revision"),
                    "items": [{k: it.get(k, "") for k in
                               ("id", "section", "title", "owner", "due", "status", "progress")}
                              for it in items]})
        elif args.cmd == "summary":
            as_of = date.fromisoformat(args.as_of) if args.as_of else None
            _print({"ok": True, "revision": store.get("revision"),
                    "source": store.get("source"), "updated_at": store.get("updated_at"),
                    "summary": summary(store, as_of)})
        elif args.cmd == "set":
            patch = {k: v for k, v in
                     {"status": args.status, "progress": args.progress, "owner": args.owner,
                      "due": args.due, "note": args.note, "title": args.title}.items()
                     if v is not None}
            store, item = update_item(store, args.item_id, patch, by=args.by)
            save(store, path)
            _print({"ok": True, "revision": store["revision"], "item": item})
        elif args.cmd == "add":
            data = {k: getattr(args, k) for k in
                    ("title", "section", "category", "priority", "impact", "depts",
                     "solution", "owner", "due", "status", "note")}
            store, item = add_item(store, data, by=args.by)
            save(store, path)
            _print({"ok": True, "revision": store["revision"], "item": item})
        elif args.cmd == "export":
            out = write_csv(store, args.out)
            _print({"ok": True, "path": str(out), "rows": len(store.get("items", []))})
        return 0
    except StoreError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    sys.exit(main())
