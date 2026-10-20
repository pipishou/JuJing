# -*- coding: utf-8 -*-
"""场景卡引擎（B18/B19）：SceneSpec v1 契约、种子加载、校验、发现草稿。

设计文档：JuJing/docs/项目文档/02-产品方案/经营模型整合与场景化决策卡设计_2026-10-20.md §4.5
  - 场景卡是 report 的**呈现层视图**，report 仍是唯一事实源；
  - 阈值 / ROI / 不可逆动作 / 动作表 / 指标目录一律引用 pack（行业包），
    spec 内不写死数值常量（只允许 "pack.<dotted.key>" 引用，或与 pack 已登记值相等的数值）；
  - validate(spec, pack, data_manifest=None) -> list[str]：六类拒绝（人话原因）；
  - load_seeds(pack) 读 packs/<pack_id>/scenes/*.json（preset 三基线），返回
   {"scenes":[合法...], "rejected":[{file, scene_id?, reasons:[...]}]}（lead 契约 m01386）；
  - discover(data_manifest, records) 用 stdlib statistics 扫 μ±2σ/环比/空值率，产出草稿（B19）。
只用 stdlib。
"""
import json
import re
import statistics
from pathlib import Path

SPEC_VERSION = "scene.v1"
ORIGINS = ("preset", "user_prompt", "discovered")
CONTROL_METHODS = ("mean_2sigma", "target", "cusum")
DIRECTIONS = ("higher_worse", "higher_better", "lower_worse", "lower_better", "neutral")
GRAINS = ("daily", "weekly", "monthly")
SCENE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]{2,95}$")
OUTPUT_CONTRACT = "delivery-card.schema"
DEFAULT_DIMS_REF = "pack.scorecard_six_dim"
HARD_GUARDRAILS_REF = "pack.scorecard_hard_guardrails"
_ACTION_COST_NUMERIC_KEYS = ("cost", "cost_value", "cost_yuan", "price", "amount", "roi")


class SceneError(RuntimeError):
    """场景卡缺失/损坏/无法落盘。"""


# --------------------------------------------------------------------------- #
# pack 引用解析
# --------------------------------------------------------------------------- #
def _pack_anchor(pack):
    """pack 里用于定位 scenes/ 目录的锚点：_pack_path 优先，其次默认包路径。"""
    path = (pack or {}).get("_pack_path")
    if path:
        return Path(path)
    from core import pack as pack_mod  # noqa: PLC0415

    return pack_mod.pack_path(str((pack or {}).get("pack_id") or "manufacturing"))


def _resolve_dotted(pack, dotted):
    node = pack
    for part in str(dotted).split("."):
        if not isinstance(node, dict) or part not in node:
            return _MISSING
        node = node[part]
    return node


class _Missing:
    def __repr__(self):
        return "<missing>"


_MISSING = _Missing()


def resolve_ref(pack, value):
    """解析 "pack.<dotted.key>" 引用；不是引用字符串则原样返回。"""
    if isinstance(value, str) and value.strip().startswith("pack."):
        return _resolve_dotted(pack, value.strip()[5:])
    return value


def _numeric_thresholds(pack):
    values = []
    for section in ("thresholds", "scene_guardrails"):
        doc = (pack or {}).get(section)
        if isinstance(doc, dict):
            for v in doc.values():
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    values.append(float(v))
    return values


def _action_table(pack):
    table = {}
    for item in (pack or {}).get("actions") or []:
        if isinstance(item, dict) and item.get("id"):
            table[str(item["id"])] = item
    return table


def _factor_ids(pack):
    return [str(x) for x in (pack or {}).get("factors") or []]


def _metric_table(pack):
    table = {}
    for item in (pack or {}).get("metrics") or []:
        if isinstance(item, dict) and item.get("id"):
            table[str(item["id"])] = item
    return table


def _manifest_fields(data_manifest):
    """从 data_manifest 提取可用字段名（兼容 field_contract/fields/records）。"""
    fields = set()
    stack = [data_manifest]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            for key in ("fields", "field_contract", "columns"):
                val = node.get(key)
                if isinstance(val, list):
                    fields.update(str(x) for x in val)
                elif isinstance(val, dict):
                    fields.update(str(x) for x in val.keys())
            for key in ("data_fields", "records", "files", "datasets"):
                val = node.get(key)
                if isinstance(val, (dict, list)):
                    stack.append(val)
        elif isinstance(node, list):
            for item in node:
                if isinstance(item, (dict, list)):
                    stack.append(item)
    return fields


def data_contract_fields(pack, data_manifest=None):
    """数据契约字段集合：pack.factors ∪ pack.metrics[].field ∪ data_manifest 字段。"""
    fields = set(_factor_ids(pack))
    for meta in _metric_table(pack).values():
        if meta.get("field"):
            fields.add(str(meta["field"]))
    fields.update(_manifest_fields(data_manifest))
    return fields


# --------------------------------------------------------------------------- #
# 种子加载（preset 三基线）
# --------------------------------------------------------------------------- #
def scenes_dir(pack):
    return _pack_anchor(pack).parent / "scenes"


def load_seeds(pack):
    """读 packs/<pack_id>/scenes/*.json；返回 {"scenes":[...], "rejected":[...]}（lead 契约 m01386）。

    - scenes：合法 SceneSpec（validate() 通过，按文件名排序，补 "_path"）
    - rejected：[{file, scene_id?, reasons:[...]}]；解析失败/结构非法/校验不通过一律进这里，
      不混入 scenes（server 决定是否透出，推荐 /api/scenes 只回合法列表）。
    """
    folder = scenes_dir(pack)
    result = {"scenes": [], "rejected": []}
    if not folder.is_dir():
        return result
    for path in sorted(folder.glob("*.json")):
        try:
            spec = json.loads(path.read_text(encoding="utf-8-sig"))
        except Exception as exc:  # noqa: BLE001
            result["rejected"].append({"file": str(path), "reasons": [f"场景种子 JSON 解析失败: {exc}"]})
            continue
        if not isinstance(spec, dict):
            result["rejected"].append({"file": str(path), "reasons": ["场景种子顶层必须是 JSON 对象"]})
            continue
        spec = dict(spec)
        spec["_path"] = str(path)
        reasons = validate(spec, pack)
        if reasons:
            result["rejected"].append({"file": str(path), "scene_id": spec.get("scene_id"), "reasons": reasons})
        else:
            result["scenes"].append(spec)
    return result


def load_seed(scene_id, pack):
    for spec in load_seeds(pack)["scenes"]:
        if spec.get("scene_id") == scene_id:
            return spec
    return None


# --------------------------------------------------------------------------- #
# 校验（六类拒绝，人话原因）
# --------------------------------------------------------------------------- #
def _check_control_line(pack, metric_id, control, reasons):
    method = control.get("method")
    metric_meta = _metric_table(pack).get(metric_id) or {}
    allowed = metric_meta.get("control_line_methods") or CONTROL_METHODS
    if method not in CONTROL_METHODS or method not in allowed:
        reasons.append(f"control_line.method 不在白名单 {list(CONTROL_METHODS)}（收到 {method!r}）")
        return
    params = control.get("params") or {}
    if not isinstance(params, dict):
        reasons.append("control_line.params 必须是对象")
        return
    registered = _numeric_thresholds(pack)
    for key, value in params.items():
        # lead 契约 m01386：只允许「数字字面量」或「pack. 开头的引用串」两种写法。
        if isinstance(value, bool) or value is None:
            reasons.append(
                f"control_line.params.{key} 取值非法：{value!r}"
                f"（只允许数字字面量或以 \"pack.\" 开头的引用串）")
            continue
        if isinstance(value, (int, float)):
            if float(value) not in registered:
                reasons.append(
                    f"control_line.params.{key}={value!r} 属自造阈值：数字字面量必须是 pack 已登记值，"
                    f"或改用 \"pack.<阈值键>\" 引用")
            continue
        if isinstance(value, str):
            if not value.startswith("pack."):
                reasons.append(
                    f"control_line.params.{key}={value!r} 取值非法：只允许数字字面量或以 \"pack.\" 开头的引用串"
                    f"（禁止裸字符串常量 / env 引用）")
                continue
            resolved = resolve_ref(pack, value)
            if resolved is _MISSING:
                reasons.append(f"control_line.params.{key} 引用了不存在的 pack 键：{value!r}")
            elif isinstance(resolved, bool) or not isinstance(resolved, (int, float)):
                reasons.append(
                    f"control_line.params.{key} 引用的 pack 键不是数值阈值：{value!r} -> {resolved!r}")
            continue
        reasons.append(
            f"control_line.params.{key} 取值非法：{value!r}"
            f"（只允许数字字面量或以 \"pack.\" 开头的引用串）")


def validate(spec, pack, data_manifest=None):
    """校验 SceneSpec v1；返回拒绝原因列表（空 = 合法）。

    六类拒绝（设计文档 §4.5.3）：
      1) spec_version 未知
      2) metric.id 不在数据契约（pack.factors/metrics ∪ data_manifest 字段）
      3) 因子引用超出 pack.factors 封闭世界
      4) actions[].id 不在 pack 动作表；reversible=false 未登记 pack.irreversible_actions
      5) control_line.method 不在白名单，或阈值自造
      6) 结构非法（guardrails/scorecard/output_contract 等）
    必填 data_requirements 字段缺失数据时**不拒绝**（降级态），见 degradation_reasons()。
    """
    reasons = []
    if not isinstance(spec, dict):
        return ["SceneSpec 顶层必须是 JSON 对象"]
    if spec.get("spec_version") != SPEC_VERSION:
        reasons.append(f"spec_version 未知：{spec.get('spec_version')!r}（只支持 {SPEC_VERSION}）")
    pack_id = spec.get("pack_id")
    if not pack_id or str(pack_id) != str((pack or {}).get("pack_id")):
        reasons.append(f"pack_id 与当前行业包不一致：{pack_id!r} / {(pack or {}).get('pack_id')!r}")
    scene_id = spec.get("scene_id")
    if not isinstance(scene_id, str) or not SCENE_ID_RE.match(scene_id):
        reasons.append(f"scene_id 非法（需稳定 id，形如 scene-refill-rate-first）：{scene_id!r}")
    if spec.get("origin") not in ORIGINS:
        reasons.append(f"origin 非法：{spec.get('origin')!r}（须为 {list(ORIGINS)}）")
    if not str(spec.get("title") or "").strip():
        reasons.append("title 必填")
    if not str(spec.get("question") or "").strip():
        reasons.append("question 必填")

    # ---- 2) metric + control_line ----
    metric = spec.get("metric")
    if not isinstance(metric, dict) or not metric.get("id"):
        reasons.append("metric 必须是含 id 的对象")
    else:
        metric_id = str(metric["id"])
        contract = data_contract_fields(pack, data_manifest)
        metrics = _metric_table(pack)
        if metric_id not in contract and metric_id not in metrics:
            reasons.append(f"metric.id 不在数据契约：{metric_id!r}")
        if metric.get("direction") not in (None,) and metric.get("direction") not in DIRECTIONS:
            reasons.append(f"metric.direction 非法：{metric.get('direction')!r}")
        if metric.get("grain") not in (None,) and metric.get("grain") not in GRAINS:
            reasons.append(f"metric.grain 非法：{metric.get('grain')!r}")
        control = metric.get("control_line")
        if not isinstance(control, dict):
            reasons.append("metric.control_line 必须是对象")
        else:
            _check_control_line(pack, metric_id, control, reasons)

    # ---- 3) 因子封闭世界 ----
    factors_ref = spec.get("factors_ref")
    known_factors = set(_factor_ids(pack))
    if not isinstance(factors_ref, dict):
        reasons.append("factors_ref 必须是对象（{pack_field:'factors', version:N}）")
    else:
        if factors_ref.get("pack_field") != "factors":
            reasons.append(f"factors_ref.pack_field 非法：{factors_ref.get('pack_field')!r}（只允许 factors）")
        expected_version = (pack or {}).get("factors_version")
        if expected_version is not None and factors_ref.get("version") != expected_version:
            reasons.append(
                f"因子引用超出 pack.factors 封闭世界：factors_ref.version={factors_ref.get('version')!r}"
                f" ≠ pack.factors_version={expected_version!r}")
        for item in spec.get("factors") or []:
            if str(item) not in known_factors:
                reasons.append(f"因子不在 pack.factors 封闭世界：{item!r}")

    # ---- 4) 动作表 + 不可逆登记 ----
    actions = spec.get("actions")
    if not isinstance(actions, list) or not actions:
        reasons.append("actions 必须是非空列表")
        actions = []
    action_table = _action_table(pack)
    irreversible = {str(x) for x in (pack or {}).get("irreversible_actions") or []}
    anchors = (pack or {}).get("roi_anchors") or {}
    for i, action in enumerate(actions):
        if not isinstance(action, dict):
            reasons.append(f"actions[{i}] 必须是对象")
            continue
        aid = str(action.get("id") or "")
        if aid not in action_table:
            reasons.append(f"动作不在 pack 允许动作表：{aid!r}")
            continue
        meta = action_table[aid]
        anchor_key = action.get("cost_anchor_key")
        if anchor_key not in anchors:
            reasons.append(f"actions[{i}].cost_anchor_key 不在 pack.roi_anchors：{anchor_key!r}")
        for key in _ACTION_COST_NUMERIC_KEYS:
            if key in action and isinstance(action[key], (int, float)) and not isinstance(action[key], bool):
                reasons.append(f"actions[{i}].{key} 不得写死数值成本：请改用 cost_anchor_key 引用 pack.roi_anchors")
        reversible = action.get("reversible")
        if not isinstance(reversible, bool):
            reasons.append(f"actions[{i}].reversible 必须是布尔值")
            continue
        if reversible is False and aid not in irreversible:
            reasons.append(f"动作 {aid!r} 标记 reversible=false，但未在 pack.irreversible_actions 登记（不可逆动作必须可追溯）")
        if reversible is True and meta.get("reversible") is False:
            reasons.append(f"动作 {aid!r} 在 pack 中登记为不可逆，spec 不得标 reversible=true")

    # ---- 5) guardrails（引用 pack）----
    guardrails = spec.get("guardrails")
    if not isinstance(guardrails, dict):
        reasons.append("guardrails 必须是对象")
    else:
        max_per_month = resolve_ref(pack, guardrails.get("max_per_month"))
        pack_guard = (pack or {}).get("scene_guardrails") or {}
        if max_per_month is _MISSING:
            reasons.append(f"guardrails.max_per_month 引用了不存在的 pack 键：{guardrails.get('max_per_month')!r}")
        elif isinstance(max_per_month, bool) or not isinstance(max_per_month, int) or max_per_month < 1:
            reasons.append(f"guardrails.max_per_month 必须是正整数：{max_per_month!r}")
        elif pack_guard.get("max_per_month") is not None and max_per_month != pack_guard.get("max_per_month"):
            reasons.append(
                f"guardrails.max_per_month 自造阈值：{max_per_month!r} ≠ pack.scene_guardrails.max_per_month="
                f"{pack_guard.get('max_per_month')!r}")
        hard_rules = guardrails.get("hard_rules") or []
        known_rules = set(pack_guard.get("hard_rules") or [])
        if not isinstance(hard_rules, list):
            reasons.append("guardrails.hard_rules 必须是列表")
        else:
            for rule in hard_rules:
                if known_rules and str(rule) not in known_rules:
                    reasons.append(f"guardrails.hard_rules 含 pack 未登记规则：{rule!r}")

    # ---- data_requirements 结构（缺失数据只在 degradation 里报）----
    reqs = spec.get("data_requirements")
    if not isinstance(reqs, list):
        reasons.append("data_requirements 必须是列表")
    else:
        for i, req in enumerate(reqs):
            if not isinstance(req, dict) or not str(req.get("field") or "").strip():
                reasons.append(f"data_requirements[{i}] 必须是含 field 的对象")
            elif not isinstance(req.get("required"), bool):
                reasons.append(f"data_requirements[{i}].required 必须是布尔值")

    # ---- 6) 呈现/输出契约 ----
    scorecard = spec.get("scorecard")
    if not isinstance(scorecard, dict):
        reasons.append("scorecard 必须是对象")
    else:
        if scorecard.get("scale") != 100:
            reasons.append(f"scorecard.scale 必须为 100（收到 {scorecard.get('scale')!r}）")
        if scorecard.get("dims_ref") != DEFAULT_DIMS_REF:
            reasons.append(f"scorecard.dims_ref 必须引用 {DEFAULT_DIMS_REF}（收到 {scorecard.get('dims_ref')!r}）")
        ref = scorecard.get("hard_guardrails_ref")
        if ref != HARD_GUARDRAILS_REF:
            reasons.append(
                f"scorecard.hard_guardrails_ref 必须引用 {HARD_GUARDRAILS_REF}（收到 {ref!r}；"
                f"设计文档中的 pack.hard_guardrails 为笔误）")
        else:
            resolved = resolve_ref(pack, ref)
            if resolved is _MISSING or not isinstance(resolved, list) or not resolved:
                reasons.append(
                    f"scorecard.hard_guardrails_ref 无法命中 pack 的 scorecard_hard_guardrails 节：{ref!r}")
    if not str(spec.get("output_contract_ref") or "").strip():
        reasons.append("output_contract_ref 必填")
    if not str(spec.get("generated_by") or "").strip():
        reasons.append("generated_by 必填")
    if not isinstance(spec.get("generated_at"), str) or not spec.get("generated_at").strip():
        reasons.append("generated_at 必填（ISO 时间字符串）")
    if not isinstance(spec.get("evidence_refs"), list):
        reasons.append("evidence_refs 必须是列表")
    return reasons


def degradation_reasons(spec, data_manifest=None):
    """必填 data_requirements 字段缺失 → 降级态原因（场景卡只渲染降级态、不生成建议）。"""
    fields = _manifest_fields(data_manifest)
    if not fields:
        return []
    missing = []
    for req in (spec or {}).get("data_requirements") or []:
        if isinstance(req, dict) and req.get("required") is True:
            field = str(req.get("field") or "")
            if field and field not in fields:
                missing.append(field)
    return [f"缺必填数据字段 {name}（场景卡降级：只渲染现状，不生成建议）" for name in missing]


# --------------------------------------------------------------------------- #
# B19：discover —— stdlib statistics 扫 μ±2σ / 环比 / 空值率，产出草稿
# --------------------------------------------------------------------------- #
def _series(records, field):
    values = []
    for row in records:
        if not isinstance(row, dict) or field not in row:
            continue
        value = row.get(field)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            values.append(None)
            continue
        values.append(float(value))
    return values


def _slug(text):
    return re.sub(r"[^A-Za-z0-9.]+", "-", str(text)).strip("-") or "metric"


# 指标方向 → 异常出现在哪一侧（枚举见 DIRECTIONS，5 值口径）：
#   higher_worse  数值越高越坏 → 异常在高位
#   lower_better  数值越高越坏 → 异常在高位（好指标冲高同样异常）
#   lower_worse   数值越低越坏 → 异常在低位
#   higher_better 数值越低越坏 → 异常在低位（好指标掉下来才异常）
#   neutral       无方向 → 只能用偏离幅度判定
_WORSE_HIGH = ("higher_worse", "lower_better")


def direction_of(metric_meta):
    """返回 (direction, worse_high)；worse_high=True 表示数值越高越坏。"""
    direction = str((metric_meta or {}).get("direction") or "").strip()
    if direction not in DIRECTIONS:
        direction = "neutral"
    return direction, direction in _WORSE_HIGH


def _draft_spec(pack, metric_meta, stats, entity_scope, generated_at):
    metric_id = str(metric_meta["id"])
    name = str(metric_meta.get("name") or metric_id)
    direction, worse_high = direction_of(metric_meta)
    if direction == "neutral":
        direction_text = "偏离"
    else:
        direction_text = "上行" if worse_high else "下行"
    return {
        "spec_version": SPEC_VERSION,
        "scene_id": f"scene-{_slug(metric_id)}-discovered",
        "pack_id": (pack or {}).get("pack_id"),
        "version": 1,
        "origin": "discovered",
        "title": f"{name}{direction_text}异常（数据发现）",
        "question": f"{name}最近一个周期相对 {stats['window_days']} 日基线出现 {direction_text}异常，是否进入归因？",
        "metric": {
            "id": metric_id,
            "name": name,
            "unit": metric_meta.get("unit"),
            "grain": metric_meta.get("grain"),
            "direction": direction,
            "current_value": stats["latest"],
            "control_line": {"method": "mean_2sigma",
                             "params": {"window_days": "pack.thresholds.t30_window_days"}},
        },
        "entity_scope": entity_scope,
        "factors_ref": {"pack_field": "factors", "version": (pack or {}).get("factors_version")},
        "actions": [dict(a) for a in (pack or {}).get("actions") or [] if a.get("reversible") is True],
        "guardrails": {"max_per_month": "pack.scene_guardrails.max_per_month",
                       "hard_rules": list(((pack or {}).get("scene_guardrails") or {}).get("hard_rules") or [])},
        "data_requirements": [{"field": metric_meta.get("field") or metric_id,
                               "required": True, "quality": "daily"}],
        "scorecard": {"scale": 100, "dims_ref": DEFAULT_DIMS_REF,
                      "hard_guardrails_ref": "pack.scorecard_hard_guardrails"},
        "output_contract_ref": OUTPUT_CONTRACT,
        "generated_by": "core.scene.discover",
        "generated_at": generated_at,
        "evidence_refs": [],
        "discovery": stats,
    }


def discover(data_manifest, records, pack=None):
    """扫数据产出场景草稿（不落盘）。返回 list[spec_draft]（origin="discovered"）。"""
    if pack is None:
        from core import pack as pack_mod  # noqa: PLC0415

        pack = pack_mod.load()
    if not isinstance(records, list) or not records:
        return []
    generated_at = _now_iso()
    drafts = []
    for metric_meta in _metric_table(pack).values():
        field = str(metric_meta.get("field") or metric_meta.get("id"))
        values = _series(records, field)
        numeric = [v for v in values if v is not None]
        if len(numeric) < 4:
            continue
        mean = statistics.fmean(numeric)
        sigma = statistics.pstdev(numeric)
        latest = numeric[-1]
        previous = numeric[-2]
        delta_ratio = (latest - previous) / previous if previous else 0.0
        null_rate = round((len(values) - len(numeric)) / len(values), 4) if values else 0.0
        direction, worse_high = direction_of(metric_meta)
        if direction == "neutral":
            anomaly = abs(latest - mean) > 2 * sigma
        else:
            anomaly = (latest > mean + 2 * sigma) if worse_high else (latest < mean - 2 * sigma)
        null_threshold = float(((pack or {}).get("thresholds") or {}).get("cell_null_rate_manual") or 0)
        flagged = bool(anomaly or abs(delta_ratio) >= 0.10 or (null_threshold and null_rate > null_threshold))
        if not flagged:
            continue
        stats = {
            "metric_id": str(metric_meta["id"]),
            "field": field,
            "window_days": len(values),
            "mean": round(mean, 4),
            "sigma": round(sigma, 4),
            "latest": round(latest, 4),
            "delta_ratio": round(delta_ratio, 4),
            "null_rate": null_rate,
            "anomaly": "over_2sigma" if anomaly else ("delta" if abs(delta_ratio) >= 0.10 else "null_rate"),
        }
        entity = _entity_scope(records)
        drafts.append(_draft_spec(pack, metric_meta, stats, entity, generated_at))
    return drafts


def _entity_scope(records):
    values = []
    for row in records:
        if isinstance(row, dict) and row.get("production_line") not in (None, ""):
            value = str(row["production_line"])
            if value not in values:
                values.append(value)
    if not values:
        return {}
    return {"field": "production_line", "values": sorted(values)}


def _now_iso():
    from datetime import datetime

    return datetime.now().astimezone().isoformat(timespec="seconds")


# --------------------------------------------------------------------------- #
# 任务运行落盘：runs/<task_id>/scene.json + sha256（server 契约，B18）
# --------------------------------------------------------------------------- #
def write_task_scene(task_dir, spec, name="scene.json"):
    """把场景卡实例原子写入 task_dir，返回 {path, sha256, size}（复用 core.provenance）。"""
    from core import fsutil, provenance  # noqa: PLC0415

    target = Path(task_dir) / name
    fsutil.atomic_write_json(target, spec)
    return {
        "path": str(target),
        "sha256": provenance.sha256_file(target),
        "size": target.stat().st_size,
    }


if __name__ == "__main__":
    import sys

    # CLI 直跑（python core/scene.py ...）时补上仓库根，保证 `from core import ...` 可用
    _ROOT = str(Path(__file__).resolve().parent.parent)
    if _ROOT not in sys.path:
        sys.path.insert(0, _ROOT)
    pack_doc = None
    if len(sys.argv) > 1 and sys.argv[1] == "seeds":
        from core import pack as pack_mod

        pack_doc = pack_mod.load()
        document = load_seeds(pack_doc)
        print(json.dumps({
            "scenes": [{k: v for k, v in s.items() if k != "_path"} for s in document["scenes"]],
            "rejected": document["rejected"],
        }, ensure_ascii=False, indent=2))
    elif len(sys.argv) > 1 and sys.argv[1] == "validate":
        from core import pack as pack_mod

        pack_doc = pack_mod.load()
        spec = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8-sig"))
        reasons = validate(spec, pack_doc)
        print(json.dumps({"ok": not reasons, "reasons": reasons}, ensure_ascii=False, indent=2))
    else:
        print("usage: python core/scene.py seeds|validate <spec.json>")
