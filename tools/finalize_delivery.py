#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""签字收口：过「可信门」后把 decision.json 更新为 approved（A3）。

M1 修复（审计 P0-1：任意签名可 approved）：
  - 不再只看 report_sha256：必须同时满足 core.gates.check_finalize 全部判据
     · demo 报告不得走真实交付
     · decision.status 必须是 proposed（rejected/approved 不得再收口）
     · decision.task_id 必须等于 report.task_id（跨任务混用拒绝）
     · signature.signer 非空（只有 report_sha256、没有签字人 = 伪签名）
     · 签字人白名单 fail closed：--allow-signers > env BOT_SIGNERS > 旧名 BOT_SIGNER_ALLOWLIST；
    三者都没有 → 拒绝（未配置 signer 白名单）
     · securityGate 必须 confirmed（pending_user_confirmation 拒绝）
     · M1.5 增补：读 gate.json（--gate-file，默认 <报告目录>/gate.json）；
       gate 文件 rejected/pending → 拒绝；confirmed 时用其 confirmed_by 回填报告
     · M1.5 D1 增补（伪造 gate 绕过）：gate.confirmed_by 必须在签字人白名单内；
       gate.json 必须含 append-only history 的 action=confirm 记录且与 confirmed_by 一致；
       同目录 run.log 必须有 phase=gate、actor 一致的审计事件；任一缺失=伪造，拒绝
     · M1.5 增补：securityGate confirmed 但 confirmed_by 为空 → 拒绝（不可追溯）
     · M1.5 task-12 增补（A9 fail-open）：报告只要「声称需要安全门」
       （security_gate_requested / 自报 security_gate / 不可逆建议），
       缺少 gate.json 外部确认记录 → 拒绝（fail-closed），不再沿用报告自证；
       task-13：入口先做报告形状校验（report_shape_errors），畸形/未知形状
       （顶层非 dict、security_gate 非 dict、decision_log_entry 非 dict、
       recommendation 非 list、security_gate_requested 非 bool）→ rc=2 单行
       JSON 错误且不写 decision；AttributeError/TypeError 不得冒泡。
       已删除无法真实生效的 --allow-legacy-gate 死开关（demo 链本就无条件拒绝）
  - 任一条不满足 → 非 0 退出且**不修改** decision.json
  - 原子写回；approval.by == 传入 signer

用法：
  python tools/finalize_delivery.py --report runs/task-B/report.json \
      --loop-dir runs/task-B/loop --signature runs/task-B/report.signature.json
"""
import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import events, fsutil, gate as gate_mod, gates  # noqa: E402


def sha256_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _traceable_gate_event(gate_file, confirmer):
    """审计溯源：确认目录的 run.log 里必须有 phase=gate 且 actor 一致的事件（M1.5 D1）。

    伪造 gate.json 的人无法凭空补出这条 append-only 事件（真实确认由 core.gate 写入）。
    """
    tdir = Path(gate_file).parent
    try:
        recs = events.iter_events(str(tdir / "run.log"))
    except Exception:  # noqa: BLE001
        return False
    for rec in recs:
        if rec.get("phase") != "gate":
            continue
        meta = rec.get("meta") if isinstance(rec.get("meta"), dict) else {}
        if str(meta.get("confirmed_by") or "").strip() == confirmer and \
                str(meta.get("status") or "") == "confirmed":
            return True
    return False


def gate_reasons(report, gate_doc, gate_file, allowlist):
    """读 gate.json 后做安全门判定，返回 (reasons, gate_view)；**不修改 report**。

    M1.5 §2.3/§3 B4 + D1（伪造 gate 绕过 finalize）+ task-12（A9 fail-open）：
    - gate.json 存在且 rejected/pending → 拒绝（gate 文件即人工结论，优先于报告旧字段）
    - gate.json 存在且 confirmed → 必须同时满足：
        · confirmed_by 非空，且在签字人白名单内（白名单为空=已由调用方拒绝）
        · task_id 与报告一致（跨任务确认拒绝）
        · history 为 append-only 数组，且含 action=confirm 记录、confirmed_by 与顶层一致
        · 同目录 run.log 有 phase=gate 且 actor 一致的审计事件（无 history/无事件=伪造）
    - gate.json 不存在且报告声称需要安全门（不可逆/security_gate_requested/自报 gate/
      形状异常）→ 拒绝（fail-closed）：不得仅凭报告自证 approved。
      task-13 起不再保留 legacy 开关（demo 链本就无条件拒绝，开关无真实生效路径，已删）。
    - gate.json 不存在且报告未声称安全门 → 无需 gate（返回空 reasons）
    - 返回的 gate_view 只在全部判据通过后由调用方回填（失败路径不得留下被覆写痕迹）
    """
    reasons = []
    if gate_doc is None:
        # task-12：报告自证 gate 不再算数——缺 gate.json 外部确认记录即 fail-closed
        if gates.report_claims_security_gate(report):
            reasons.append(
                "报告声称需要 securityGate（不可逆/security_gate_requested/报告自报 security_gate），"
                f"但缺少 gate.json 确认记录（{gate_file}，fail-closed）："
                "不可逆动作必须经人工确认留痕，不得仅凭报告自证 approved"
            )
        return reasons, None
    status = str(gate_doc.get("status") or "")
    if status != "confirmed":
        reasons.append(f"securityGate 未确认（{gate_file} status={status!r}），不得 approved")
        return reasons, None
    confirmer = str(gate_doc.get("confirmed_by") or "").strip()
    if not confirmer:
        reasons.append(f"securityGate 已确认但 confirmed_by 为空（{gate_file}），人工确认不可追溯")
        return reasons, None
    gate_task = str(gate_doc.get("task_id") or "").strip()
    report_task = str((report or {}).get("task_id") or "").strip()
    if gate_task and report_task and gate_task != report_task:
        reasons.append(f"gate.json task_id={gate_task!r} 与报告 task_id={report_task!r} 不一致（跨任务确认）")
        return reasons, None
    if confirmer not in allowlist:
        reasons.append(f"securityGate confirmed_by={confirmer!r} 不在签字人白名单 {allowlist}（伪造/越权确认）")
        return reasons, None

    history = gate_doc.get("history")
    if not isinstance(history, list) or not history:
        reasons.append(f"伪造 gate.json（{gate_file}）：缺少 append-only history，拒绝 approved")
        return reasons, None
    confirms = [h for h in history
                if isinstance(h, dict)
                and str(h.get("action") or "") == "confirm"
                and str(h.get("status") or "") == "confirmed"]
    if not confirms:
        reasons.append(f"伪造 gate.json（{gate_file}）：history 无 action=confirm 记录，拒绝 approved")
        return reasons, None
    if not any(str(h.get("confirmed_by") or "").strip() == confirmer for h in confirms):
        reasons.append(f"伪造 gate.json（{gate_file}）：history.confirmed_by 与 gate.confirmed_by 不一致，拒绝 approved")
        return reasons, None
    if not _traceable_gate_event(gate_file, confirmer):
        reasons.append(f"伪造 gate.json（{gate_file}）：{Path(gate_file).parent / 'run.log'} "
                       f"无 phase=gate 且 actor={confirmer!r} 的审计事件，拒绝 approved")
        return reasons, None

    view = {
        "status": "confirmed",
        "confirmed_by": confirmer,
        "confirmed_at": gate_doc.get("confirmed_at"),
        "comment": gate_doc.get("comment") or "",
    }
    top = report.get("security_gate")
    requested_at = (top or {}).get("requested_at") if isinstance(top, dict) else None
    if not requested_at:
        dl = report.get("decision_log_entry")
        if isinstance(dl, dict) and isinstance(dl.get("security_gate"), dict):
            requested_at = dl["security_gate"].get("requested_at")
    if requested_at:
        view["requested_at"] = requested_at
    return reasons, view


def apply_gate_view(report, view):
    """全部校验通过后才把 confirmed 视图写进内存 report（磁盘写只发生在 decision 收口时）。"""
    if not view:
        return
    report["security_gate"] = dict(view)
    dl = report.get("decision_log_entry")
    if not isinstance(dl, dict):
        dl = {}
        report["decision_log_entry"] = dl
    dl["security_gate"] = dict(view)


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", required=True)
    ap.add_argument("--loop-dir", required=True)
    ap.add_argument("--signature", required=True)
    ap.add_argument("--allow-signers", default="", help="逗号分隔的合法签字人白名单（优先级高于 env BOT_SIGNERS）")
    ap.add_argument("--gate-file", default="",
                    help="securityGate 确认文件；默认 <报告目录>/gate.json（M1.5 §2.3）")
    ap.add_argument("--sha256", default="",
                    help="兼容参数：仅与现算哈希做一致性校验，不再允许覆写（防绕过）")
    args = ap.parse_args()

    try:
        report = json.loads(Path(args.report).read_text(encoding="utf-8-sig"))
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"ok": False, "error": f"读不到报告: {exc}"}, ensure_ascii=False, indent=2))
        return 2
    # task-13：入口先做形状校验——畸形/未知结构 rc=2 单行 JSON，不写 decision 且不冒泡异常
    shape_errors = gates.report_shape_errors(report)
    if shape_errors:
        print(json.dumps({"ok": False, "status": "rejected", "reasons": shape_errors,
                          "shape_error": True}, ensure_ascii=False))
        return 2
    try:
        sig = json.loads(Path(args.signature).read_text(encoding="utf-8-sig"))
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"ok": False, "error": f"读不到签名文件: {exc}"}, ensure_ascii=False, indent=2))
        return 2
    if not isinstance(sig, dict):
        print(json.dumps({"ok": False, "status": "rejected", "shape_error": True,
                          "reasons": [f"签名文件顶层不是 JSON 对象（实际 {type(sig).__name__}）：结构畸形"]},
                         ensure_ascii=False))
        return 2
    dpath = Path(args.loop_dir) / "decision.json"
    if not dpath.is_file():
        print("[ERROR] decision.json not found", file=sys.stderr)
        return 2
    dec = json.loads(dpath.read_text(encoding="utf-8-sig"))
    if not isinstance(dec, dict):
        print(json.dumps({"ok": False, "status": "rejected", "shape_error": True,
                          "reasons": [f"decision.json 顶层不是 JSON 对象（实际 {type(dec).__name__}）：结构畸形"]},
                         ensure_ascii=False))
        return 2

    actual = sha256_file(args.report)
    if args.sha256 and args.sha256 != actual:
        print(json.dumps({"ok": False, "error": "--sha256 与报告整文件哈希不一致（不允许覆写哈希）",
                          "expected": actual, "given": args.sha256},
                         ensure_ascii=False, indent=2))
        return 2
    if not sig.get("report_sha256"):
        print(json.dumps({"ok": False, "error": "签名缺少 report_sha256"}, ensure_ascii=False, indent=2))
        return 2
    if actual != sig.get("report_sha256"):
        print(json.dumps({"ok": False, "error": "signature hash mismatch",
                          "expected": sig.get("report_sha256"), "actual": actual},
                         ensure_ascii=False, indent=2))
        return 2

    # A3②：signer 白名单 fail closed —— 未显式配置就拒绝（伪 signer 不得蒙混过关）
    raw_allow = (args.allow_signers or os.environ.get("BOT_SIGNERS")
                 or os.environ.get("BOT_SIGNER_ALLOWLIST") or "")
    allowlist = [x.strip() for x in raw_allow.split(",") if x.strip()]
    if not allowlist:
        print(json.dumps({"ok": False, "status": "rejected",
                          "reasons": ["未配置 signer 白名单：请用 --allow-signers 或 env BOT_SIGNERS 指定合法签字人（fail closed）"],
                          "decision": dec.get("id")}, ensure_ascii=False, indent=2))
        return 2
    report = dict(report)
    report["_sha256"] = actual
    gate_file = (Path(args.gate_file) if args.gate_file
                 else Path(args.report).parent / gate_mod.GATE_FILE)
    gate_doc = gate_mod.read_file(gate_file)
    reasons, gate_view = gate_reasons(report, gate_doc, gate_file, allowlist)
    if not reasons:
        # D1：先跑完全部判据（sha/状态机/task_id/signer/gate/allowlist），
        # 只有全部通过才把 confirmed 视图回填内存报告；失败路径不改报告、不写 decision。
        apply_gate_view(report, gate_view)
        reasons += gates.check_finalize(dec, report, sig, allowlist=allowlist,
                                        gate_confirmed=bool(gate_view))
        # F28: confirmed action must equal reported action
        _mm = gates.gate_action_mismatch(gate_doc, report)
        if _mm:
            reasons.append(_mm)
    if reasons:
        print(json.dumps({"ok": False, "status": "rejected", "reasons": reasons,
                          "gate_file": str(gate_file),
                          "gate_status": (gate_doc or {}).get("status"),
                          "decision": dec.get("id")}, ensure_ascii=False, indent=2))
        return 2

    dec["status"] = "approved"
    dec["approval"] = {"by": sig.get("signer"), "at": sig.get("signed_at"), "mode": "explicit"}
    dec["signature_sha256"] = actual
    dec["signature_file"] = str(args.signature)
    fsutil.atomic_write_json(dpath, dec)

    # 状态同步（c2 修复）：收口成功即把 status.json 推到 signed/finalized，
    # 与 server post_finalize 的落点同语义；任务目录＝报告所在目录。
    # 只在 status.json 已存在时更新（生产任务由 run_task.py 预写；测试临时目录无此文件则跳过）。
    status_file = Path(args.report).parent / "status.json"
    if status_file.is_file():
        try:
            sdoc = json.loads(status_file.read_text(encoding="utf-8-sig"))
        except Exception:  # noqa: BLE001 - status.json 损坏不阻断收口，仅重置为最小骨架
            sdoc = {}
        if not isinstance(sdoc, dict):
            sdoc = {}
        sdoc["state"] = "signed"
        sdoc["step"] = "finalized"
        sdoc["decision_status"] = dec.get("status")
        sdoc["approval"] = dec.get("approval")
        sdoc["updated_at"] = datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds")
        fsutil.atomic_write_json(status_file, sdoc)

    print(json.dumps({"ok": True, "decision": dec.get("id"), "status": "approved",
                      "signer": sig.get("signer"), "signature_sha256": actual},
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
