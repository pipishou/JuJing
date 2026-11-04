"""飞轮四齿端到端验证（真数据、真服务、真事件流；零外部依赖）。

验证目标：一次交付必须真的留下四条判断事件——
  A1 bot_team_card_issued     出卡
  A3 bot_team_card_signed     签字
  A4 bot_team_outcome_recorded 回填
  F2.5 bot_team_artifact_saved 产物
再验证 F3 周报能读到它们（不再恒 0）。

跑法：python -B tests/test_flywheel_e2e.py
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TMP = ROOT.parent / "_tmp_fw_e2e"
EVENTS = TMP / "events.jsonl"
RUNS = TMP / "runs"
TASK = "T-FW-0001"

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {detail}")


def events() -> list[dict]:
    if not EVENTS.is_file():
        return []
    out = []
    for line in EVENTS.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return out


def kinds() -> list[str]:
    return [e.get("event", "") for e in events()]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def post(url: str, body: dict) -> tuple[int, dict]:
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/json; charset=utf-8"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, {"raw": raw}


def prepare() -> dict:
    if TMP.exists():
        shutil.rmtree(TMP)
    (RUNS / TASK).mkdir(parents=True)
    src = TMP / "source" / "refill_B.csv"
    src.parent.mkdir(parents=True)
    src.write_text("date,production_line,refill_rate,output_volume\n2026-10-01,B,5.1,1000\n2026-10-02,B,5.4,980\n",
                   encoding="utf-8")
    (RUNS / TASK / "task.json").write_text(json.dumps({
        "task_id": TASK, "goal": "B线补料率异常归因", "line": "B", "created_by": "老板",
    }, ensure_ascii=False), encoding="utf-8")
    adv = {
        "schema": "advisory-report.v1",
        "goal": "B线补料率 5.1%→5.4% 的异常归因与处置建议",
        "finding": [
            {"factor": "mold_status", "statement": "M-03 模具状态与补料率同向变化",
             "evidence": ["10-07起M-03由normal转slight_wear，补料率同日抬升"], "confidence": 0.6},
        ],
        "cannot_attribute": [
            {"factor": "raw_batch", "reason": "原料批次同日首次切换，无法与模具变化分离"},
        ],
        "scorecard": {"data_completeness": 5, "timeliness": 5, "significance": 5,
                      "exclusivity": 4, "history": 5, "human_ai_consistency": 5},
        "recommendation": [
            {"action": "maintenance", "statement": "M-03 补保养并实测型腔", "irreversibility": "reversible",
             "roi": {"cost": 500, "avoided": 1200, "anchor": "maintenance"}},
        ],
        "decision_log_entry": {
            "actor": "lead", "advice": "先补保养再看一批", "id": "", "date": "2026-11-04",
            "options": ["补保养", "换模", "停线"], "irreversibility": "reversible",
            "expected_result": "补料率 7 日内回落至 5.3% 以下", "data_refs": [],
            "context": "B 线 10-07 起异常", "action_cost": 500,
            "outcome_window": "7d", "attribution_method": "factor_exclusion",
            "verification_plan": "7 日后复算 B 线补料率并与 30 日基线比对（<5.3% 视为命中）",
        },
        "sources": [str(src)],
    }
    adv_path = TMP / "advisory.json"
    adv_path.write_text(json.dumps(adv, ensure_ascii=False), encoding="utf-8")
    return {"advisory": adv_path}


def main() -> int:
    print("== 0 准备隔离环境")
    paths = prepare()
    env = dict(os.environ)
    env.update({
        "BOT_EVENTS_FILE": str(EVENTS),
        "BOT_RUNS_ROOT": str(RUNS),
        "BOT_LEDGER_ROOT": str(TMP / "ledger"),
        "PYTHONIOENCODING": "utf-8",
    })

    print("== 1 A1 出卡（真并链 → 真报告 → 真事件）")
    proc = subprocess.run(
        [sys.executable, "-B", "tools/advisory_to_task.py",
         "--advisory", str(paths["advisory"]), "--task-id", TASK, "--runs-root", str(RUNS)],
        cwd=str(ROOT), env=env, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    check("并链 rc=0", proc.returncode == 0, (proc.stdout or "")[-400:] + (proc.stderr or "")[-400:])
    check("report.json 落盘", (RUNS / TASK / "report.json").is_file())
    check("A1 card_issued 事件", "bot_team_card_issued" in kinds(), str(kinds()[-6:]))

    print("== 2 A3 签字（真服务 /sign）")
    port = free_port()
    srv_env = dict(env)
    srv_env.update({"BOT_SIGNERS": "老板", "BOT_PACK": str(ROOT / "packs" / "manufacturing" / "pack.json")})
    srv = subprocess.Popen(
        [sys.executable, "-B", "app.py", "--port", str(port), "--root", str(ROOT)],
        cwd=str(ROOT / "server"), env=srv_env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
    )
    base = f"http://127.0.0.1:{port}"
    ready = False
    for _ in range(40):
        try:
            with urllib.request.urlopen(base + "/api/health", timeout=2):
                ready = True
                break
        except Exception:  # noqa: BLE001
            time.sleep(0.5)
    check("服务就绪", ready)
    try:
        code, body = post(f"{base}/api/tasks/{TASK}/sign", {"signer": "老板"})
        check("签字 200", code == 200, f"{code} {body}")
        check("A3 card_signed 事件", "bot_team_card_signed" in kinds(), str(kinds()[-4:]))
        code, body = post(f"{base}/api/tasks/{TASK}/finalize", {"by": "老板"})
        check("定案 200", code == 200, f"{code} {body}")

        print("== 3 A4 回填")
        code, body = post(f"{base}/api/tasks/{TASK}/outcome",
                          {"observed_outcome": "补料率 7 日回落至 5.2%", "outcome_window": "7d", "source": "manual"})
        check("回填 200", code == 200, f"{code} {body}")
        check("A4 outcome_recorded 事件", "bot_team_outcome_recorded" in kinds(), str(kinds()[-4:]))
    finally:
        srv.terminate()
        try:
            srv.wait(timeout=10)
        except subprocess.TimeoutExpired:
            srv.kill()

    print("== 4 F2.5 产物清单")
    (RUNS / TASK / "artifacts").mkdir(parents=True, exist_ok=True)
    (RUNS / TASK / "artifacts" / "report.md").write_text("# B线补料率归因\n结论：先补保养。", encoding="utf-8")
    sys.path.insert(0, str(ROOT))
    os.environ["BOT_EVENTS_FILE"] = str(EVENTS)
    from tools.artifact_manifest import build as build_manifest  # noqa: E402

    man = build_manifest(RUNS / TASK, TASK, is_demo=False, emit=True)
    check("清单已生成", (RUNS / TASK / "artifacts" / "manifest.json").is_file(), str(man)[:200])
    check("F2.5 artifact_saved 事件", "bot_team_artifact_saved" in kinds(), str(kinds()[-4:]))

    print("== 5 F3 周报读到判断（不再恒 0）")
    from tools.flywheel_weekly import build as build_weekly  # noqa: E402

    rep = build_weekly(events(), days=30)
    check("issued>=1", (rep.get("issued") or 0) >= 1, str(rep.get("issued")))
    check("signed>=1", (rep.get("signed") or 0) >= 1, str(rep.get("signed")))
    check("outcomes>=1", (rep.get("outcomes") or 0) >= 1, str(rep.get("outcomes")))
    check("n1 有分母(样本不足则 null+note)", rep.get("n1_adoption_rate") == 1.0 or
          (rep.get("n1_adoption_rate") is None and rep.get("notes")), str(rep.get("n1_adoption_rate")))

    print(f"\nRESULT: {'PASS' if FAIL == 0 else 'FAIL'}（PASS {PASS} / FAIL {FAIL}）")
    print(f"事件文件：{EVENTS}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
