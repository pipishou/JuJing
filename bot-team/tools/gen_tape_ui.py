#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 DSH 会话 tape（runs/dsh-tape-sample.json）生成 UI 可加载的 ui/tape-data.js。

用法：python tools/gen_tape_ui.py
规则（第一版，显式可审计）：
  - tool/call 按工具语义映射到角色（读类→分析师，执行类→业务专家，交付类→工作助理）
  - assistant 文本按关键词映射（归因→归因专家，ROI/保养→业务专家，快照→分析师，其余→工作助理）
  - 只取前 120 beats；保留 kind/actor/text/tool，供舞台回放
"""
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC = ROOT / "runs" / "dsh-tape-sample.json"
OUT = ROOT / "ui" / "tape-data.js"

TOOL_ACTOR = {
    "read": "analyst", "glob": "analyst", "grep": "analyst", "web_search": "analyst",
    "web_fetch": "analyst", "session_query": "analyst", "diagnose-windows-sandbox-acl": "analyst",
    "pwsh": "biz", "bash": "biz", "jobs": "biz", "job_output": "biz", "write": "host", "edit": "host",
    "todo_write": "host", "present": "host", "workflow": "host", "spawn_teammate": "host",
    "send_message": "host", "team_task_create": "host", "team_task_update": "host", "skill": "host",
    "ask_user_question": "host", "create_goal": "host", "get_goal": "host", "update_goal": "host",
}


def actor_for_text(text: str) -> str:
    if any(k in text for k in ("归因", "排除", "cannot_attribute", "残差")):
        return "attr"
    if any(k in text for k in ("ROI", "保养", "换模", "停机", "算账", "建议")):
        return "biz"
    if any(k in text for k in ("快照", "补料率", "数据", "字段", "空值")):
        return "analyst"
    return "host"


def main():
    tape = json.loads(SRC.read_text(encoding="utf-8"))
    beats = []
    for b in tape.get("beats", []):
        if b["kind"] == "tool":
            tool = b.get("tool", "?")
            beats.append({
                "kind": "tool", "actor": TOOL_ACTOR.get(tool, "host"), "tool": tool,
                "text": f"工具 {tool}：{b.get('args', '')[:90]}",
            })
        elif b["kind"] == "speak":
            text = b.get("text", "")
            beats.append({"kind": "speak", "actor": actor_for_text(text), "tool": "", "text": text[:220]})
        else:
            beats.append({"kind": "user", "actor": "host", "tool": "", "text": b.get("text", "")[:220]})
    sample = beats[:120]
    data = {"meta": tape.get("meta"), "source": str(SRC.name), "total": len(sample), "beats": sample}
    OUT.write_text("window.DSH_TAPE = " + json.dumps(data, ensure_ascii=False, indent=2) + ";\n", encoding="utf-8")
    print(f"wrote {OUT} beats={len(data['beats'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

