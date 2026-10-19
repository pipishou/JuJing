# -*- coding: utf-8 -*-
"""可信门（§2.3）：真实报告门 + 签字收口门（A2/A3 的机器判据）。

一、validate_real_report(report, base)
    real 报告不允许：demo/占位证据、缺证据、data_refs 不存在或 hash 不符、
    scorecard.total/verdict 缺失或与独立计算不一致。
二、check_finalize(decision, report, signature, allowlist) -> list[str]
    空列表 = 可以 approved；任何一条 = 必须拒绝。
三、finalize 另需在 CLI 侧核对 signature.report_sha256 == sha256(report 文件)，
   这里通过 report["_sha256"]（CLI 注入）比对。
只依赖 stdlib。
"""
import json

from . import provenance, report_contract

PLACEHOLDER_TOKENS = ("demo 占位", "demo占位", "占位", "placeholder", "…")

DECISION_LOG_REQUIRED = ("id", "actor", "context", "advice", "expected_result", "verification_plan")


class GateError(RuntimeError):
    """可信门拒绝。"""


def is_placeholder_evidence(text):
    """文本是否含占位标记（demo 占位/占位/placeholder/省略号）。"""
    if text is None:
        return True
    s = str(text)
    if not s.strip():
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


def check_finalize(decision, report, signature, allowlist=()):
    """签字收口门：返回拒绝原因列表；空列表=可 approved。"""
    reasons = []
    decision = decision if isinstance(decision, dict) else {}
    report = report if isinstance(report, dict) else {}
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
    if not signer:
        reasons.append("签名缺少 signer（伪签名：只有 report_sha256、没有签字人）")
    elif allowlist and signer not in set(str(x) for x in allowlist):
        reasons.append(f"signer={signer!r} 不在签字人白名单 {list(allowlist)}")
    sha = signature.get("report_sha256")
    if not sha:
        reasons.append("签名缺少 report_sha256")
    elif report.get("_sha256") and sha != report.get("_sha256"):
        reasons.append(f"签名哈希与被签报告不一致: signature={sha} report={report.get('_sha256')}")
    sig_task = signature.get("task_id")
    if sig_task and report.get("task_id") and sig_task != report.get("task_id"):
        reasons.append(f"签名 task_id={sig_task!r} 与被签报告 task_id={report.get('task_id')!r} 不一致")

    # 5) securityGate：不可逆动作必须人工确认后才可 approved
    dl = report.get("decision_log_entry") or {}
    gate = dl.get("security_gate")
    irreversible = any((x or {}).get("irreversibility") == "irreversible"
                       for x in (report.get("recommendation") or []) if isinstance(x, dict))
    need_gate = bool(report.get("security_gate_requested")) or irreversible or gate is not None
    if need_gate:
        if not isinstance(gate, dict) or not gate:
            reasons.append("报告含不可逆动作/请求了 securityGate，但缺少 security_gate 记录")
        elif gate.get("status") != "confirmed":
            reasons.append(f"securityGate 未确认（status={gate.get('status')!r}），不得 approved")
    return reasons


if __name__ == "__main__":
    import sys
    path = sys.argv[1]
    doc = json.loads(open(path, encoding="utf-8-sig").read())
    for err in real_report_errors(doc, "."):
        print("[FAIL]", err)
    print("RESULT:", "PASS" if not real_report_errors(doc, ".") else "FAIL")
