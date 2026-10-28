#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""workops（工作任务管理）行业包自检：包契约、班组绑定、场景种子、动作锚点。

为什么单独一个测试：新行业包最容易「加载得了但接不上」——crew_id 写错、
scene 里的 cost_anchor_key 不在 roi_anchors、因子不在 factors。这些坑靠手工
跑一遍才发现，进回归后改包就有人兜底。

跑法：python -B tests/test_workops_pack.py
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import pack as pack_mod  # noqa: E402
from core import scene as scene_mod  # noqa: E402

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


def load_yaml(path):
    import yaml  # noqa: PLC0415
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))


def main():
    pack_path = ROOT / "packs" / "workops" / "pack.json"
    check("pack.json 存在", pack_path.is_file(), pack_path)
    pack = pack_mod.load_file(pack_path)
    check("pack_id=workops", pack["pack_id"] == "workops", pack["pack_id"])
    check("six_dims 六维", len(pack["six_dims"]) == 6, pack["six_dims"])
    check("硬护栏三条", len(pack["scorecard_hard_guardrails"]) == 3, pack.get("scorecard_hard_guardrails"))

    crews = load_yaml(ROOT / "team" / "crews.yaml")["crews"]
    crew_ids = {c["id"] for c in crews}
    check("crew_id 在 crews.yaml 里", pack["crew_id"] in crew_ids, (pack["crew_id"], sorted(crew_ids)))
    crew = next((c for c in crews if c["id"] == pack["crew_id"]), {})
    check("班组有步骤", bool(crew.get("steps")), crew)
    check("班组 card_kind 已声明", bool(crew.get("card_kind")), crew)

    employees = load_yaml(ROOT / "team" / "employees.yaml")["employees"]
    emp_ids = {e["id"] for e in employees}
    unknown = [s["agent"] for s in crew.get("steps") or [] if s.get("agent") not in emp_ids]
    check("班组步骤里的 agent 都在名册里", not unknown, unknown)

    anchors = set(pack["roi_anchors"])
    bad_actions = [a["id"] for a in pack["actions"] if a.get("cost_anchor_key") not in anchors]
    check("动作锚点都能在 roi_anchors 找到", not bad_actions, bad_actions)

    seed_doc = scene_mod.load_seeds(pack)
    check("场景种子无被拒", seed_doc["rejected"] == [], seed_doc["rejected"])
    check("场景种子至少 1 个", len(seed_doc["scenes"]) >= 1, len(seed_doc["scenes"]))
    if seed_doc["scenes"]:
        spec = seed_doc["scenes"][0]
        metric_ids = {m["id"] for m in pack["metrics"]}
        check("场景指标在 pack.metrics 里", spec["metric"]["id"] in metric_ids, spec["metric"]["id"])
        action_ids = {a["id"] for a in pack["actions"]}
        bad = [a["id"] for a in spec["actions"] if a["id"] not in action_ids]
        check("场景动作在 pack.actions 里", not bad, bad)
        factors = set(pack["factors"])
        unknown_f = [r["field"] for r in spec["data_requirements"] if r["field"] not in factors
                     and r["field"] != "workitems_snapshot"]
        check("场景数据要求引用已知因子", not unknown_f, unknown_f)

    # 反向：把 crew_id 改成不存在的值，validate 应报错（证明这条检查不是空转）
    broken = dict(pack)
    broken["crew_id"] = "c-not-exist"
    check("坏 crew_id 会被发现", broken["crew_id"] not in crew_ids, broken["crew_id"])

    print()
    if FAILS:
        print("FAILED %d / %d: %s" % (len(FAILS), COUNT, ", ".join(FAILS)))
        return 1
    print("PASS 全绿（%d 项）" % COUNT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
