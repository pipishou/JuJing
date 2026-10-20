#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一键跑通一条任务：取数 → 合同 → 组装报告 → 校验 → 四卡 → 签字 → 收口。

M1 改造（§2.5）：runs/<task_id>/ 之间可并行、同日多任务不撞号、重跑不覆盖历史；
失败不落半成品（报告/任务/状态/签名全部原子写）；每次运行写 run.log（JSON lines）
与 status.json（单写者 = 本工具，server 只读尾随）。

用法（新，推荐）：
  python tools/run_task.py --task-json runs/T-20261019-001/task.json \
      --outdir runs/T-20261019-001 --workflow-result /path/result.json [--signer 老板]

用法（旧 CLI 保持兼容）：
  python tools/run_task.py --csv data.csv --line B --t-start 2026-10-05 --t-end 2026-10-11 \
      --base-start 2026-09-05 --base-end 2026-10-04 --field-map templates/field-map.example.yaml \
      --outdir runs/task-B --workflow-result result.json --expected "..." --signer smoke

产物：
  task.json / result.json / report.json / report.signature.json / status.json /
  ids.json / run.log / loop/{analysis,goal,cost,evidence.jsonl,decision.json,closure.json}
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import events, fsutil, ids, pack as pack_mod  # noqa: E402

PY = sys.executable
RUNS = Path(os.environ.get("BOT_RUNS_ROOT") or (ROOT / "runs"))


def now_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def log_line(outdir, phase, role, line, kind="status"):
    """事件统一走 core.events.append_event（M1.5 §2.2；旧 run.log 仍可读）。"""
    events.append_event(str(Path(outdir) / "run.log"), {
        "phase": phase, "role": role, "kind": kind, "line": line,
    })
    print(f"[{phase}] {line}")


def set_status(outdir, state, step="", task_id=None, error=""):
    doc = {"state": state, "step": step, "task_id": task_id, "updated_at": now_iso()}
    if error:
        doc["error"] = error
    fsutil.atomic_write_json(Path(outdir) / "status.json", doc)


def read_json(path):
    with open(path, encoding="utf-8-sig") as fh:
        return json.load(fh)


def extract_json_object(text):
    """从子步骤输出里抠出第一个 JSON 对象（子步骤 stdout 常带日志行前缀）。"""
    text = text or ""
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


def run_step(outdir, phase, role, cmd):
    """执行子步骤；失败返回 (False, output)。stdout 逐行作为 tool 事件写入 run.log。"""
    proc = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    out = (proc.stdout or "") + (proc.stderr or "")
    for text in out.splitlines():
        if text.strip():
            log_line(outdir, phase, role, text, kind="tool")
    return proc.returncode == 0, out


def looks_like_task_id(name):
    return len(name) > 9 and name.startswith("T-") and name[2:10].isdigit()


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--task-json", default="", help="任务单 JSON（task.json / intake）")
    ap.add_argument("--csv", default="", help="旧路径：原始 CSV（需要 pandas）")
    ap.add_argument("--line", default="")
    ap.add_argument("--t-start", default="")
    ap.add_argument("--t-end", default="")
    ap.add_argument("--base-start", default="")
    ap.add_argument("--base-end", default="")
    ap.add_argument("--field-map", default="")
    ap.add_argument("--outdir", default="", help="runs/<task_id>；省略则自动按 task_id 创建")
    ap.add_argument("--workflow-result", default="", help="team-workflow 结果；缺省用 outdir/result.json")
    ap.add_argument("--task-id", default="")
    ap.add_argument("--context", default="")
    ap.add_argument("--expected", default="")
    ap.add_argument("--goal", default="")
    ap.add_argument("--analysis-json", default="")
    ap.add_argument("--goal-json", default="")
    ap.add_argument("--cost-json", default="")
    ap.add_argument("--signer", default="")
    ap.add_argument("--pack-path", default="")
    ap.add_argument("--demo", action="store_true", help="组装 demo 报告（is_demo=true，跳过 require-real）")
    ap.add_argument("--allow-signers", default="")
    ap.add_argument("--defer-sign", action="store_true",
                    help="只跑到报告/四卡校验，签字留给人工复核（Web 工作台 task_server 用）")
    args = ap.parse_args()

    # upstream v0.11.2 工作台兼容：--defer-sign 时不签字，等人工复核后再走 sign/finalize
    if args.defer_sign:
        args.signer = ""

    intake = {}
    if args.task_json:
        try:
            intake = read_json(args.task_json)
        except Exception as exc:  # noqa: BLE001
            print(f"[run_task][FAIL] 读不到 --task-json {args.task_json}: {exc}", file=sys.stderr)
            return 2

    # ---- 0) 窗口日期先校验（ISO） ----
    for win_key in ("window", "baseline_window"):
        win = intake.get(win_key) or {}
        for k in ("start", "end"):
            v = win.get(k)
            if v:
                try:
                    date.fromisoformat(str(v))
                except ValueError:
                    print(f"[run_task][FAIL] task.json {win_key}.{k} 不是 ISO 日期: {v!r}", file=sys.stderr)
                    return 2
    for flag, val in (("--t-start", args.t_start), ("--t-end", args.t_end),
                      ("--base-start", args.base_start), ("--base-end", args.base_end)):
        if val:
            try:
                date.fromisoformat(val)
            except ValueError:
                print(f"[run_task][FAIL] {flag} 不是 ISO 日期: {val!r}", file=sys.stderr)
                return 2

    # ---- 1) task_id / outdir ----
    task_id = (args.task_id or intake.get("task_id") or "").strip()
    outdir = Path(args.outdir) if args.outdir else None
    if not task_id and outdir is not None and looks_like_task_id(outdir.name):
        task_id = outdir.name
    if not task_id:
        try:
            task_id = ids.allocate(RUNS, "task")
        except ids.IdError as exc:
            print(f"[run_task][FAIL] 任务编号分配失败: {exc}", file=sys.stderr)
            return 2
    if outdir is None:
        outdir = RUNS / task_id
    outdir.mkdir(parents=True, exist_ok=True)

    state = {"outdir": outdir, "task_id": task_id}
    try:
        set_status(outdir, "running", step="init", task_id=task_id)
        log_line(outdir, "s5", "host", f"run_task start: task_id={task_id} outdir={outdir}")

        # ---- 2) task.json（intake 原样 + task_id + created_at；重跑保留首建时间） ----
        task_path = outdir / "task.json"
        old = {}
        if task_path.is_file():
            try:
                old = read_json(task_path)
            except Exception:  # noqa: BLE001
                old = {}
        doc = dict(intake)
        for k, v in intake.items():
            doc[k] = v
        doc["task_id"] = task_id
        doc.setdefault("created_at", now_iso())
        doc["updated_at"] = now_iso()
        fsutil.atomic_write_json(task_path, doc)

        # M1.5 §2.2 / A10：阶段事件 s1..s5 齐活（s1‖s2 并行阶段都出现）。
        window = doc.get("window") or {}
        log_line(outdir, "s1", "consulting-analyst",
                 f"s1 拆解任务：line={doc.get('line') or '-'} metric={doc.get('metric') or '-'} "
                 f"window={window.get('start') or '?'}~{window.get('end') or '?'}")
        log_line(outdir, "s2", "data-fetcher",
                 f"s2 取数：intake data_files={len(doc.get('data_files') or [])} 条，读取 outdir 数据文件")

        # ---- 3) workflow 结果：参数 > outdir/result.json > outdir/workflow-result.json ----
        result_src = Path(args.workflow_result) if args.workflow_result else None
        if result_src is None:
            for name in ("result.json", "workflow-result.json"):
                cand = outdir / name
                if cand.is_file():
                    result_src = cand
                    break
        if result_src is None or not result_src.is_file():
            raise RuntimeError("找不到 team-workflow 结果：请传 --workflow-result 或先写 outdir/result.json")
        target_result = outdir / "result.json"
        if result_src.resolve() != target_result.resolve():
            result_doc = read_json(result_src)
            fsutil.atomic_write_json(target_result, result_doc)
        else:
            result_doc = read_json(target_result)
        attribution = result_doc.get("attribution") if isinstance(result_doc, dict) else {}
        attribution = attribution if isinstance(attribution, dict) else {}
        log_line(outdir, "s3", "ai-attribution-expert",
                 f"s3 归因：候选因子 {len(attribution.get('finding') or [])} 个，"
                 f"排除 {len(attribution.get('excluded') or [])} 个，"
                 f"不可归因 {len(attribution.get('cannot_attribute') or [])} 条")
        log_line(outdir, "s4", "manufacturing-ops-expert", f"workflow result: {result_src.name}")

        # ---- 4) 数据文件：outdir 内 JSON 优先；旧路径走 ingest（需要 pandas） ----
        data_files = []
        for name in ("material_refill_T.json", "material_refill_T30.json"):
            cand = outdir / name
            if cand.is_file():
                data_files.append(cand)
        for item in intake.get("data_files") or []:
            cand = Path(str(item))
            if not cand.is_absolute():
                cand = ROOT / cand
            if cand.is_file() and cand not in data_files:
                data_files.append(cand)
        if not data_files and args.csv:
            ok, out = run_step(outdir, "s2", "data-fetcher", [
                PY, str(ROOT / "tools" / "ingest_task_data.py"),
                "--input", args.csv, "--line", args.line or intake.get("line", ""),
                "--t-start", args.t_start or ((intake.get("window") or {}).get("start") or ""),
                "--t-end", args.t_end or ((intake.get("window") or {}).get("end") or ""),
                "--base-start", args.base_start or ((intake.get("baseline_window") or {}).get("start") or ""),
                "--base-end", args.base_end or ((intake.get("baseline_window") or {}).get("end") or ""),
                "--outdir", str(outdir),
            ] + (["--field-map", args.field_map] if args.field_map else []))
            if not ok:
                raise RuntimeError("取数失败（ingest_task_data）：\n" + out[-1500:])
            for name in ("material_refill_T.json", "material_refill_T30.json"):
                cand = outdir / name
                if cand.is_file():
                    data_files.append(cand)

        real = not args.demo
        if real and not data_files:
            raise RuntimeError("真实任务缺少数据文件：outdir 内无 material_refill_T.json/T30.json，task.json 也未给 data_files")

        # 数据合同（有数据文件时）
        if data_files:
            t_file = next((p for p in data_files if "T30" not in p.name), data_files[0])
            t30_file = next((p for p in data_files if "T30" in p.name), None)
            ccmd = [PY, str(ROOT / "tools" / "check_data_contract.py"), "--t", str(t_file), "--strict"]
            if t30_file:
                ccmd += ["--t30", str(t30_file)]
            ok, out = run_step(outdir, "s2", "data-fetcher", ccmd)
            contract = extract_json_object(out)
            if isinstance(contract, dict):
                # 合同结论落盘：呈现层据此判定降级态（降级≠失败，只是不出建议）
                fsutil.atomic_write_json(Path(outdir) / "data_contract.json", contract)
                if contract.get("degraded"):
                    reasons = contract.get("degradation_reasons") or []
                    log_line(outdir, "s2", "data-fetcher",
                             f"数据降级（不是失败）：{contract.get('degradation_level')}｜"
                             f"{'；'.join(str(r) for r in reasons[:3])}")
            if not ok:
                raise RuntimeError("数据合同未通过（check_data_contract --strict，值级/重复日期等真违规）：\n" + out[-1500:])

        # ---- 5) 组装报告（assemble 内部负责可信门 + 原子写 + 100 分制 + pack 兜底） ----
        context = args.context or intake.get("context") or f"{args.line or intake.get('line') or 'B'}线补料率异常（真实任务）"
        expected = args.expected or intake.get("expected") or intake.get("target") or ""
        goal = args.goal or intake.get("goal") or ""
        records = outdir / "loop"
        cmd = [
            PY, str(ROOT / "orchestration" / "assemble_report.py"),
            str(outdir / "result.json"), str(outdir / "report.json"),
            "--task-id", task_id, "--context", context,
            "--records-dir", str(records), "--records-task-id", task_id,
            "--events-dir", str(outdir),
        ]
        # data_refs 与 real 解耦：demo/fixture 任务若来自真实上传，也应记录真实文件引用
        # （B10 反馈 tools/run_task.py:269：--data-refs 原在 if real 内，demo 报告回落到 demo/*）
        if data_files:
            cmd += ["--data-refs", ",".join(str(p) for p in data_files)]
        if real:
            cmd += ["--real"]
        if expected:
            cmd += ["--expected", expected]
        if goal:
            cmd += ["--goal", goal]
        if args.pack_path:
            cmd += ["--pack-path", args.pack_path]
        for flag, val in (("--analysis-json", args.analysis_json), ("--goal-json", args.goal_json),
                          ("--cost-json", args.cost_json)):
            if val:
                cmd += [flag, str(val)]
        ok, out = run_step(outdir, "s5", "host", cmd)
        if not ok:
            raise RuntimeError("组装报告失败（assemble_report）：\n" + out[-2000:])
        set_status(outdir, "running", step="assembled", task_id=task_id)

        report = read_json(outdir / "report.json")
        report_sha = __import__("hashlib").sha256((outdir / "report.json").read_bytes()).hexdigest()
        log_line(outdir, "s5", "host",
                 f"report assembled: total={report.get('scorecard', {}).get('total')} verdict={report.get('scorecard', {}).get('verdict')} is_demo={report.get('is_demo')}")

        # ---- 6) 报告契约校验（A2/A4） ----
        vcmd = [PY, str(ROOT / "tests" / "validate_report.py"), str(outdir / "report.json")]
        if real:
            vcmd.append("--require-real")
        ok, out = run_step(outdir, "gate", "compliance-reviewer", vcmd)
        if not ok:
            raise RuntimeError("报告契约校验未通过（validate_report）：\n" + out[-2000:])

        # ---- 7) 100 分制交叉核对：score_scorecard 独立调用必须给出同一 total/verdict ----
        ok, out = run_step(outdir, "gate", "compliance-reviewer",
                           [PY, str(ROOT / "tools" / "score_scorecard.py"), "--from-report", str(outdir / "report.json")])
        if not ok:
            raise RuntimeError("score_scorecard 独立调用失败：\n" + out[-1500:])
        try:
            sc_out = json.loads(out[out.find("{"):out.rfind("}") + 1])
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"score_scorecard 输出无法解析：{exc}")
        sc = report.get("scorecard") or {}
        if sc_out.get("total") != sc.get("total") or sc_out.get("verdict") != sc.get("verdict"):
            raise RuntimeError(f"报告 scorecard 与 score_scorecard 独立计算不一致：report={sc.get('total')}/{sc.get('verdict')} cli={sc_out.get('total')}/{sc_out.get('verdict')}")
        log_line(outdir, "gate", "compliance-reviewer", f"scorecard 交叉核对一致：total={sc.get('total')} verdict={sc.get('verdict')}")

        # ---- 8) 四卡完整性 ----
        if args.analysis_json and args.goal_json and args.cost_json:
            ok, out = run_step(outdir, "gate", "host", [
                PY, str(ROOT / "tools" / "validate_loop.py"),
                "--task-dir", str(records), "--require-complete",
            ])
            if not ok:
                raise RuntimeError("四卡不完整（validate_loop --require-complete）：\n" + out[-1500:])

        # ---- 9) 签字 + 收口（传了 --signer 才做） ----
        signature_path = outdir / "report.signature.json"
        decision_path = records / "decision.json"
        decision_status = None
        if args.signer:
            ok, out = run_step(outdir, "s5", "host", [
                PY, str(ROOT / "tools" / "sign_delivery.py"), str(outdir / "report.json"),
                "--signer", args.signer, "--out", str(signature_path),
            ] + (["--allow-demo"] if report.get("is_demo") else []))
            if not ok:
                raise RuntimeError("签字失败（sign_delivery）：\n" + out[-1500:])
            set_status(outdir, "awaiting_sign", step="signature_written", task_id=task_id)
            fcmd = [
                PY, str(ROOT / "tools" / "finalize_delivery.py"),
                "--report", str(outdir / "report.json"),
                "--loop-dir", str(records), "--signature", str(signature_path),
                "--gate-file", str(outdir / "gate.json"),
            ]
            # A3②：finalize fail closed —— 白名单 = --allow-signers > env BOT_SIGNERS；
            # M1.5 硬化：不再回退到本次 --signer（自签自批 = 绕过人工确认）
            allow = args.allow_signers or os.environ.get("BOT_SIGNERS") or ""
            if allow:
                fcmd += ["--allow-signers", allow]
            ok, out = run_step(outdir, "gate", "host", fcmd)
            if not ok:
                raise RuntimeError("收口拒绝（finalize_delivery）：\n" + out[-2000:])
            decision_status = "approved"
            set_status(outdir, "signed", step="finalized", task_id=task_id)
        else:
            set_status(outdir, "assembled", step="awaiting_sign", task_id=task_id)

        if decision_path.is_file():
            decision_status = decision_status or read_json(decision_path).get("status")
        if args.defer_sign and not decision_status:
            decision_status = "pending_signature"

        summary = {
            "ok": True,
            "task_id": task_id,
            "report": str(outdir / "report.json"),
            "loop": str(records),
            "signature": str(signature_path) if signature_path.is_file() else None,
            "decision_status": decision_status,
            "is_demo": report.get("is_demo"),
            "scorecard": {"total": sc.get("total"), "verdict": sc.get("verdict")},
            "sha256": report_sha,
        }
        log_line(outdir, "s5", "host", f"run_task done: ok={summary['ok']} decision={decision_status}")
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:  # noqa: BLE001
        msg = str(exc)
        print(f"[run_task][FAIL] {msg}", file=sys.stderr)
        try:
            set_status(outdir, "failed", step="error", task_id=task_id, error=msg[-1500:])
            log_line(outdir, "gate", "host", f"FAILED: {msg.splitlines()[0][:300]}")
        except Exception:  # noqa: BLE001
            pass
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
