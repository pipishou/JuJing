# -*- coding: utf-8 -*-
"""报告契约 & 六维评分卡（§2.3/§2.5 的机器判据，A4 依赖）。

要点：
  - 六维等权，100 分制**精确**换算：total = round(sum(0-5 分)/30*100, 2)
    → 六维全 5 分 = 100.0（不是 16.67*6=100.02）
  - 每维展示分 points 仍按 16.67 分/维（与 assets/scorecard_six_dim.yaml 一致）
  - 硬护栏 / 80-60-40 阈值与 assets/scorecard_six_dim.yaml 对齐
  - 报告必须带 scorecard.total / scorecard.verdict，且与独立调用一致
只依赖 stdlib。
"""
import json
import math

SIX_DIMS = ["data_completeness", "timeliness", "significance", "exclusivity",
            "history", "human_ai_consistency"]

WEIGHT_EACH_DISPLAY = 16.67
FULL_SCALE = 5.0
TOTAL_MAX = 100.0

# 与 assets/scorecard_six_dim.yaml 的 thresholds 对齐（min/max 闭区间）
THRESHOLDS = [
    {"min": 80, "max": 100, "action": "output", "label": "系统建议"},
    {"min": 60, "max": 79.99, "action": "output_with_flag", "label": "建议人工复核"},
    {"min": 40, "max": 59.99, "action": "reference_only", "label": "需人工判断"},
    {"min": 0, "max": 39.99, "action": "no_output", "label": "待人工排查"},
]

GUARDRAILS = [
    {"id": "human_ai_conflict", "condition": "human_ai_consistency == 0", "action": "force_manual_review"},
    {"id": "too_many_hypotheses", "condition": "exclusivity <= 10", "action": "force_manual_review"},
    {"id": "data_gap", "condition": "data_completeness <= 15", "action": "force_manual_review"},
]

CALIBRATION = {
    "cold_start": {"rule": "六维等权 16.67 分，所有输出标注\"冷启动模式\"", "target_records": 30},
    "phase1": {"trigger_records": 50, "method": "ordinal_logistic_regression"},
    "phase2": {"every_records": 30, "stable_when_weight_change_lt": 0.05, "final_frequency": "quarterly"},
}


def to_score(value, strict=False):
    """把单维分数规整为 float（0-5）。bool / NaN / 越界 / 叙述 → ValueError。"""
    if isinstance(value, bool):
        raise ValueError("boolean is not a score")
    if isinstance(value, dict):
        return to_score(value.get("score"), strict)
    if isinstance(value, (int, float)):
        score = float(value)
    elif isinstance(value, str):
        if strict:
            raise ValueError(f"strict 模式要求数值分数，收到字符串：{value[:40]}")
        import re
        m = re.match(r"^\s*(\d+(?:\.\d+)?)\s*/\s*5", value)
        if m:
            score = float(m.group(1))
        else:
            m = re.match(r"^\s*(\d+(?:\.\d+)?)", value)
            if not m:
                raise ValueError(f"无法解析分数：{value[:40]}")
            score = float(m.group(1))
    else:
        raise ValueError(f"unsupported score type: {type(value)}")
    if math.isnan(score) or math.isinf(score):
        raise ValueError(f"score 非有限数值: {value!r}")
    if score < 0 or score > FULL_SCALE:
        raise ValueError(f"score out of range 0-5: {score}")
    return score


def normalize(raw, strict=False):
    """返回 {六维: float}；缺维/非法 → ValueError（real 报告不允许缺维）。"""
    if not isinstance(raw, dict):
        raise ValueError("scorecard 必须是对象")
    dims, errors = {}, []
    for dim in SIX_DIMS:
        if dim not in raw:
            errors.append(f"{dim}: 缺失")
            continue
        try:
            dims[dim] = to_score(raw[dim], strict)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{dim}: {exc}")
    if errors:
        raise ValueError("；".join(errors))
    return dims


def compute(raw, strict=False):
    """计算 100 分制：{dimensions_0_5, points, total, guardrails, verdict, label}。"""
    dims = normalize(raw, strict)
    points = {d: round(dims[d] / FULL_SCALE * WEIGHT_EACH_DISPLAY, 2) for d in SIX_DIMS}
    # 精确 100 分制：6 维满分为 30 分 → 100 分；避免 6*16.67=100.02 的浮点放大
    total = round(sum(dims.values()) / (FULL_SCALE * len(SIX_DIMS)) * TOTAL_MAX, 2)

    guards = []
    if dims["human_ai_consistency"] <= 0:
        guards.append("human_ai_conflict")
    if points["exclusivity"] <= 10:
        guards.append("too_many_hypotheses")
    if points["data_completeness"] <= 15:
        guards.append("data_gap")

    verdict, label = "no_output", "待人工排查"
    for t in THRESHOLDS:
        if t["min"] <= total <= t["max"]:
            verdict, label = t["action"], t["label"]
            break
    if guards:
        verdict, label = "force_manual_review", "建议人工复核（硬护栏触发）"
    return {
        "dimensions_0_5": dims,
        "points": points,
        "total": total,
        "guardrails": guards,
        "verdict": verdict,
        "label": label,
    }


def apply(report):
    """把 total/verdict/label/guardrails/points 写进 report['scorecard']（原地修改并返回计算结果）。"""
    sc = report.get("scorecard")
    if not isinstance(sc, dict):
        raise ValueError("report.scorecard 必须是对象")
    result = compute(sc)
    sc["total"] = result["total"]
    sc["verdict"] = result["verdict"]
    sc["label"] = result["label"]
    sc["guardrails"] = result["guardrails"]
    sc["points"] = result["points"]
    return result


def consistency_errors(scorecard):
    """校验 scorecard.total/verdict 与独立计算一致；返回错误列表（空=一致）。"""
    if not isinstance(scorecard, dict):
        return ["scorecard 必须是对象"]
    errors = []
    try:
        result = compute(scorecard)
    except Exception as exc:  # noqa: BLE001
        return [f"scorecard 无法计算: {exc}"]
    if "total" not in scorecard:
        errors.append("scorecard.total 缺失（真实报告必须落 100 分制总分）")
    else:
        try:
            declared = round(float(scorecard["total"]), 2)
        except Exception:  # noqa: BLE001
            declared = None
        if declared is None or declared != result["total"]:
            errors.append(f"scorecard.total 与独立计算不一致: declared={scorecard.get('total')!r} computed={result['total']}")
    if "verdict" not in scorecard:
        errors.append("scorecard.verdict 缺失（真实报告必须落判定区间）")
    elif scorecard["verdict"] != result["verdict"]:
        errors.append(f"scorecard.verdict 与独立计算不一致: declared={scorecard.get('verdict')!r} computed={result['verdict']!r}")
    return errors


if __name__ == "__main__":
    import sys
    raw = json.loads(sys.argv[1]) if len(sys.argv) > 1 else {}
    print(json.dumps(compute(raw), ensure_ascii=False, indent=2))
