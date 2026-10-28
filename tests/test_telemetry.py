# -*- coding: utf-8 -*-
"""遥测契约机检：schema 校验、标量红线、聚合口径、空分母。

跑法：python tests/test_telemetry.py    （非零退出 = 红）
"""
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import telemetry  # noqa: E402
from tools import flywheel_report  # noqa: E402

CASES = []


def case(fn):
    CASES.append(fn)
    return fn


@case
def 合法事件可写入且可读回(tmp):
    seq = telemetry.emit("task_created", {"task_id": "T1", "scene_id": "scene-refill",
                                          "source": "manual"}, root=tmp, mirror=False)
    rows = telemetry.read(tmp)
    assert seq == 1, seq
    assert rows[0]["event"] == "bot_team_task_created", rows[0]
    assert rows[0]["product_id"] == "bot_team", rows[0]


@case
def 缺必需字段直接抛(tmp):
    try:
        telemetry.emit("reply", {"task_id": "T1"}, root=tmp, mirror=False)
    except ValueError as exc:
        assert "letter" in str(exc), exc
        return
    raise AssertionError("缺 letter 竟然没报错")


@case
def 嵌套对象被拒(tmp):
    try:
        telemetry.emit("task_created", {"task_id": "T1", "scene_id": "x",
                                        "source": {"nested": 1}}, root=tmp, mirror=False)
    except ValueError as exc:
        assert "标量" in str(exc), exc
        return
    raise AssertionError("嵌套 props 竟然通过")


@case
def 未注册事件被拒(tmp):
    try:
        telemetry.emit("random_event", {"task_id": "T1"}, root=tmp, mirror=False)
    except ValueError as exc:
        assert "未注册" in str(exc), exc
        return
    raise AssertionError("未注册事件竟然通过")


@case
def 未声明字段被拒(tmp):
    try:
        telemetry.emit("task_created", {"task_id": "T1", "scene_id": "x",
                                        "whatever": 1}, root=tmp, mirror=False)
    except ValueError as exc:
        assert "未声明" in str(exc), exc
        return
    raise AssertionError("未声明字段竟然通过")


@case
def 空账本不出假指标(tmp):
    result = flywheel_report.analyze([])
    assert result["adoption"]["adopt_rate"] is None, result["adoption"]
    assert result["review"]["hit_rate"] is None, result["review"]
    assert result["funnel"]["reported_rate"] is None, result["funnel"]


@case
def 采纳率与命中率口径正确(tmp):
    for letter in ("a", "a", "b"):
        telemetry.emit("reply", {"task_id": "T1", "letter": letter}, root=tmp, mirror=False)
    telemetry.emit("review", {"task_id": "T1", "hit": True}, root=tmp, mirror=False)
    telemetry.emit("review", {"task_id": "T2", "hit": False}, root=tmp, mirror=False)
    result = flywheel_report.analyze(telemetry.read(tmp))
    assert result["adoption"]["adopt_rate"] == 66.7, result["adoption"]
    assert result["adoption"]["intervention_rate"] == 33.3, result["adoption"]
    assert result["review"]["hit_rate"] == 50.0, result["review"]


@case
def 角色失败率按步统计(tmp):
    telemetry.emit("step_run", {"task_id": "T1", "step": "s2", "role": "analyst",
                                "status": "ok", "duration_ms": 100, "cost_cny": 0.01},
                   root=tmp, mirror=False)
    telemetry.emit("step_run", {"task_id": "T1", "step": "s3", "role": "analyst",
                                "status": "fail", "duration_ms": 50, "error_kind": "schema"},
                   root=tmp, mirror=False)
    result = flywheel_report.analyze(telemetry.read(tmp))
    role = result["roles"]["analyst"]
    assert role["fail_rate"] == 50.0, role
    assert result["cost"]["per_task_avg"] == 0.01, result["cost"]


@case
def 版本对比生成前后哈希(tmp):
    telemetry.emit("reply", {"task_id": "T1", "letter": "b"}, root=tmp, mirror=False)
    telemetry.emit("role_change", {"agent_id": "9BZAQT", "field": "system_prompt",
                                   "before_hash": "aaa", "after_hash": "bbb",
                                   "reason": "收紧归因口径"}, root=tmp, mirror=False)
    telemetry.emit("reply", {"task_id": "T2", "letter": "a"}, root=tmp, mirror=False)
    result = flywheel_report.analyze(telemetry.read(tmp))
    assert len(result["versions"]) == 1, result["versions"]
    assert result["versions"][0]["reason"] == "收紧归因口径", result["versions"][0]


@case
def 必需字段空串也算缺失(tmp):
    try:
        telemetry.emit("step_run", {"task_id": "T1", "step": "s2", "role": "",
                                    "status": "ok", "duration_ms": 1},
                       root=tmp, mirror=False)
    except ValueError as exc:
        assert "role" in str(exc), exc
        return
    raise AssertionError("role='' 竟然通过（会被 events 补成 host，指标失真）")


@case
def 回信归属依据可记录(tmp):
    telemetry.emit("reply", {"task_id": "T1", "letter": "a", "attr": "push_index"},
                   root=tmp, mirror=False)
    rows = telemetry.read(tmp)
    assert rows[0]["attr"] == "push_index", rows[0]


@case
def 推送失败必须能记原因(tmp):
    telemetry.emit("push_sent", {"task_id": "T1", "agent_id": "JJG00N", "ok": False,
                                 "error_kind": "gateway_rc"}, root=tmp, mirror=False)
    result = flywheel_report.analyze(telemetry.read(tmp))
    assert result["events_total"] == 1, result


@case
def 回看判定可采集入库(tmp):
    runs = tmp / "runs" / "T-1"
    (runs / "loop").mkdir(parents=True)
    (runs / "task.json").write_text(json.dumps({"created_at": "2026-10-20T10:00:00+08:00"}),
                                    encoding="utf-8")
    (runs / "loop" / "closure.json").write_text(
        json.dumps({"verdict": "hit", "actual_outcome": "补料率回升 2.1pp"}, ensure_ascii=False),
        encoding="utf-8")
    got = flywheel_report.ingest_reviews(tmp)
    assert got == ["T-1"], got
    rows = telemetry.read(tmp)
    assert rows[0]["name"] == "review" and rows[0]["hit"] is True, rows
    assert flywheel_report.ingest_reviews(tmp) == [], "重复采集（幂等破了）"


@case
def 作废任务整单排除出指标(tmp):
    """append-only 下不删历史行：void 事件把整单排除出所有指标。"""
    for tid in ("T-KEEP", "T-POLLUTED"):
        telemetry.emit("task_created", {"task_id": tid, "scene_id": "s", "source": "manual"},
                       root=tmp, mirror=False)
        telemetry.emit("step_run", {"task_id": tid, "step": "s2", "role": "data",
                                    "status": "ok", "duration_ms": 1}, root=tmp, mirror=False)
    telemetry.emit("void", {"task_id": "T-POLLUTED", "reason": "pytest 合成数据"},
                   root=tmp, mirror=False)
    result = flywheel_report.analyze(telemetry.read(tmp))
    assert result["events_total"] == 5, result
    assert result["voided"]["tasks"] == ["T-POLLUTED"], result
    assert result["voided"]["rows_excluded"] == 3, result          # void 行 + 该单 2 行
    assert result["funnel"]["created"] == 1, result["funnel"]      # 只剩 T-KEEP
    assert list(result["roles"]) == ["data"], result["roles"]
    assert result["roles"]["data"]["ok"] == 1, result["roles"]


@case
def 账本路径认_BOT_LEDGER_ROOT(tmp):
    """隔离 runs 根时账本必须同址，否则测试会污染真实账本。"""
    import os
    old = os.environ.get("BOT_LEDGER_ROOT")
    os.environ["BOT_LEDGER_ROOT"] = str(tmp / "iso")
    try:
        assert telemetry.ledger_path() == tmp / "iso" / ".telemetry" / "flywheel.jsonl"
        telemetry.emit("task_created", {"task_id": "T1", "scene_id": "s", "source": "manual"},
                       mirror=False)
        assert (tmp / "iso" / ".telemetry" / "flywheel.jsonl").is_file()
    finally:
        if old is None:
            os.environ.pop("BOT_LEDGER_ROOT", None)
        else:
            os.environ["BOT_LEDGER_ROOT"] = old


@case
def 跑了_run_task_测试也不许写真实账本(tmp):
    """回归：冒烟脚本把 runs 指到临时目录时，账本必须跟着走（2026-10-27 事故复现点）。"""
    import os
    import subprocess
    import sys

    real = ROOT / ".telemetry" / "flywheel.jsonl"
    before = len(real.read_text(encoding="utf-8").splitlines()) if real.is_file() else 0
    env = dict(os.environ, BOT_RUNS_ROOT=str(tmp / "runs"), BOT_LEDGER_ROOT=str(tmp))
    env.pop("TRACK_ENDPOINT", None)
    proc = subprocess.run(
        [sys.executable, "-B", str(ROOT / "tools" / "run_task.py"), "--demo",
         "--outdir", str(tmp / "a4-task"),
         "--workflow-result", str(ROOT / "runs" / "demo-result-2026-10-19.json"),
         "--expected", "T+7 复查"],
        cwd=str(ROOT), env=env, capture_output=True, text=True,
        encoding="utf-8", errors="replace")
    assert proc.returncode == 0, proc.stderr[-800:]
    after = len(real.read_text(encoding="utf-8").splitlines()) if real.is_file() else 0
    assert after == before, "真实账本被测试写入了 %d 行" % (after - before)
    isolated = tmp / ".telemetry" / "flywheel.jsonl"
    assert isolated.is_file() and len(telemetry.read(tmp)) > 0, "隔离账本没收到事件"


def main():
    failed = 0
    for fn in CASES:
        with tempfile.TemporaryDirectory() as tmp:
            try:
                fn(Path(tmp))
                print("PASS", fn.__name__)
            except AssertionError as exc:
                failed += 1
                print("FAIL", fn.__name__, "->", exc)
            except Exception as exc:  # noqa: BLE001
                failed += 1
                print("ERROR", fn.__name__, "->", repr(exc))
    print("\n%d/%d passed" % (len(CASES) - failed, len(CASES)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
