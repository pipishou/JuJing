#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""B18/B19 场景卡引擎自测（纯 stdlib，%TEMP% 沙盒，不依赖 pytest）。

覆盖：
  1. load_seeds：packs/manufacturing/scenes/*.json 三个 preset 种子且全部 validate 通过
  2. 种子纪律：阈值/成本/不可逆动作/动作表一律引用 pack 键（不写死数值常量）
  3. validate 六类拒绝（spec_version / metric.id / 因子封闭世界 / 动作表+不可逆登记 /
     control_line 方法与自造阈值 / 结构&引用）
  4. degradation_reasons：缺必填数据字段只降级、不拒绝
  5. discover（B19）：stdlib statistics 扫 μ±2σ/环比/空值率 → 草稿；同输入 scene_id/关键字段确定
  6. write_task_scene：runs/<task>/scene.json + sha256
运行：venv python -B tests/test_scene.py
"""
import json
import sys
import tempfile
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import pack as pack_mod  # noqa: E402
from core import scene  # noqa: E402

FAILS = []
CHECKS = 0


def check(name, ok, detail=""):
    global CHECKS
    CHECKS += 1
    if not ok:
        FAILS.append(name)
        print(f"[FAIL] {name}  <- {detail}")
    else:
        print(f"[OK]   {name}")


def load_pack():
    return pack_mod.load()


def clone(spec):
    return json.loads(json.dumps(spec, ensure_ascii=False))


def seeds_of(pack):
    """load_seeds 契约（lead m01386）：返回 {"scenes":[...], "rejected":[...]}。"""
    document = scene.load_seeds(pack)
    check("load_seeds 返回 {scenes, rejected}", isinstance(document, dict)
          and isinstance(document.get("scenes"), list) and isinstance(document.get("rejected"), list))
    return document


def case_seeds(pack):
    document = seeds_of(pack)
    seeds = document["scenes"]
    check("真实种子 rejected 为空", document["rejected"] == [], document["rejected"])
    check("seeds 数量=3", len(seeds) == 3, [s.get("scene_id") for s in seeds])
    ids = [s.get("scene_id") for s in seeds]
    check("seeds scene_id 唯一", len(ids) == len(set(ids)), ids)
    check("seeds 含补料率/停机/人均产量",
          any("refill" in i for i in ids) and any("downtime" in i for i in ids)
          and any("output" in i for i in ids), ids)
    for spec in seeds:
        reasons = scene.validate(spec, pack)
        check(f"seed {spec.get('scene_id')} validate 全绿", reasons == [], reasons)
        # 种子纪律：control_line.params 的数值必须是 pack 引用字符串或已登记值
        params = ((spec.get("metric") or {}).get("control_line") or {}).get("params") or {}
        for key, value in params.items():
            registered = [v for v in ((pack.get("thresholds") or {}).values())]
            check(f"{spec.get('scene_id')}.control_line.{key} 引用 pack",
                  (isinstance(value, str) and value.startswith("pack."))
                  or (isinstance(value, (int, float)) and value in registered),
                  value)
        check(f"{spec.get('scene_id')}.max_per_month 引用 pack",
              isinstance((spec.get("guardrails") or {}).get("max_per_month"), str),
              (spec.get("guardrails") or {}).get("max_per_month"))
        # 动作只用 pack 动作表，不可逆动作必须已登记
        allowed = {a["id"] for a in pack.get("actions") or []}
        irrev = set(pack.get("irreversible_actions") or [])
        for action in spec.get("actions") or []:
            check(f"{spec.get('scene_id')} 动作 {action.get('id')} 在 pack 表内", action.get("id") in allowed)
            if action.get("reversible") is False:
                check(f"{spec.get('scene_id')} 不可逆动作 {action.get('id')} 已登记",
                      action.get("id") in irrev)


def case_validation(pack, seed):
    base = clone(seed)
    check("基线 spec validate 空", scene.validate(base, pack) == [])

    n = clone(base); n["spec_version"] = "scene.v2"
    r = scene.validate(n, pack)
    check("拒绝：spec_version 未知", bool(r) and any("spec_version" in x for x in r), r)

    n = clone(base); n["metric"]["id"] = "bogus_metric"
    r = scene.validate(n, pack)
    check("拒绝：metric.id 不在数据契约", bool(r) and any("metric.id" in x for x in r), r)

    n = clone(base); n["factors_ref"]["version"] = 99
    r = scene.validate(n, pack)
    check("拒绝：因子引用超出 pack.factors", bool(r) and any("factors" in x for x in r), r)

    n = clone(base); n["factors"] = ["bogus_factor"]
    r = scene.validate(n, pack)
    check("拒绝：因子不在封闭世界", bool(r) and any("bogus_factor" in x for x in r), r)

    n = clone(base); n["actions"][0]["id"] = "bogus_action"
    r = scene.validate(n, pack)
    check("拒绝：动作不在 pack 动作表", bool(r) and any("动作" in x for x in r), r)

    n = clone(base)
    for action in n["actions"]:
        action["reversible"] = False
    n["actions"][-1]["id"] = "maintenance"  # maintenance 可逆，标 false → 未登记
    r = scene.validate(n, pack)
    check("拒绝：reversible=false 未登记不可逆", bool(r) and any("reversible=false" in x for x in r), r)

    n = clone(base); n["metric"]["control_line"]["method"] = "invented"
    r = scene.validate(n, pack)
    check("拒绝：control_line.method 不在白名单", bool(r) and any("control_line.method" in x for x in r), r)

    n = clone(base); n["metric"]["control_line"]["params"]["window_days"] = 45
    r = scene.validate(n, pack)
    check("拒绝：control_line 阈值自造", bool(r) and any("自造阈值" in x for x in r), r)

    n = clone(base); n["metric"]["control_line"]["params"]["window_days"] = "30"
    r = scene.validate(n, pack)
    check("拒绝：params 裸字符串常量", bool(r) and any("裸字符串常量" in x for x in r), r)

    n = clone(base); n["metric"]["control_line"]["params"]["window_days"] = "env:T30"
    r = scene.validate(n, pack)
    check("拒绝：params env 引用", bool(r) and any("裸字符串常量" in x for x in r), r)

    n = clone(base); n["metric"]["control_line"]["params"]["window_days"] = "pack.thresholds.not_exists"
    r = scene.validate(n, pack)
    check("拒绝：params 引用不存在的 pack 键", bool(r) and any("不存在的 pack 键" in x for x in r), r)

    n = clone(base); n["metric"]["control_line"]["params"]["window_days"] = "pack.roi_anchors"
    r = scene.validate(n, pack)
    check("拒绝：params pack 引用非数值阈值", bool(r) and any("不是数值阈值" in x for x in r), r)

    n = clone(base); n["metric"]["control_line"]["params"]["window_days"] = 30
    r = scene.validate(n, pack)
    check("接受：params 使用 pack 已登记数值字面量", r == [], r)

    n = clone(base); n["scorecard"]["hard_guardrails_ref"] = "pack.hard_guardrails"
    r = scene.validate(n, pack)
    check("拒绝：hard_guardrails_ref 用文档笔误 pack.hard_guardrails",
          bool(r) and any("笔误" in x for x in r), r)

    n = clone(base); n["scorecard"]["hard_guardrails_ref"] = "pack.not_guardrails"
    r = scene.validate(n, pack)
    check("拒绝：hard_guardrails_ref 非正名", bool(r) and any("必须引用" in x for x in r), r)

    n = clone(base); n["metric"]["control_line"]["params"]["threshold_key"] = "not_in_pack"
    r = scene.validate(n, pack)
    check("拒绝：阈值键不在 pack", bool(r) and any("threshold_key" in x for x in r), r)

    n = clone(base); n["actions"][0]["cost"] = 800
    r = scene.validate(n, pack)
    check("拒绝：动作写死数值成本", bool(r) and any("cost_anchor_key" in x for x in r), r)

    n = clone(base); n["guardrails"]["max_per_month"] = 9
    r = scene.validate(n, pack)
    check("拒绝：max_per_month 自造阈值", bool(r) and any("max_per_month" in x for x in r), r)

    n = clone(base); n["scorecard"]["scale"] = 5
    r = scene.validate(n, pack)
    check("拒绝：scorecard.scale 非 100", bool(r) and any("scale" in x for x in r), r)

    # 缺必填数据字段 → 只降级、不拒绝
    spec = clone(base)
    reasons = scene.validate(spec, pack, data_manifest={"fields": ["refill_rate"]})
    check("缺数据字段不拒绝（仍是合法 spec）", reasons == [], reasons)
    degrade = scene.degradation_reasons(spec, {"fields": ["refill_rate"]})
    check("缺数据字段 → 降级态原因", bool(degrade) and any("降级" in x for x in degrade), degrade)
    full = {"fields": [req["field"] for req in spec.get("data_requirements") or []]}
    check("数据齐全 → 无降级", scene.degradation_reasons(spec, full) == [])


def case_discover(pack):
    rows = []
    for i in range(20):
        rows.append({
            "date": f"2026-10-{i + 1:02d}",
            "production_line": "B",
            "refill_rate": 5.0 if i < 18 else 9.0,
            "output_volume": 100 + i,
            "downtime_records": 5,
            "mold_status": "ok",
        })
    drafts = scene.discover({"fields": ["refill_rate", "output_volume", "downtime_records"]}, rows, pack)
    check("discover 产出草稿", len(drafts) >= 1, [d.get("scene_id") for d in drafts])
    hit = [d for d in drafts if (d.get("discovery") or {}).get("metric_id") == "refill_rate"]
    check("discover 命中补料率异常", bool(hit), [d.get("discovery") for d in drafts])
    if hit:
        d = hit[0]
        check("草稿 origin=discovered", d.get("origin") == "discovered", d.get("origin"))
        check("草稿 validate 通过", scene.validate(d, pack) == [], scene.validate(d, pack))
        check("草稿带 μ/σ 统计", all(k in d["discovery"] for k in ("mean", "sigma", "latest", "delta_ratio", "null_rate")))
        check("草稿异常类型 over_2sigma", d["discovery"]["anomaly"] == "over_2sigma", d["discovery"]["anomaly"])
        again = scene.discover({"fields": ["refill_rate"]}, rows, pack)
        again_hit = [x for x in again if x.get("scene_id") == d.get("scene_id")]
        check("同一输入草稿 scene_id 稳定", bool(again_hit))
        if again_hit:
            keys = ("title", "question", "metric", "actions", "guardrails", "scorecard")
            same = all(json.dumps(d.get(k), ensure_ascii=False, sort_keys=True)
                       == json.dumps(again_hit[0].get(k), ensure_ascii=False, sort_keys=True) for k in keys)
            check("同一输入关键字段一致（U8）", same)
    flat = [{"date": f"2026-10-{i + 1:02d}", "production_line": "B",
             "refill_rate": 5.0, "downtime_records": 5, "output_volume": 100} for i in range(20)]
    check("平稳数据不产草稿", scene.discover({"fields": ["refill_rate"]}, flat, pack) == [])
    check("空记录不产草稿", scene.discover(None, [], pack) == [])


def case_load_seeds_rejected(pack):
    """种子目录里解析失败/校验不通过的文件必须进 rejected，不混入 scenes。"""
    tmp = Path(tempfile.mkdtemp(prefix="scene_seeds_"))
    try:
        (tmp / "scenes").mkdir(parents=True, exist_ok=True)
        good = clone(scene.load_seeds(pack)["scenes"][0])
        bad = clone(good)
        bad["metric"]["id"] = "bogus_metric"
        bad["scene_id"] = "scene-bogus"
        (tmp / "scenes" / "good.json").write_text(json.dumps(good, ensure_ascii=False), encoding="utf-8")
        (tmp / "scenes" / "bad.json").write_text(json.dumps(bad, ensure_ascii=False), encoding="utf-8")
        (tmp / "scenes" / "broken.json").write_text("{not json", encoding="utf-8")
        tmp_pack = clone(pack)
        tmp_pack["_pack_path"] = str(tmp / "pack.json")
        document = scene.load_seeds(tmp_pack)
        check("rejected 收集 2 个非法种子", len(document["rejected"]) == 2,
              [r.get("file") for r in document["rejected"]])
        check("scenes 只保留合法种子", len(document["scenes"]) == 1
              and document["scenes"][0].get("scene_id") == good.get("scene_id"),
              [s.get("scene_id") for s in document["scenes"]])
        fields_ok = all(isinstance(r, dict) and isinstance(r.get("file"), str)
                        and isinstance(r.get("reasons"), list) and r["reasons"]
                        for r in document["rejected"])
        check("rejected 条目结构 {file, reasons:[...]}", fields_ok, document["rejected"])
        bad_entry = [r for r in document["rejected"] if "bad.json" in r.get("file", "")]
        check("校验失败条目带 scene_id", bool(bad_entry) and bad_entry[0].get("scene_id") == "scene-bogus",
              bad_entry)
        broken_entry = [r for r in document["rejected"] if "broken.json" in r.get("file", "")]
        check("解析失败条目给原因", bool(broken_entry) and broken_entry[0]["reasons"], broken_entry)
        check("load_seed 只从合法列表取", scene.load_seed(good.get("scene_id"), tmp_pack) is not None
              and scene.load_seed("scene-bogus", tmp_pack) is None)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def case_write_scene(pack):
    from core import provenance

    tmp = Path(tempfile.mkdtemp(prefix="scene_m15_"))
    try:
        spec = clone(scene.load_seeds(pack)["scenes"][0])
        info = scene.write_task_scene(tmp, spec)
        target = tmp / "scene.json"
        check("scene.json 落盘", target.is_file(), str(target))
        check("返回 sha256 与文件一致", info["sha256"] == provenance.sha256_file(target))
        check("返回 size 与文件一致", info["size"] == target.stat().st_size)
        loaded = json.loads(target.read_text(encoding="utf-8-sig"))
        check("落盘内容一致", loaded.get("scene_id") == spec.get("scene_id"))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    pack = load_pack()
    document = scene.load_seeds(pack)
    seeds = document["scenes"]
    if not document["rejected"] == []:
        print("RESULT: FAIL（真实种子目录存在 rejected）")
        return 1
    if not seeds:
        print("RESULT: FAIL（缺少 packs/manufacturing/scenes/*.json 种子）")
        return 1
    case_seeds(pack)
    case_validation(pack, seeds[0])
    case_discover(pack)
    case_load_seeds_rejected(pack)
    case_write_scene(pack)
    print()
    if FAILS:
        print(f"RESULT: FAIL ({len(FAILS)}/{CHECKS} 项失败)")
        for name in FAILS:
            print("  -", name)
        return 1
    print(f"RESULT: PASS（{CHECKS} 项检查全绿）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
