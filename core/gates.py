# -*- coding: utf-8 -*-
"""可信门（§2.3）：真实报告门 + 签字收口门（A2/A3 的机器判据）。

一、validate_real_report(report, base)
    real 报告不允许：demo/占位证据、缺证据、data_refs 不存在或 hash 不符、
    scorecard.total/verdict 缺失或与独立计算不一致。
二、check_finalize(decision, report, signature, allowlist,
                   gate_confirmed=False) -> list[str]
    空列表 = 可以 approved；任何一条 = 必须拒绝。
    先做 report_shape_errors 形状校验：畸形结构（顶层非 dict / security_gate 非
    dict / decision_log_entry 非 dict / recommendation 非 list 等）直接 fail-closed。
    报告只要「声称需要 securityGate」（security_gate_requested / 自报
    security_gate / 不可逆建议），就必须由 CLI 侧校验过 gate.json 后带
    gate_confirmed=True 传入；否则一律 fail-closed——不得仅凭报告自证放行。
    task-13 起移除 allow_legacy_gate 死开关（demo 链本就无条件拒绝，开关无法
    真实生效，已删除，不留绕过口）。
三、finalize 另需在 CLI 侧核对 signature.report_sha256 == sha256(report 文件)，
   这里通过 report["_sha256"]（CLI 注入）比对。
只依赖 stdlib。
"""
import json
import re
from pathlib import Path

from . import provenance, report_contract

PLACEHOLDER_TOKENS = ("demo 占位", "demo占位", "占位", "placeholder", "…", "待补充", "待核实")
# F4（G2.5 判例 J-04/K-06）：代数占位。`2.1X`/`3Y` 这类「数字+未知量」不是正式占位词，
# 却在报告里冒充具体数值；单靠 token 表永远漏，按形状兜住。
_PLACEHOLDER_VALUE_RE = re.compile(r"(?<![A-Za-z0-9])\d+(?:\.\d+)?\s*[XYZ](?![A-Za-z0-9])")

# F5：证据档位。real 门只收实测/混合；纯推理档（advisory 推演、模型自答）不得走 real。
EVIDENCE_TIERS = ("measured", "mixed", "inference_only")

# 零宽字符（ZWSP/ZWNJ/ZWJ/WORD JOINER/BOM/SOFT HYPHEN/MONGOLIAN VOWEL SEPARATOR）：
# 用来伪装「占\u200b位」绕过占位检测，判据前必须先剔掉（M1 遗留硬化）
ZERO_WIDTH_CHARS = "\u200b\u200c\u200d\u2060\ufeff\u00ad\u180e"

DECISION_LOG_REQUIRED = ("id", "actor", "context", "advice", "expected_result", "verification_plan")


def strip_zero_width(text):
    """剔除零宽字符（按码点删除，不改变其余字符）。"""
    return str(text).translate({ord(ch): None for ch in ZERO_WIDTH_CHARS})


class GateError(RuntimeError):
    """可信门拒绝。"""


def is_placeholder_evidence(text):
    """文本是否含占位标记（demo 占位/占位/placeholder/省略号/待补充/待核实）。

    零宽字符先剔除再判，避免「占\\u200b位」这类伪装绕过（M1 遗留硬化）。
    """
    if text is None:
        return True
    s = strip_zero_width(text).strip()
    if not s:
        return True
    low = s.lower()
    if any(tok.lower() in low for tok in PLACEHOLDER_TOKENS):
        return True
    return bool(_PLACEHOLDER_VALUE_RE.search(s))


def _iter_strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _iter_strings(item)
    elif isinstance(value, dict):
        for item in value.values():
            yield from _iter_strings(item)


def scorecard_errors(sc, require_total=True):
    """六维评分卡判据（attribution 强制 total/verdict 与独立计算一致；自评卡只验分值）。"""
    errors = []
    for dim in report_contract.SIX_DIMS:
        value = sc.get(dim)
        if isinstance(value, dict):
            score = value.get("score")
            evidence = value.get("evidence")
            if not isinstance(score, (int, float)) or isinstance(score, bool):
                errors.append(f"scorecard.{dim} 分数必须是数值 0-5，收到 {score!r}")
            else:
                try:
                    report_contract.to_score(score)
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"scorecard.{dim}: {exc}")
            if is_placeholder_evidence(evidence):
                errors.append(f"scorecard.{dim}.evidence 含占位/缺证据: {str(evidence)[:40]!r}")
        else:
            try:
                report_contract.to_score(value)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"scorecard.{dim}: {exc}")
    if require_total:
        try:
            errors.extend(report_contract.consistency_errors(sc))
        except Exception as exc:  # noqa: BLE001
            errors.append(f"scorecard 校验异常: {exc}")
    return errors


def _nonempty_text(value):
    return isinstance(value, str) and value.strip() != ""


def content_plan_errors(content_plan):
    """content 卡强制字段：投稿计划（C-1 契约，选题判断/结构/标题/渠道/复盘/标签）。"""
    if not isinstance(content_plan, dict):
        return ["content 卡缺少 content_plan（对象）：选题判断/结构/标题/渠道/复盘/标签必须齐全"]
    errors = []
    if not _nonempty_text(content_plan.get("angle")):
        errors.append("content_plan.angle 必须是非空字符串（选题判断）")
    structure = content_plan.get("structure")
    if not isinstance(structure, list) or not structure:
        errors.append("content_plan.structure 必须是非空 list（逐段用途）")
    titles = content_plan.get("titles")
    if not isinstance(titles, list):
        errors.append("content_plan.titles 必须是 list（5–10 条标题候选）")
    else:
        bad = [i for i, t in enumerate(titles) if not _nonempty_text(t)]
        if bad:
            errors.append(f"content_plan.titles[{bad[0]}] 必须是非空字符串（标题候选）")
        if not (5 <= len(titles) <= 10):
            errors.append(f"content_plan.titles 需 5–10 条，收到 {len(titles)} 条")
    if not _nonempty_text(content_plan.get("channel")):
        errors.append("content_plan.channel 必须是非空字符串（渠道+首帧）")
    metrics = content_plan.get("review_metrics")
    if not isinstance(metrics, list) or not metrics:
        errors.append("content_plan.review_metrics 必须是非空 list（48h 复盘口径）")
    labels = content_plan.get("labels")
    if not isinstance(labels, list) or not labels:
        errors.append("content_plan.labels 必须是非空 list（标签）")
    return errors


def _observed_ok(value):
    """T+7 实测值是否已填（0 是有效值；空串/占位/空容器不算）。"""
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return True
    if isinstance(value, str):
        return value.strip() != "" and not is_placeholder_evidence(value)
    if isinstance(value, (list, dict, tuple)):
        return len(value) > 0
    return False


def review_card_errors(report):
    """review 卡强制字段：复盘必须含非空 observed_outcome（review.* 或顶层 outcome.*）。

    注意：这是「回看卡」的字段契约，与人工复核放行（review_errors / review.json）不是一回事。
    """
    observed = None
    review = report.get("review")
    if isinstance(review, dict):
        observed = review.get("observed_outcome")
    if not _observed_ok(observed):
        outcome = report.get("outcome")
        if isinstance(outcome, dict):
            observed = outcome.get("observed_outcome")
    if _observed_ok(observed):
        return []
    return ["review 卡缺少非空 observed_outcome（review.observed_outcome 或顶层 outcome.observed_outcome，T+7 实测）"]


def _load_pack_doc(report, base):
    """F13：pack 允许 dict（{"pack_id":...}）或 str（"manufacturing"）；解析失败 raise（fail-closed）。"""
    p = report.get("pack")
    pid = p.get("pack_id") if isinstance(p, dict) else (p if isinstance(p, str) else None)
    pid = str(pid).strip() if pid else ""
    if not pid:
        raise ValueError("pack 缺失或 pack_id 为空：无法确定不可逆动作表（fail-closed）")
    path = Path(base) / "packs" / pid / "pack.json"
    if not path.is_file():
        # 隔离 runs 根（临时目录）里不会有 packs/：包是随仓库的资产，回落到仓库根
        alt = Path(__file__).resolve().parents[1] / "packs" / pid / "pack.json"
        if alt.is_file():
            path = alt
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"pack={pid!r} 无法加载（{exc}）") from exc


WEEKLY_METRIC_KEYS = ("reminders_issued", "judged_ok", "judged_not_ok", "items_changed", "overdue_now")


def weekly_errors(report, base=None):
    """周汇总卡（card_kind=weekly）的机器判据：周期、计数、样本口径、证据可溯源。"""
    errors = []
    period = report.get("period") or {}
    start, end = str(period.get("start") or ""), str(period.get("end") or "")
    if not start or not end:
        errors.append("weekly 卡缺 period.start/period.end")
    else:
        try:
            d1, d2 = __import__("datetime").date.fromisoformat(start[:10]), __import__("datetime").date.fromisoformat(end[:10])
            if d2 < d1:
                errors.append("weekly 卡 period.end 早于 period.start")
        except ValueError:
            errors.append("weekly 卡 period 必须是 ISO 日期")
    metrics = report.get("metrics")
    if not isinstance(metrics, dict):
        errors.append("weekly 卡缺 metrics 对象")
    else:
        for key in WEEKLY_METRIC_KEYS:
            if key not in metrics:
                errors.append(f"weekly.metrics 缺 {key}")
            elif not isinstance(metrics[key], int) or metrics[key] < 0:
                errors.append(f"weekly.metrics.{key} 必须是非负整数")
        rate = metrics.get("hit_rate")
        judged = int(metrics.get("judged_ok") or 0) + int(metrics.get("judged_not_ok") or 0)
        if rate is not None:
            if judged < 8:
                errors.append(f"weekly.metrics.hit_rate 在样本 <8 时不得给数（当前 {judged}）")
            elif not (0.0 <= float(rate) <= 1.0):
                errors.append("weekly.metrics.hit_rate 必须在 0–1 之间")
    if not (report.get("decision_log_entry") or {}).get("actor"):
        errors.append("weekly 卡缺 decision_log_entry.actor")
    refs = report.get("data_refs") or []
    if not refs:
        errors.append("weekly 卡 data_refs 不能为空（证据必须可溯源）")
    elif base is not None:
        import hashlib
        import pathlib as _p
        root = _p.Path(base)
        for i, ref in enumerate(refs):
            if not isinstance(ref, dict) or not ref.get("path"):
                errors.append(f"weekly.data_refs[{i}] 必须是 {{path, sha256}} 对象")
                continue
            fp = root / str(ref["path"])
            if not fp.exists():
                errors.append(f"weekly.data_refs[{i}] 文件不存在: {ref['path']}")
                continue
            if ref.get("sha256"):
                actual = hashlib.sha256(fp.read_bytes()).hexdigest()
                if actual != ref["sha256"]:
                    errors.append(f"weekly.data_refs[{i}] sha256 不一致: {ref['path']}")
    return errors


def real_report_errors(report, base):
    """返回真实报告必须满足的判据错误列表（空=通过）。"""
    errors = []
    if not isinstance(report, dict):
        return ["report 必须是 JSON 对象"]
    # 周汇总卡：走自己的判据，不套归因卡的 finding/cannot_attribute/ROI 形状
    try:
        _kind = report_contract.card_kind(report)
    except (ValueError, AttributeError):
        _kind = None
    if _kind == "weekly":
        if report.get("is_demo") is not False:
            errors.append("real 报告 is_demo 必须为 false")
        if not report.get("task_id"):
            errors.append("real 报告 task_id 不能为空")
        if not report.get("goal"):
            errors.append("weekly 卡缺 goal")
        return errors + weekly_errors(report, base)
    # C-1：卡型决定强制面。未知 card_kind 只报错、不静默按 attribution 处理。
    try:
        kind = report_contract.card_kind(report)
    except ValueError as exc:
        errors.append(str(exc))
        kind = None
    if report.get("is_demo") is not False:
        errors.append("real 报告 is_demo 必须为 false")
    # F5：纯推理档不得走 real 门（legacy 报告无该字段时放行，避免历史批次全红）
    tier = report.get("evidence_tier")
    if tier is not None and tier not in ("measured", "mixed"):
        errors.append(f"real 报告 evidence_tier={tier!r}：纯推理/演示证据不得走 real 门（须 measured/mixed）")
    if not report.get("task_id"):
        errors.append("real 报告 task_id 不能为空")

    finding = report.get("finding") or []
    if not isinstance(finding, list) or not (0 <= len(finding) <= 2):
        errors.append(f"finding 必须是 0-2 条，收到 {type(finding).__name__}/{len(finding) if isinstance(finding, list) else '?'}")
    else:
        for i, f in enumerate(finding):
            if not isinstance(f, dict):
                errors.append(f"finding[{i}] 必须是对象")
                continue
            if not f.get("factor"):
                errors.append(f"finding[{i}] 缺 factor")
            if f.get("confidence") in (None, ""):
                errors.append(f"finding[{i}] 缺 confidence")
            else:
                try:
                    _conf = float(str(f.get("confidence")).strip())
                except ValueError:
                    _conf = None
                if report.get("evidence_tier") == "inference_only" and _conf is not None and _conf > 0.40:
                    errors.append(f"finding[{i}] confidence={_conf:.2f} 超出 inference_only 档上限 0.40（纯推理证据不得给实测级置信度）")
            ev = f.get("evidence") or []
            if not ev:
                errors.append(f"finding[{i}] 缺证据 evidence")
            else:
                # 2026-11-04 修：整串只有空白/换行的条目是排版残留（markdown 列表的 `- `），
                # 不是占位。先过滤掉再判，避免把真实中文证据判成「空证据」；
                # 过滤后若一条有效证据都没有，仍按缺证据拒绝（不放宽判定面）。
                items = []
                for j, item in enumerate(ev):
                    if isinstance(item, str) and not strip_zero_width(item).strip():
                        continue
                    items.append((j, item))
                if not items:
                    errors.append(f"finding[{i}] 缺有效证据 evidence（条目均为空白）")
                for j, item in items:
                    if is_placeholder_evidence(item):
                        errors.append(f"finding[{i}].evidence[{j}] 含占位/空证据: {str(item)[:40]!r}")

    ca = report.get("cannot_attribute") or []
    if not isinstance(ca, list) or not ca:
        errors.append("cannot_attribute 不能为空")

    sc = report.get("scorecard")
    if kind == "attribution":
        if not isinstance(sc, dict):
            errors.append("scorecard 必须是对象")
        else:
            errors.extend(scorecard_errors(sc, require_total=True))
    elif isinstance(sc, dict) and all(dim in sc for dim in report_contract.SIX_DIMS):
        # 非 attribution 卡允许自评：给了完整六维就验分值，不强制 total/verdict
        errors.extend(scorecard_errors(sc, require_total=False))

    rec = report.get("recommendation") or []
    if not isinstance(rec, list) or not rec:
        errors.append("recommendation 不能为空")
    else:
        for i, item in enumerate(rec):
            if not isinstance(item, dict):
                errors.append(f"recommendation[{i}] 必须是对象")
                continue
            if not item.get("action"):
                errors.append(f"recommendation[{i}] 缺 action")
            roi = item.get("roi") or {}
            for key in ("cost", "avoided", "anchor"):
                if is_placeholder_evidence(roi.get(key)):
                    errors.append(f"recommendation[{i}].roi.{key} 含占位/空值: {str(roi.get(key))[:40]!r}")
            if item.get("irreversibility") not in ("reversible", "irreversible"):
                errors.append(f"recommendation[{i}].irreversibility 非法: {item.get('irreversibility')!r}")
            if item.get("irreversibility") == "irreversible":
                # F2：不可逆标注必须与行业包动作表一致，防「挂错对象」骗过安全门
                try:
                    _pk = _load_pack_doc(report, base)
                except ValueError as _exc:
                    errors.append(f"recommendation[{i}] {_exc}")
                    _pk = None
                if _pk is not None:
                    from .pack import is_irreversible as _is_irr
                    if not _is_irr(_pk, item.get("action") or ""):
                        errors.append(f"recommendation[{i}].irreversibility=irreversible 但 action 不在 pack.irreversible_actions（不可逆标注错挂）：{str(item.get('action'))[:40]!r}")

    # F8：裁决三字段必填——不得靠「字段不存在」跳过整段门
    for _k, _ok, _why in (
        ("verdict_one_line", isinstance(report.get("verdict_one_line"), str) and 0 < len(report.get("verdict_one_line") or "") <= 30, "非空且≤30字"),
        ("sign_scope", report.get("sign_scope") in ("approve", "no_stop", "approve_B1", "approve_B2", "other"), "枚举 approve/no_stop/…"),
        ("evidence_tier", report.get("evidence_tier") in ("measured", "mixed", "inference_only"), "枚举 measured/mixed/inference_only"),
    ):
        if not _ok:
            errors.append(f"{_k} 缺失或非法（{_why}）：交付层裁决字段不得跳过")

    dl = report.get("decision_log_entry") or {}

    # F2（G2.5 判例 J-04）：不可逆动作必须被点名——gate 存在时 action 必须在 pack 不可逆表里
    _sg = report.get("security_gate")
    if isinstance(_sg, dict) and (_sg.get("status") or _sg.get("confirmed_by") or _sg.get("action")):
        _act = str(_sg.get("action") or "").strip()
        if not _act:
            errors.append("security_gate 缺 action：安全门必须绑定到具体不可逆动作（不得只写状态）")
        else:
            try:
                _pk2 = _load_pack_doc(report, base)
            except ValueError as _exc2:
                errors.append(f"security_gate {_exc2}")
                _pk2 = None
            if _pk2 is not None:
                from .pack import is_irreversible as _is_irr2
                if not _is_irr2(_pk2, _act):
                    errors.append(f"security_gate.action={_act!r} 不是 pack 不可逆动作（安全门挂错对象）")

    # C-1：review/content 卡各自的强制面（六维只在 attribution 强制，见上）
    if kind == "content":
        errors.extend(content_plan_errors(report.get("content_plan")))
    elif kind == "review":
        errors.extend(review_card_errors(report))

    # data_refs 可能在顶层（§2.5）或 decision_log_entry 内（旧格式）：两处都校验，
    # 任意一处指向不存在/被篡改的文件都必须拒绝（A2）。
    refs = []
    top_refs = report.get("data_refs")
    dl_refs = dl.get("data_refs")
    if top_refs:
        refs.extend(top_refs if isinstance(top_refs, list) else [top_refs])
    if dl_refs:
        refs.extend(dl_refs if isinstance(dl_refs, list) else [dl_refs])
    if not refs:
        errors.append("data_refs 为空：真实报告必须记录数据溯源")
    else:
        try:
            provenance.verify_refs(refs, base, require_sha=True)
        except provenance.ProvenanceError as exc:
            errors.append(f"data_refs 不可溯源: {exc}")

    return errors


def validate_real_report(report, base):
    """真实报告门：不通过抛 GateError（消息含全部判据错误）。"""
    errors = real_report_errors(report, base)
    if errors:
        raise GateError("；".join(errors))


def _nonempty(value):
    return str(value or "").strip() != ""


def report_shape_errors(report):
    """报告结构校验（task-13：畸形形状不得被静默跳过，一律 fail-closed）。

    判定面形状敏感曾造成 fail-open：把 recommendation 改成 dict、security_gate
    改成字符串、decision_log_entry 改成非 dict 时，旧实现静默跳过 gate 判定直接
    approved。这里把「未知/畸形形状」统一判成错误，调用方（CLI/check_finalize）
    必须在做任何字段判定前先过这一关，且不得让 AttributeError/TypeError 冒泡。
    """
    if not isinstance(report, dict):
        return [f"报告顶层不是 JSON 对象（实际 {type(report).__name__}）：结构畸形，fail-closed 拒绝 approved"]
    errors = []
    sg = report.get("security_gate")
    if sg is not None and not isinstance(sg, dict):
        errors.append(f"报告 security_gate 不是 dict（实际 {type(sg).__name__}）：结构畸形，fail-closed 拒绝 approved")
    dl = report.get("decision_log_entry")
    if dl is not None and not isinstance(dl, dict):
        errors.append(f"decision_log_entry 不是 dict（实际 {type(dl).__name__}）：结构畸形，fail-closed 拒绝 approved")
    elif isinstance(dl, dict):
        dlg = dl.get("security_gate")
        if dlg is not None and not isinstance(dlg, dict):
            errors.append(f"decision_log_entry.security_gate 不是 dict（实际 {type(dlg).__name__}）："
                          "结构畸形，fail-closed 拒绝 approved")
    recs = report.get("recommendation")
    if recs is not None and not isinstance(recs, list):
        errors.append(f"recommendation 不是 list（实际 {type(recs).__name__}）：结构畸形，fail-closed 拒绝 approved")
    elif isinstance(recs, list):
        for i, rec in enumerate(recs):
            if not isinstance(rec, dict):
                errors.append(f"recommendation[{i}] 不是 dict（实际 {type(rec).__name__}）："
                              "结构畸形，fail-closed 拒绝 approved")
    sgr = report.get("security_gate_requested")
    if sgr is not None and not isinstance(sgr, bool):
        errors.append(f"security_gate_requested 不是 bool（实际 {type(sgr).__name__}）："
                      "按「声称需要安全门」处理，fail-closed 拒绝 approved")
    # C-1：未知卡型一律报错（不得静默按 attribution 处理）
    ck = report.get("card_kind")
    if ck is not None and not isinstance(ck, str):
        errors.append(f"card_kind 不是字符串（实际 {type(ck).__name__}）：未知卡型，fail-closed 拒绝 approved")
    elif isinstance(ck, str) and ck.strip() and ck.strip() not in report_contract.CARD_KINDS:
        errors.append(f"未知 card_kind: {ck.strip()!r}（允许 {list(report_contract.CARD_KINDS)}）")
    return errors


def report_claims_security_gate(report):
    """报告是否「声称需要 securityGate」（≠ 已有外部确认）。

    任一成立即视为声称（task-12 收紧 A9 fail-open；task-13 形状硬化）：
    - report 顶层非 dict（无法判定 → 按声称处理）；
    - report.security_gate_requested 为真，或为「非 bool 的非 None 值」（如 "false"）；
    - 顶层 report.security_gate、decision_log_entry、decision_log_entry.security_gate、
      recommendation 存在但形状异常（非 dict/非 list）；
    - 顶层或 dl 的 security_gate.status / confirmed_by 非空；
    - 任一 recommendation.irreversibility == "irreversible"。
    这种报告缺 gate.json 外部确认记录时必须拒绝，不能靠报告自证。
    """
    if not isinstance(report, dict):
        return True
    sgr = report.get("security_gate_requested")
    if isinstance(sgr, bool):
        if sgr:
            return True
    elif sgr is not None:
        # task-13：非 bool（如字符串 "false"）一律按声称需要安全门处理（fail-closed 方向）
        return True
    if "security_gate" in report and report.get("security_gate") is not None \
            and not isinstance(report.get("security_gate"), dict):
        return True
    top = report.get("security_gate")
    if isinstance(top, dict) and (_nonempty(top.get("status")) or _nonempty(top.get("confirmed_by"))):
        return True
    dl = report.get("decision_log_entry")
    if dl is not None and not isinstance(dl, dict):
        return True
    if isinstance(dl, dict):
        dlg = dl.get("security_gate")
        if dlg is not None and not isinstance(dlg, dict):
            return True
        if isinstance(dlg, dict) and (_nonempty(dlg.get("status")) or _nonempty(dlg.get("confirmed_by"))):
            return True
    recs = report.get("recommendation")
    if recs is not None and not isinstance(recs, list):
        return True
    for rec in recs or []:
        if not isinstance(rec, dict):
            return True
        if rec.get("irreversibility") == "irreversible":
            return True
    return False


SIGNABLE_VERDICTS = ("output",)


def signable_verdict(verdict) -> bool:
    """评分卡档位是否允许签字定案（未知/缺失一律不可签，fail-closed）。"""
    return str(verdict or "").strip() in SIGNABLE_VERDICTS


# --------------------------------------------------------------------------- #
# 人工复核（甲方案：复核只放行、不改分）
#
# 背景（2026-11-02 实测）：终审门改成白名单后，诚实的 46.67 分（force_manual_review）
# 永远签不了，且全仓没有人工复核入口——产品在逼用户抬分造假。
#
# 甲方案：复核记录 runs/<task_id>/review.json 是 append-only 独立文件，
#   - 不修改 report.json 任何字节、不改 scorecard.total/verdict、不改 sign_scope；
#   - 报告原档位照旧（original_verdict 只记录），放行由门判断；
#   - 复核钉死在签名时绑定的 report sha256 上（防「复核 A、签 B」）；
#   - no_output（<40 分，最差档）一律不接受复核放行。
# --------------------------------------------------------------------------- #
REVIEW_SCHEMA = "review.v1"
REVIEW_FILE = "review.json"
REVIEW_MAX_HISTORY = 20
REVIEW_DECISION_VERDICTS = ("accept", "reject", "modify")
REVIEW_REASON_MIN_CHARS = 10
# 允许提交复核的任务状态：报告已 assembled 之后（含已签字/已收口，用于重复复核留痕）。
REVIEWABLE_STATES = ("assembled", "awaiting_sign", "signed")


def load_review(task_dir):
    """读 <task_dir>/review.json（append-only 人工复核记录）。

    缺失/不可解析/顶层非对象一律返回 None（fail-closed：没有记录就是没有放行依据）。
    """
    path = Path(task_dir) / REVIEW_FILE
    if not path.is_file():
        return None
    try:
        doc = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:  # noqa: BLE001 - 坏文件按「无复核」处理，绝不 fail-open
        return None
    return doc if isinstance(doc, dict) else None


def _iso_with_tz(text):
    """reviewed_at 必须是带时区的 ISO 8601（无时区=无法判断先后，拒）。"""
    from datetime import datetime
    try:
        dt = datetime.fromisoformat(str(text).strip().replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return False
    return dt.tzinfo is not None


def _review_decision_errors(decisions):
    errors = []
    if not isinstance(decisions, list) or not decisions:
        return ["review.decisions 必须是非空 list（逐条给出 finding/recommendation 的处置）"]
    for i, item in enumerate(decisions):
        if not isinstance(item, dict):
            errors.append(f"review.decisions[{i}] 必须是对象")
            continue
        fid = str(item.get("finding_id") or item.get("recommendation_id") or "").strip()
        if not fid:
            errors.append(f"review.decisions[{i}] 缺 finding_id/recommendation_id（复核必须指向具体条目）")
        verdict = str(item.get("verdict") or "").strip()
        if verdict not in REVIEW_DECISION_VERDICTS:
            errors.append(f"review.decisions[{i}].verdict={verdict!r} 非法（允许 {list(REVIEW_DECISION_VERDICTS)}）")
        if not _nonempty_text(item.get("note")):
            errors.append(f"review.decisions[{i}].note 必须是非空字符串（处置理由）")
    return errors


def review_field_errors(review, allowlist=None):
    """复核记录自身的字段契约（写入前校验 + 放行前校验共用，纯函数）。

    allowlist=None 表示不做白名单判定（仅结构校验）；给出白名单（含空）即 fail-closed。
    """
    if not isinstance(review, dict):
        return ["review 必须是 JSON 对象（schema=review.v1）"]
    errors = []
    if str(review.get("schema") or "") != REVIEW_SCHEMA:
        errors.append(f"review.schema={review.get('schema')!r} 非法（必须是 {REVIEW_SCHEMA}）")
    if not _nonempty_text(review.get("task_id")):
        errors.append("review.task_id 必须是非空字符串")
    sha = str(review.get("report_sha256") or "").strip()
    if not re.fullmatch(r"[0-9a-f]{64}", sha):
        errors.append(f"review.report_sha256 必须是 64 位小写 sha256（收到 {sha[:16]!r}）：复核必须钉死在一份具体报告上")
    rv_by = str(review.get("reviewed_by") or "").strip()
    if not rv_by:
        errors.append("review.reviewed_by 必须是非空字符串（谁复核的）")
    elif allowlist is not None:
        allow = [str(x).strip() for x in (allowlist or ()) if str(x).strip()]
        if not allow:
            errors.append("未配置 reviewed_by 白名单（allowlist 为空）：复核放行 fail closed")
        elif rv_by not in set(allow):
            errors.append(f"review.reviewed_by={rv_by!r} 不在白名单 {allow}（复核人须与签字人白名单同源）")
    if not _iso_with_tz(review.get("reviewed_at")):
        errors.append("review.reviewed_at 必须是带时区的 ISO 8601（如 2026-11-02T10:00:00+08:00）")
    reason = str(review.get("reason") or "").strip()
    if len(reason) < REVIEW_REASON_MIN_CHARS:
        errors.append(f"review.reason 必须写明复核理由（≥{REVIEW_REASON_MIN_CHARS} 字，收到 {len(reason)} 字）")
    errors.extend(_review_decision_errors(review.get("decisions")))
    if not _nonempty_text(review.get("original_verdict")):
        errors.append("review.original_verdict 必须记录复核时的评分卡档位（只记录，不许改分）")
    history = review.get("history")
    if not isinstance(history, list):
        errors.append("review.history 必须是 list（append-only，重复复核保留旧记录）")
    elif len(history) > REVIEW_MAX_HISTORY:
        errors.append(f"review.history 最多保留 {REVIEW_MAX_HISTORY} 条，收到 {len(history)} 条")
    elif any(not isinstance(h, dict) for h in history):
        errors.append("review.history 每条必须是对象")
    return errors


def review_errors(report, signature, review, allowlist=()):
    """人工复核放行判据（甲方案，纯函数）。空列表=可放行；否则维持原拒绝。

    仅当 scorecard.verdict 不是 output 时由 F7 调用。必须同时满足：
      a) 存在 review.json 且 schema=review.v1（字段契约齐全）；
      b) review.report_sha256 == signature.report_sha256（同一份报告，防「复核 A 签 B」）；
      c) reviewed_by 在签字人白名单内（与 signer 同源）；
      d) reason 非空且 ≥10 字、decisions 非空（逐条处置）；
      e) verdict != no_output（<40 分最差档一律不放行）；且 scorecard.total 不得 <40
         （防「把 <40 的报告改标成 force_manual_review」，最差档按实测总分也拒）。
    复核只放行、不改分：本函数不写任何字段，也不碰 report。
    """
    errors = []
    if not isinstance(report, dict):
        return ["report 顶层不是 JSON 对象：无法复核放行（fail-closed）"]
    sc = report.get("scorecard") if isinstance(report.get("scorecard"), dict) else {}
    verdict = str(sc.get("verdict") or "").strip()
    if verdict == "no_output":
        return [f"scorecard.verdict=no_output（<40 分，最差档）：一律不接受人工复核放行"
                f"（复核只放行 force_manual_review/reference_only/未知档，不支持最差档）"]
    try:
        total_num = float(sc.get("total"))
    except (TypeError, ValueError):
        total_num = None
    if total_num is not None and total_num < 40:
        return [f"scorecard.total={total_num:g} 低于 40 分（最差档）：一律不接受人工复核放行"
                f"（按实测总分拦，不论 scorecard.verdict={verdict or '缺失'} 标注为何）"]
    if review is None:
        return [f"缺少人工复核记录 {REVIEW_FILE}（schema={REVIEW_SCHEMA}）："
                f"verdict={verdict or '缺失'} 不可签，需人工复核放行（复核只放行、不改分）"]
    errors.extend(review_field_errors(review, allowlist=allowlist))
    # 结构错误已能保证下面字段类型可用；仍用防御式取值，绝不让 AttributeError 冒泡
    if not isinstance(review, dict):
        return errors or ["review 必须是 JSON 对象"]
    sig_sha = ""
    if isinstance(signature, dict):
        sig_sha = str(signature.get("report_sha256") or "").strip()
    review_sha = str(review.get("report_sha256") or "").strip()
    if not sig_sha:
        errors.append("签名缺少 report_sha256：复核无法钉到具体报告（fail-closed）")
    elif review_sha and review_sha != sig_sha:
        errors.append(f"复核绑定的报告 sha256 与签名不一致（复核 {review_sha[:16]}… / 签名 {sig_sha[:16]}…）："
                      "疑似「复核 A 签 B」，不放行；请对当前报告重新复核")
    report_sha = str(report.get("_sha256") or "").strip()
    if review_sha and report_sha and review_sha != report_sha:
        errors.append(f"复核绑定的报告 sha256 与当前报告不一致（复核 {review_sha[:16]}… / 报告 {report_sha[:16]}…）："
                      "报告已变，需重新复核")
    if not verdict:
        errors.append("报告缺少 scorecard.verdict：人工复核无法钉住档位（fail-closed）")
    rv_task = str(review.get("task_id") or "").strip()
    report_task = str(report.get("task_id") or "").strip()
    if rv_task and report_task and rv_task != report_task:
        errors.append(f"复核 task_id={rv_task!r} 与报告 task_id={report_task!r} 不一致（跨任务复核）")
    original = str(review.get("original_verdict") or "").strip()
    if verdict and original != verdict:
        errors.append(f"复核记录原档位={original!r} 与当前 scorecard.verdict={verdict!r} 不一致："
                      "报告档位已变，旧复核失效（需重新复核）")
    return errors




def check_finalize(decision, report, signature, allowlist=(), gate_confirmed=False, base=None,
                   review=None):
    """签字收口门：返回拒绝原因列表；空列表=可 approved。

    review：runs/<task_id>/review.json 的人工复核记录（可由 gates.load_review 读入）。
    甲方案：只有 scorecard.verdict 不是 output 时才查复核；复核通过则 F7 不报该项，
    其余判据（签名/白名单/task_id/gate…）照旧。
    """
    # 0) 形状校验优先：畸形结构直接 fail-closed，不进入任何字段判定（task-13）
    shape_errors = report_shape_errors(report)
    if shape_errors:
        return shape_errors
    reasons = []
    decision = decision if isinstance(decision, dict) else {}
    signature = signature if isinstance(signature, dict) else {}

    # F18: signature-time action binding (block forged security_gate.action)
    _base = base or str(Path(__file__).resolve().parents[1])
    try:
        _pk = _load_pack_doc(report, _base)
    except Exception as _exc:  # noqa: BLE001
        reasons.append(f"pack unparsable: {_exc} (finalize fail-closed)")
        _pk = None
    if _pk is not None:
        from .pack import is_irreversible as _is_irr3
        for _g in (report.get("security_gate"), (decision or {}).get("security_gate")):
            if isinstance(_g, dict) and (_g.get("action") or _g.get("confirmed_by")):
                _act = str(_g.get("action") or "").strip()
                # F18 本意是拦「伪造 action」：声明了就必须是包内不可逆动作；
                # 未声明动作（""）＝未绑定动作，不构成伪造，交给 gate 状态机判
                if _act and not _is_irr3(_pk, _act):
                    reasons.append(f"security_gate.action={_act!r} is not a pack irreversible action (finalize rejected)")

    # 可签字的评分卡档位：只有 output（≥80 且无硬护栏）才允许走 approved。
# 历史缺陷（2026-11-02 实测）：原判定只拦 force_manual_review，于是
# no_output(33.33)/reference_only(43.33) 反而能 approved，而 60–79 被拒——门是反的。
# F7：自判不可签 / 无停止范围 → 不得 approved（不得靠 post_sign 绕过）
    _vl = str(report.get("verdict_one_line") or "")
    _ss = report.get("sign_scope")
    _sc = report.get("scorecard") if isinstance(report.get("scorecard"), dict) else {}
    # 甲方案（2026-11-02）：档位不可签时先查人工复核（append-only review.json）。
    # 复核通过 → F7 不报该项（放行只此一处，不改分、不改 sign_scope）；不通过 → 报缺什么。
    _review_ok = False
    _review_problems: list[str] = []
    if not signable_verdict(_sc.get("verdict")):
        _review_problems = review_errors(report, signature, review, allowlist=allowlist)
        _review_ok = not _review_problems
    if _ss == "no_stop" or "不可签" in _vl or not signable_verdict(_sc.get("verdict")):
        if not _review_ok:
            _detail = ("；".join(_review_problems) if _review_problems
                       else f"需要人工复核记录（{REVIEW_FILE}，schema={REVIEW_SCHEMA}）")
            reasons.append(
                f"不可签（verdict={_sc.get('verdict')!r}, sign_scope={_ss!r}, "
                f"verdict_one_line={_vl[:40]!r}）：不得 approved；{_detail}"
            )

    # 1) demo 不得走真实交付
    if report.get("is_demo") is not False or signature.get("is_demo") is True:
        reasons.append("demo 报告/签名不得走真实交付（is_demo=true）")

    # 2) decision 状态机：只有 proposed 能变成 approved
    status = decision.get("status")
    if status != "proposed":
        reasons.append(f"decision.status={status!r}，只有 proposed 才能 approved（rejected/approved 不得再收口）")

    # 3) 跨任务混用
    if decision.get("task_id") != report.get("task_id"):
        reasons.append(f"decision.task_id={decision.get('task_id')!r} 与 report.task_id={report.get('task_id')!r} 不一致（跨任务混用）")

    # 4) 签名内容
    signer = str(signature.get("signer") or "").strip()
    allow = [str(x).strip() for x in (allowlist or ()) if str(x).strip()]
    if not signer:
        reasons.append("签名缺少 signer（伪签名：只有 report_sha256、没有签字人）")
    elif not allow:
        # M1 遗留硬化：空白名单必须 fail closed（M1 里 `elif allowlist and ...` 会放行）
        reasons.append("未配置 signer 白名单（allowlist 为空）：签字收口 fail closed，拒绝 approved")
    elif signer not in set(allow):
        reasons.append(f"signer={signer!r} 不在签字人白名单 {allow}")
    sha = signature.get("report_sha256")
    if not sha:
        reasons.append("签名缺少 report_sha256")
    elif report.get("_sha256") and sha != report.get("_sha256"):
        reasons.append(f"签名哈希与被签报告不一致: signature={sha} report={report.get('_sha256')}")
    sig_task = signature.get("task_id")
    if sig_task and report.get("task_id") and sig_task != report.get("task_id"):
        reasons.append(f"签名 task_id={sig_task!r} 与被签报告 task_id={report.get('task_id')!r} 不一致")

    # 5) securityGate：不可逆动作必须「外部确认」（gate.json 人工确认留痕）才可 approved。
    #    task-12 收紧：报告自证 security_gate=confirmed 不再算数，必须由 CLI 校验过
    #    gate.json（status/confirmed_by/白名单/history/审计事件）后传 gate_confirmed=True。
    dl = report.get("decision_log_entry") or {}
    gate = dl.get("security_gate")
    claims_gate = report_claims_security_gate(report)
    if claims_gate:
        if gate_confirmed:
            if not isinstance(gate, dict) or not gate:
                reasons.append("gate.json 已验证，但报告缺少 security_gate 记录（未同步人工确认结果）")
            elif gate.get("status") != "confirmed":
                reasons.append(f"gate.json 已验证，但报告 security_gate.status={gate.get('status')!r}，不得 approved")
            elif not str(gate.get("confirmed_by") or "").strip():
                reasons.append("securityGate 已确认但 confirmed_by 为空（人工确认不可追溯），不得 approved")
        else:
            reasons.append(
                "报告声称需要 securityGate（不可逆动作/security_gate_requested/报告自报 security_gate），"
                "但缺少 gate.json 外部确认记录（fail-closed）：不得仅凭报告自证 approved"
            )
    return reasons


if __name__ == "__main__":
    import sys
    path = sys.argv[1]
    doc = json.loads(open(path, encoding="utf-8-sig").read())
    for err in real_report_errors(doc, "."):
        print("[FAIL]", err)
    print("RESULT:", "PASS" if not real_report_errors(doc, ".") else "FAIL")


def gate_action_mismatch(gate_doc, report):
    """F28: confirmed gate action must equal the action the report will execute."""
    ga = str((gate_doc or {}).get("action") or "").strip()
    ra = str((report.get("security_gate") or {}).get("action") or "").strip()
    if ga and ra and ga != ra:
        return f"gate.json action={ga!r} != report action={ra!r} (confirm/execute mismatch)"
    return None


# --------------------------------------------------------------------------- #
# 指标游戏（metric gaming）：只标记、不阻断
#
# 2026-11-02 案例实测：模型建议「把已逾期事项移出在办统计口径」——不动业务、只动
# 口径就能把逾期率做低，系统照单全收并写进决策卡。改口径也可能是正常治理动作，
# 所以这里只产出 flags 交给人工复核，绝不参与 approve/reject 判据。
# --------------------------------------------------------------------------- #
METRIC_GAMING_PATTERNS = (
    # 改统计口径 / 调口径
    ("改口径", re.compile(r"(?:修改?|调整?|改变?|重设|换)(?:统计)?口径")),
    # 移出统计（含「移出本周在办统计口径」这类中间夹修饰语的写法）
    ("移出统计", re.compile(r"移出[^。；;\n]{0,10}统计")),
    # 调整分母 / 缩小分母
    ("调整分母", re.compile(r"(?:调整?|修改?|缩小|压减|换)分母")),
    # 修改统计范围 / 统计边界
    ("修改统计范围", re.compile(r"(?:修改?|调整?|缩小|扩大|重设)统计(?:范围|口径|边界)")),
    # 不计入统计 / 从统计中剔除
    ("不计入统计", re.compile(r"不计入[^。；;\n]{0,8}统计"
                             r"|(?:剔除|扣除)[^。；;\n]{0,8}(?:分母|统计)")),
)


def metric_gaming_flags(report):
    """扫描 recommendation 的字符串，返回「指标游戏」命中列表（只标记不阻断）。

    返回每项：{"index": 条目下标, "phrase": 命中短语, "path": 字段路径,
              "text": 命中的原句（截断 160 字）}
    只读：不抛错、不改报告、不影响任何门判据。
    """
    hits = []
    if not isinstance(report, dict):
        return hits
    recs = report.get("recommendation")
    if not isinstance(recs, list):
        return hits
    seen = set()
    for index, item in enumerate(recs):
        pairs = item.items() if isinstance(item, dict) else (("", item),)
        for key, value in pairs:
            for text in _iter_strings(value):
                text = str(text)
                for phrase, rx in METRIC_GAMING_PATTERNS:
                    if (index, text) in seen or not rx.search(text):
                        continue
                    seen.add((index, text))
                    suffix = f".{key}" if key else ""
                    hits.append({"index": index, "phrase": phrase,
                                 "path": f"recommendation[{index}]{suffix}",
                                 "text": text[:160]})
    return hits


def apply_metric_gaming_flags(report):
    """把命中写进 report["flags"]（list，code=metric_gaming），返回命中列表。

    只标记不阻断：报告仍然合法、不判红；重复调用不会重复追加同一条 flag。
    """
    hits = metric_gaming_flags(report)
    if not isinstance(report, dict):
        return hits
    flags = report.get("flags")
    flags = list(flags) if isinstance(flags, list) else []
    flags = [f for f in flags
             if not (isinstance(f, dict) and f.get("code") == "metric_gaming")]
    if hits:
        flags.append({"code": "metric_gaming", "flag": "metric_gaming",
                      "count": len(hits), "items": hits, "blocking": False,
                      "note": "改口径/移出统计/调整分母等指标游戏嫌疑，仅提示，人工复核判断"})
    if flags or "flags" in report:
        report["flags"] = flags
    return hits
