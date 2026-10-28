# -*- coding: utf-8 -*-
"""报告契约 & 六维评分卡（§2.3/§2.5 的机器判据，A4 依赖；B16 升 100 分制）。

要点：
  - 六维等权，100 分制**精确**换算：total = round(sum(0-5 分)/30*100, 2)
    → 六维全 5 分 = 100.0（不是 16.67*6=100.02）
  - 兼容两种输入口径（B16）：
      * 旧口径 0-5 分/维（LLM 原样输出、"4/5" 字符串）→ 自动 ×20 映射到百分制
      * 新口径 0-100 百分制/维（> 5 的数值按百分制解释）
    对外 dimensions_0_5 仍是规整后的 0-5 分；dimensions_100 为百分制视图。
  - 每维展示分 points 仍按 16.67 分/维（与 assets/scorecard_six_dim.yaml 一致）
  - 档位（B16）：>=80 output / 系统建议；60-79 **force_manual_review**（不得直接输出）；
    40-59 reference_only；<40 no_output
  - 硬护栏阈值不再写死在代码里：从 packs/<pack_id>/pack.json 的
    scorecard_hard_guardrails 读取（任一命中强制人工复核，且不得 output）
  - 报告必须带 scorecard.total / scorecard.verdict，且与独立调用一致
只依赖 stdlib。
"""
import json
import math
import re

SIX_DIMS = ["data_completeness", "timeliness", "significance", "exclusivity",
            "history", "human_ai_consistency"]

# C-1：交付卡三卡型。六维评分卡只对 attribution 生效；review/content 走各自
# 强制字段（见 core.gates.content_plan_errors / review_errors）。
# 报告顶层缺 card_kind 视为 attribution（向后兼容旧报告）；未知值一律报错不静默。
CARD_KINDS = ("attribution", "review", "content")
DEFAULT_CARD_KIND = "attribution"

WEIGHT_EACH_DISPLAY = 16.67
FULL_SCALE = 5.0
TOTAL_MAX = 100.0

# 与 assets/scorecard_six_dim.yaml 的 thresholds 对齐（min/max 闭区间）
# B16：60-79 收口为 force_manual_review —— 该区间不得直接 output。
THRESHOLDS = [
    {"min": 80, "max": 100, "action": "output", "label": "系统建议"},
    {"min": 60, "max": 79.99, "action": "force_manual_review", "label": "建议人工复核"},
    {"min": 40, "max": 59.99, "action": "reference_only", "label": "需人工判断"},
    {"min": 0, "max": 39.99, "action": "no_output", "label": "待人工排查"},
]

# 硬护栏阈值由行业包声明（B16：代码只读 pack，不写死阈值）。
PACK_GUARDRAIL_KEY = "scorecard_hard_guardrails"
_COMPARE_OPS = ("==", "<=", "<", ">=", ">")

CALIBRATION = {
    "cold_start": {"rule": "六维等权 16.67 分，所有输出标注\"冷启动模式\"", "target_records": 30},
    "phase1": {"trigger_records": 50, "method": "ordinal_logistic_regression"},
    "phase2": {"every_records": 30, "stable_when_weight_change_lt": 0.05, "final_frequency": "quarterly"},
}


def to_score(value, strict=False):
    """把单维分数规整为 0-5 分（对外口径不变）。

    兼容（B16）：
      - 0-5 旧口径：原样返回
      - 0-100 百分制（数值 > 5，或 "80/100"）：÷20 映射到 0-5
      - "4/5" 字符串：按旧口径取 4
    bool / NaN / 越界 / 叙述 → ValueError。
    """
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
        m = re.match(r"^\s*(\d+(?:\.\d+)?)\s*/\s*(\d+(?:\.\d+)?)", value)
        if m:
            score = float(m.group(1))
            scale = float(m.group(2))
            if scale and scale != FULL_SCALE:
                score = score / scale * FULL_SCALE
        else:
            m = re.match(r"^\s*(\d+(?:\.\d+)?)", value)
            if not m:
                raise ValueError(f"无法解析分数：{value[:40]}")
            score = float(m.group(1))
    else:
        raise ValueError(f"unsupported score type: {type(value)}")
    if math.isnan(score) or math.isinf(score):
        raise ValueError(f"score 非有限数值: {value!r}")
    if score < 0 or score > TOTAL_MAX:
        raise ValueError(f"score out of range（旧口径 0-5 / 新口径 0-100）: {score}")
    if score > FULL_SCALE:  # 百分制 → 0-5
        score = score / (TOTAL_MAX / FULL_SCALE)
    return score


def to_percent(value, strict=False):
    """单维分数 → 百分制（0-100，两位小数）。"""
    return round(to_score(value, strict) * (TOTAL_MAX / FULL_SCALE), 2)


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


def guardrail_defs(pack=None):
    """读取行业包声明的硬护栏阈值（B16：唯一事实源是 pack.json，不写死代码）。

    pack 里格式（列表，兼容对象写法）：
      [{"id":"human_ai_conflict","dim":"human_ai_consistency","op":"==","value":0}, ...]
    评口径是 points（每维 0-16.67 展示分）。
    """
    doc = pack
    if doc is None:
        from core import pack as pack_mod  # noqa: PLC0415

        doc = pack_mod.load()
    raw = doc.get(PACK_GUARDRAIL_KEY) if isinstance(doc, dict) else None
    if not isinstance(raw, list) or not raw:
        path = (doc or {}).get("_pack_path") if isinstance(doc, dict) else None
        raise ValueError(
            f"行业包缺少 {PACK_GUARDRAIL_KEY}（硬护栏阈值必须由 pack.json 声明，代码不写死）: {path}"
        )
    defs = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError(f"{PACK_GUARDRAIL_KEY}[{i}] 必须是对象")
        dim = item.get("dim") or item.get("dimension")
        if dim not in SIX_DIMS:
            raise ValueError(f"{PACK_GUARDRAIL_KEY}[{i}].dim 非法: {dim!r}（须为六维之一）")
        op = str(item.get("op") or "<=")
        if op not in _COMPARE_OPS:
            raise ValueError(f"{PACK_GUARDRAIL_KEY}[{i}].op 非法: {op!r}")
        value = item.get("value")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{PACK_GUARDRAIL_KEY}[{i}].value 必须是数值")
        defs.append({
            "id": str(item.get("id") or f"guard_{dim}"),
            "dim": dim,
            "op": op,
            "value": float(value),
            "action": str(item.get("action") or "force_manual_review"),
        })
    return defs


def _compare(value, op, threshold):
    if op == "==":
        return value == threshold
    if op == "<=":
        return value <= threshold
    if op == "<":
        return value < threshold
    if op == ">=":
        return value >= threshold
    if op == ">":
        return value > threshold
    raise ValueError(f"不支持的比较符: {op}")


def compute(raw, strict=False, pack=None):
    """计算 100 分制：{dimensions_0_5, dimensions_100, points, total, guardrails, verdict, label}。

    pack：行业包 dict（缺省按 core.pack.load() 读取）——硬护栏阈值只从 pack 读。
    """
    dims = normalize(raw, strict)
    percent = {d: round(dims[d] * (TOTAL_MAX / FULL_SCALE), 2) for d in SIX_DIMS}
    points = {d: round(dims[d] / FULL_SCALE * WEIGHT_EACH_DISPLAY, 2) for d in SIX_DIMS}
    # 精确 100 分制：6 维满分为 30 分 → 100 分；避免 6*16.67=100.02 的浮点放大
    total = round(sum(dims.values()) / (FULL_SCALE * len(SIX_DIMS)) * TOTAL_MAX, 2)

    guards = []
    guardrail_details = []
    for guard in guardrail_defs(pack):
        if _compare(points[guard["dim"]], guard["op"], guard["value"]):
            guards.append(guard["id"])
            guardrail_details.append(dict(guard, triggered_value=points[guard["dim"]]))

    verdict, label = "no_output", "待人工排查"
    for t in THRESHOLDS:
        if t["min"] <= total <= t["max"]:
            verdict, label = t["action"], t["label"]
            break
    if guards:
        verdict, label = "force_manual_review", "建议人工复核（硬护栏触发）"
    return {
        "dimensions_0_5": dims,
        "dimensions_100": percent,
        "points": points,
        "total": total,
        "guardrails": guards,
        "guardrail_details": guardrail_details,
        "verdict": verdict,
        "label": label,
    }


def card_kind(report):
    """读取交付卡卡型：缺省 attribution；未知值 → ValueError（C-1，不静默）。

    兼容：card_kind 为 None/空白字符串时按缺省处理；非字符串 → ValueError。
    """
    if not isinstance(report, dict):
        raise ValueError("report 必须是 JSON 对象才能判定 card_kind")
    raw = report.get("card_kind", DEFAULT_CARD_KIND)
    if raw is None:
        return DEFAULT_CARD_KIND
    if not isinstance(raw, str):
        raise ValueError(f"card_kind 必须是字符串，收到 {type(raw).__name__}（允许 {list(CARD_KINDS)}）")
    kind = raw.strip()
    if not kind:
        return DEFAULT_CARD_KIND
    if kind not in CARD_KINDS:
        raise ValueError(f"未知 card_kind: {kind!r}（允许 {list(CARD_KINDS)}）")
    return kind


def apply(report, pack=None):
    """把 total/verdict/label/guardrails/points 写进 report['scorecard']（原地修改并返回计算结果）。

    C-1 三卡型：
      - attribution（缺省）：行为完全不变（六维强制）。
      - review/content：无 scorecard 或缺六维 → 不抛异常、不改写 scorecard，
        返回 {"skipped": True, "card_kind": kind}；给了完整六维仍照常计算（允许自评）。
    """
    if not isinstance(report, dict):
        raise ValueError("report 必须是 JSON 对象")
    kind = card_kind(report)  # 未知 card_kind 在这里报错（fail closed）
    sc = report.get("scorecard")
    if kind != "attribution":
        if not isinstance(sc, dict) or any(dim not in sc for dim in SIX_DIMS):
            return {"skipped": True, "card_kind": kind}
    if not isinstance(sc, dict):
        raise ValueError("report.scorecard 必须是对象")
    result = compute(sc, pack=pack)
    sc["total"] = result["total"]
    sc["verdict"] = result["verdict"]
    sc["label"] = result["label"]
    sc["guardrails"] = result["guardrails"]
    sc["points"] = result["points"]
    out = dict(result)
    out["card_kind"] = kind
    return out


def consistency_errors(scorecard, pack=None):
    """校验 scorecard.total/verdict 与独立计算一致；返回错误列表（空=一致）。"""
    if not isinstance(scorecard, dict):
        return ["scorecard 必须是对象"]
    errors = []
    try:
        result = compute(scorecard, pack=pack)
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


# --------------------------------------------------------------------------- #
# B17：DEC 决策日志扩展字段（设计文档 §3.3）+ T+7 回填校验
#   - 全部可选；缺省不写 null 占位，不改评分逻辑（B16 并行改本文件时只会有增量冲突）
#   - attribution_method 值域受 tests/validate_structure.py:195 约束：
#     "exclusion" | "counterfactual" | "rule_based"
# --------------------------------------------------------------------------- #
DECISION_LOG_EXTENSION_FIELDS = (
    "attribution_method",
    "actual_action",
    "override_reason",
    "outcome_window",
    "observed_outcome",
    "action_cost",
    "increment_value",
)

ATTRIBUTION_METHODS = ("exclusion", "counterfactual", "rule_based")

OUTCOME_DEFAULT_WINDOW = "7d"

# T+7 回填合法字段（未知字段一律 400，防止 UI 把展示字段写进事实源）
OUTCOME_FIELDS = ("observed_outcome", "source", "actual_action", "override_reason",
                  "action_cost", "increment_value", "outcome_window")
OUTCOME_SOURCES = ("manual", "observed", "auto", "import")
_OUTCOME_WINDOW_RE = re.compile(r"^\d{1,4}d$")


def is_empty(value):
    """未填写判定：None/空串/空容器 → True；0 与 False 是有效值（不能丢）。"""
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, dict, tuple, set)):
        return len(value) == 0
    return False


def extension_fields(source, keys=None):
    """从 source 拣出「有值」的扩展字段（不写 null/空占位）。"""
    if not isinstance(source, dict):
        return {}
    picked = {}
    for key in (keys or DECISION_LOG_EXTENSION_FIELDS):
        value = source.get(key)
        if not is_empty(value):
            picked[key] = value
    return picked


def extend_decision_log(entry, source=None):
    """decision_log_entry + 有值扩展字段 → 新 dict（不改入参）。

    B17 约定：扩展字段全部可选、有值才写，不写 null 占位（lead 指令 #2）；
    服务端 T+7 回填视图/报告生成共用同一合并逻辑。
    """
    merged = dict(entry) if isinstance(entry, dict) else {}
    merged.update(extension_fields(source))
    return merged


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def validate_outcome(payload):
    """校验 POST /api/tasks/<id>/outcome 的 body（B17，strict）。

    返回 (clean, errors)：
      clean  = 已提供且合法的字段（outcome_window/source 缺省补默认值）
      errors = 可读错误列表（空 = 合法）
    字段：
      observed_outcome  str|int|float|dict（必填，非空）
      actual_action     str
      override_reason   str
      action_cost       int|float|dict（数值须 ≥0）
      increment_value   int|float|dict（数值须 ≥0）
      outcome_window    str，形如 "7d"/"14d"（缺省 "7d"）
      source            manual|observed|auto|import（缺省 "manual"）
    未知字段 → 400：DEC 是事实源，UI 展示字段不得混入。
    """
    if not isinstance(payload, dict):
        return {}, ["body 必须是 JSON 对象"]
    errors, clean = [], {}
    unknown = [str(key) for key in payload if key not in OUTCOME_FIELDS]
    if unknown:
        errors.append(f"未知字段：{', '.join(sorted(unknown))}（允许 {list(OUTCOME_FIELDS)}）")

    def _non_negative(field, value):
        if _is_number(value) and value < 0:
            errors.append(f"{field} 不能为负: {value!r}")

    def _cost_or_number(field, value):
        if _is_number(value):
            return True
        if isinstance(value, dict) and value:
            return True
        errors.append(f"{field} 类型非法：需有限数值或非空 dict（收到 {type(value).__name__}）")
        return False

    if "observed_outcome" not in payload or is_empty(payload.get("observed_outcome")):
        errors.append("observed_outcome 必填（T+7 实测值/描述）")
    else:
        observed = payload["observed_outcome"]
        if _is_number(observed) or (isinstance(observed, str) and observed.strip()) \
                or (isinstance(observed, dict) and observed):
            clean["observed_outcome"] = observed
        else:
            errors.append("observed_outcome 类型非法：需非空 str/dict 或有限数值")

    for field in ("actual_action", "override_reason"):
        if field in payload and not is_empty(payload.get(field)):
            value = payload[field]
            if isinstance(value, str):
                clean[field] = value.strip()
            else:
                errors.append(f"{field} 必须是字符串")

    for field in ("action_cost", "increment_value"):
        if field not in payload or is_empty(payload.get(field)):
            continue
        value = payload[field]
        before = len(errors)
        _cost_or_number(field, value)
        _non_negative(field, value)
        if len(errors) == before:
            clean[field] = value

    if "outcome_window" in payload and not is_empty(payload.get("outcome_window")):
        window = payload["outcome_window"]
        if not isinstance(window, str) or not _OUTCOME_WINDOW_RE.match(window.strip()):
            errors.append('outcome_window 非法：需形如 "7d"/"14d"（数字+小写 d）')
        else:
            clean["outcome_window"] = window.strip()
    else:
        clean.setdefault("outcome_window", OUTCOME_DEFAULT_WINDOW)

    if "source" in payload and not is_empty(payload.get("source")):
        source = payload["source"]
        if isinstance(source, str) and source.strip() in OUTCOME_SOURCES:
            clean["source"] = source.strip()
        else:
            errors.append(f"source 非法：允许 {list(OUTCOME_SOURCES)}")
    else:
        clean.setdefault("source", "manual")

    return clean, errors


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 2 and sys.argv[1] == "validate-outcome":
        payload = json.loads(sys.argv[2])
        print(json.dumps({"clean": validate_outcome(payload)[0],
                          "errors": validate_outcome(payload)[1]},
                         ensure_ascii=False, indent=2))
    else:
        raw = json.loads(sys.argv[1]) if len(sys.argv) > 1 else {}
        print(json.dumps(compute(raw), ensure_ascii=False, indent=2))
