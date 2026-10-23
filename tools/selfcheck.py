#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""团队自检：把「聚鲸工作流」里靠自觉的三件事变成可复算的机检。

判据（任一 FAIL → 非零退出）：
  S1 子角色权限闭环：agents/*.md 的 frontmatter 必须显式声明 tools: []
     （缺该键 = 继承父 agent 全量工具，SOUL 的「第一层硬限制」失效）
  S2 单一事实源：SOUL.md 磁盘正文 == Octop 库 agents.system_prompt
     （不一致 = 改了磁盘没同步 DB，或反之）
  S3 门禁脚本在位：team/gates.md 里 `python <path>` 引用的脚本必须真实存在
  S4 回归门已挂：gates.md 的交付前机检块必须出现 `--regression`
     （注意：这是文档一致性断言，不是行为证明；行为证明靠 S5 实跑）
  S5 冒烟可跑：validate_structure.py 与 validate_report.py 实跑退出码为 0
  S6 信号 WAL 未撞名：metrics/ 下 signal-wal.jsonl（教训信号）与
     traffic.jsonl（GitHub 流量计数）必须分离，signal-wal.jsonl 不得是流量数据

用法：
  python tools/selfcheck.py            # 人读
  python tools/selfcheck.py --json     # 机读
  python tools/selfcheck.py --skip-run # 跳过 S5 实跑（快速看配置面）

环境：
  OCTOP_HOME  指向 .octop 目录本身；未设则回落 <repo>/.octop
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent          # bot-team
REPO = ROOT.parent.parent                              # bot-2
OCTOP = Path(os.environ.get("OCTOP_HOME") or (REPO / ".octop"))
EXPERT = OCTOP / "expert_market" / "manufacturing-business-team"
SOUL = EXPERT / "SOUL.md"
AGENTS_DIR = EXPERT / "agents"
DB = OCTOP / "octop.db"
GATES = ROOT / "team" / "gates.md"
SIGNAL_WAL = REPO / "JuJing" / "metrics" / "signal-wal.jsonl"
TRAFFIC = REPO / "JuJing" / "metrics" / "traffic.jsonl"

RESULTS: list[dict] = []


def record(sid: str, ok: bool, detail: str, skipped: bool = False) -> None:
    RESULTS.append({"id": sid, "ok": ok, "detail": detail, "skipped": skipped})


def parse_frontmatter(text: str) -> dict:
    """只解析首尾 --- 之间的顶层键；够用于本自检，不引入额外依赖。"""
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end == -1:
        return {}
    block = text[3:end]
    out: dict = {}
    for line in block.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line[0] in " \t":           # 嵌套行交给下面的值判断处理
            continue
        m = re.match(r"^([A-Za-z_][\w-]*)\s*:\s*(.*)$", line)
        if not m:
            continue
        key, val = m.group(1), m.group(2).strip()
        out[key] = val
    return out


def check_s1() -> None:
    if not AGENTS_DIR.is_dir():
        record("S1", False, f"目录不存在：{AGENTS_DIR}")
        return
    files = sorted(p for p in AGENTS_DIR.glob("*.md"))
    if not files:
        record("S1", False, "agents/ 下没有子角色定义")
        return
    bad = []
    for p in files:
        fm = parse_frontmatter(p.read_text(encoding="utf-8"))
        val = fm.get("tools", "<missing>")
        if val.replace(" ", "") != "[]":
            bad.append(f"{p.name}: tools={val}")
    if bad:
        record("S1", False, "子角色未显式禁用工具 → " + "; ".join(bad))
    else:
        record("S1", True, f"{len(files)} 个子角色均显式 tools: []")


def check_s2() -> None:
    if not SOUL.is_file():
        record("S2", False, f"SOUL.md 不存在：{SOUL}")
        return
    if not DB.is_file():
        record("S2", False, f"数据库不存在：{DB}", skipped=True)
        return
    disk = SOUL.read_text(encoding="utf-8")
    try:
        con = sqlite3.connect(str(DB))
        row = con.execute(
            "select system_prompt from agents where agent_id='RC98TE'"
        ).fetchone()
        con.close()
    except sqlite3.Error as exc:
        record("S2", False, f"读库失败：{exc}")
        return
    if row is None:
        record("S2", False, "库内找不到 agent_id=RC98TE")
        return
    dbtext = row[0] or ""
    if disk.strip() == dbtext.strip():
        record("S2", True, f"磁盘 == DB（{len(disk)} 字符）")
    else:
        record(
            "S2",
            False,
            f"磁盘 {len(disk)} 字符 ≠ DB {len(dbtext)} 字符；改了磁盘必须 PATCH 同步再重启",
        )


def _gate_script_refs() -> list[str]:
    text = GATES.read_text(encoding="utf-8") if GATES.is_file() else ""
    refs = re.findall(r"python\s+([\w./-]+\.py)", text)
    return sorted(set(refs))


def check_s3() -> None:
    if not GATES.is_file():
        record("S3", False, f"gates.md 不存在：{GATES}")
        return
    refs = _gate_script_refs()
    if not refs:
        record("S3", False, "gates.md 未引用任何机检脚本（门=口号）")
        return
    missing = [r for r in refs if not (ROOT / r).is_file()]
    if missing:
        record("S3", False, "门引用了不存在的脚本 → " + ", ".join(missing))
    else:
        record("S3", True, f"{len(refs)} 个门脚本在位")


def check_s4() -> None:
    if not GATES.is_file():
        record("S4", False, "gates.md 不存在")
        return
    text = GATES.read_text(encoding="utf-8")
    if "--regression" in text:
        record("S4", True, "交付前机检块已挂 --regression")
    else:
        record(
            "S4",
            False,
            "回归套件未挂到交付门（只有 structure/report 两条）；文档一致性断言，非行为证明",
        )


def check_s5(run: bool) -> None:
    if not run:
        record("S5", True, "已按 --skip-run 跳过实跑", skipped=True)
        return
    for script, extra in (
        ("tests/validate_structure.py", []),
        ("tests/validate_report.py", ["runs/demo-report-2026-10-19.json"]),
    ):
        if not (ROOT / script).is_file():
            record("S5", False, f"缺少 {script}")
            return
        proc = subprocess.run(
            [sys.executable, "-B", script, *extra],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=300,
        )
        if proc.returncode != 0:
            tail = (proc.stdout or "").strip().splitlines()[-3:]
            record("S5", False, f"{script} 退出码 {proc.returncode}：" + " | ".join(tail))
            return
    record("S5", True, "validate_structure / validate_report 实跑退出码 0")


def check_s6() -> None:
    if not SIGNAL_WAL.is_file():
        record("S6", False, f"信号 WAL 不存在：{SIGNAL_WAL}")
        return
    if not TRAFFIC.is_file():
        record("S6", False, f"流量计数文件不存在（改名未落地）：{TRAFFIC}")
        return
    head = SIGNAL_WAL.read_text(encoding="utf-8", errors="replace")[:2000]
    if '"type":"views"' in head or '"type": "views"' in head:
        record(
            "S6",
            False,
            "signal-wal.jsonl 里混入 GitHub 流量计数（views/clones）——信号与虚荣指标又撞了",
        )
        return
    record("S6", True, "signal-wal.jsonl（信号）与 traffic.jsonl（流量）已分离")


def main() -> int:
    run = "--skip-run" not in sys.argv
    as_json = "--json" in sys.argv
    check_s1()
    check_s2()
    check_s3()
    check_s4()
    check_s5(run)
    check_s6()

    failed = [r for r in RESULTS if not r["ok"]]
    if as_json:
        print(json.dumps({"ok": not failed, "results": RESULTS}, ensure_ascii=False, indent=2))
    else:
        for r in RESULTS:
            tag = "SKIP" if r["skipped"] and r["ok"] else ("OK  " if r["ok"] else "FAIL")
            print(f"[{tag}] {r['id']}  {r['detail']}")
        print(f"\nRESULT: {'GREEN' if not failed else 'RED'}  ({len(RESULTS) - len(failed)}/{len(RESULTS)} 通过)")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
