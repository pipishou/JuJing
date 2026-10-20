# -*- coding: utf-8 -*-
"""P2 决策卡（DEC §3.3）：report 的**呈现层视图**，不新增事实源。

契约（设计文档 §3.3 §3.1）：
  9 个顶层键：decision_id / entity / decided_at / actor / context / attribution /
              recommendation / evidence / sign（+ outcome，T+7 回填后出现）

三条硬约束：
  1. report.json 仍是唯一事实源。本模块只做映射，**不把 DEC 写回 report.json**
     ——否则 sign.report_sha256 与 report.json 整文件 sha256 会形成自指悖论。
  2. 降级态（数据合同 degraded）：卡面只显示降级原因与缺失字段清单，**不出建议**
     （assets/degradation_rules.yaml：降级不是失败，但降级态不得直接给处置建议）。
  3. 卡面禁词：不得出现模型名 / token / 轨迹 等内部词（§3.1）。render_html 的模板
     与本模块产出的键名都遵守这一条。

只用 stdlib。
"""
import json
import statistics
from pathlib import Path

SIX_DIMS = ("data_completeness", "timeliness", "significance", "exclusivity",
            "history", "human_ai_consistency")

DEC_KEYS = ("decision_id", "entity", "decided_at", "actor", "context",
            "attribution", "recommendation", "evidence", "sign")

# §3.1 禁词：模型名 / token / 内部轨迹词
FORBIDDEN_WORDS = ("deepseek", "gpt-", "claude", "chatgpt", "llm", "token",
                   "prompt", "轨迹", "trace")


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #
def _is_empty(value):
    return value is None or value == "" or value == [] or value == {}


def _sha256_file(path: Path):
    import hashlib

    try:
        digest = hashlib.sha256()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return ""


def _read_json(path: Path):
    try:
        with path.open("r", encoding="utf-8-sig") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def resolve_metric(pack_doc, metric_hint):
    """按 id / name / field 匹配 pack 指标目录；匹配不到返回 None。"""
    hint = str(metric_hint or "").strip()
    for meta in (pack_doc or {}).get("metrics") or []:
        if not isinstance(meta, dict):
            continue
        if hint and hint in (str(meta.get("id") or ""), str(meta.get("name") or ""),
                             str(meta.get("field") or "")):
            return meta
    return None


# --------------------------------------------------------------------------- #
# 指标上下文：控制线（μ±2σ）+ 7 日趋势
# --------------------------------------------------------------------------- #
def _records(path: Path):
    doc = _read_json(path)
    if isinstance(doc, dict):
        recs = doc.get("records")
        if isinstance(recs, list):
            return recs
    return []


def _data_path(tdir: Path, name: str) -> Path:
    """取数文件可能落在 outdir 根（JSON 直传）或 outdir/data/（表格上传转换）。"""
    for candidate in (Path(tdir) / name, Path(tdir) / "data" / name):
        if candidate.is_file():
            return candidate
    return Path(tdir) / name


def _series(records, field):
    out = []
    for row in records:
        if not isinstance(row, dict):
            continue
        value = row.get(field)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        out.append((str(row.get("date") or ""), float(value)))
    return out


def metric_context(tdir: Path, metric_meta, root: Path = None):
    """从任务数据算出控制线与 7 日趋势。

    基线取 T30 文件（T-30 窗口，mean_2sigma 的 window_days 见 pack.thresholds），
    当期取 T 文件；trend_7d 为当期的逐日序列。
    """
    field = str((metric_meta or {}).get("field") or (metric_meta or {}).get("id") or "")
    tdir = Path(tdir)
    current = _series(_records(_data_path(tdir, "material_refill_T.json")), field)
    baseline = _series(_records(_data_path(tdir, "material_refill_T30.json")), field)
    base_values = [v for _, v in baseline]
    cur_values = [v for _, v in current]
    control = {
        "method": "mean_2sigma",
        "mean": None,
        "sigma": None,
        "upper_2sigma": None,
        "lower_2sigma": None,
        "window_days": len(base_values),
        "baseline_records": len(base_values),
        "current_records": len(cur_values),
        "source": "T-30 基线 μ±2σ",
    }
    if base_values:
        mean = statistics.fmean(base_values)
        sigma = statistics.pstdev(base_values) if len(base_values) > 1 else 0.0
        control["mean"] = round(mean, 4)
        control["sigma"] = round(sigma, 4)
        control["upper_2sigma"] = round(mean + 2 * sigma, 4)
        control["lower_2sigma"] = round(mean - 2 * sigma, 4)
    trend = [{"date": day, "value": value} for day, value in current[-7:]]
    metric = {
        "id": str((metric_meta or {}).get("id") or field),
        "name": str((metric_meta or {}).get("name") or field),
        "unit": (metric_meta or {}).get("unit"),
        "grain": (metric_meta or {}).get("grain"),
        "direction": (metric_meta or {}).get("direction"),
        "current_value": cur_values[-1] if cur_values else None,
    }
    return metric, control, trend


# --------------------------------------------------------------------------- #
# DEC 组装
# --------------------------------------------------------------------------- #
def _degradation_view(contract):
    contract = contract if isinstance(contract, dict) else {}
    if not contract.get("degraded"):
        return None
    return {
        "level": contract.get("degradation_level"),
        "reasons": list(contract.get("degradation_reasons") or []),
        "missing_fields": list(contract.get("core_missing") or contract.get("missing_factors") or []),
        "null_rate_cell": contract.get("null_rate_cell"),
        "core_cell_missing_rate": contract.get("core_cell_missing_rate"),
        "advice_withheld": True,
        "note": "数据不足，本次不出处置建议；已转人工并写清缺失项。",
    }


def _recommendation_view(report, dle, degraded):
    if degraded:
        return []
    expected = dle.get("expected_result")
    out = []
    for item in report.get("recommendation") or []:
        if not isinstance(item, dict):
            continue
        roi = item.get("roi") if isinstance(item.get("roi"), dict) else {}
        out.append({
            "action": item.get("action"),
            "expected": item.get("expected") or expected,
            "cost": roi.get("cost") if roi else None,
            "roi": roi,
            "irreversibility": item.get("irreversibility"),
        })
    return out


def _evidence_view(report, dle, root: Path = None):
    refs = []
    for ref in report.get("data_refs") or []:
        if isinstance(ref, dict):
            if ref.get("sha256"):
                refs.append({"path": ref.get("path"), "sha256": ref.get("sha256"),
                             "size": ref.get("size")})
            continue
        path = str(ref)
        sha = ""
        if root is not None:
            candidate = Path(path)
            if not candidate.is_absolute():
                candidate = Path(root) / path
            if candidate.is_file():
                sha = _sha256_file(candidate)
        if sha:
            refs.append({"path": path, "sha256": sha})
    return {
        "refs": refs,
        "finding_evidence": [
            {"factor": (item or {}).get("factor"),
             "confidence": (item or {}).get("confidence"),
             "evidence": (item or {}).get("evidence")}
            for item in (report.get("finding") or []) if isinstance(item, dict)
        ],
        "verification_plan": (dle or {}).get("verification_plan"),
    }


def build_card(report, task=None, contract=None, outcome=None, signature=None,
               report_sha256="", metric=None, control_line=None, trend=None,
               root: Path = None):
    """从 report + 任务上下文组装 DEC 决策卡（纯映射，不改 report）。"""
    report = report if isinstance(report, dict) else {}
    task = task if isinstance(task, dict) else {}
    dle = report.get("decision_log_entry") if isinstance(report.get("decision_log_entry"), dict) else {}
    scorecard = report.get("scorecard") if isinstance(report.get("scorecard"), dict) else {}
    degraded = bool((contract or {}).get("degraded"))

    metric = metric if isinstance(metric, dict) else {}
    control_line = control_line if isinstance(control_line, dict) else {}
    trend = list(trend or [])
    signature = signature if isinstance(signature, dict) else {}

    card = {
        "decision_id": str(dle.get("id") or ""),
        "entity": {
            "task_id": report.get("task_id") or task.get("task_id") or "",
            "line": task.get("line") or "",
            "metric_id": metric.get("id"),
            "metric_name": metric.get("name"),
            "scope": task.get("entity_scope") or task.get("scope") or {},
        },
        "decided_at": str(dle.get("date") or report.get("generated_at") or ""),
        "actor": str(dle.get("actor") or "manufacturing-ops-team-dsh"),
        "context": {
            "metric": metric,
            "control_line": control_line,
            "trend_7d": trend,
            "goal": report.get("goal") or "",
            "expected": dle.get("expected_result") or "",
            "degradation": _degradation_view(contract),
        },
        "attribution": {
            "candidates": report.get("finding") or [],
            "cannot_attribute": report.get("cannot_attribute") or [],
            "scorecard": {dim: scorecard.get(dim) for dim in SIX_DIMS},
            "total": scorecard.get("total"),
            "verdict": scorecard.get("verdict"),
            "hard_guardrails": scorecard.get("guardrails") or [],
            "method": dle.get("attribution_method"),
        },
        "recommendation": _recommendation_view(report, dle, degraded),
        "evidence": _evidence_view(report, dle, root),
        "sign": {
            "report_sha256": report_sha256 or "",
            "signer": signature.get("signer"),
            "signed_at": signature.get("signed_at"),
            "status": "signed" if signature.get("report_sha256") else "pending",
        },
    }
    outcome = outcome if isinstance(outcome, dict) else {}
    if outcome:
        card["outcome"] = {
            "observed": outcome.get("observed_outcome"),
            "actual_action": outcome.get("actual_action"),
            "override_reason": outcome.get("override_reason"),
            "action_cost": outcome.get("action_cost"),
            "increment_value": outcome.get("increment_value"),
            "outcome_window": outcome.get("outcome_window"),
            "source": outcome.get("source"),
            "submitted_at": outcome.get("submitted_at"),
        }
    else:
        card["outcome"] = None
    return card


def build_for_run(tdir, pack=None, root=None):
    """从 runs/<task_id> 目录组装 DEC（server 侧唯一入口，只读不写）。

    读：report.json（唯一事实源）/ task.json / data_contract.json /
        outcome.json（T+7 回填）/ 签名文件；
    不写回 report.json —— sign.report_sha256 必须是 report.json 整文件指纹，
    往 report 里写 DEC 会造成自指悖论。
    """
    tdir = Path(tdir)
    report = _read_json(tdir / "report.json")
    if not isinstance(report, dict):
        return None
    task = _read_json(tdir / "task.json")
    task = task if isinstance(task, dict) else {}
    contract = _read_json(tdir / "data_contract.json")
    contract = contract if isinstance(contract, dict) else {}
    outcome = _read_json(tdir / "outcome.json")
    outcome = outcome if isinstance(outcome, dict) else {}
    signature = _read_json(tdir / "report.signature.json")
    if not isinstance(signature, dict):
        signature = _read_json(tdir / "signature.json")
    signature = signature if isinstance(signature, dict) else {}
    pack = pack if isinstance(pack, dict) else {}

    metric_meta = resolve_metric(pack, task.get("metric") or report.get("metric"))
    metric, control, trend = {}, {}, []
    if metric_meta:
        metric, control, trend = metric_context(tdir, metric_meta, root=root)
    return build_card(
        report, task=task, contract=contract, outcome=outcome or None,
        signature=signature,
        report_sha256=_sha256_file(tdir / "report.json"),
        metric=metric, control_line=control, trend=trend, root=root,
    )


# --------------------------------------------------------------------------- #
# 卡面渲染（可打印 / 可转发）：只出卡面内容，不带内部过程信息
# --------------------------------------------------------------------------- #
def _esc(text):
    return (str(text if text is not None else "")
            .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _rows(pairs):
    out = []
    for key, value in pairs:
        if _is_empty(value):
            continue
        out.append(f'<div class="row"><span class="k">{_esc(key)}</span>'
                   f'<span class="v">{_esc(value)}</span></div>')
    return "\n".join(out)


def render_html(card):
    """把 DEC 渲染成可打印的单页 HTML（自带 @media print 样式，无内部过程信息）。"""
    card = card if isinstance(card, dict) else {}
    ctx = card.get("context") or {}
    attr = card.get("attribution") or {}
    metric = ctx.get("metric") or {}
    ctl = ctx.get("control_line") or {}
    sign = card.get("sign") or {}
    degradation = ctx.get("degradation")
    scorecard = attr.get("scorecard") or {}

    blocks = [
        "<h1>决策卡 " + _esc(card.get("decision_id") or "") + "</h1>",
        _rows([
            ("指标", f"{metric.get('name') or ''}（{metric.get('id') or ''}）"),
            ("范围", f"{card.get('entity', {}).get('line') or ''}"),
            ("决策时间", card.get("decided_at")),
            ("决策人", card.get("actor")),
            ("目标", ctx.get("goal")),
        ]),
        "<h2>一、现状与控制线</h2>",
        _rows([
            ("当前值", metric.get("current_value")),
            ("基线均值", ctl.get("mean")),
            ("基线标准差", ctl.get("sigma")),
            ("上控制线 μ+2σ", ctl.get("upper_2sigma")),
            ("下控制线 μ-2σ", ctl.get("lower_2sigma")),
        ]),
    ]
    if degradation:
        blocks.append("<h2>二、数据不足说明</h2>")
        blocks.append("<ul>" + "".join(
            f"<li>{_esc(reason)}</li>" for reason in degradation.get("reasons") or []) + "</ul>")
        blocks.append(_rows([("缺失字段", "、".join(degradation.get("missing_fields") or [])),
                             ("处理", degradation.get("note"))]))
    else:
        blocks.append("<h2>二、归因</h2>")
        candidates = attr.get("candidates") or []
        items = "".join(
            f"<li>{_esc(item.get('factor'))}（置信度 {_esc(item.get('confidence'))}）</li>"
            for item in candidates if isinstance(item, dict))
        blocks.append("<ul>" + (items or "<li>无候选因子</li>") + "</ul>")
        blocks.append(_rows([
            ("不可归因", "；".join(str(x) for x in (attr.get("cannot_attribute") or []))),
            ("评分总分", attr.get("total")),
            ("判定", attr.get("verdict")),
        ]))
    recs = card.get("recommendation") or []
    blocks.append("<h2>三、处置建议</h2>")
    if recs:
        blocks.append("<ol>" + "".join(
            f"<li>{_esc(item.get('action'))}（预期：{_esc(item.get('expected'))}；"
            f"成本：{_esc(item.get('cost'))}；可逆性：{_esc(item.get('irreversibility'))}）</li>"
            for item in recs if isinstance(item, dict)) + "</ol>")
    else:
        blocks.append("<p>本次不出处置建议（数据不足，已转人工并写清缺失项）。</p>")
    blocks.append("<h2>四、签收</h2>")
    blocks.append(_rows([
        ("报告指纹", sign.get("report_sha256")),
        ("签字人", sign.get("signer")),
        ("签字时间", sign.get("signed_at")),
        ("状态", sign.get("status")),
    ]))
    blocks.append(_rows([("数据完整度", scorecard.get("data_completeness"))]))

    return (
        "<!doctype html>\n<html lang=\"zh-CN\">\n<head>\n<meta charset=\"utf-8\">\n"
        "<title>决策卡</title>\n<style>\n"
        "body{font-family:-apple-system,\"PingFang SC\",\"Microsoft YaHei\",sans-serif;"
        "margin:24px;color:#16202c;line-height:1.6}\n"
        "h1{font-size:20px}h2{font-size:15px;margin-top:20px}\n"
        ".row{display:flex;gap:8px;border-bottom:1px solid #e6eaef;padding:5px 0}\n"
        ".k{width:120px;color:#5b6b7c;flex:none}.v{flex:1;word-break:break-all}\n"
        "@media print{body{margin:0}h2{page-break-after:avoid}.row{page-break-inside:avoid}}\n"
        "</style>\n</head>\n<body>\n" + "\n".join(blocks) + "\n</body>\n</html>\n"
    )
