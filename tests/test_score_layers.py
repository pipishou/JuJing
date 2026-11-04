#!/usr/bin/env python3
"""评分分层 + 抽样校准的自检（离线）。"""
import json
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from core import report_contract as rc  # noqa: E402
import consistency_sample as CS        # noqa: E402

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name} {detail}")


def full_notes(overrides=None):
    notes = {}
    for dim in rc.SIX_DIMS:
        layer = rc.LAYER_DEFAULT[dim]["layer"]
        if layer == "judgment":
            notes[dim] = {"rationale": "因为统计口径与清单一致，且已排除换人、换班两个共因"}
        elif layer == "measured":
            notes[dim] = {"evidence_refs": ["runtime/workitems/current.json#summary"]}
        else:
            notes[dim] = {"sample_ref": "consistency.json#1", "score": 3}
    if overrides:
        notes.update(overrides)
    return notes


def rep_with(notes, **kw):
    doc = {"card_kind": "attribution", "is_demo": False, "task_id": "T-1",
           "scorecard": {d: 3 for d in rc.SIX_DIMS}, **kw}
    if notes is not None:
        doc["scorecard"]["dimension_notes"] = notes
    return doc


def tmpdir(name):
    d = Path("D:/tmp") / f"sl_{name}"
    if d.exists():
        shutil.rmtree(d)
    d.mkdir(parents=True)
    return d


def main():
    check("pack 有分层声明", len(rc.scorecard_layers({"scorecard_layers": rc.LAYER_DEFAULT})) == 6)
    check("全齐 → 通过", rc.layer_errors(rep_with(full_notes())) == [], str(rc.layer_errors(rep_with(full_notes()))))
    check("缺 dimension_notes → 红", "dimension_notes" in rc.layer_errors(rep_with(None))[0])

    bad = full_notes({"significance": {"rationale": "看着像"}})
    errs = rc.layer_errors(rep_with(bad))
    check("判断层理由过短 → 红", any("significance" in e and "rationale" in e for e in errs), str(errs))

    bad2 = full_notes({"timeliness": {"duration": "3天"}})
    errs2 = rc.layer_errors(rep_with(bad2))
    check("测量层缺 evidence_refs → 红", any("timeliness" in e and "evidence_refs" in e for e in errs2), str(errs2))

    bad3 = full_notes({"human_ai_consistency": {"sample_ref": ""}})
    doc3 = rep_with(bad3)
    doc3["scorecard"]["human_ai_consistency"] = 3
    errs3 = rc.layer_errors(doc3)
    check("抽样层未抽样且分非 0 → 红", any("human_ai_consistency" in e for e in errs3), str(errs3))

    unsampled = full_notes({"human_ai_consistency": {"sample_ref": "", "score": 0}})
    doc4 = rep_with(unsampled)
    doc4["scorecard"]["human_ai_consistency"] = 0
    check("未抽样且 score=0 → 通过", rc.layer_errors(doc4) == [], str(rc.layer_errors(doc4)))

    check("周卡不校验六维", rc.layer_errors({"card_kind": "weekly", "scorecard": {}}) == [])

    # ---- 抽样校准 ----
    base = tmpdir("consistency")
    runs = base / "runs"
    runs.mkdir()
    tdir = runs / "T-20261104-001"
    tdir.mkdir()
    (tdir / "report.json").write_text(json.dumps(
        {"card_kind": "attribution", "is_demo": False, "goal": "g",
         "scorecard": {d: 3 for d in rc.SIX_DIMS}}, ensure_ascii=False), encoding="utf-8")

    cands = CS.candidates(runs)
    check("候选可列", len(cands) == 1 and cands[0]["task_id"] == "T-20261104-001", str(cands))
    prop = CS.propose("T-20261104-001", runs)
    check("propose 给出六维与提问", len(prop["dims"]) == 6 and prop["ask"])

    out = CS.record("T-20261104-001", [3, 3, 3, 3, 3, 3], runs=runs, events=base / "ev.jsonl")
    check("全一致 → 偏差 0", out["mean_abs_deviation"] == 0 and out["flagged"] is False, str(out))
    check("旁证落盘", (tdir / "consistency.json").is_file())
    doc = json.loads((tdir / "consistency.json").read_text(encoding="utf-8"))
    check("history 累加", doc["count"] == 1 and doc["history"][0]["scores"] == [3, 3, 3, 3, 3, 3])

    out2 = CS.record("T-20261104-001", [5, 5, 0, 0, 5, 0], runs=runs, events=base / "ev.jsonl")
    check("大偏差 → flagged", out2["flagged"] is True and out2["mean_abs_deviation"] > 1.5, str(out2))

    st = CS.status(runs)
    check("status 汇总", st["sampled"] == 1 and st["flagged"] == 1 and st["candidates"] == 1, str(st))

    try:
        CS.record("T-20261104-001", [1, 2], runs=runs, events=base / "ev.jsonl")
        check("分数个数不对 → 报错", False)
    except CS.SampleError:
        check("分数个数不对 → 报错", True)
    try:
        CS.record("T-20261104-001", [1, 2, 3, 4, 5, 9], runs=runs, events=base / "ev.jsonl")
        check("越界分数 → 报错", False)
    except CS.SampleError:
        check("越界分数 → 报错", True)
    try:
        CS.propose("T-999", runs)
        check("不存在任务 → 报错", False)
    except CS.SampleError:
        check("不存在任务 → 报错", True)

    check("demo 卡不进候选", CS.candidates(tmpdir("empty")) == [])

    print(f"\nRESULT: {'PASS' if FAIL == 0 else 'FAIL'}（PASS {PASS} / FAIL {FAIL}）")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
