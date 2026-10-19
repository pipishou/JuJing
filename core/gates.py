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

from . import provenance, report_contract

PLACEHOLDER_TOKENS = ("demo 占位", "demo占位", "占位", "placeholder", "…", "待补充", "待核实")

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
    return any(tok.lower() in low for tok in PLACEHOLDER_TOKENS)


def _iter_strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _iter_strings(item)
    elif isinstance(value, dict):
        for item in value.values():
            yield from _iter_strings(item)


def real_report_errors(report, base):
    """返回真实报告必须满足的判据错误列表（空=通过）。"""
    errors = []
    if not isinstance(report, dict):
        return ["report 必须是 JSON 对象"]
    if report.get("is_demo") is not False:
        errors.append("real 报告 is_demo 必须为 false")
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
            ev = f.get("evidence") or []
            if not ev:
                errors.append(f"finding[{i}] 缺证据 evidence")
            else:
                for j, item in enumerate(ev):
                    if is_placeholder_evidence(item):
                        errors.append(f"finding[{i}].evidence[{j}] 含占位/空证据: {str(item)[:40]!r}")

    ca = report.get("cannot_attribute") or []
    if not isinstance(ca, list) or not ca:
        errors.append("cannot_attribute 不能为空")

    sc = report.get("scorecard")
    if not isinstance(sc, dict):
        errors.append("scorecard 必须是对象")
    else:
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
        try:
            errors.extend(report_contract.consistency_errors(sc))
        except Exception as exc:  # noqa: BLE001
            errors.append(f"scorecard 校验异常: {exc}")

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

    dl = report.get("decision_log_entry") or {}

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


def check_finalize(decision, report, signature, allowlist=(), gate_confirmed=False):
    """签字收口门：返回拒绝原因列表；空列表=可 approved。"""
    # 0) 形状校验优先：畸形结构直接 fail-closed，不进入任何字段判定（task-13）
    shape_errors = report_shape_errors(report)
    if shape_errors:
        return shape_errors
    reasons = []
    decision = decision if isinstance(decision, dict) else {}
    signature = signature if isinstance(signature, dict) else {}

    # 1) demo 不得走真实交付
    if report.get("is_demo") is True or signature.get("is_demo") is True:
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
