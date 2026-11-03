#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tobs_doctor —— Tobs 配置体检（只读，零依赖）

回答一个具体问题：**我配的跟它实际在用的，是不是一回事？**
只读 `~/.dsh/botharness/botharness.db`（`?mode=ro`）与 `~/.dsh/botharness/*` 文件，
不写任何东西、不调用模型、不需要 Lease。

用法:
    python -B tools/tobs_doctor.py            # 人话报告
    python -B tools/tobs_doctor.py --json     # 机器可读
    python -B tools/tobs_doctor.py --db <path> --home <dsh_home>

退出码: 0 全部通过 / 1 有待办 / 2 环境错误（库不存在等）
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import sqlite3
import sys

OK, WARN, FAIL = "OK", "WARN", "FAIL"


def _home() -> pathlib.Path:
    return pathlib.Path(os.environ.get("DSH_HOME") or (pathlib.Path.home() / ".dsh"))


def _connect(db: pathlib.Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def _table_names(conn: sqlite3.Connection) -> set:
    return {r[0] for r in conn.execute("select name from sqlite_master where type='table'")}


def _model_plan(body) -> dict:
    if isinstance(body, (bytes, bytearray)):
        body = body.decode("utf-8", "replace")
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except Exception:
            return {}
    return body if isinstance(body, dict) else {}


def _parse_plan(plan: dict) -> dict:
    """把 modelPlan 拆成 主模型/任务模型 两行。"""
    out = {"orchestrator": None, "assignment": None, "raw_keys": sorted(plan.keys())}
    for key in ("orchestrator", "assignment", "assignmentDefault"):
        val = plan.get(key)
        if isinstance(val, dict):
            out[key] = {
                "provider": val.get("provider"),
                "model": val.get("model"),
                "effort": val.get("reasoningEffort") or val.get("effort"),
            }
        elif isinstance(val, (int, str)) and key == "assignmentDefault":
            out[key] = {"ref": val}
    return out


def collect(db: pathlib.Path, home: pathlib.Path) -> dict:
    report = {"db": str(db), "home": str(home), "bots": [], "checks": [], "counts": {}}
    conn = _connect(db)
    tables = _table_names(conn)

    counts = {}
    for name in ("persona_bots", "bot_schedules", "bot_schedule_firings",
                 "assignments", "usage_receipts", "inbox_admissions", "workspace_grants"):
        if name in tables:
            counts[name] = conn.execute(f"select count(*) from {name}").fetchone()[0]
    report["counts"] = counts

    sched_by_bot = {}
    if "bot_schedules" in tables:
        cols = {r[1] for r in conn.execute("pragma table_info(bot_schedules)")}
        bot_col = "bot_slug" if "bot_slug" in cols else None
        enabled_col = "enabled" if "enabled" in cols else None
        if bot_col:
            q = f"select {bot_col} as b{', ' + enabled_col if enabled_col else ''} from bot_schedules"
            for row in conn.execute(q):
                entry = sched_by_bot.setdefault(row["b"], {"total": 0, "enabled": 0})
                entry["total"] += 1
                if enabled_col is None or row[enabled_col]:
                    entry["enabled"] += 1

    bots = []
    for row in conn.execute("select slug, body from persona_bots"):
        body = _model_plan(row["body"])
        plan = _parse_plan(body.get("modelPlan") or {})
        slug = row["slug"] or body.get("slug") or ""
        name = body.get("displayName") or body.get("name") or slug[-8:]
        memory_dir = body.get("memoryDir") or ""
        bots.append({
            "id": body.get("id") or slug, "name": name, "slug": slug,
            "memory_dir": memory_dir,
            "plan": plan,
            "schedules": sched_by_bot.get(slug) or {"total": 0, "enabled": 0},
        })
    report["bots"] = bots

    # ---- 检查项 ----
    def add(cid, level, msg, hint=""):
        report["checks"].append({"id": cid, "level": level, "msg": msg, "hint": hint})

    if not bots:
        add("B0", FAIL, "库里没有任何 Bot", "先在面板建 Bot 或跑 tools/dsh_create_bots.sh")

    missing_task = [b["name"] for b in bots if not b["plan"].get("assignment")]
    if missing_task:
        add("B1", FAIL,
            f"{len(missing_task)}/{len(bots)} 个 Bot 没配任务模型（派活会走 fallback）",
            "面板 → Bot → Model → 任务模型 选 opencode-go/deepseek-v4.1-flash/high；或 CLI model-preset-create")
    else:
        add("B1", OK, "全部 Bot 已配任务模型")

    no_plan = [b["name"] for b in bots if not b["plan"].get("orchestrator")]
    if no_plan:
        add("B2", FAIL, f"{len(no_plan)} 个 Bot 连主模型都没配：{', '.join(no_plan[:3])}", "面板 Model → 主模型")

    with_sched = [b for b in bots if b["schedules"]["enabled"] > 0]
    if not with_sched:
        add("B3", FAIL, "没有任何启用的 Schedule（主动提醒为 0）",
            "面板 → Bot → Schedules 新建；或跑 tools/dsh_create_schedules.sh（需先退出 DSH）")
    else:
        add("B3", OK, f"{len(with_sched)} 个 Bot 有启用 Schedule，共 {sum(b['schedules']['enabled'] for b in bots)} 条")

    no_mem = [b["name"] for b in bots if not b["memory_dir"]]
    if no_mem:
        add("B4", WARN, f"{len(no_mem)} 个 Bot 没设记忆目录（记忆不会进 git）", "面板高级设置里指向 bot-memory/<角色>")
    else:
        outside = [b["name"] for b in bots if b["memory_dir"] and "bot-memory" not in b["memory_dir"].replace("\\", "/")]
        if outside:
            add("B4", WARN, f"{len(outside)} 个 Bot 的记忆目录不在 bot-memory 仓内", "统一指到 bot-memory/<角色>，否则跨设备同步不到")
        else:
            add("B4", OK, "记忆目录都在 bot-memory 仓内")

    sold = [b for b in bots if b["memory_dir"] and (pathlib.Path(b["memory_dir"]) / "SOUL.md").exists()]
    if bots and len(sold) < len(bots):
        add("B5", WARN, f"只有 {len(sold)}/{len(bots)} 个 Bot 的记忆目录里能读到 SOUL.md", "记忆目录填错或尚未生成")

    try:
        rows = conn.execute("select count(*) from bot_schedules").fetchone()[0]
        over = [b for b in bots if b["schedules"]["total"] > 20]
        if over:
            add("B6", WARN, "有 Bot 超过 20 条 Schedule 上限", "保留高频的，其余删或暂停")
        _ = rows
    except Exception:
        pass

    # 遥测 / 配置文件
    cfg = home / "botharness" / "config.json"
    tele = None
    if cfg.exists():
        try:
            tele = json.loads(cfg.read_text(encoding="utf-8")).get("telemetry")
        except Exception:
            tele = "unparsable"
    if tele is None:
        add("S1", WARN, "未找到 config.json 的 telemetry 设置（代码默认开）", "要关就在 config.json 里写 telemetry: false")
    elif tele is False:
        add("S1", OK, "遥测已关")
    else:
        add("S1", WARN, f"遥测当前 = {tele}（想看它关没关）", "config.json 写 telemetry: false")

    # 单写者 lease 是否被占
    lock = home / "botharness" / "botharness.writer.json"
    if lock.exists():
        try:
            info = json.loads(lock.read_text(encoding="utf-8"))
            pid = info.get("pid")
            alive = False
            if pid:
                try:
                    os.kill(int(pid), 0)   # 只探活，不发信号
                    alive = True
                except Exception:
                    alive = False
            add("L1", WARN if alive else OK,
                f"writer lease：pid={pid} {'存活（DSH 正在跑，CLI 会拒写）' if alive else '已释放（CLI 可写）'}",
                "" if alive else "现在可以跑建 Bot / 建 Schedule 的脚本")
        except Exception:
            add("L1", WARN, "writer lease 文件存在但读不出内容")
    else:
        add("L1", OK, "没有 writer lease（CLI 可写）")

    return report


def render(rep: dict) -> str:
    lines = []
    lines.append(f"Tobs 体检｜{rep['db']}")
    c = rep["counts"]
    lines.append(f"  Bot {c.get('persona_bots', '?')}｜Schedule {c.get('bot_schedules', '?')}"
                 f"｜派活 {c.get('assignments', '?')}｜用量 {c.get('usage_receipts', '?')}"
                 f"｜工作区授权 {c.get('workspace_grants', '?')}")
    lines.append("")
    lines.append("Bot 清单")
    for b in rep["bots"]:
        p = b["plan"]
        o = p.get("orchestrator") or {}
        a = p.get("assignment")
        mark = "✔" if isinstance(a, dict) else "✘"
        lines.append(f"  · {b['name']:<10} 主={o.get('model', '-'):<20} 任务={mark} "
                     f"schedule={b['schedules']['enabled']} 记忆={'有' if b.get('memory_dir') else '无'}")
    lines.append("")
    lines.append("检查")
    for ck in rep["checks"]:
        lines.append(f"  [{ck['level']:<4}] {ck['id']}  {ck['msg']}")
        if ck["hint"]:
            lines.append(f"          → {ck['hint']}")
    bad = [c for c in rep["checks"] if c["level"] == FAIL]
    warn = [c for c in rep["checks"] if c["level"] == WARN]
    lines.append("")
    lines.append(f"结论：{len(bad)} 项待办，{len(warn)} 项提醒")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Tobs 配置体检（只读）")
    ap.add_argument("--db", default=None, help="botharness.db 路径（默认 $DSH_HOME/botharness/botharness.db）")
    ap.add_argument("--home", default=None, help="DSH_HOME（默认 ~/.dsh）")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    home = pathlib.Path(args.home) if args.home else _home()
    db = pathlib.Path(args.db) if args.db else home / "botharness" / "botharness.db"
    if not db.exists():
        print(f"[FATAL] 找不到 {db}", file=sys.stderr)
        return 2
    try:
        rep = collect(db, home)
    except sqlite3.Error as exc:
        print(f"[FATAL] 读库失败：{exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
    else:
        print(render(rep))
    return 1 if any(c["level"] == FAIL for c in rep["checks"]) else 0


if __name__ == "__main__":
    sys.exit(main())
