# -*- coding: utf-8 -*-
"""L0 意图层（G1 批次）：用户提问 -> intent.json（意图卡）。

契约（全链路有效性与对话层方案 §3 L0 行 + 断言 A0）：
  - 意图卡四字段：场景候选 / 实体 / 时间窗 / 数据源——status=ok 时必须齐全，
    缺则追问（status=ambiguous），禁止编造。
  - 失败分支显式：意图不清 -> ambiguous（带追问）；场景未命中 ->
    scene_not_matched（列可选场景）；LLM 侧失败 -> llm_unavailable /
    parse_failed——一律不产场景卡，不猜。
  - 本模块只做纯函数（目录/解析/校验）；Octop 网络调用在 core.octop_client，
    L1 接线在 tools/intent_turn.py。

数据源注册（H1：demo 样本视为真实结构，真实工单后只换 data_refs 不改代码）：
  SCENE_DATASETS 把 scene_id 映射到数据集名；DEMO_DATASETS 把数据集名映射到
  T/T30 records 文件。新场景接入 = 注册表加一行，不改解析逻辑。
"""
import json
from datetime import datetime
from pathlib import Path

INTENT_VERSION = "intent.v1"

STATUS_OK = "ok"
STATUS_AMBIGUOUS = "ambiguous"
STATUS_SCENE_NOT_MATCHED = "scene_not_matched"
STATUS_PARSE_FAILED = "parse_failed"
STATUS_LLM_UNAVAILABLE = "llm_unavailable"
STATUS_DATA_INSUFFICIENT = "data_insufficient"

ROOT = Path(__file__).resolve().parent.parent

# scene_id -> 数据集名（G1 只注册 demo 已有的首发场景）
SCENE_DATASETS = {
    "scene-refill-rate-first": "material_refill",
}

# 数据集名 -> records 文件（相对 bot-team 根；T=当期窗，T30=基线窗）
DEMO_DATASETS = {
    "material_refill": {
        "current": "demo/material_refill_T.json",
        "baseline": "demo/material_refill_T30.json",
        "grain": "daily",
    },
}


def load_catalog(pack):
    """场景注册表：pack 种子卡 -> [{scene_id,title,question,metric,entity_scope}]。"""
    from core import scene as scene_mod

    seeds = scene_mod.load_seeds(pack)
    catalog = []
    for spec in seeds["scenes"]:
        metric = spec.get("metric") if isinstance(spec.get("metric"), dict) else {}
        catalog.append({
            "scene_id": str(spec.get("scene_id") or ""),
            "title": str(spec.get("title") or ""),
            "question": str(spec.get("question") or ""),
            "metric_id": str(metric.get("id") or ""),
            "metric_name": str(metric.get("name") or ""),
            "entity_scope": spec.get("entity_scope") if isinstance(spec.get("entity_scope"), dict) else {},
        })
    return catalog


def build_catalog_text(catalog):
    """给解析 agent 的场景清单紧凑 JSON（prompt 用，控制 token）。"""
    slim = [{"scene_id": c["scene_id"], "title": c["title"],
             "question": c["question"], "metric": c["metric_name"]} for c in catalog]
    return json.dumps(slim, ensure_ascii=False, separators=(",", ":"))


def catalog_by_id(catalog, scene_id):
    sid = str(scene_id or "").strip()
    for item in catalog:
        if item["scene_id"] == sid:
            return item
    return None


def parse_llm_json(text):
    """从 LLM 输出抠第一个 JSON 对象；没有/解析失败返回 None（不抛）。"""
    text = str(text or "")
    for i, ch in enumerate(text):
        if ch != "{":
            continue
        try:
            obj, _ = json.JSONDecoder().raw_decode(text[i:])
        except ValueError:
            continue
        if isinstance(obj, dict):
            return obj
    return None


def now_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _now_iso():
    return now_iso()


def _scene_options(catalog):
    return [{"scene_id": c["scene_id"], "title": c["title"], "question": c["question"]}
            for c in catalog]


def resolve_intent(question, llm_text, catalog):
    """LLM 解析输出 + 场景目录 -> intent.v1 文档（A0 全部分支，纯函数）。

    分支优先级：解析失败 > 追问(ambiguous) > 场景未命中 > 实体缺失 > ok。
    四字段只在 status=ok 时齐全；其余状态一律缺卡，只带人话原因。
    """
    base = {
        "spec_version": INTENT_VERSION,
        "question": str(question or ""),
        "generated_at": _now_iso(),
        "generated_by": "core.intent.resolve_intent",
    }
    doc = parse_llm_json(llm_text)
    if doc is None:
        return {**base, "status": STATUS_PARSE_FAILED,
                "reason": "解析器输出不是合法 JSON，已丢弃（禁编造）",
                "raw_excerpt": str(llm_text or "")[:500]}

    if doc.get("ambiguous"):
        clarify = str(doc.get("clarify") or "").strip()
        return {**base, "status": STATUS_AMBIGUOUS,
                "clarify_question": clarify or "请说明要查的场景和对象。",
                "scene_candidates": _scene_options(catalog)}

    scene_id = str(doc.get("scene_id") or "").strip()
    seed = catalog_by_id(catalog, scene_id)
    if doc.get("scene_not_matched") or not scene_id or seed is None:
        return {**base, "status": STATUS_SCENE_NOT_MATCHED,
                "reason": "问题未命中已注册场景，禁止编造",
                "options": _scene_options(catalog)}

    entities = doc.get("entities") if isinstance(doc.get("entities"), dict) else {}
    production_line = str(entities.get("production_line") or "").strip()
    if not production_line:
        return {**base, "status": STATUS_AMBIGUOUS,
                "scene_candidates": [{"scene_id": seed["scene_id"], "title": seed["title"]}],
                "clarify_question": f"要查「{seed['title']}」，请指明对象（如 B 线）。"}

    window = str(doc.get("time_window") or "").strip().upper()
    if window not in ("T7", "T30"):
        return {**base, "status": STATUS_AMBIGUOUS,
                "scene_candidates": [{"scene_id": seed["scene_id"], "title": seed["title"]}],
                "clarify_question": "时间窗要说清楚：近一周（T7）还是近一个月（T30）？"}

    dataset = SCENE_DATASETS.get(scene_id)
    data_refs = DEMO_DATASETS.get(dataset)
    if not dataset or not data_refs:
        return {**base, "status": STATUS_SCENE_NOT_MATCHED,
                "reason": f"场景 {scene_id} 未注册数据源，禁止编造",
                "options": _scene_options(catalog)}

    return {
        **base,
        "status": STATUS_OK,
        "scene_candidates": [{"scene_id": seed["scene_id"], "title": seed["title"],
                              "metric_id": seed["metric_id"], "metric_name": seed["metric_name"]}],
        "scene_id": scene_id,
        "entities": {"production_line": production_line},
        "time_window": {"kind": window, "grain": data_refs["grain"]},
        "data_source": {"dataset": dataset,
                        "current": data_refs["current"],
                        "baseline": data_refs["baseline"]},
    }


def check_a0(intent):
    """断言 A0 机检：status=ok 时四字段必须齐全；返回违规原因列表（空=通过）。"""
    reasons = []
    if not isinstance(intent, dict):
        return ["intent 不是对象"]
    if intent.get("status") == STATUS_OK:
        if not intent.get("scene_candidates"):
            reasons.append("A0: 场景候选为空")
        if not intent.get("scene_id"):
            reasons.append("A0: 缺 scene_id")
        entities = intent.get("entities")
        if not isinstance(entities, dict) or not str(entities.get("production_line") or "").strip():
            reasons.append("A0: 缺实体（production_line）")
        window = intent.get("time_window")
        if not isinstance(window, dict) or window.get("kind") not in ("T7", "T30"):
            reasons.append("A0: 缺合法时间窗")
        source = intent.get("data_source")
        if not isinstance(source, dict) or not source.get("dataset"):
            reasons.append("A0: 缺数据源")
    else:
        if not str(intent.get("reason") or intent.get("clarify_question") or "").strip():
            reasons.append(f"A0: 非法状态 {intent.get('status')!r} 必须带人话原因或追问")
        if intent.get("status") not in (STATUS_AMBIGUOUS, STATUS_SCENE_NOT_MATCHED,
                                        STATUS_PARSE_FAILED, STATUS_LLM_UNAVAILABLE,
                                        STATUS_DATA_INSUFFICIENT):
            reasons.append(f"A0: 未知状态 {intent.get('status')!r}")
    return reasons


def load_records(rel_path, root=None):
    """读 records JSON（demo 或 ingest 产物），返回 (meta, records)。路径越界拒绝。"""
    base = Path(root or ROOT).resolve()
    target = (base / rel_path).resolve()
    if base not in target.parents:
        raise ValueError(f"数据路径越界: {rel_path}")
    doc = json.loads(target.read_text(encoding="utf-8-sig"))
    if not isinstance(doc, dict):
        raise ValueError(f"数据文件顶层必须是对象: {rel_path}")
    records = doc.get("records")
    if not isinstance(records, list):
        raise ValueError(f"数据文件缺 records 数组: {rel_path}")
    return {k: v for k, v in doc.items() if k != "records"}, records


def usable_fields(records):
    """实际可用字段：至少一条记录中键存在且值非 None（A1 看数据实况，不看纸面契约）。"""
    fields = set()
    for row in records or []:
        if not isinstance(row, dict):
            continue
        for key, value in row.items():
            if value is not None:
                fields.add(str(key))
    return fields
