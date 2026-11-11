#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""kb_query — 阶段进门清单（索引行，不读全文）

用法：
  python scripts/kb_query.py --stage 03            # 该阶段的候选经验清单
  python scripts/kb_query.py --stage 04 --tags 断言  # 追加标签过滤
  python scripts/kb_query.py --stats               # 知识库体检（状态/TTL/锚点）
  python scripts/kb_query.py --stage 03 --json     # 机读

纪律（锚点`进门清单`）：
  1. 只输出索引行（≤params.knowledge.intake_max_entries 行、≤intake_max_lines 行总量），
     全文展开由调用方按 intake_fullread_max 自行 Read —— 本脚本不读条目正文。
  2. 零命中输出「本阶段无可用知识」，不编造。
  3. 不做判定（只报不判）：状态与 TTL 只标注，不阻断。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
INDEX = ROOT / "knowledge" / "INDEX.md"
PARAMS = ROOT / "assets" / "params.json"

# 阶段 → 标签关键词（语义映射；数值预算在 params.json，此处只放映射）
STAGE_TAGS: dict[str, list[str]] = {
    "01": ["立项", "选型", "减法", "需求"],
    "02": ["需求", "访谈", "验收", "判据"],
    "02b": ["方案", "依赖", "切片", "设计", "范围"],
    "03": ["开发", "编码", "范围", "减法", "方案"],
    "04": ["测试", "断言", "机检", "绿测", "验收"],
    "05": ["部署", "发布", "配置"],
    "06": ["版本", "发布", "部署"],
    "07": ["排障", "取证", "潜伏", "调试"],
    "08": ["运营", "减法", "反馈", "知识"],
}


def load_params() -> dict:
    try:
        return json.loads(PARAMS.read_text(encoding="utf-8"))
    except Exception:
        return {}


def parse_rows() -> list[dict]:
    """解析 INDEX.md 主表（到下一个 '## ' 为止）。"""
    text = INDEX.read_text(encoding="utf-8")
    rows: list[dict] = []
    in_main = False
    for line in text.splitlines():
        if line.startswith("## "):
            in_main = line.strip().startswith("## 索引")
            continue
        if not in_main or not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 8 or cells[0] in ("ID", "---") or set(cells[0]) <= {"-"}:
            continue
        kid, path, tags, form, anchor, status, repro, landing = cells[:8]
        if not kid.startswith("KB-"):
            continue
        m = re.search(r"`([^`]+)`", path)
        rel = m.group(1) if m else ""
        rows.append(
            dict(id=kid, rel=rel, tags=tags, form=form, anchor=anchor.strip("`"),
                 status=status, repro=repro, landing=landing)
        )
    return rows


def one_liner(row: dict) -> str:
    p = ROOT / row["rel"] if row["rel"] else None
    if p and p.exists():
        try:
            for line in p.read_text(encoding="utf-8").splitlines():
                if line.startswith("# "):
                    return line[2:].split("·", 1)[-1].strip()[:24]
        except Exception:
            pass
    return row["landing"][:24] if row["landing"] else "—"


def ttl_hint(row: dict, ttl_days: int) -> str:
    if not row["rel"]:
        return ""
    p = ROOT / row["rel"]
    if not p.exists() or ttl_days <= 0:
        return ""
    age = datetime.now() - datetime.fromtimestamp(p.stat().st_mtime)
    if age > timedelta(days=ttl_days):
        return f"（{age.days} 天未改，TTL 超期→待复核）"
    return ""


def match(row: dict, stage: str, extra: list[str]) -> bool:
    keys = STAGE_TAGS.get(stage, [])
    hay = row["tags"] + " " + row["landing"]
    hit = (not keys) or any(k in hay for k in keys)
    if extra:
        hit = hit and any(k in hay or k in one_liner(row) for k in extra)
    return hit


def cmd_list(a) -> int:
    conf = load_params()
    kcfg = conf.get("knowledge", {})
    limit = a.limit or int(kcfg.get("intake_max_entries", 3))
    max_lines = int(kcfg.get("intake_max_lines", 15))
    ttl_days = int(kcfg.get("ttl_days", 90))
    rows = [r for r in parse_rows() if r["status"] != "retired" and match(r, a.stage, a.tags)]
    rows.sort(key=lambda r: (r["status"] != "candidate", r["id"]))
    picked = rows[: min(limit, max(1, max_lines))]

    if a.json:
        print(json.dumps({"stage": a.stage, "count": len(picked),
                          "budget": {"max_entries": limit, "max_lines": max_lines},
                          "items": [{**r, "one_liner": one_liner(r)} for r in picked]},
                         ensure_ascii=False, indent=1))
        return 0

    print(f"[kb] 阶段 {a.stage} 进门清单（≤{limit} 条；全文展开上限 "
          f"{kcfg.get('intake_fullread_max', 1)} 条；禁止整目录读）")
    if not picked:
        print("本阶段无可用知识（清单为空＝照常开工，禁止编造引用）")
        return 0
    for r in picked:
        tip = ttl_hint(r, ttl_days)
        print(f"- {r['id']}｜{r['tags']}｜{one_liner(r)}｜{r['status']}｜{r['rel'] or '（已升正文）'}{tip}")
    chars = sum(len(l) for l in sys.stdout.getvalue().splitlines()) if hasattr(sys.stdout, "getvalue") else 0
    print(f"[kb] 共 {len(picked)} 行；另有 {max(0, len(rows) - len(picked))} 条相似条目未列出")
    return 0


def cmd_stats(a) -> int:
    conf = load_params()
    kcfg = conf.get("knowledge", {})
    ttl_days = int(kcfg.get("ttl_days", 90))
    rows = parse_rows()
    pool = conf.get("clauses", []) if isinstance(conf.get("clauses"), list) else []
    by_status: dict[str, int] = {}
    stale, anchor_bad = [], []
    for r in rows:
        by_status[r["status"]] = by_status.get(r["status"], 0) + 1
        if ttl_hint(r, ttl_days):
            stale.append(r["id"])
        if r["anchor"] and r["anchor"] not in pool:
            anchor_bad.append(f"{r['id']}:{r['anchor']}")
    print(f"[kb] 条目 {len(rows)} 条｜状态 {by_status}")
    print(f"[kb] TTL 超期（>{ttl_days} 天未改）: {stale or '无'}（只标注，不阻断）")
    print(f"[kb] 锚点不在池: {anchor_bad or '无'}")
    print("[kb] 恒例：复现未达门不晋升；零消费走 probe.retire-check，不静默删")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="阶段进门清单（索引行）")
    ap.add_argument("--stage", default="", help="阶段号，如 02b/03/04")
    ap.add_argument("--tags", nargs="*", default=[], help="追加标签过滤")
    ap.add_argument("--limit", type=int, default=0, help="覆盖 params 的条数上限")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--stats", action="store_true", help="知识库体检")
    a = ap.parse_args()
    if a.stats:
        return cmd_stats(a)
    if not a.stage:
        ap.error("需要 --stage 或 --stats")
    return cmd_list(a)


if __name__ == "__main__":
    sys.exit(main())
