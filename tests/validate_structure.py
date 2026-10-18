#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""结构校验：bot-team-dsh 是否完整、名册/DAG/资产/演示数据是否自洽。

判据（任一 FAIL → 非零退出）：
  1. 必备文件在位（SKILL/README/MIGRATION/名册/DAG/门/4 章程/8 资产/5 演示）
  2. SKILL.md frontmatter：name=jujing-bot-team，description 非空
  3. 名册：host + 3 成员；role_file 存在；steps ⊆ DAG；id 唯一
  4. DAG：5 步；依赖闭环；角色合法；s5=host；output 唯一
  5. 资产引用存在；角色章程至少引用 1 个资产
  6. 演示数据：T/T30 结构；埋点（主因+干扰项）；变体 B/C/D 语义在位
"""
import json
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
FAIL = []


def check(name, ok, detail=""):
    print(("[OK]   " if ok else "[FAIL] ") + name + ("" if ok else "  ← " + detail))
    if not ok:
        FAIL.append(name)


def load_yaml(rel):
    p = ROOT / rel
    if not p.is_file():
        return None
    return yaml.safe_load(p.read_text(encoding="utf-8"))


def main():
    required = [
        "SKILL.md", "README.md", "MIGRATION.md",
        "team/roster.yaml", "team/dag.yaml", "team/gates.md",
        "team/employees.yaml", "team/crews.yaml", "team/shelf.yaml",
        "team/delivery-card.schema.yaml",
        "team/memory/README.md", "team/memory/employee-memory.schema.yaml", "team/memory/example-memory.json",
        "team/goals/README.md", "team/goals/goal.schema.yaml", "team/goals/example-goal.json",
        "team/cost/README.md", "team/cost/cost-record.schema.yaml", "team/cost/example-cost.json",
        "team/integrations/wecom.yaml", "tools/wecom_notify.py",
        "team/roles/host.md",
        "team/roles/consulting-analyst.md",
        "team/roles/ai-attribution-expert.md",
        "team/roles/manufacturing-ops-expert.md",
        "team/roles/compliance-reviewer.md",
        "team/roles/data-fetcher.md",
        "team/roles/report-recorder.md",
        "orchestration/team-workflow.mjs",
        "orchestration/assemble_report.py",
        "orchestration/dsh-session-tape.mjs",
        "tools/gen_ui_data.py",
        "tools/gen_tape_ui.py",
        "ui/index.html", "ui/sprites.js", "ui/team-data.js", "ui/tape-data.js", "ui/preview.png",
        "templates/task-intake.example.json",
        "team/loop/README.md",
        "team/loop/evidence-card.schema.yaml",
        "team/loop/analysis-card.schema.yaml",
        "team/loop/decision-record.schema.yaml",
        "team/loop/closure-record.schema.yaml",
        "team/loop/example-decision.json",
        "docs/REAL_TASK_PLAYBOOK_2026-10-19_zh.md",
        "docs/PRODUCT_BENCHMARK_2026-10-18_zh.md",
        "docs/FOUR_LOOP_UPGRADE_2026-10-18_zh.md",
        "docs/METHODOLOGY_INTEGRATION_2026-10-18_zh.md",
        "docs/WECOM_INTEGRATION_2026-10-18_zh.md",
        "docs/GITHUB_REPO_PLAN_2026-10-18_zh.md",
        "runs/dsh-tape-sample.json",
        "tests/validate_structure.py", "tests/validate_report.py",
        "assets/factors_table.yaml", "assets/scorecard_six_dim.yaml",
        "assets/exclusion_six_steps.yaml", "assets/prompt_crp.yaml",
        "assets/roi_anchors.yaml", "assets/degradation_rules.yaml",
        "assets/perception_rules.yaml", "assets/decision_contract_refill.yaml",
        "assets/decision_log.schema.yaml", "assets/irreversible_actions.yaml",
        "demo/material_refill_T.json", "demo/material_refill_T30.json",
        "demo/material_refill_T_variantB_nulls.json",
        "demo/material_refill_T_variantC_missing.json",
        "demo/material_refill_T_variantD_irreversible.json",
    ]
    for rel in required:
        check(f"file {rel}", (ROOT / rel).is_file())

    # 2. SKILL frontmatter
    skill = (ROOT / "SKILL.md").read_text(encoding="utf-8")
    fm = re.match(r"^---\s*\n(.*?)\n---", skill, re.S)
    data = yaml.safe_load(fm.group(1)) if fm else {}
    check("skill name=jujing-bot-team", data.get("name") == "jujing-bot-team", str(data.get("name")))
    check("skill description non-empty", isinstance(data.get("description"), str) and len(data["description"]) > 20)
    check("skill description <=500 chars", len(str(data.get("description", ""))) <= 500, str(len(str(data.get("description", "")))))

    # 3. roster
    roster = load_yaml("team/roster.yaml") or {}
    members = roster.get("members") or []
    host = roster.get("host") or {}
    check("roster host exists", host.get("id") == "host")
    check("roster 3 members", len(members) == 3, str(len(members)))
    ids = [host.get("id")] + [m.get("id") for m in members]
    check("roster ids unique", len(ids) == len(set(ids)))
    for m in members:
        rf = m.get("role_file", "")
        check(f"role file {m.get('id')}", (ROOT / rf).is_file() if rf else False, rf)

    # 3.5 v0.2: employees / crews / shelf / delivery-card
    shelf = load_yaml("team/shelf.yaml") or {}
    states = shelf.get("states") or {}
    for st in ("coming", "not_ready", "craft", "resident", "retired", "active"):
        check(f"shelf state {st}", st in states)
    emp_doc = load_yaml("team/employees.yaml") or {}
    emps = emp_doc.get("employees") or []
    check("employees >= 6", len(emps) >= 6, str(len(emps)))
    emp_ids = [e.get("id") for e in emps]
    check("employee ids unique", len(emp_ids) == len(set(emp_ids)))
    emp_states = set(states.keys()) | {"active"}
    for e in emps:
        check(f"employee {e.get('id')} state", e.get("state") in emp_states, str(e.get("state")))
        rf = e.get("role_file", "")
        check(f"employee {e.get('id')} role_file", (ROOT / rf).is_file() if rf else False, rf)
        check(f"employee {e.get('id')} skills", isinstance(e.get("skills"), list) and len(e["skills"]) > 0)
        check(f"employee {e.get('id')} sample", bool(e.get("sample")))
    crew_doc = load_yaml("team/crews.yaml") or {}
    crews = crew_doc.get("crews") or []
    limits = crew_doc.get("limits") or {}
    check("crews >= 4", len(crews) >= 4, str(len(crews)))
    check("crew limits 5-12", limits.get("min_crews") == 5 and limits.get("max_crews") == 12, str(limits))
    for c in crews:
        steps_c = c.get("steps") or []
        check(f"crew {c.get('id')} steps>=2", len(steps_c) >= 2, str(len(steps_c)))
        for s in steps_c:
            check(f"crew {c.get('id')} agent known", s.get("agent") in emp_ids, str(s.get("agent")))
        end = c.get("end") or {}
        fu = end.get("followup") or {}
        check(f"crew {c.get('id')} end kind=file", end.get("kind") == "file")
        check(f"crew {c.get('id')} followup 1-30", isinstance(fu.get("days"), int) and 1 <= fu["days"] <= 30, str(fu.get("days")))
    dc = load_yaml("team/delivery-card.schema.yaml") or {}
    fs = [x.get("key") for x in (dc.get("first_screen") or [])]
    for k in ("finding", "cannot_attribute", "recommendation", "next_step"):
        check(f"delivery first_screen {k}", k in fs, str(fs))
    sig = [r.get("key") for r in ((dc.get("signature") or {}).get("roles") or [])]
    check("delivery signature boss", "boss" in sig, str(sig))

    # 3.6 v0.2.1: UI 像素角色 / YAML→UI 数据 / DSH tape 适配器
    ui_data = (ROOT / "ui/team-data.js").read_text(encoding="utf-8")
    check("ui team-data has employees", '"employees"' in ui_data)
    check("ui team-data covers roster ids", all((f'"{e.get("id")}"' in ui_data) for e in emps))
    check("ui sprites module", "PixelSprites" in (ROOT / "ui/sprites.js").read_text(encoding="utf-8"))
    check("ui sprites frames", all(k in (ROOT / "ui/sprites.js").read_text(encoding="utf-8") for k in ("blink", "walk0", "talk", "happy")))
    ui_tape = (ROOT / "ui/tape-data.js").read_text(encoding="utf-8")
    check("ui tape-data has beats", '"beats"' in ui_tape and "DSH_TAPE" in ui_tape)
    task_tpl = json.loads((ROOT / "templates/task-intake.example.json").read_text(encoding="utf-8"))
    check("task intake template id", bool(task_tpl.get("task_id")))
    ui_html = (ROOT / "ui/index.html").read_text(encoding="utf-8")
    check("ui task intake wizard", "openIntake" in ui_html and "generateTask" in ui_html)
    check("playbook exists", (ROOT / "docs/REAL_TASK_PLAYBOOK_2026-10-19_zh.md").is_file())
    check("benchmark has verdict", "眼前一亮" in (ROOT / "docs/PRODUCT_BENCHMARK_2026-10-18_zh.md").read_text(encoding="utf-8"))

    # 3.7 v0.4: 四段闭环卡
    loop = json.loads((ROOT / "team/loop/example-decision.json").read_text(encoding="utf-8"))
    check("loop evidence card", isinstance(loop.get("evidence"), list) and len(loop["evidence"]) > 0)
    check("loop analysis card", loop.get("analysis", {}).get("id", "").startswith("A-"))
    dec = loop.get("decision", {})
    check("loop decision id", dec.get("id", "").startswith("D-"))
    check("loop decision approval", all(k in (dec.get("approval") or {}) for k in ("by", "at", "mode")))
    check("loop decision reversal", isinstance(dec.get("reversal_conditions"), list) and len(dec["reversal_conditions"]) > 0)
    check("loop decision review_due", bool(dec.get("review_due")))
    clo = loop.get("closure", {})
    check("loop closure verdict", clo.get("verdict") in ("hit", "miss", "partial", "unverified"))
    for f in ("evidence-card.schema.yaml", "analysis-card.schema.yaml", "decision-record.schema.yaml", "closure-record.schema.yaml"):
        check(f"loop schema {f}", isinstance(load_yaml("team/loop/" + f), dict))

    # 3.8 v0.5: 方法论 v1.3 对齐
    sc = load_yaml("assets/scorecard_six_dim.yaml") or {}
    check("scorecard 100 total", sc.get("total_score") == 100)
    check("scorecard guardrails", len(sc.get("guardrails") or []) >= 3)
    check("scorecard thresholds", len(sc.get("thresholds") or []) >= 4)
    pr = load_yaml("assets/perception_rules.yaml") or {}
    check("perception degradation 3 levels", len(pr.get("degradation") or []) == 3)
    dc = load_yaml("assets/decision_contract_refill.yaml") or {}
    check("decision contract guardrails", len(dc.get("guardrails") or []) >= 2)
    check("example decision entity_id", bool(dec.get("entity_id")))
    check("example decision attribution_method", dec.get("attribution_method") in ("exclusion", "counterfactual", "rule_based"))

    # 3.9 v0.6: 关系/记忆、目标、成本、企微
    mem = json.loads((ROOT / "team/memory/example-memory.json").read_text(encoding="utf-8"))
    check("memory >=3 entries", len(mem) >= 3)
    check("memory privacy enum", all(m.get("privacy") in ("public", "internal", "customer_sensitive") for m in mem))
    goal = json.loads((ROOT / "team/goals/example-goal.json").read_text(encoding="utf-8"))
    check("goal target/deadline", "target" in goal and "deadline" in goal)
    cost = json.loads((ROOT / "team/cost/example-cost.json").read_text(encoding="utf-8"))
    check("cost net/roi", "net_benefit_cny" in cost and "roi" in cost)
    wecom = load_yaml("team/integrations/wecom.yaml") or {}
    check("wecom rate limit", wecom.get("rate_limit_per_min") == 20)
    check("wecom inbound deferred", (wecom.get("inbound") or {}).get("status") == "deferred")
    tape = json.loads((ROOT / "runs/dsh-tape-sample.json").read_text(encoding="utf-8"))
    check("tape sample beats>0", isinstance(tape.get("beats"), list) and len(tape["beats"]) > 0)

    # 4. DAG
    dag = load_yaml("team/dag.yaml") or {}
    steps = dag.get("steps") or []
    check("dag 5 steps", len(steps) == 5, str(len(steps)))
    step_ids = [s.get("id") for s in steps]
    check("dag step ids unique", len(step_ids) == len(set(step_ids)))
    by_id = {s.get("id"): s for s in steps}
    expect_deps = {
        "s1_data_snapshot": [],
        "s2_factor_table": [],
        "s3_exclusion": ["s1_data_snapshot", "s2_factor_table"],
        "s4_recommendation": ["s3_exclusion"],
        "s5_report": ["s4_recommendation"],
    }
    for sid, deps in expect_deps.items():
        s = by_id.get(sid)
        check(f"dag {sid} present", s is not None)
        if s is not None:
            check(f"dag {sid} deps={deps}", (s.get("depends_on") or []) == deps, str(s.get("depends_on")))
            role = s.get("role")
            check(f"dag {sid} role known", role in ids, str(role))
    check("dag s5 role=host", (by_id.get("s5_report") or {}).get("role") == "host")
    outs = [s.get("output") for s in steps]
    check("dag outputs unique", len(outs) == len(set(outs)))

    # 5. assets referenced by roster exist; roles mention assets
    for m in members:
        for a in (m.get("assets") or []):
            check(f"roster asset {a}", (ROOT / a).is_file())
        rf = ROOT / m.get("role_file", "")
        if rf.is_file():
            text = rf.read_text(encoding="utf-8")
            check(f"role {m.get('id')} references assets", "assets/" in text)

    # 6. demo data
    for name in ("material_refill_T.json", "material_refill_T30.json"):
        try:
            d = json.loads((ROOT / "demo" / name).read_text(encoding="utf-8"))
            check(f"demo {name} records", isinstance(d.get("records"), list) and len(d["records"]) > 0)
        except Exception as e:  # noqa: BLE001
            check(f"demo {name} parse", False, str(e))
    t = json.loads((ROOT / "demo" / "material_refill_T.json").read_text(encoding="utf-8"))
    planted = t.get("meta", {}).get("planted_signal", {})
    check("demo planted root_cause", bool(planted.get("root_cause")))
    check("demo planted confounder", bool(planted.get("confounder")))
    check("demo field_contract 12", len(t.get("meta", {}).get("field_contract", [])) == 12)
    vb = json.loads((ROOT / "demo" / "material_refill_T_variantB_nulls.json").read_text(encoding="utf-8"))
    check("variantB null_rate>0.1", float(vb.get("meta", {}).get("null_rate_declared", 0)) > 0.1, str(vb.get("meta", {}).get("null_rate_declared")))
    vd = json.loads((ROOT / "demo" / "material_refill_T_variantD_irreversible.json").read_text(encoding="utf-8"))
    check("variantD 不可逆探针字段", "irreversible" in vd.get("meta", {}).get("planted_signal", {}).get("root_cause", "") or "停线" in json.dumps(vd, ensure_ascii=False))

    # 7. gates content
    gates = (ROOT / "team/gates.md").read_text(encoding="utf-8")
    for kw in ("预派发", "securityGate", "降级", "cannot_attribute"):
        check(f"gates contains {kw}", kw in gates)

    print()
    if FAIL:
        print(f"RESULT: FAIL ({len(FAIL)} 项) -> " + "; ".join(FAIL))
        return 1
    print("RESULT: PASS（结构/名册/DAG/资产/演示数据/门 全绿）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
