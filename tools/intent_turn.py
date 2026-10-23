#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""L0+L1 一条龙（G1 批次）：一句提问 -> intent.json（意图卡）-> scene.json（场景实例）。

用法（在线，骑 Octop）：
  python tools/intent_turn.py --question "B线补料率最近一周怎么样" \
      [--base-url http://127.0.0.1:8088] [--outdir runs/intent-xxx]
  凭据走环境变量 OCTOP_USERNAME / OCTOP_PASSWORD（setup 时的管理员账号）。

用法（离线，跳过 LLM，直接给定解析结果——测试与调试用）：
  python tools/intent_turn.py --skip-llm --scene-id scene-refill-rate-first \
      --line B --window T7 --question "B线补料率最近一周怎么样"

退出码：0=ok 出卡；2=ambiguous（追问）；3=scene_not_matched；
4=parse_failed/llm_unavailable；5=data_insufficient（scene.json 仍产出但降级，
A1：数据不足不出决策建议——决策卡由 L2+ 的 gate 把守）；1=内部错误。

产物：<outdir>/intent.json + <outdir>/scene.json（均原子写）。
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import fsutil, intent as intent_mod, pack as pack_mod  # noqa: E402
from core import scene as scene_mod  # noqa: E402
from core import octop_client  # noqa: E402

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_AMBIGUOUS = 2
EXIT_SCENE_NOT_MATCHED = 3
EXIT_PARSE_FAILED = 4
EXIT_DATA_INSUFFICIENT = 5

_SOFT_STATUSES = {
    intent_mod.STATUS_AMBIGUOUS: (EXIT_AMBIGUOUS, "需要追问"),
    intent_mod.STATUS_SCENE_NOT_MATCHED: (EXIT_SCENE_NOT_MATCHED, "场景未命中"),
    intent_mod.STATUS_PARSE_FAILED: (EXIT_PARSE_FAILED, "解析失败"),
    intent_mod.STATUS_LLM_UNAVAILABLE: (EXIT_PARSE_FAILED, "Octop 侧不可用"),
}


def _fake_llm_doc(args):
    """--skip-llm 的模拟解析输出（走同一条 resolve_intent 校验链）。"""
    doc = {}
    if args.scene_id:
        doc["scene_id"] = args.scene_id
    if args.line:
        doc["entities"] = {"production_line": args.line}
    if args.window:
        doc["time_window"] = args.window
    return json.dumps(doc, ensure_ascii=False)


def run(args):
    pack = pack_mod.load(args.pack)
    catalog = intent_mod.load_catalog(pack)

    if args.skip_llm:
        llm_text = _fake_llm_doc(args)
    else:
        try:
            _, llm_text = octop_client.parse_question(
                args.base_url, args.question, intent_mod.build_catalog_text(catalog),
                timeout=args.timeout)
        except octop_client.OctopClientError as exc:
            llm_text = None
            intent = {
                "spec_version": intent_mod.INTENT_VERSION,
                "question": args.question,
                "generated_at": intent_mod.now_iso(),
                "generated_by": "tools.intent_turn",
                "status": intent_mod.STATUS_LLM_UNAVAILABLE,
                "reason": f"Octop 侧失败：{exc}",
            }

    if llm_text is not None:
        intent = intent_mod.resolve_intent(args.question, llm_text, catalog)

    outdir = Path(args.outdir) if args.outdir else ROOT / "runs" / f"intent-{intent_mod.now_iso().replace(':', '')}"
    outdir.mkdir(parents=True, exist_ok=True)
    fsutil.atomic_write_json(outdir / "intent.json", intent)

    a0_reasons = intent_mod.check_a0(intent)
    if a0_reasons:
        # A0 违规 = 实现缺陷而非用户问题：不允许静默出卡。
        print(json.dumps({"status": "a0_violation", "reasons": a0_reasons,
                          "intent_path": str(outdir / "intent.json")},
                         ensure_ascii=False))
        return EXIT_ERROR

    if intent["status"] != intent_mod.STATUS_OK:
        code, label = _SOFT_STATUSES.get(intent["status"], (EXIT_ERROR, "未知状态"))
        print(json.dumps({"status": intent["status"], "label": label,
                          "reason": intent.get("reason") or intent.get("clarify_question"),
                          "options": intent.get("options"),
                          "intent_path": str(outdir / "intent.json")},
                         ensure_ascii=False))
        return code

    # ---- L1：种子卡 + 两窗数据 -> 场景实例（A1 判定在此）----
    scene_id = intent["scene_id"]
    seed = scene_mod.load_seed(scene_id, pack)
    if seed is None:
        print(json.dumps({"status": "internal_error",
                          "reason": f"意图选中的场景 {scene_id} 不在种子表（目录/种子不一致）"},
                         ensure_ascii=False))
        return EXIT_ERROR

    _, current_records = intent_mod.load_records(intent["data_source"]["current"])
    _, baseline_records = intent_mod.load_records(intent["data_source"]["baseline"])

    lines = {str(r.get("production_line")) for r in current_records if r.get("production_line")}
    line = intent["entities"]["production_line"]
    # 确定性归一化：自然话术「B线」-> 枚举值「B」。仅当归一化后命中数据集
    # 枚举才采纳；命不中仍按原值保守拒绝——不引入新信息，不是编造。
    if line not in lines and line.endswith("线") and line[:-1] in lines:
        line = line[:-1]
        intent["entities"]["production_line"] = line
        # 归一化结果回写意图卡（首轮写盘发生在实体校验之前）。
        fsutil.atomic_write_json(outdir / "intent.json", intent)
    if line not in lines:
        intent["status"] = intent_mod.STATUS_DATA_INSUFFICIENT
        intent["reason"] = f"数据集中没有产线 {line!r}（现有：{sorted(lines)}），禁止编造"
        fsutil.atomic_write_json(outdir / "intent.json", intent)
        print(json.dumps({"status": intent_mod.STATUS_DATA_INSUFFICIENT,
                          "reason": intent["reason"],
                          "intent_path": str(outdir / "intent.json")},
                         ensure_ascii=False))
        return EXIT_DATA_INSUFFICIENT

    instance = scene_mod.build_instance(seed, pack, baseline_records, current_records)
    written = scene_mod.write_task_scene(outdir, instance)

    # A1 判定看数据实况：两窗都实际可用的字段才进 manifest（variantC 这类
    # 纸面契约齐全但记录全 null 的样本必须被拦下）。
    manifest = {"fields": sorted(intent_mod.usable_fields(current_records)
                                 & intent_mod.usable_fields(baseline_records))}
    degradation = scene_mod.degradation_reasons(seed, manifest)

    summary = {
        "status": intent_mod.STATUS_DATA_INSUFFICIENT if degradation else intent_mod.STATUS_OK,
        "intent_path": str(outdir / "intent.json"),
        "scene_path": written["path"],
        "scene_sha256": written["sha256"],
        "current_value": instance.get("current_value"),
        "control_line": instance.get("control_line"),
        "degradation": degradation,
    }
    if degradation:
        intent["status"] = intent_mod.STATUS_DATA_INSUFFICIENT
        intent["reason"] = "；".join(degradation)
        intent["scene_ref"] = {"path": written["path"], "sha256": written["sha256"]}
        fsutil.atomic_write_json(outdir / "intent.json", intent)
        print(json.dumps(summary, ensure_ascii=False))
        return EXIT_DATA_INSUFFICIENT

    print(json.dumps(summary, ensure_ascii=False))
    return EXIT_OK


def main(argv=None):
    parser = argparse.ArgumentParser(description="L0 意图卡 + L1 场景实例一条龙")
    parser.add_argument("--question", required=True, help="用户原话提问")
    parser.add_argument("--base-url", default=octop_client.DEFAULT_BASE)
    parser.add_argument("--pack", default=pack_mod.DEFAULT_PACK_ID)
    parser.add_argument("--outdir", default=None, help="缺省 runs/intent-<时间戳>")
    parser.add_argument("--timeout", type=float, default=180.0, help="WS 轮次超时秒数")
    parser.add_argument("--skip-llm", action="store_true", help="跳过 Octop，用下面三个参数模拟解析")
    parser.add_argument("--scene-id", default=None)
    parser.add_argument("--line", default=None, help="产线，如 B")
    parser.add_argument("--window", default=None, choices=["T7", "T30"])
    args = parser.parse_args(argv)
    try:
        return run(args)
    except (pack_mod.PackError, ValueError, OSError) as exc:
        print(json.dumps({"status": "internal_error", "reason": str(exc)}, ensure_ascii=False))
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
