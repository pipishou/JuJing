#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""甲方案：人工复核只放行、不改分（review.v1）+ 门/入口/CLI 三层验收。

背景（2026-11-02 实测证据）：终审门被改成白名单（`SIGNABLE_VERDICTS=("output",)`）后，
诚实的 46.67 分（force_manual_review）永远签不了，且全仓没有人工复核入口
——产品在逼用户抬分造假。

甲方案（产品负责人拍板）：
  - 复核记录 runs/<task_id>/review.json（append-only，原子写）；
  - 复核钉在签名时绑定的 report sha256 上（防「复核 A、签 B」）；
  - 只放行、不改分：report.json 字节 / scorecard.total / scorecard.verdict / sign_scope 一律不动；
  - no_output（<40 分，最差档）一律不接受复核放行。

本文件自带负例（故意造假必须红）：
  正控：46.67 + 合法 review → 放行（check_finalize / CLI / HTTP 三层）
  负例：缺 review、sha 不一致、reviewed_by 不在白名单、reason 空、decisions 空、
        no_output + 合法 review、schema 错、原档位不一致、跨任务、reviewed_at 无时区、
        「可签档 + no_stop」不得被复核豁免。

只依赖 stdlib；服务用临时端口 + BOT_RUNS_ROOT/BOT_LEDGER_ROOT/BOT_WORKITEMS_FILE/
BOT_EVENTS_FILE 指向临时目录，不污染真 runs。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import core.gate as gate_mod  # noqa: E402
import core.gates as gates  # noqa: E402

REPORTS: list[str] = []
CHECKS = 0
FAILS: list[str] = []


def check(name: str, ok: bool, detail="") -> None:
    global CHECKS
    CHECKS += 1
    if ok:
        print(f"[ ok ] {name}")
    else:
        FAILS.append(name)
        print(f"[FAIL] {name} :: {str(detail)[:400]}")


def _python() -> Path:
    for cand in (Path(os.environ.get("BOT_PY", "")),
                 ROOT.parent / "venv/Scripts/python.exe",
                 ROOT.parent / "venv/bin/python"):
        if cand and Path(cand).is_file():
            return Path(cand)
    return Path(sys.executable)


PY = _python()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(Path(path).read_bytes())


def dump_json(path: Path, doc) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def make_report(task_id: str, total: float = 46.67, verdict: str = "force_manual_review",
                one_line: str = "不可签·待人工复核：归因证据不足", scope: str = "no_stop") -> dict:
    """46.67 分（60–79 force_manual_review）的诚实报告：门里 F7 本来就该拒，只有复核能放行。"""
    return {
        "task_id": task_id,
        "is_demo": False,
        "pack": "manufacturing",
        "goal": "复核放行验收（不改分）",
        "verdict_one_line": one_line,
        "sign_scope": scope,
        "evidence_tier": "mixed",
        "scorecard": {"total": total, "verdict": verdict},
        "finding": [{"factor": "模具磨损", "confidence": 0.5,
                     "evidence": ["换模记录与补料率爬升同步（现场核实）"]}],
        "cannot_attribute": ["信息员换班带来的口径变化未完全排除"],
        "recommendation": [],
        "decision_log_entry": {"id": "DL-" + task_id, "actor": "manufacturing-ops-team-dsh"},
        "data_refs": [],
    }


def make_review(report: dict, report_sha: str, **overrides) -> dict:
    """合法复核记录（默认钉在当前报告 sha 上）。overrides 用来造负例。"""
    doc = {
        "schema": gates.REVIEW_SCHEMA,
        "task_id": report.get("task_id"),
        "report_sha256": report_sha,
        "reviewed_by": "老板",
        "reviewed_at": "2026-11-02T10:00:00+08:00",
        "reason": "人工复核：评分偏严但证据与结论一致，接受原结论、不改分，放行交付。",
        "decisions": [{"finding_id": "F1", "verdict": "accept",
                       "note": "换模记录与补料率爬升同步，证据成立"}],
        "original_verdict": (report.get("scorecard") or {}).get("verdict"),
        "history": [],
    }
    doc.update(overrides)
    return doc


def sig_for(report: dict, sha: str) -> dict:
    return {"signer": "老板", "report_sha256": sha, "task_id": report.get("task_id")}


def decision_for(report: dict) -> dict:
    return {"id": "D-" + str(report.get("task_id")), "status": "proposed",
            "task_id": report.get("task_id")}


# --------------------------------------------------------------------------- #
# 1) 门判据（core.gates.check_finalize / review_errors，纯函数）
# --------------------------------------------------------------------------- #
def case_gate(tmp: Path) -> None:
    print("\n[1] 门判据：46.67 + 复核放行；造假一律拒")
    task = "T-REVIEW-UNIT-01"
    sha = "a" * 64
    report = make_report(task)
    report["_sha256"] = sha
    sig = sig_for(report, sha)
    dec = decision_for(report)

    baseline = gates.check_finalize(dec, dict(report), sig, allowlist=["老板"])
    check("负 baseline：无复核 → F7 拒", any("不得 approved" in x for x in baseline), baseline)
    check("负 baseline：原因写明缺 review.json",
          any("review.json" in x for x in baseline), baseline)

    ok_review = make_review(report, sha)
    released = gates.check_finalize(dec, dict(report), sig, allowlist=["老板"], review=ok_review)
    check("正控：46.67 + 合法 review → 放行", released == [], released)
    check("正控：review_errors 空列表", gates.review_errors(report, sig, ok_review, ["老板"]) == [])

    def reject(name, want, review=None, rep=None, signature=None, allowlist=("老板",)):
        _rep = dict(rep if rep is not None else report)
        _sig = signature if signature is not None else sig
        errs = gates.check_finalize(dec, _rep, _sig, allowlist=list(allowlist), review=review)
        joined = "；".join(errs)
        check(name, bool(errs) and want in joined, joined)

    reject("负例：缺 review → 仍拒", "review.json", review=None)
    reject("负例：sheet 不一致（复核 A 签 B）→ 拒", "sha256",
           review=make_review(report, "b" * 64))
    reject("负例：「复核 A 签 B」点名", "复核 A 签 B", review=make_review(report, "b" * 64))
    reject("负例：reviewed_by 不在白名单 → 拒", "不在白名单",
           review=make_review(report, sha, reviewed_by="路人甲"))
    reject("负例：reason 空 → 拒", "reason",
           review=make_review(report, sha, reason=""))
    reject("负例：reason 只有 3 字 → 拒", "≥10 字",
           review=make_review(report, sha, reason="太短了"))
    reject("负例：decisions 空 → 拒", "decisions",
           review=make_review(report, sha, decisions=[]))
    reject("负例：schema 不是 review.v1 → 拒", "schema",
           review=make_review(report, sha, schema="review.v0"))
    reject("负例：original_verdict 与现档位不一致 → 拒", "原档位",
           review=make_review(report, sha, original_verdict="reference_only"))
    reject("负例：跨任务复核 → 拒", "跨任务",
           review=make_review(report, sha, task_id="T-OTHER-99"))
    reject("负例：reviewed_at 无时区 → 拒", "时区",
           review=make_review(report, sha, reviewed_at="2026-11-02T10:00:00"))
    reject("负例：review 绑定的 sha 与当前报告不一致 → 拒", "当前报告不一致",
           review=make_review(report, "c" * 64))
    reject("负例：decisions 条目缺 verdict → 拒", "verdict",
           review=make_review(report, sha,
                              decisions=[{"finding_id": "F1", "note": "只有 id 没处置"}]))

    # no_output（<40 分，最差档）：即便复核本身合法，也一律不放行
    worst = make_report("T-REVIEW-UNIT-02", total=33.33, verdict="no_output",
                        one_line="不可签：数据不足，待人工排查", scope="no_stop")
    worst["_sha256"] = sha
    worst_sig = sig_for(worst, sha)
    worst_dec = decision_for(worst)
    worst_review = make_review(worst, sha)
    errs = gates.check_finalize(worst_dec, dict(worst), worst_sig,
                                allowlist=["老板"], review=worst_review)
    check("负例：no_output(33.33) + 合法 review → 仍拒", bool(errs), errs)
    check("负例：no_output 原因点名最差档",
          any("no_output" in x and "最差档" in x for x in errs), errs)
    check("负例：no_output 的 review_errors 直接拒",
          any("no_output" in x for x in gates.review_errors(worst, worst_sig, worst_review, ["老板"])))

    # 最差档按实测总分兜底：把 33.33 分报告改标成 force_manual_review 也放不出去
    mislabeled = make_report("T-REVIEW-UNIT-04", total=33.33, verdict="force_manual_review",
                             one_line="不可签：数据不足", scope="no_stop")
    mislabeled["_sha256"] = sha
    _e = gates.review_errors(mislabeled, sig_for(mislabeled, sha), make_review(mislabeled, sha),
                             ["老板"])
    check("负例：33.33 分改标 force_manual_review + 合法 review → 仍拒",
          bool(_e) and any("低于 40 分" in x for x in _e), _e)

    # 复核只对「档位不可签」生效：可签档（output）自判 no_stop 不得被复核豁免
    mixed = make_report("T-REVIEW-UNIT-03", total=86.67, verdict="output",
                        one_line="不可签：口径待补", scope="no_stop")
    mixed["_sha256"] = sha
    mixed_review = make_review(mixed, sha)
    errs = gates.check_finalize(decision_for(mixed), dict(mixed), sig_for(mixed, sha),
                                allowlist=["老板"], review=mixed_review)
    check("负例：可签档 + no_stop 不被复核豁免", bool(errs) and any("不得 approved" in x for x in errs), errs)

    # 空白名单 fail-closed（复核人白名单与签字人同源）
    errs = gates.check_finalize(dec, dict(report), sig, allowlist=(), review=ok_review)
    check("负例：空白名单 → 复核 fail closed", any("allowlist 为空" in x for x in errs), errs)

    # history 上限 20（append-only，不允许无限膨胀）
    too_long = make_review(report, sha, history=[{"reviewed_by": "老板"}] * 21)
    check("负例：history > 20 条 → 拒",
          any("最多保留 20 条" in x for x in gates.review_field_errors(too_long, ["老板"])))

    # 坏 review.json 当「无复核」处理（绝不 fail-open）
    bad_dir = tmp / "bad_review_dir"
    bad_dir.mkdir(parents=True, exist_ok=True)
    (bad_dir / "review.json").write_text("{not json", encoding="utf-8")
    check("负例：review.json 损坏 → load_review=None（fail-closed）",
          gates.load_review(bad_dir) is None)

    # gate confirm 回写报告字节 → 旧复核必然失效；返回体必须显式回显，不静默作废
    inv_dir = tmp / "confirm_invalidation"
    inv_dir.mkdir(parents=True, exist_ok=True)
    rep = {"task_id": "T-REVIEW-UNIT-05", "security_gate_requested": True,
           "security_gate": {"status": "pending_user_confirmation"}, "recommendation": []}
    dump_json(inv_dir / "report.json", rep)
    pre = sha256_bytes((inv_dir / "report.json").read_bytes())
    dump_json(inv_dir / "review.json", make_review(rep, pre))
    doc = gate_mod.confirm(str(inv_dir), "老板", "同意", json.loads(json.dumps(rep)))
    check("confirm 回写字节 → review_invalidated=true", doc.get("review_invalidated") is True, doc)
    check("review_invalidated 为 bool", isinstance(doc.get("review_invalidated"), bool), doc)

    clean_dir = tmp / "confirm_no_review"
    clean_dir.mkdir(parents=True, exist_ok=True)
    dump_json(clean_dir / "report.json", {"task_id": "T-REVIEW-UNIT-06"})
    doc = gate_mod.confirm(str(clean_dir), "老板", "", {"task_id": "T-REVIEW-UNIT-06"})
    check("无 review.json → review_invalidated=false", doc.get("review_invalidated") is False, doc)


# --------------------------------------------------------------------------- #
# 1b) /sign 入口判据（server.app.sign_block_reason，行为级）
# --------------------------------------------------------------------------- #
def case_sign_entry() -> None:
    print("\n[1b] /sign 入口：复核放行与门同口径；可签档自判 no_stop 不被豁免")
    import types

    _nl = types.ModuleType("notify_lark")
    for _name in ("notify_signed", "notify", "send", "notify_gate"):
        setattr(_nl, _name, lambda *a, **k: None)
    sys.modules.setdefault("notify_lark", _nl)
    from server.app import sign_block_reason  # noqa: E402

    sha = "a" * 64
    task = "T-REVIEW-SIGN-01"
    report = make_report(task)
    report["_sha256"] = sha
    review = make_review(report, sha)
    assembled = {"state": "assembled"}

    check("入口正控：46.67 + 合法 review → 允许签字",
          sign_block_reason(report, assembled, {}, review=review,
                            allowlist=("老板",), report_sha256=sha) is None)
    check("入口负例：无 review → 拒签",
          sign_block_reason(report, assembled, {}, review=None,
                            allowlist=("老板",), report_sha256=sha) is not None)
    check("入口负例：复核 A 签 B → 拒签",
          sign_block_reason(report, assembled, {}, review=make_review(report, "b" * 64),
                            allowlist=("老板",), report_sha256=sha) is not None)
    check("入口负例：reviewed_by 不在白名单 → 拒签",
          sign_block_reason(report, assembled, {},
                            review=make_review(report, sha, reviewed_by="路人甲"),
                            allowlist=("老板",), report_sha256=sha) is not None)
    check("入口负例：blocked 状态有复核也不放行",
          sign_block_reason(report, {"state": "blocked"}, {}, review=review,
                            allowlist=("老板",), report_sha256=sha) is not None)

    worst = make_report("T-REVIEW-SIGN-02", total=33.33, verdict="no_output",
                        one_line="不可签：数据不足，待人工排查")
    worst["_sha256"] = sha
    _block = sign_block_reason(worst, assembled, {}, review=make_review(worst, sha),
                               allowlist=("老板",), report_sha256=sha)
    check("入口负例：no_output + 合法 review → 仍拒签", _block is not None and "no_output" in _block)

    mislabeled = make_report("T-REVIEW-SIGN-04", total=33.33, verdict="force_manual_review",
                             one_line="不可签：数据不足", scope="no_stop")
    mislabeled["_sha256"] = sha
    _block = sign_block_reason(mislabeled, assembled, {}, review=make_review(mislabeled, sha),
                               allowlist=("老板",), report_sha256=sha)
    check("入口负例：33.33 分改标 force_manual_review 也不放行",
          _block is not None and "低于 40 分" in _block, _block)

    mixed = make_report("T-REVIEW-SIGN-03", total=86.67, verdict="output",
                        one_line="不可签：口径待补", scope="no_stop")
    mixed["_sha256"] = sha
    _block = sign_block_reason(mixed, assembled, {}, review=make_review(mixed, sha),
                               allowlist=("老板",), report_sha256=sha)
    check("入口负例：output + no_stop 不被复核豁免",
          _block is not None and "复核只放行非 output 档" in _block, _block)


# --------------------------------------------------------------------------- #
# 2) CLI：tools/finalize_delivery.py（真文件、真 sha、真的不改分）
# --------------------------------------------------------------------------- #
def _finalize_args(report_path: Path, loop_dir: Path, sig_path: Path) -> list:
    return [str(PY), "-B", str(ROOT / "tools" / "finalize_delivery.py"),
            "--report", str(report_path), "--loop-dir", str(loop_dir),
            "--signature", str(sig_path), "--allow-signers", "老板"]


def _run_finalize(report_path: Path, loop_dir: Path, sig_path: Path):
    proc = subprocess.run(_finalize_args(report_path, loop_dir, sig_path),
                          cwd=str(ROOT), capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=120)
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def _case_dir(base: Path, name: str, report: dict) -> tuple:
    tdir = base / name
    (tdir / "loop").mkdir(parents=True, exist_ok=True)
    report_path = tdir / "report.json"
    dump_json(report_path, report)
    dump_json(tdir / "loop" / "decision.json", decision_for(report))
    sha = sha256_file(report_path)
    dump_json(tdir / "report.signature.json", sig_for(report, sha))
    return tdir, report_path, sha


def case_cli(tmp: Path) -> None:
    print("\n[2] CLI：finalize_delivery.py 认 review.json，且报告字节与分数不变")
    base = tmp / "cli"
    task = "T-REVIEW-CLI-01"
    report = make_report(task)
    tdir, report_path, sha = _case_dir(base, "no_review", report)
    before = report_path.read_bytes()
    rc, out = _run_finalize(report_path, tdir / "loop", tdir / "report.signature.json")
    check("负例 CLI：无 review → 非 0 退出", rc != 0, out[-500:])
    check("负例 CLI：原因含 review.json", "review.json" in out, out[-500:])
    check("负例 CLI：不改 decision（仍 proposed）",
          json.loads((tdir / "loop" / "decision.json").read_text(encoding="utf-8"))["status"] == "proposed")
    check("负例 CLI：报告字节不变", report_path.read_bytes() == before)

    # 正控：写入合法 review（钉在当前报告字节的 sha 上）→ approved
    dump_json(tdir / "review.json", make_review(report, sha))
    before = report_path.read_bytes()
    before_sha = sha256_bytes(before)
    rc, out = _run_finalize(report_path, tdir / "loop", tdir / "report.signature.json")
    decision = json.loads((tdir / "loop" / "decision.json").read_text(encoding="utf-8"))
    check("正控 CLI：46.67 + 合法 review → approved", rc == 0 and decision.get("status") == "approved",
          out[-700:])
    after = report_path.read_bytes()
    check("正控 CLI：报告字节 sha 不变（复核不改报告）", sha256_bytes(after) == before_sha)
    check("正控 CLI：report.json 逐字节不变", after == before)
    scored = json.loads(after.decode("utf-8"))
    check("正控 CLI：scorecard.total 仍是 46.67（不改分）",
          scored["scorecard"]["total"] == 46.67, scored["scorecard"])
    check("正控 CLI：scorecard.verdict 仍是 force_manual_review",
          scored["scorecard"]["verdict"] == "force_manual_review", scored["scorecard"])
    check("正控 CLI：sign_scope 字段本身未被改写", scored["sign_scope"] == "no_stop")
    check("正控 CLI：输出 review_applied=true", '"review_applied": true' in out, out[-400:])

    # 负例：复核 A 签 B（review 绑的是另一份报告的 sha）
    task2 = "T-REVIEW-CLI-02"
    report2 = make_report(task2)
    tdir2, report_path2, sha2 = _case_dir(base, "sha_mismatch", report2)
    dump_json(tdir2 / "review.json", make_review(report2, "b" * 64))
    rc, out = _run_finalize(report_path2, tdir2 / "loop", tdir2 / "report.signature.json")
    check("负例 CLI：复核 A 签 B → 拒", rc != 0 and "sha256" in out, out[-700:])
    check("负例 CLI：复核 A 签 B 点名",
          "复核 A 签 B" in out or "重新复核" in out, out[-700:])

    # 负例：no_output(33.33) + 合法 review → 仍拒
    task3 = "T-REVIEW-CLI-03"
    worst = make_report(task3, total=33.33, verdict="no_output",
                        one_line="不可签：数据不足，待人工排查")
    tdir3, report_path3, sha3 = _case_dir(base, "no_output", worst)
    dump_json(tdir3 / "review.json", make_review(worst, sha3))
    rc, out = _run_finalize(report_path3, tdir3 / "loop", tdir3 / "report.signature.json")
    check("负例 CLI：no_output(33.33) + 合法 review → 仍拒", rc != 0 and "no_output" in out, out[-700:])
    check("负例 CLI：no_output 不改 decision",
          json.loads((tdir3 / "loop" / "decision.json").read_text(encoding="utf-8"))["status"] == "proposed")


# --------------------------------------------------------------------------- #
# 3) HTTP：POST /review → /sign → /finalize（两个入口都认复核）
# --------------------------------------------------------------------------- #
def _free_port() -> int:
    sock = socket.socket()
    try:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
    finally:
        sock.close()


def _http(method: str, url: str, body=None, timeout=20):
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(raw)
        except ValueError:
            return exc.code, {"raw": raw}


def _wait_health(port: int, log_path: Path, deadline: float = 20.0) -> bool:
    url = f"http://127.0.0.1:{port}/api/health"
    end = time.time() + deadline
    while time.time() < end:
        try:
            status, _ = _http("GET", url, timeout=1)
            if status == 200:
                return True
        except Exception:  # noqa: BLE001 - 启动期连接被拒是正常的
            pass
        time.sleep(0.2)
    try:
        print("[debug] server log tail:", log_path.read_text(encoding="utf-8", errors="replace")[-800:])
    except OSError:
        pass
    return False


def _make_task(runs: Path, task_id: str, state: str, report: dict | None = None,
               with_report: bool = True) -> Path:
    tdir = runs / task_id
    tdir.mkdir(parents=True, exist_ok=True)
    dump_json(tdir / "task.json", {"task_id": task_id, "title": "复核验收", "goal": "复核放行"})
    if with_report:
        doc = report if report is not None else make_report(task_id)
        dump_json(tdir / "report.json", doc)
        dump_json(tdir / "loop" / "decision.json", decision_for(doc))
    dump_json(tdir / "status.json", {"state": state, "step": "assemble"})
    return tdir


def case_http(tmp: Path) -> None:
    print("\n[3] HTTP：POST /review → /sign → /finalize（复核只放行、不改分）")
    runs = tmp / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    log_path = tmp / "server.log"
    port = _free_port()
    env = dict(os.environ)
    env.update({
        "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1",
        "BOT_RUNS_ROOT": str(runs),
        "BOT_LEDGER_ROOT": str(tmp / "ledger"),
        "BOT_WORKITEMS_FILE": str(tmp / "workitems.json"),
        "BOT_EVENTS_FILE": str(tmp / "events.jsonl"),
    })
    env.pop("BOT_SIGNERS", None)
    env.pop("BOT_SIGNER", None)
    log_fh = open(log_path, "w", encoding="utf-8")
    proc = subprocess.Popen(
        [str(PY), "-B", str(ROOT / "server" / "app.py"), "--port", str(port),
         "--root", str(ROOT), "--allow-signers", "老板,boss"],
        cwd=str(ROOT), env=env, stdout=log_fh, stderr=subprocess.STDOUT, text=True,
        encoding="utf-8", errors="replace")
    base = f"http://127.0.0.1:{port}"
    try:
        if not _wait_health(port, log_path):
            check("HTTP：服务启动", False, "health 超时")
            return
        check("HTTP：服务启动", True)

        # 3a) 校验 400：reason 太短 / 未 assembled 409 / 报告不存在 409
        task_ok = "T-REVIEW-HTTP-01"
        tdir = _make_task(runs, task_ok, "assembled")
        status, payload = _http("POST", f"{base}/api/tasks/{task_ok}/review",
                                {"reviewed_by": "老板", "reason": "太短", "decisions": []})
        check("HTTP 负例：reason 太短 → 400", status == 400, (status, payload))
        status, payload = _http("POST", f"{base}/api/tasks/{task_ok}/review",
                                {"reviewed_by": "路人甲", "reason": "理由足够长但不在白名单里",
                                 "decisions": [{"finding_id": "F1", "verdict": "accept", "note": "x"}]})
        check("HTTP 负例：reviewed_by 不在白名单 → 403", status == 403, (status, payload))

        task_running = "T-REVIEW-HTTP-02"
        _make_task(runs, task_running, "running")
        status, payload = _http("POST", f"{base}/api/tasks/{task_running}/review",
                                {"reviewed_by": "老板", "reason": "任务还在跑，不该能复核",
                                 "decisions": [{"finding_id": "F1", "verdict": "accept", "note": "x"}]})
        check("HTTP 负例：未 assembled → 409", status == 409, (status, payload))

        task_noreport = "T-REVIEW-HTTP-03"
        _make_task(runs, task_noreport, "assembled", with_report=False)
        status, payload = _http("POST", f"{base}/api/tasks/{task_noreport}/review",
                                {"reviewed_by": "老板", "reason": "没有报告时不该能复核",
                                 "decisions": [{"finding_id": "F1", "verdict": "accept", "note": "x"}]})
        check("HTTP 负例：报告不存在 → 409", status == 409, (status, payload))

        # 3b) 正控：写 review → 报告字节不变 → /sign（复核放行）→ /finalize
        report_path = tdir / "report.json"
        before = report_path.read_bytes()
        decisions = [
            {"finding_id": "F1", "verdict": "accept", "note": "换模记录与补料率爬升同步，证据成立"},
            {"recommendation_id": "R1", "verdict": "reject",
             "note": "ROI 口径偏乐观，交付时按人工复核口径说明，不改分"},
        ]
        status, payload = _http("POST", f"{base}/api/tasks/{task_ok}/review",
                                {"reviewed_by": "老板",
                                 "reason": "复核原始数据与证据链：结论成立，评分偏严，接受原结论不改分，放行交付。",
                                 "decisions": decisions})
        check("HTTP 正控：POST /review → 200", status == 200, (status, payload))
        review_path = tdir / "review.json"
        check("HTTP 正控：落盘 runs/<id>/review.json", review_path.is_file())
        review_doc = json.loads(review_path.read_text(encoding="utf-8")) if review_path.is_file() else {}
        check("HTTP 正控：review.schema=review.v1", review_doc.get("schema") == "review.v1", review_doc)
        check("HTTP 正控：review.report_sha256 == 当前报告 sha",
              review_doc.get("report_sha256") == sha256_bytes(before), review_doc)
        check("HTTP 正控：original_verdict 记录为 force_manual_review",
              review_doc.get("original_verdict") == "force_manual_review", review_doc)
        check("HTTP 正控：reviewed_at 带时区", "+" in str(review_doc.get("reviewed_at")), review_doc)
        check("HTTP 正控：路径在响应里", str(review_path) == str(payload.get("path") or ""), payload)
        check("HTTP 正控：复核不改报告字节", report_path.read_bytes() == before)

        # 复核必须留审计事件（run.log：phase=review / action=confirmed）
        run_log = tdir / "run.log"
        rev_events = []
        if run_log.is_file():
            for _line in run_log.read_text(encoding="utf-8").splitlines():
                _line = _line.strip()
                if not _line:
                    continue
                try:
                    _ev = json.loads(_line)
                except ValueError:
                    continue
                if _ev.get("phase") == "review":
                    rev_events.append(_ev)
        check("HTTP 正控：复核写 run.log 审计事件（phase=review/action=confirmed）",
              len(rev_events) == 1 and rev_events[0].get("action") == "confirmed"
              and (rev_events[0].get("meta") or {}).get("reviewed_by") == "老板",
              rev_events)

        status, payload = _http("POST", f"{base}/api/tasks/{task_ok}/sign", {"signer": "老板"})
        check("HTTP 正控：/sign 认复核 → 200", status == 200, (status, payload))
        check("HTTP 正控：/sign 标 review_applied=true", payload.get("review_applied") is True, payload)

        status, payload = _http("POST", f"{base}/api/tasks/{task_ok}/finalize")
        decision = json.loads((tdir / "loop" / "decision.json").read_text(encoding="utf-8"))
        check("HTTP 正控：/finalize → approved", status == 200 and decision.get("status") == "approved",
              (status, payload, decision.get("status")))
        after = report_path.read_bytes()
        check("HTTP 正控：收口后报告字节仍不变", after == before)
        scored = json.loads(after.decode("utf-8"))
        check("HTTP 正控：total 不变（46.67）", scored["scorecard"]["total"] == 46.67, scored["scorecard"])
        check("HTTP 正控：verdict 不变（force_manual_review）",
              scored["scorecard"]["verdict"] == "force_manual_review")
        check("HTTP 正控：sign_scope 不变（no_stop）", scored["sign_scope"] == "no_stop")

        # 3c) 重复复核 → append-only history 保留旧记录（最多 20）
        status, payload = _http("POST", f"{base}/api/tasks/{task_ok}/review",
                                {"reviewed_by": "老板",
                                 "reason": "二次复核：T+7 数据仍支持原结论，维持不改分，继续放行。",
                                 "decisions": decisions})
        review2 = json.loads(review_path.read_text(encoding="utf-8"))
        check("HTTP 正控：重复复核 → 200", status == 200, (status, payload))
        check("HTTP 正控：history 保留旧记录",
              len(review2.get("history") or []) == 1
              and (review2["history"][0].get("reason") or "").startswith("复核原始数据"), review2.get("history"))
        check("HTTP 正控：重复复核重新钉 sha",
              review2.get("report_sha256") == sha256_bytes(report_path.read_bytes()))

        # 3d) /sign 复核 A 签 B：复核后报告被改字节 → 409，且原因点名 sha
        task_tamper = "T-REVIEW-HTTP-04"
        tdir_t = _make_task(runs, task_tamper, "assembled")
        status, payload = _http("POST", f"{base}/api/tasks/{task_tamper}/review",
                                {"reviewed_by": "老板",
                                 "reason": "复核通过，随后报告被改动以验证钉死不失效。",
                                 "decisions": [{"finding_id": "F1", "verdict": "accept", "note": "证据成立"}]})
        check("HTTP 夹具：篡改前复核 200", status == 200, (status, payload))
        (tdir_t / "report.json").write_bytes((tdir_t / "report.json").read_bytes() + b"\n")
        status, payload = _http("POST", f"{base}/api/tasks/{task_tamper}/sign", {"signer": "老板"})
        check("HTTP 负例：报告改动后 /sign → 409（复核钉死）", status == 409, (status, payload))
        check("HTTP 负例：409 原因点名 sha256/重新复核",
              "sha256" in str(payload) or "重新复核" in str(payload), payload)

        # 3e) no_output：复核写入后 /sign 仍拒
        task_worst = "T-REVIEW-HTTP-05"
        worst = make_report(task_worst, total=33.33, verdict="no_output",
                            one_line="不可签：数据不足，待人工排查")
        tdir_w = _make_task(runs, task_worst, "assembled", report=worst)
        status, payload = _http("POST", f"{base}/api/tasks/{task_worst}/review",
                                {"reviewed_by": "老板",
                                 "reason": "最差档也尝试复核：33.33 分不得被复核放行，这里必须仍是拒。",
                                 "decisions": [{"finding_id": "F1", "verdict": "reject", "note": "证据不足"}]})
        check("HTTP 夹具：no_output 复核记录可写入（留痕）", status == 200, (status, payload))
        status, payload = _http("POST", f"{base}/api/tasks/{task_worst}/sign", {"signer": "老板"})
        check("HTTP 负例：no_output + 复核 → /sign 仍 409", status == 409, (status, payload))
        check("HTTP 负例：no_output 的 409 点名最差档", "no_output" in str(payload), payload)

        # 3f) 真实顺序：/gate confirm（会回写 report.json）→ /review → /sign → /finalize
        task_g = "T-REVIEW-HTTP-06"
        tdir_g = _make_task(runs, task_g, "assembled")
        status, payload = _http("POST", f"{base}/api/tasks/{task_g}/gate",
                                {"action": "confirm", "confirmer": "老板", "comment": "同意交付口径"})
        check("HTTP 3f：/gate confirm → 200", status == 200, (status, payload))
        check("HTTP 3f：无复核时 confirm 回显 review_invalidated=false",
              payload.get("review_invalidated") is False, payload)
        gate_sha = sha256_bytes((tdir_g / "report.json").read_bytes())
        status, payload = _http("POST", f"{base}/api/tasks/{task_g}/review",
                                {"reviewed_by": "老板",
                                 "reason": "gate 确认后复核：报告字节已定稿，证据与结论一致，放行不改分。",
                                 "decisions": [{"finding_id": "F1", "verdict": "accept", "note": "证据成立"}]})
        check("HTTP 3f：confirm 后复核 → 钉住 confirm 之后的报告字节",
              status == 200 and (payload.get("review") or {}).get("report_sha256") == gate_sha,
              (status, payload))
        status, payload = _http("POST", f"{base}/api/tasks/{task_g}/sign", {"signer": "老板"})
        check("HTTP 3f：confirm 后复核 → /sign 200", status == 200, (status, payload))
        status, payload = _http("POST", f"{base}/api/tasks/{task_g}/finalize")
        _dec_g = json.loads((tdir_g / "loop" / "decision.json").read_text(encoding="utf-8"))
        check("HTTP 3f：confirm→review→sign→finalize → approved",
              status == 200 and _dec_g.get("status") == "approved", (status, payload, _dec_g.get("status")))

        # 反向顺序：先复核再 confirm → confirm 回写报告字节，旧复核失效（fail-closed）
        task_g2 = "T-REVIEW-HTTP-07"
        tdir_g2 = _make_task(runs, task_g2, "assembled")
        status, _ = _http("POST", f"{base}/api/tasks/{task_g2}/review",
                          {"reviewed_by": "老板",
                           "reason": "先复核后 confirm：验证 confirm 回写报告会作废旧复核，必须拒签。",
                           "decisions": [{"finding_id": "F1", "verdict": "accept", "note": "证据成立"}]})
        check("HTTP 3f 夹具：先复核 200", status == 200)
        status, payload_c = _http("POST", f"{base}/api/tasks/{task_g2}/gate",
                                  {"action": "confirm", "confirmer": "老板", "comment": "同意"})
        check("HTTP 3f：先复核后 confirm → 响应回显 review_invalidated=true",
              status == 200 and payload_c.get("review_invalidated") is True, (status, payload_c))
        status, payload = _http("POST", f"{base}/api/tasks/{task_g2}/sign", {"signer": "老板"})
        check("HTTP 3f：先复核后 confirm → /sign 409（旧复核失效，需重做）",
              status == 409, (status, payload))
        check("HTTP 3f：409 文案明确「复核已被 gate 确认作废，请重新复核」",
              "作废" in str(payload), payload)

        # 作废后重新复核 → 正常放行（作废不是死路）
        status, _ = _http("POST", f"{base}/api/tasks/{task_g2}/review",
                          {"reviewed_by": "老板",
                           "reason": "confirm 回写后重新复核：以当前报告字节为准，结论不变，放行不改分。",
                           "decisions": [{"finding_id": "F1", "verdict": "accept", "note": "证据成立"}]})
        check("HTTP 3f：作废后重新复核 → 200", status == 200, status)
        status, payload = _http("POST", f"{base}/api/tasks/{task_g2}/sign", {"signer": "老板"})
        check("HTTP 3f：重新复核后 /sign → 200", status == 200, (status, payload))
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass
        log_fh.close()


# --------------------------------------------------------------------------- #
# 4) Maker–Checker：复核人白名单（BOT_REVIEWERS_FILE）与签字人分离
# --------------------------------------------------------------------------- #
def case_http_reviewers(tmp: Path) -> None:
    print("\n[4] Maker–Checker：复核人白名单 + 复核人≠签字人（BOT_REVIEWERS_FILE）")
    runs = tmp / "runs_reviewers"
    runs.mkdir(parents=True, exist_ok=True)
    log_path = tmp / "server_reviewers.log"
    reviewers_file = tmp / "reviewers.json"
    dump_json(reviewers_file, {"reviewers": [
        {"id": "ou_zhang", "id_type": "open_id", "name": "张复核",
         "reviewer": "张复核", "chat_id": ""},
        {"id": "ou_li", "id_type": "open_id", "name": "李复核",
         "reviewer": "李复核", "chat_id": ""},
    ]})
    # 结构同 im_signers.json：可直接复用（signer 当 reviewer），坏文件 fail-closed
    from server.app import load_reviewers  # noqa: PLC0415 - 需 notify_lark 桩（case_sign_entry 已装）
    im_shape = tmp / "im_signers_shape.json"
    dump_json(im_shape, {"signers": [
        {"id": "ou_x", "id_type": "open_id", "name": "Mo", "signer": "老板", "chat_id": ""}]})
    check("复核人白名单兼容 im_signers 结构（signer 即 reviewer）",
          load_reviewers(im_shape) == ("老板",), load_reviewers(im_shape))
    bad_file = tmp / "reviewers_bad.json"
    bad_file.write_text("{not json", encoding="utf-8")
    try:
        load_reviewers(bad_file)
        check("坏复核人白名单 → ValueError（fail-closed）", False, "未抛错")
    except ValueError as exc:
        check("坏复核人白名单 → ValueError（fail-closed）", "不可解析" in str(exc), str(exc))
    port = _free_port()
    env = dict(os.environ)
    env.update({
        "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1",
        "BOT_RUNS_ROOT": str(runs),
        "BOT_LEDGER_ROOT": str(tmp / "ledger_reviewers"),
        "BOT_WORKITEMS_FILE": str(tmp / "workitems_reviewers.json"),
        "BOT_EVENTS_FILE": str(tmp / "events_reviewers.jsonl"),
        "BOT_REVIEWERS_FILE": str(reviewers_file),
    })
    env.pop("BOT_SIGNERS", None)
    env.pop("BOT_SIGNER", None)
    log_fh = open(log_path, "w", encoding="utf-8")
    proc = subprocess.Popen(
        [str(PY), "-B", str(ROOT / "server" / "app.py"), "--port", str(port),
         "--root", str(ROOT), "--allow-signers", "老板,boss,张复核,李复核"],
        cwd=str(ROOT), env=env, stdout=log_fh, stderr=subprocess.STDOUT, text=True,
        encoding="utf-8", errors="replace")
    base = f"http://127.0.0.1:{port}"
    try:
        if not _wait_health(port, log_path):
            check("HTTP 复核人：服务启动", False, "health 超时")
            return
        check("HTTP 复核人：服务启动", True)

        task_a = "T-REVIEW-RV-01"
        _make_task(runs, task_a, "assembled")
        status, payload = _http(
            "POST", f"{base}/api/tasks/{task_a}/review",
            {"reviewed_by": "老板",
             "reason": "复核人白名单启用后，签字人不得自己复核自己，必须拒。",
             "decisions": [{"finding_id": "F1", "verdict": "accept", "note": "证据成立"}]})
        check("HTTP 复核人负例：不在 reviewers 白名单 → 403",
              status == 403 and "复核人" in str(payload), (status, payload))

        task_b = "T-REVIEW-RV-02"
        _make_task(runs, task_b, "assembled")
        status, payload = _http(
            "POST", f"{base}/api/tasks/{task_b}/review",
            {"reviewed_by": "张复核",
             "reason": "复核人独立于签字人：先复核，随后由同一人签字应被拒。",
             "decisions": [{"finding_id": "F1", "verdict": "accept", "note": "证据成立"}]})
        check("HTTP 复核人正控：白名单内复核 → 200", status == 200, (status, payload))
        status, payload = _http("POST", f"{base}/api/tasks/{task_b}/sign", {"signer": "张复核"})
        check("HTTP 负例：复核人=签字人 → 403 且文案点名",
              status == 403 and "复核人不得与签字人相同" in str(payload), (status, payload))

        task_c = "T-REVIEW-RV-03"
        _make_task(runs, task_c, "assembled")
        status, payload = _http(
            "POST", f"{base}/api/tasks/{task_c}/review",
            {"reviewed_by": "李复核",
             "reason": "复核人李复核与签字人老板分离：复核后由老板签字应放行。",
             "decisions": [{"finding_id": "F1", "verdict": "accept", "note": "证据成立"}]})
        check("HTTP 复核人正控：复核 200（李复核）", status == 200, (status, payload))
        status, payload = _http("POST", f"{base}/api/tasks/{task_c}/sign", {"signer": "老板"})
        check("HTTP 正控：复核人≠签字人 → /sign 200", status == 200, (status, payload))
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass
        log_fh.close()


# --------------------------------------------------------------------------- #
def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    tmp = Path(tempfile.mkdtemp(prefix="bot-review-gate-"))
    try:
        case_gate(tmp)
        case_sign_entry()
        case_cli(tmp)
        case_http(tmp)
        case_http_reviewers(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{CHECKS - len(FAILS)}/{CHECKS} 通过")
    if FAILS:
        print("RESULT: FAIL | " + "; ".join(FAILS[:6]))
        return 1
    print("RESULT: PASS（复核只放行不改分：正控 46.67 放行；缺记录/错 sha/越权/空理由/空处置/no_output 全拒）")
    return 0


def test_review_gate() -> None:
    """pytest 入口（本文件同时可 `python tests/test_review_gate.py` 直跑）。"""
    assert main() == 0


if __name__ == "__main__":
    raise SystemExit(main())
