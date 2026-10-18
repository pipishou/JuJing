#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从 team/*.yaml 生成 UI 数据（单一事实源 → ui/team-data.js）。

用法：python tools/gen_ui_data.py
输出：ui/team-data.js（window.TEAM_DATA），index.html 优先使用它；缺失时回退内联假数据。
"""
import json
import pathlib
import sys

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
UI = ROOT / "ui"


def load(rel):
    return yaml.safe_load((ROOT / rel).read_text(encoding="utf-8"))


def main():
    shelf = load("team/shelf.yaml")
    employees_doc = load("team/employees.yaml")
    crews_doc = load("team/crews.yaml")

    states = shelf.get("states") or {}
    cats = {c["key"]: c["name"] for c in (shelf.get("categories") or [])}

    out = []
    for e in employees_doc.get("employees") or []:
        st = e.get("state", "active")
        label = (states.get(st) or {}).get("label", "在岗")
        out.append({
            "id": e["id"],
            "name": e["name"],
            "role": f"{e.get('layer', '-')} · {cats.get(e.get('category'), '')}".strip(" ·"),
            "layer": e.get("layer", "-"),
            "cat": e.get("category", "deliver"),
            "state": "" if st == "active" else ("res" if st == "resident" else st),
            "stateText": label,
            "init": e["id"],
            "tags": (e.get("skills") or [])[:3],
            "sample": e.get("sample", ""),
            "summary": e.get("summary", ""),
            "hireable": st in ("active", "craft"),
        })
    for c in employees_doc.get("coming") or []:
        out.append({
            "id": c["id"], "name": c["name"], "role": "coming · 待上架",
            "layer": "-", "cat": "coming", "state": "coming", "stateText": "待上架",
            "init": c["id"], "tags": ["能力包缺失"], "sample": "能力包未写",
            "summary": c.get("reason", "尚未就绪，不上架"), "hireable": False,
        })

    crews = []
    for c in crews_doc.get("crews") or []:
        crews.append({
            "id": c["id"], "name": c["name"], "purpose": c.get("purpose", ""),
            "steps": [{"agent": s.get("agent"), "what": s.get("what")} for s in (c.get("steps") or [])],
            "followup_days": ((c.get("end") or {}).get("followup") or {}).get("days"),
        })

    data = {"generated_at": "2026-10-18", "source": "team/employees.yaml+crews.yaml+shelf.yaml",
            "employees": out, "crews": crews}

    # UI 绑定用示例上下文（Goal/Memory/Cost）；真实任务由任务目录数据替代
    try:
        goal = json.loads((ROOT / "team/goals/example-goal.json").read_text(encoding="utf-8"))
        mem = json.loads((ROOT / "team/memory/example-memory.json").read_text(encoding="utf-8"))
        cost = json.loads((ROOT / "team/cost/example-cost.json").read_text(encoding="utf-8"))
        memories = {}
        for m in mem:
            memories.setdefault(m.get("employee_id", "host"), []).append(m.get("text", ""))
        data["context"] = {"goal": goal, "memories": memories, "cost": cost, "source": "examples"}
    except Exception as exc:  # noqa: BLE001
        print("context skipped:", exc)

    js = "window.TEAM_DATA = " + json.dumps(data, ensure_ascii=False, indent=2) + ";\n"
    (UI / "team-data.js").write_text(js, encoding="utf-8")
    print(f"wrote {UI / 'team-data.js'} employees={len(out)} crews={len(crews)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
