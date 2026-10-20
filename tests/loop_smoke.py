#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""B17 闭环回归（纯标准库，venv 零第三方依赖）。

覆盖：
  1. assemble_report 产出的 report.decision_log_entry 已带闭环扩展字段（attribution_method/
     outcome_window/actual_action/override_reason/action_cost/increment_value）；
  2. core.report_contract.validate_outcome 的 400/500 校验矩阵；
  3. write_loop_bundle 写出的 decision.json/closure.json 带闭环扩展字段（结构化 dict）；
  4. server/app.py 的 post_outcome：finalized 才可回填、幂等覆盖、outcome.json 落盘、
     签字 report.json 字节不变、run.log 追加 followup 事件、GET 视图合并回填字段。

用法：& "<venv-python>" -B tests/loop_smoke.py
成功打印 RESULT: PASS（n/n），失败打印 RESULT: FAIL 并以非零退出。
"""
import hashlib
import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

BT = Path(__file__).resolve().parents[1]
PY = sys.executable
TMP_ID = "T-20269999-999"
CHECKS = []


def check(name, ok, detail=""):
    CHECKS.append((name, bool(ok), detail))
    print(("PASS  " if ok else "FAIL  ") + name + (f"  [{detail}]" if detail and not ok else ""))


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def run_py(args, expect=0):
    proc = subprocess.run([PY, "-B", *args], cwd=str(BT), capture_output=True,
                          text=True, encoding="utf-8", errors="replace")
    if expect is not None and proc.returncode != expect:
        raise SystemExit(f"command failed rc={proc.returncode}: {' '.join(args)}\n"
                         f"STDOUT:{proc.stdout}\nSTDERR:{proc.stderr}")
    return proc


def load_module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def test_assemble(tmp):
    src = BT / "runs" / "demo-result-2026-10-19.json"
    out = tmp / "report.json"
    run_py(["orchestration/assemble_report.py", str(src), str(out),
            "--task-id", "demo", "--date", "2026-10-19"])
    report = json.loads(out.read_text(encoding="utf-8"))
    entry = report.get("decision_log_entry") or {}
    check("assemble: decision_log_entry exists", bool(entry))
    check("assemble: attribution_method in 三值",
          entry.get("attribution_method") in ("exclusion", "counterfactual", "rule_based"),
          str(entry.get("attribution_method")))
    check("assemble: outcome_window=7d", entry.get("outcome_window") == "7d",
          str(entry.get("outcome_window")))
    check("assemble: 回填字段有值才写（不写 null 占位）",
          entry.get("actual_action") is None and entry.get("override_reason") is None
          and entry.get("observed_outcome") is None)
    check("assemble: action_cost/increment_value 缺失或有值（无 null 占位）",
          "action_cost" in entry and entry["action_cost"] is not None
          or "action_cost" not in entry)
    check("assemble: increment_value 缺失或有值（无 null 占位）",
          "increment_value" in entry and entry["increment_value"] is not None
          or "increment_value" not in entry)
    return out, report


def test_validate_outcome(tmp):
    rc = load_module(BT / "core" / "report_contract.py", "rc_mod")
    _clean, errors = rc.validate_outcome({})
    check("validate_outcome: 缺 observed_outcome -> 拒", bool(errors))
    _clean, errors = rc.validate_outcome({"observed_outcome": "x", "unknown_key": 1})
    check("validate_outcome: 未知字段 -> 拒", bool(errors))
    _clean, errors = rc.validate_outcome({"observed_outcome": "x", "source": "guess"})
    check("validate_outcome: 非法 source -> 拒", bool(errors))
    _clean, errors = rc.validate_outcome({"observed_outcome": "x", "outcome_window": "abc"})
    check("validate_outcome: 非法 window -> 拒", bool(errors))
    clean, errors = rc.validate_outcome({
        "observed_outcome": "T+7 补料率 7.8%，目标 7.2%", "source": "manual",
        "actual_action": "按建议换模", "override_reason": "",
        "action_cost": {"cny": 800, "items": [{"label": "换模工时", "cny": 800}]},
        "increment_value": {"cny": 12000}, "outcome_window": "7d",
    })
    check("validate_outcome: 合法 payload 通过", not errors, str(errors[:2]))
    check("validate_outcome: clean 保留 source/window",
          clean.get("source") == "manual" and clean.get("outcome_window") == "7d",
          json.dumps(clean, ensure_ascii=False))
    return rc


def test_write_loop_bundle(tmp, report_file):
    loop_dir = tmp / "loop"
    run_py(["tools/write_loop_bundle.py", "--report", str(report_file), "--outdir", str(loop_dir),
            "--task-id", "demo", "--review-days", "14"])
    decision = json.loads((loop_dir / "decision.json").read_text(encoding="utf-8"))
    closure = json.loads((loop_dir / "closure.json").read_text(encoding="utf-8"))
    check("bundle: decision.attribution_method in 三值",
          decision.get("attribution_method") in ("exclusion", "counterfactual", "rule_based"))
    check("bundle: decision.outcome_window=14d", decision.get("outcome_window") == "14d",
          str(decision.get("outcome_window")))
    check("bundle: 回填字段有值才写（不写 null 占位）",
          decision.get("observed_outcome") is not None or "observed_outcome" not in decision)
    check("bundle: action_cost 有值时非 null", "action_cost" not in decision or decision["action_cost"] is not None)
    check("bundle: increment_value 有值时非 null",
          "increment_value" not in decision or decision["increment_value"] is not None)
    check("bundle: closure.attribution_method 非空", bool(closure.get("attribution_method")))
    check("bundle: closure.outcome_window=14d", closure.get("outcome_window") == "14d",
          str(closure.get("outcome_window")))


def test_post_outcome(tmp, report_file, rc_module):
    app = load_module(BT / "server" / "app.py", "bot_server_app")
    cfg = app.Config(root=str(BT), owner="老板", allow_signers="老板,boss")
    tdir = BT / "runs" / TMP_ID
    shutil.rmtree(tdir, ignore_errors=True)
    tdir.mkdir(parents=True, exist_ok=True)
    report = json.loads(report_file.read_text(encoding="utf-8"))
    report["task_id"] = TMP_ID
    (tdir / "report.json").write_bytes(json.dumps(report, ensure_ascii=False, indent=2).encode("utf-8"))
    (tdir / "task.json").write_text(json.dumps({"task_id": TMP_ID, "title": "B17 闭环冒烟"}, ensure_ascii=False),
                                    encoding="utf-8")
    (tdir / "run.log").write_text(json.dumps({"ts": "2026-10-19T10:00:00+08:00", "phase": "assemble",
                                              "role": "host", "line": "报告已生成"}, ensure_ascii=False) + "\n",
                                  encoding="utf-8")
    (tdir / "status.json").write_text(json.dumps({"task_id": TMP_ID, "state": "assembled"}),
                                      encoding="utf-8")
    try:
        try:
            app.post_outcome(cfg, TMP_ID, {"observed_outcome": "x"})
            check("server: 未 finalized 回填 -> 409", False, "未抛错")
        except app.ApiError as exc:
            check("server: 未 finalized 回填 -> 409", exc.status == 409, str(exc.status))
        (tdir / "status.json").write_text(json.dumps({"task_id": TMP_ID, "state": "signed"}),
                                          encoding="utf-8")
        try:
            app.post_outcome(cfg, TMP_ID, {})
            check("server: 缺 observed_outcome -> 400", False, "未抛错")
        except app.ApiError as exc:
            check("server: 缺 observed_outcome -> 400", exc.status == 400, str(exc.status))
        before = sha256(tdir / "report.json")
        status, payload = app.post_outcome(cfg, TMP_ID, {
            "observed_outcome": "T+7 补料率 7.8%", "actual_action": "按建议先小批量换模",
            "action_cost": {"cny": 800, "items": []}, "increment_value": {"cny": 12000},
        })
        after = sha256(tdir / "report.json")
        outcome = json.loads((tdir / "outcome.json").read_text(encoding="utf-8"))
        check("server: 回填 200", status == 200, str(status))
        check("server: outcome.json 落盘且含回填字段",
              outcome.get("observed_outcome") == "T+7 补料率 7.8%" and outcome.get("report_sha256") == before)
        check("server: 签字 report.json 字节不变（A6）", before == after)
        check("server: decision_log_entry 合并回填值",
              (payload.get("decision_log_entry") or {}).get("observed_outcome") == "T+7 补料率 7.8%")
        status_doc = json.loads((tdir / "status.json").read_text(encoding="utf-8"))
        check("server: status.step=outcome 且记 sha256",
              status_doc.get("step") == "outcome" and status_doc.get("outcome_sha256"))
        events = [json.loads(line) for line in
                  (tdir / "run.log").read_text(encoding="utf-8").splitlines() if line.strip()]
        check("server: run.log 追加 followup 事件",
              any(ev.get("phase") == "followup" for ev in events), str([e.get("phase") for e in events]))
        status2, _payload2 = app.post_outcome(cfg, TMP_ID, {
            "observed_outcome": "T+7 补料率 8.0%", "source": "manual",
        })
        outcome2 = json.loads((tdir / "outcome.json").read_text(encoding="utf-8"))
        check("server: 重复回填幂等覆盖 200",
              status2 == 200 and outcome2.get("observed_outcome") == "T+7 补料率 8.0%")
        check("server: history 保留上一版", len(outcome2.get("history") or []) == 1,
              str(len(outcome2.get("history") or [])))
        report_now = json.loads((tdir / "report.json").read_text(encoding="utf-8"))
        view = app.decision_log_view(report_now, outcome2)
        check("server: 视图 observed_outcome 在新值", view.get("observed_outcome") == "T+7 补料率 8.0%")
        check("server: 视图保留原 attribution_method",
              view.get("attribution_method") in ("exclusion", "counterfactual", "rule_based"))
        off = app.get_outcome(cfg, TMP_ID)
        check("server: GET outcome pending=false 且带 outcome/decision_log_entry",
              off.get("outcome_pending") is False
              and off["outcome"].get("observed_outcome") == "T+7 补料率 8.0%"
              and bool(off.get("decision_log_entry")))
        check("server: validate_outcome 与 server 同一契约",
              callable(getattr(rc_module, "validate_outcome", None)))
    finally:
        shutil.rmtree(tdir, ignore_errors=True)


def main():
    tmp = Path(tempfile.mkdtemp(prefix="bot-loop-smoke-"))
    try:
        report_file, _report = test_assemble(tmp)
        rc_module = test_validate_outcome(tmp)
        test_write_loop_bundle(tmp, report_file)
        test_post_outcome(tmp, report_file, rc_module)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    passed = sum(1 for _n, ok, _d in CHECKS if ok)
    total = len(CHECKS)
    print(f"\nRESULT: {'PASS' if passed == total else 'FAIL'} ({passed}/{total})")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
