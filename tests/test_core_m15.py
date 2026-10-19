#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""M1.5 B4：core 新模块 + M1 遗留硬化自测（纯 stdlib、%TEMP% 沙盒）。

覆盖：
  1. core.paths   上传文件名 / 任务 id 白名单（穿越、盘符、保留名、控制字符）
  2. core.events  append_event（seq 自增/并发互斥） / iter_events（坏行容错/旧 run.log 兼容）
  3. core.team    load_team 契约（sprite_key/persona/crews/shelf/pack + 校验不抛负例）
  4. core.gate    gate.json confirm/reject/read + 报告回写 + 事件 + 负例
  5. core.gates   占位词零宽清洗/待补充/待核实；空 allowlist fail-closed；confirmed 无 confirmed_by
  6. core.pack    不可逆词表来自 pack.json（无代码硬编码）；ROI 锚点/阈值
  7. finalize     读 gate.json（默认 <报告目录>/gate.json）：confirmed 放行；
                  rejected / 无 confirmed_by / 跨任务 / pending 一律必拒
  8. run_task     run.log 事件契约（seq/ts/phase/role/agent/kind/line）；
                  --signer 无白名单不再「自签自批」（M1 遗留硬化）
  9. validate_report --require-real 顶层与 decision_log_entry 两处 data_refs 都查 demo/

运行：venv python -B tests/test_core_m15.py
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import events, gate, gates, pack, paths, report_contract, team  # noqa: E402

PY = sys.executable
SIX = report_contract.SIX_DIMS
FAILS = []
CHECKS = 0


def check(name, cond, detail=""):
    global CHECKS
    CHECKS += 1
    if cond:
        print(f"  PASS {name}")
    else:
        print(f"  FAIL {name}  ← {detail}")
        FAILS.append(name)


def write(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def dump(path, doc):
    write(path, json.dumps(doc, ensure_ascii=False, indent=2))


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def run(args, env=None, cwd=None):
    base = dict(os.environ)
    base.update({"PYTHONDONTWRITEBYTECODE": "1", "PYTHONIOENCODING": "utf-8"})
    if env:
        base.update(env)
    proc = subprocess.run([str(a) for a in args], cwd=str(cwd or ROOT), env=base,
                          capture_output=True, text=True, encoding="utf-8", errors="replace")
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


# ---------------------------------------------------------------------------
# 1. core.paths
# ---------------------------------------------------------------------------
def case_paths(tmp):
    print("\n[1] core.paths")
    good = ["ok.json", "material_refill_T.json", "data/ok.json", "塑胶厂-数据.json", "a..b"]
    for name in good:
        try:
            out = paths.safe_relative(name)
            check(f"safe_relative 放行 {name!r}", out == name, repr(out))
        except Exception as exc:  # noqa: BLE001
            check(f"safe_relative 放行 {name!r}", False, repr(exc))
    bad = ["", ".", "..", "../x", "..\\x", "C:\\x", "/abs", "a\\b", "a/b/c", "a//b",
           "con", "NUL.txt", "aux", "a/../b", "a/./b", "a.", "a ", "a\x00b", "a\tb",
           "~/.ssh/id", "file://x", "a%2fb"]
    for name in bad:
        try:
            out = paths.safe_relative(name)
            check(f"safe_relative 拒绝 {name!r}", False, f"未拒绝 -> {out!r}")
        except paths.PathError:
            check(f"safe_relative 拒绝 {name!r}", True)
    try:
        paths.safe_relative(123)
        check("safe_relative 非字符串拒绝", False)
    except paths.PathError:
        check("safe_relative 非字符串拒绝", True)

    ok_tids = ["T-20261019-B-refill-01", "T-20261019-X", "T-20261019-a_b-c"]
    for tid in ok_tids:
        try:
            check(f"safe_task_id 放行 {tid}", paths.safe_task_id(tid) == tid)
        except Exception as exc:  # noqa: BLE001
            check(f"safe_task_id 放行 {tid}", False, repr(exc))
    bad_tids = ["", "T-2026-1-x", "T-20261019-", "t-20261019-x", "../T-20261019-x",
                "T-20261019-x y", "T-20261019-x/y", "T-202610190-x", "T-20261019-x\x00"]
    for tid in bad_tids:
        try:
            paths.safe_task_id(tid)
            check(f"safe_task_id 拒绝 {tid!r}", False)
        except paths.PathError:
            check(f"safe_task_id 拒绝 {tid!r}", True)
    check("safe_task_id 超长拒绝", _raises(lambda: paths.safe_task_id("T-20261019-" + "a" * 200)))


def _raises(fn):
    try:
        fn()
    except Exception:  # noqa: BLE001
        return True
    return False


# ---------------------------------------------------------------------------
# 2. core.events
# ---------------------------------------------------------------------------
def case_events(tmp):
    print("\n[2] core.events")
    log = tmp / "events" / "run.log"
    s1 = events.append_event(str(log), {"phase": "s1", "role": "host", "line": "拆解任务"})
    s2 = events.append_event(str(log), {"phase": "s2", "role": "data-fetcher",
                                        "kind": "tool", "line": "取数完成"})
    s3 = events.append_event(str(log), {"phase": "s3", "role": "ai-attribution-expert",
                                        "kind": "speak", "line": "结论：模具磨损",
                                        "meta": {"factor": "模具磨损"}, "task_id": "T-1"})
    check("append_event seq 自增", [s1, s2, s3] == [1, 2, 3], [s1, s2, s3])
    recs = events.iter_events(str(log))
    check("iter_events 条数", len(recs) == 3, len(recs))
    required = {"seq", "ts", "phase", "role", "agent", "kind", "line"}
    check("事件字段齐", all(required <= set(r) for r in recs), recs)
    check("role 长 id 归一", recs[1]["role"] == "data" and recs[2]["role"] == "attr",
          [r["role"] for r in recs])
    check("agent 补真名", recs[0]["agent"] == "工作助理" and recs[1]["agent"] == "取数员",
          [r["agent"] for r in recs])
    check("kind 默认 status", recs[0]["kind"] == "status", recs[0]["kind"])
    check("meta 保留", recs[2].get("meta") == {"factor": "模具磨损"}, recs[2].get("meta"))
    check("业务字段透传", recs[2].get("task_id") == "T-1", recs[2].get("task_id"))
    try:
        datetime.fromisoformat(recs[0]["ts"])
        check("ts 是 ISO 时间", True)
    except ValueError:
        check("ts 是 ISO 时间", False, recs[0]["ts"])
    check("since 过滤", [r["seq"] for r in events.iter_events(str(log), since=2)] == [3])
    check("last_seq", events.last_seq(str(log)) == 3, events.last_seq(str(log)))
    check("append_event 非 dict → TypeError",
          _raises(lambda: events.append_event(str(log), "not-a-dict")))

    # 坏行容错 + 旧 run.log 兼容（只有 ts/phase/role/line）
    with open(log, "a", encoding="utf-8") as fh:
        fh.write("this is broken\n")
        fh.write('{"ts": "2026-10-19T10:00:00+08:00", "phase": "s5", "role": "host", "line": "旧日志行"}\n')
    errs = []
    recs2 = events.iter_events(str(log), errors=errs)
    check("坏行跳过且不留残骸", len(recs2) == 4, [r["seq"] for r in recs2])
    check("坏行计入 errors", len(errs) == 1 and errs[0]["line"] == 4, errs)
    legacy = recs2[-1]
    check("旧行补 kind=status", legacy["kind"] == "status", legacy["kind"])
    check("旧行补 agent", legacy["agent"] == "工作助理", legacy["agent"])
    check("旧行 seq 按行号回填", legacy["seq"] == 5, legacy["seq"])

    # 纯旧 run.log（仓库里 M1 遗留的那份）也能读
    old_log = ROOT / "runs" / "T-20261019-B-refill-01" / "run.log"
    if old_log.is_file():
        legacy_all = events.iter_events(str(old_log))
        check("仓库旧 run.log 可读", all(r["kind"] == "status" for r in legacy_all) and bool(legacy_all),
              legacy_all[:1])

    # 并发 append：锁保证 seq 唯一且连续
    clog = tmp / "events" / "concurrent.log"
    errs_box = []

    def _worker():
        for _ in range(5):
            try:
                events.append_event(str(clog), {"phase": "s1", "role": "host", "line": "并发"})
            except Exception as exc:  # noqa: BLE001
                errs_box.append(repr(exc))

    threads = [threading.Thread(target=_worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    seqs = [r["seq"] for r in events.iter_events(str(clog))]
    check("并发 append 无异常", not errs_box, errs_box)
    check("并发 seq 唯一且 1..20", sorted(seqs) == list(range(1, 21)), seqs)

    # 旧文件上继续 append：seq 从 max(max_seq, 行数)+1 起
    legacy_log = tmp / "events" / "legacy.log"
    write(legacy_log, '{"ts": "t1", "phase": "s1", "role": "host", "line": "a"}\n'
                      '{"ts": "t2", "phase": "s2", "role": "host", "line": "b"}\n')
    check("旧文件 append seq 不撞号", events.append_event(str(legacy_log),
                                                          {"phase": "s5", "role": "host", "line": "c"}) == 3)
    check("旧文件 append 后 seq 1..3",
          [r["seq"] for r in events.iter_events(str(legacy_log))] == [1, 2, 3])


# ---------------------------------------------------------------------------
# 3. core.team（%TEMP% 沙盒 YAML）
# ---------------------------------------------------------------------------
EMPLOYEES_YAML = """\
version: 0.2.0
team_id: m15-test-team
categories: [deliver, quality]
employees:
  - id: host
    name: 工作助理
    layer: L3
    category: resident
    state: resident
    summary: 主理人
    role_file: team/roles/host.md
    skills: [收口]
    sample: "帮我看下这条线"
  - id: consulting-analyst
    name: 咨询分析师
    layer: L0
    category: active
    state: active
    summary: 拆解问题
    role_file: team/roles/consulting-analyst.md
    skills: [拆解, 结构化]
    sample: "先界定口径"
  - id: data-fetcher
    name: 取数员
    layer: craft
    category: deliver
    state: craft
    summary: 拉数
    role_file: team/roles/data-fetcher.md
    skills: [取数]
    sample: "把 37 天数据拉出来"
"""

CREWS_YAML = """\
version: 0.2.0
crews:
  - id: c-test-flow
    name: 测试班组
    purpose: 测试五步
    want: 判断异常
    needs: 数据
    steps:
      - agent: consulting-analyst
        what: 拆解
      - agent: host
        what: 收口
    end:
      kind: file
      followup:
        days: 7
        steps: [host]
"""

SHELF_YAML = """\
version: 0.2.0
states:
  resident:
    label: 常驻
    meaning: 长期在岗
    action: 直接派活
  active:
    label: 在岗
    meaning: 在岗
    action: 派活
  craft:
    label: 工序
    meaning: 支撑工序
    action: 按步调用
badges:
  flagship:
    label: 旗舰
categories:
  - id: deliver
    name: 交付
  - id: quality
    name: 质检
"""

ROSTER_YAML = """\
version: 0.1.0
team_id: m15-test-team
members: []
"""

PACK_JSON = {
    "pack_id": "manufacturing",
    "version": "1.1.0",
    "industry": "制造业",
    "thresholds": {"refill_baseline_pct": 5.40, "refill_target_pct": 7.2,
                   "cell_null_rate_manual": 0.10, "t30_window_days": 30},
    "roi_anchors": {"overtime_clean": 1200, "mold_change": 800, "maintenance": 500},
    "irreversible_actions": ["line_stop"],
    "irreversible_keywords": ["停线"],
    "six_dims": list(SIX),
    "crew_id": "c-test-flow",
}


def make_team_sandbox(base):
    root = Path(base) / "team-root"
    if root.exists():
        shutil.rmtree(root)
    write(root / "team" / "employees.yaml", EMPLOYEES_YAML)
    write(root / "team" / "crews.yaml", CREWS_YAML)
    write(root / "team" / "shelf.yaml", SHELF_YAML)
    write(root / "team" / "roster.yaml", ROSTER_YAML)
    write(root / "team" / "roles" / "host.md", "# 工作助理\n\n无 frontmatter 的角色卡。\n")
    write(root / "team" / "roles" / "consulting-analyst.md",
          "---\nname: 咨询分析师\nemoji: 🧭\ncolor: '#5b8def'\n---\n\n# 咨询分析师\n")
    write(root / "team" / "roles" / "data-fetcher.md", "# 取数员\n\n无 frontmatter。\n")
    dump(root / "packs" / "manufacturing" / "pack.json", PACK_JSON)
    return root


def case_team(tmp):
    print("\n[3] core.team")
    sandbox = make_team_sandbox(tmp)
    doc = team.load_team(sandbox)
    check("load_team team_id", doc["team_id"] == "m15-test-team", doc["team_id"])
    ids = [e["id"] for e in doc["employees"]]
    check("load_team 员工 3 人且 id 唯一", ids == ["host", "consulting-analyst", "data-fetcher"], ids)
    sprites = {e["id"]: e["sprite_key"] for e in doc["employees"]}
    check("sprite_key 长 id→短 key",
          sprites == {"host": "host", "consulting-analyst": "analyst", "data-fetcher": "data"},
          sprites)
    persona = {e["id"]: e["persona"] for e in doc["employees"]}
    check("有 frontmatter 的 persona 解析", persona["consulting-analyst"].get("name") == "咨询分析师",
          persona["consulting-analyst"])
    check("无 frontmatter 的 persona 为空 dict", persona["host"] == {}, persona["host"])
    check("crews 解析（steps/followup）",
          len(doc["crews"]) == 1 and len(doc["crews"][0]["steps"]) == 2
          and doc["crews"][0]["followup"].get("days") == 7
          and doc["crews"][0]["end"].get("kind") == "file",
          doc["crews"])
    check("shelf states/badges/categories",
          set(doc["shelf"]["states"]) >= {"resident", "active", "craft"}
          and "flagship" in doc["shelf"]["badges"] and len(doc["shelf"]["categories"]) == 2,
          doc["shelf"])
    check("pack 装载", doc["pack"].get("crew_id") == "c-test-flow", doc["pack"].get("crew_id"))
    check("沙盒正控无 warnings/invalid", not doc["warnings"] and not doc["invalid"],
          {"w": doc["warnings"], "i": doc["invalid"]})

    # 负例 1：员工 id 重复 → warning + invalid，不抛
    dup = Path(tmp) / "team-dup"
    shutil.copytree(sandbox, dup)
    text = (dup / "team" / "employees.yaml").read_text(encoding="utf-8")
    text = text.replace("employees:\n", "employees:\n  - id: host\n    name: 重复员工\n    state: active\n", 1)
    write(dup / "team" / "employees.yaml", text)
    d = team.load_team(dup)
    kinds = {i.get("kind") for i in d["invalid"]}
    check("重复员工 id → invalid", "duplicate_employee_id" in kinds, kinds)
    check("重复员工 id 仍返回其余员工", [e["id"] for e in d["employees"]].count("host") == 1,
          [e["id"] for e in d["employees"]])

    # 负例 2：crew step agent 不在员工册
    bad_step = Path(tmp) / "team-badstep"
    shutil.copytree(sandbox, bad_step)
    text = (bad_step / "team" / "crews.yaml").read_text(encoding="utf-8")
    write(bad_step / "team" / "crews.yaml", text.replace("agent: host", "agent: ghost-agent", 1))
    d = team.load_team(bad_step)
    check("crew step 未知 agent → invalid",
          "crew_step_agent" in {i.get("kind") for i in d["invalid"]},
          [i.get("kind") for i in d["invalid"]])
    check("crew step 未知 agent → warning 留痕",
          any("ghost-agent" in w for w in d["warnings"]), d["warnings"])

    # 负例 3：pack.crew_id 不在 crews
    bad_pack = Path(tmp) / "team-badpack"
    shutil.copytree(sandbox, bad_pack)
    doc_json = json.loads((bad_pack / "packs" / "manufacturing" / "pack.json").read_text(encoding="utf-8"))
    doc_json["crew_id"] = "c-ghost"
    dump(bad_pack / "packs" / "manufacturing" / "pack.json", doc_json)
    d = team.load_team(bad_pack)
    check("pack.crew_id 未知 → invalid",
          "pack_crew_id" in {i.get("kind") for i in d["invalid"]},
          [i.get("kind") for i in d["invalid"]])

    # 负例 4：未知员工 id → sprite 回退 host + warning
    unknown = Path(tmp) / "team-unknown"
    shutil.copytree(sandbox, unknown)
    text = (unknown / "team" / "employees.yaml").read_text(encoding="utf-8")
    text += ("  - id: mystery-op\n    name: 神秘员工\n    state: active\n"
             "    role_file: team/roles/host.md\n    skills: [未知]\n    sample: 测试\n")
    write(unknown / "team" / "employees.yaml", text)
    d = team.load_team(unknown)
    mystery = [e for e in d["employees"] if e["id"] == "mystery-op"]
    check("未知员工 sprite 回退 host", bool(mystery) and mystery[0]["sprite_key"] == "host",
          mystery)
    check("未知员工 warning 留痕", any("回退 host" in w for w in d["warnings"]), d["warnings"])

    # 负例 5：缺文件 / 坏 YAML 不抛
    missing = Path(tmp) / "team-missing"
    shutil.copytree(sandbox, missing)
    (missing / "team" / "employees.yaml").unlink()
    d = team.load_team(missing)
    check("缺 employees.yaml 不抛且留 invalid", d["employees"] == []
          and any(i.get("kind") == "missing_file" for i in d["invalid"]), d["invalid"])
    broken = Path(tmp) / "team-broken"
    shutil.copytree(sandbox, broken)
    write(broken / "team" / "crews.yaml", "crews: [oops\n  - id: [\n")
    d = team.load_team(broken)
    check("坏 YAML 不抛且记 bad_yaml",
          any(i.get("kind") == "bad_yaml" for i in d["invalid"]), d["invalid"])
    check("坏 YAML 仍返回员工", len(d["employees"]) == 3, len(d["employees"]))

    # 真实仓库数据（/api/team 的单一事实源）
    real = team.load_team()
    real_ids = [e["id"] for e in real["employees"]]
    check("真实 team 员工 7 人", len(real_ids) == 7, real_ids)
    real_sprites = [e["sprite_key"] for e in real["employees"]]
    check("真实 team sprite 唯一", len(set(real_sprites)) == len(real_sprites), real_sprites)
    check("真实 team sprite⊆像素角色库", set(real_sprites) <= set(team.SPRITE_KEYS), real_sprites)
    real_crews = {c["id"] for c in real["crews"]}
    check("真实 pack.crew_id∈crews（A7 前置）",
          real["pack"].get("crew_id") in real_crews, (real["pack"].get("crew_id"), real_crews))
    check("真实 team persona 三份", sum(1 for e in real["employees"] if e["persona"]) == 3,
          [e["id"] for e in real["employees"] if e["persona"]])


# ---------------------------------------------------------------------------
# 4. core.gate
# ---------------------------------------------------------------------------
def case_gate(tmp):
    print("\n[4] core.gate")
    tdir = Path(tmp) / "gate" / "T-20261019-CORE-01"
    task_id = tdir.name
    report = {
        "task_id": task_id,
        "security_gate_requested": True,
        "security_gate": {"status": "pending_user_confirmation", "requested_at": "2026-10-19"},
        "decision_log_entry": {"security_gate": {"status": "pending_user_confirmation",
                                                 "requested_at": "2026-10-19"}},
    }
    dump(tdir / "report.json", report)
    check("read 无文件 → None", gate.read(tdir) is None)
    check("read_file 不存在 → None", gate.read_file(tdir / "nope.json") is None)
    try:
        gate.confirm(tdir, "   ", "没人签名", dict(report))
        check("confirm 空 confirmed_by 必拒", False)
    except gates.GateError as exc:
        check("confirm 空 confirmed_by 必拒", "confirmed_by" in str(exc), str(exc))
    check("空 confirmed_by 不落 gate.json", not gate.gate_path(tdir).is_file())

    doc = gate.confirm(tdir, "老板", "同意停线检修", dict(report))
    check("confirm 返回 status=confirmed", doc.get("status") == "confirmed", doc)
    check("confirm 记 confirmed_by", doc.get("confirmed_by") == "老板", doc.get("confirmed_by"))
    check("gate.json history 追加", len(doc.get("history") or []) == 1, doc.get("history"))
    on_disk = gate.read(tdir)
    check("gate.json 落盘可读", on_disk and on_disk.get("status") == "confirmed", on_disk)
    written_report = json.loads((tdir / "report.json").read_text(encoding="utf-8"))
    top_gate = written_report.get("security_gate") or {}
    dl_gate = (written_report.get("decision_log_entry") or {}).get("security_gate") or {}
    check("confirm 回写报告 security_gate",
          top_gate.get("status") == "confirmed" and top_gate.get("confirmed_by") == "老板", top_gate)
    check("confirm 回写 decision_log 且保留 requested_at",
          dl_gate.get("confirmed_by") == "老板" and dl_gate.get("requested_at") == "2026-10-19", dl_gate)
    evs = events.iter_events(str(tdir / "run.log"))
    check("confirm 写 gate 事件",
          bool(evs) and evs[-1]["phase"] == "gate" and evs[-1]["kind"] == "status"
          and "confirmed" in evs[-1]["line"], evs)

    doc2 = gate.confirm(tdir, "boss", "二次确认")
    check("再次 confirm history append-only",
          doc2.get("status") == "confirmed" and len(doc2.get("history") or []) == 2,
          doc2.get("history"))
    doc3 = gate.reject(tdir, "老板", "数据口径待补")
    check("reject 写 status=rejected", doc3.get("status") == "rejected", doc3.get("status"))
    check("reject history 三条", len(doc3.get("history") or []) == 3, doc3.get("history"))
    check("reject 后 read 为 rejected", gate.read(tdir).get("status") == "rejected")

    # 坏 gate.json → read 视为没有确认（不抛）
    bad = Path(tmp) / "gate-bad"
    write(bad / "gate.json", "{not-json")
    check("坏 gate.json → None", gate.read(bad) is None)
    # 没有 report.json 也能确认（只有 gate.json；/finalize 会按 gate.json 判）
    bare = Path(tmp) / "gate" / "T-20261019-CORE-02"
    doc4 = gate.confirm(bare, "老板", "无报告场景")
    check("无报告目录也可确认", doc4.get("status") == "confirmed" and doc4.get("task_id") == bare.name,
          doc4)


# ---------------------------------------------------------------------------
# 5. core.gates 硬化
# ---------------------------------------------------------------------------
def case_gates_hardening():
    print("\n[5] core.gates 硬化")
    samples = [("demo 占位", True), ("占位", True), ("placeholder", True), ("…", True),
               ("待补充", True), ("数据待补充", True), ("信息员换人（待核实）", True),
               ("占\u200b位", True), ("待补\u200b充", True), ("", True), (None, True),
               ("字段齐全，无空值", False), ("37 天逐日数据齐全", False)]
    for text, want in samples:
        got = gates.is_placeholder_evidence(text)
        check(f"占位判定 {text!r}→{want}", got == want, got)
    check("strip_zero_width", gates.strip_zero_width("占\u200b位") == "占位")

    task = "T-20261019-CORE-01"
    sig = {"signer": "老板", "report_sha256": "abc123", "task_id": task}
    base_report = {"task_id": task, "is_demo": False, "_sha256": "abc123"}
    dec = {"status": "proposed", "task_id": task}
    check("合法链 check_finalize 空 reasons",
          gates.check_finalize(dec, dict(base_report), sig, allowlist=["老板"]) == [],
          gates.check_finalize(dec, dict(base_report), sig, allowlist=["老板"]))
    r = gates.check_finalize(dec, dict(base_report), sig, allowlist=())
    check("空 allowlist → fail closed",
          any("allowlist 为空" in x for x in r) and any("fail closed" in x for x in r), r)
    r = gates.check_finalize(dec, dict(base_report), sig, allowlist=["boss"])
    check("白名单外 signer 拒绝", any("白名单" in x for x in r), r)
    r = gates.check_finalize(dec, dict(base_report), {"signer": "", "report_sha256": "abc123"}, ["老板"])
    check("伪签名（无 signer）拒绝", any("signer" in x for x in r), r)

    gate_report = dict(base_report)
    gate_report["security_gate_requested"] = True
    gate_report["decision_log_entry"] = {"security_gate": {"status": "confirmed", "confirmed_by": ""}}
    r = gates.check_finalize(dec, gate_report, sig, allowlist=["老板"], gate_confirmed=True)
    check("confirmed 但 confirmed_by 空 → 拒绝",
          any("confirmed_by 为空" in x for x in r), r)
    r = gates.check_finalize(dec, gate_report, sig, allowlist=["老板"])
    check("报告自证 security_gate 但无外部确认 → fail closed（task-12）",
          any("gate.json" in x for x in r), r)
    gate_report["decision_log_entry"] = {"security_gate": {"status": "pending_user_confirmation"}}
    r = gates.check_finalize(dec, gate_report, sig, allowlist=["老板"])
    check("securityGate pending 且无外部确认 → 拒绝且原因含 securityGate",
          any("securityGate" in x for x in r) and any("gate.json" in x for x in r), r)
    gate_report["decision_log_entry"] = {"security_gate": {"status": "confirmed", "confirmed_by": "老板"}}
    r = gates.check_finalize(dec, gate_report, sig, allowlist=["老板"])
    check("报告自报 confirmed+confirmed_by 不再放行（task-12）",
          any("gate.json" in x for x in r), r)
    check("gate_confirmed=True（CLI 已验 gate.json）才放行",
          gates.check_finalize(dec, gate_report, sig, allowlist=["老板"],
                               gate_confirmed=True) == [],
          gates.check_finalize(dec, gate_report, sig, allowlist=["老板"], gate_confirmed=True))
    # task-13 形状硬化：畸形结构一律 fail-closed，且不得抛 AttributeError/TypeError
    malformed = [
        ("recommendation 为 dict", {"recommendation": {"irreversibility": "irreversible"}}),
        ("security_gate 为字符串", {"security_gate": "confirmed"}),
        ("decision_log_entry 为字符串", {"decision_log_entry": "x"}),
        ("recommendation 元素非 dict", {"recommendation": [1]}),
    ]
    for name, extra in malformed:
        rep = dict(base_report)
        rep.update(extra)
        try:
            r = gates.check_finalize(dec, rep, sig, allowlist=["老板"])
        except Exception as exc:  # noqa: BLE001
            r = [f"异常冒泡: {exc!r}"]
        check(f"畸形报告（{name}）→ 拒绝且不崩溃", any("畸形" in x for x in r), r)
    try:
        r = gates.check_finalize(dec, [1, 2], sig, ["老板"])
    except Exception as exc:  # noqa: BLE001
        r = [f"异常冒泡: {exc!r}"]
    check("顶层 list 报告 → 拒绝且不崩溃", any("顶层" in x for x in r), r)
    check("security_gate_requested 非 bool → 视为声称需要安全门",
          gates.report_claims_security_gate({"security_gate_requested": "false"}) is True)
    r = gates.check_finalize(dec, dict(base_report, security_gate_requested="false"), sig, ["老板"])
    check("security_gate_requested 非 bool → shape 拒绝",
          any("security_gate_requested" in x for x in r), r)
    demo = dict(base_report)
    demo["is_demo"] = True
    check("demo 报告拒绝", any("demo" in x for x in gates.check_finalize(dec, demo, sig, ["老板"])))


# ---------------------------------------------------------------------------
# 6. core.pack
# ---------------------------------------------------------------------------
def case_pack():
    print("\n[6] core.pack")
    p = pack.load()
    check("pack 加载 pack_id", p.get("pack_id") == "manufacturing", p.get("pack_id"))
    check("不可逆词表来自 pack.json",
          p.get("irreversible_keywords") == ["停线", "line_stop", "换模", "conditional_mold_change"],
          p.get("irreversible_keywords"))
    check("停线检修 → 不可逆", pack.is_irreversible(p, "停线检修") is True)
    check("换模 → 不可逆", pack.is_irreversible(p, "换模作业") is True)
    check("line_stop → 不可逆", pack.is_irreversible(p, "line_stop") is True)
    check("补数据重跑 → 可逆", pack.is_irreversible(p, "补数据重跑") is False)
    check("阈值可读", pack.threshold(p, "refill_baseline_pct") == 5.40,
          pack.threshold(p, "refill_baseline_pct"))
    check("缺少阈值 → PackError",
          _raises(lambda: pack.threshold(p, "no_such_threshold")))
    check("roi 锚点按 pack 定价", pack.roi_for(p, "停机清理")["cost"] == "¥1200",
          pack.roi_for(p, "停机清理"))
    check("anchor_key_for 命中 overtime_clean", pack.anchor_key_for("停机清理") == "overtime_clean",
          pack.anchor_key_for("停机清理"))
    bad_doc = dict(p)
    bad_doc.pop("thresholds")
    check("缺必需键 → PackError",
          _raises(lambda: pack.validate(bad_doc, "mem://pack.json")))
    check("不存在的 pack 文件 → PackError",
          _raises(lambda: pack.load_file("no/such/pack.json")))

    # pack.json 与 team 数据一致（A7：/api/team 拿 pack.crew_id 找 crew）
    tdoc = team.load_team()
    check("pack.crew_id 是真实 crew id",
          tdoc["pack"].get("crew_id") == "c-refill-attribution"
          and tdoc["pack"].get("crew_id") in {c["id"] for c in tdoc["crews"]},
          tdoc["pack"].get("crew_id"))


# ---------------------------------------------------------------------------
# 7. finalize_delivery 读 gate.json
# ---------------------------------------------------------------------------
def build_real_report(tmp):
    """构造一份能过 gates.real_report_errors 的真实报告（refs 用仓库内真实文件）。"""
    demo = json.loads((ROOT / "runs" / "demo-report-2026-10-19.json").read_text(encoding="utf-8"))
    task = "T-20261019-CORE-01"
    r = json.loads(json.dumps(demo))
    r["is_demo"] = False
    r["task_id"] = task
    r["finding"] = [{"factor": "模具磨损", "confidence": 0.72,
                     "evidence": ["换模记录与补料率爬升同步（现场核实）"]}]
    r["cannot_attribute"] = ["信息员换班带来的口径变化未完全排除"]
    score = {d: {"score": 4, "evidence": "37 天逐日数据齐全，缺 1 天已标注"} for d in SIX}
    computed = report_contract.compute(score)
    score.update({"total": computed["total"], "verdict": computed["verdict"],
                  "label": computed["label"]})
    r["scorecard"] = score
    r["recommendation"] = [{
        "action": "补数据后重跑归因",
        "roi": {"cost": "¥500", "avoided": "¥1200", "anchor": "maintenance（pack:manufacturing）"},
        "irreversibility": "reversible",
    }]
    r["security_gate_requested"] = False
    r.pop("security_gate", None)
    from core import provenance

    clean = provenance.refs_record([ROOT / "runs" / "demo-result-2026-10-19.json"], ROOT)
    demo_refs = provenance.refs_record([ROOT / "demo" / "material_refill_T.json"], ROOT)
    r["data_refs"] = clean
    dl = {
        "id": "DL-20261019-CORE-01", "actor": "manufacturing-ops-team-dsh", "task_id": task,
        "context": "B 线补料率 7 日爬升", "advice": "先用 exclusion 排除法排除口径变化",
        "expected_result": "T+7 补料率回落至 ≤5.40%",
        "verification_plan": "T+7 复查 7 日均值，未回落则扩数据重跑",
        "data_refs": clean,
    }
    r["decision_log_entry"] = dl
    return r, clean, demo_refs


def make_finalize_case(tmp, name):
    tdir = Path(tmp) / "finalize" / name
    (tdir / "loop").mkdir(parents=True, exist_ok=True)
    report, clean, _ = build_real_report(tmp)
    report_path = tdir / "report.json"
    dump(report_path, report)
    task = report["task_id"]
    dump(tdir / "loop" / "decision.json",
         {"id": "D-20261019-CORE-01", "status": "proposed", "task_id": task,
          "date": "2026-10-19", "actor": "manufacturing-ops-team-dsh"})
    sig = {"signer": "老板", "report_sha256": sha256(report_path), "task_id": task,
           "signed_at": "2026-10-19T10:00:00+08:00"}
    dump(tdir / "report.signature.json", sig)
    return tdir, report_path, sig


def finalize_args(tdir, allow=True, gate_file=None):
    args = [PY, "-B", str(ROOT / "tools" / "finalize_delivery.py"),
            "--report", str(tdir / "report.json"),
            "--loop-dir", str(tdir / "loop"),
            "--signature", str(tdir / "report.signature.json")]
    if allow:
        args += ["--allow-signers", "老板"]
    if gate_file:
        args += ["--gate-file", str(gate_file)]
    return args


def reset_decision(tdir):
    task = json.loads((tdir / "report.json").read_text(encoding="utf-8"))["task_id"]
    dump(tdir / "loop" / "decision.json",
         {"id": "D-20261019-CORE-01", "status": "proposed", "task_id": task,
          "date": "2026-10-19", "actor": "manufacturing-ops-team-dsh"})


def case_finalize_gate(tmp):
    print("\n[7] finalize_delivery 读 gate.json")
    tdir, report_path, sig = make_finalize_case(tmp, "T-20261019-CORE-01")
    rc, out = run(finalize_args(tdir))
    decision = json.loads((tdir / "loop" / "decision.json").read_text(encoding="utf-8"))
    check("无 gate 需求 → approved", rc == 0 and decision.get("status") == "approved",
          (rc, out[-400:], decision.get("status")))
    check("approval.by = 签字人", (decision.get("approval") or {}).get("by") == "老板",
          decision.get("approval"))

    # gate.json rejected → 必拒（即便报告自身 security_gate 是 confirmed）
    reset_decision(tdir)
    gate.reject(tdir, "老板", "数据口径待补")
    rc, out = run(finalize_args(tdir))
    check("gate.json rejected → 拒绝", rc != 0 and "securityGate 未确认" in out, (rc, out[-600:]))
    check("拒绝不改 decision", json.loads((tdir / "loop" / "decision.json")
                                          .read_text(encoding="utf-8"))["status"] == "proposed")

    # gate.json confirmed 但 confirmed_by 空 → 必拒
    dump(tdir / "gate.json", {"task_id": tdir.name, "status": "confirmed",
                              "confirmed_by": "", "confirmed_at": "2026-10-19T10:00:00+08:00"})
    rc, out = run(finalize_args(tdir))
    check("gate.json confirmed 无 confirmed_by → 拒绝",
          rc != 0 and "confirmed_by 为空" in out, (rc, out[-600:]))

    # 跨任务确认 → 拒绝
    dump(tdir / "gate.json", {"task_id": "T-20261019-OTHER-99", "status": "confirmed",
                              "confirmed_by": "老板", "confirmed_at": "2026-10-19T10:00:00+08:00"})
    rc, out = run(finalize_args(tdir))
    check("gate.json 跨任务 → 拒绝", rc != 0 and "跨任务确认" in out, (rc, out[-600:]))

    # D1 伪造：confirmed_by 在白名单，但 history 为空 → 拒绝
    reset_decision(tdir)
    dump(tdir / "gate.json", {"task_id": tdir.name, "status": "confirmed",
                              "confirmed_by": "老板", "confirmed_at": "2026-10-19T10:00:00+08:00",
                              "history": []})
    rc, out = run(finalize_args(tdir))
    check("伪造 gate.json（history 空）→ 拒绝", rc != 0 and "伪造" in out, (rc, out[-600:]))

    # D1 伪造：history 齐全，但 run.log 里没有 phase=gate 审计事件 → 拒绝
    reset_decision(tdir)
    (tdir / "gate.json").unlink()
    gate.confirm(tdir, "老板", "合法确认（随后抹掉审计事件）")
    log = tdir / "run.log"
    kept = [ln for ln in log.read_text(encoding="utf-8").splitlines()
            if ln.strip() and '"gate"' not in ln]
    log.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
    rc, out = run(finalize_args(tdir))
    check("伪造 gate.json（无 run.log 审计事件）→ 拒绝",
          rc != 0 and "审计事件" in out, (rc, out[-700:]))

    # 正常确认（不传 --gate-file，走默认 <报告目录>/gate.json）→ approved
    reset_decision(tdir)
    (tdir / "gate.json").unlink()
    gate.confirm(tdir, "老板", "同意补数据重跑")
    gate_doc = json.loads((tdir / "gate.json").read_text(encoding="utf-8"))
    check("合法 confirm 留痕：history 非空",
          isinstance(gate_doc.get("history"), list) and len(gate_doc["history"]) >= 1,
          gate_doc.get("history"))
    check("合法 confirm 写 run.log phase=gate 审计事件（task-12 正例）",
          '"gate"' in (tdir / "run.log").read_text(encoding="utf-8"))
    rc, out = run(finalize_args(tdir))
    decision = json.loads((tdir / "loop" / "decision.json").read_text(encoding="utf-8"))
    check("gate.json confirmed → approved（默认 gate 文件）",
          rc == 0 and decision.get("approval", {}).get("by") == "老板", (rc, out[-600:]))

    # 报告自带 pending gate + 无 gate.json → 必拒（M1 兼容路径）
    reset_decision(tdir)
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["security_gate_requested"] = True
    report["decision_log_entry"]["security_gate"] = {
        "requested_at": "2026-10-19", "confirmed_by": None,
        "status": "pending_user_confirmation"}
    dump(report_path, report)
    sig = {"signer": "老板", "report_sha256": sha256(report_path),
           "task_id": report["task_id"], "signed_at": "2026-10-19T10:00:00+08:00"}
    dump(tdir / "report.signature.json", sig)
    (tdir / "gate.json").unlink()
    rc, out = run(finalize_args(tdir))
    check("pending securityGate → 拒绝", rc != 0 and "securityGate" in out, (rc, out[-600:]))

    # task-12 A9 fail-open 负例：报告自证 security_gate=confirmed（顶层+dl）、无 gate.json
    # + --allow-signers 老板 → 必须 rc≠0，且 decision 不得 approved
    reset_decision(tdir)
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["security_gate_requested"] = True
    self_gate = {"status": "confirmed", "confirmed_by": "老板",
                 "confirmed_at": "2026-10-19T10:00:00+08:00"}
    report["security_gate"] = dict(self_gate)
    report["decision_log_entry"]["security_gate"] = dict(self_gate)
    dump(report_path, report)
    sig = {"signer": "老板", "report_sha256": sha256(report_path),
           "task_id": report["task_id"], "signed_at": "2026-10-19T10:00:00+08:00"}
    dump(tdir / "report.signature.json", sig)
    if (tdir / "gate.json").exists():
        (tdir / "gate.json").unlink()
    rc, out = run(finalize_args(tdir))
    decision = json.loads((tdir / "loop" / "decision.json").read_text(encoding="utf-8"))
    check("自证 gate 无 gate.json → 拒绝（fail-closed，task-12 负例）",
          rc != 0 and "gate.json" in out and decision.get("status") == "proposed",
          (rc, out[-700:], decision.get("status")))

    # --allow-signers 缺省 → fail closed（未配置白名单）
    reset_decision(tdir)
    rc, out = run(finalize_args(tdir, allow=False))
    check("finalize 无白名单 → fail closed",
          rc != 0 and "未配置 signer 白名单" in out, (rc, out[-400:]))

    # task-13 形状硬化：畸形报告 → rc≠0、无 traceback、单行 JSON、decision 不 approved
    malformed_cases = [
        ("recommendation 为 dict", {"recommendation": {"irreversibility": "irreversible"}}),
        ("security_gate 为字符串", {"security_gate": "confirmed"}),
        ("decision_log_entry 为字符串", {"decision_log_entry": "x"}),
    ]
    for name, extra in malformed_cases:
        reset_decision(tdir)
        report, _clean, _demo = build_real_report(tmp)
        report.pop("security_gate", None)
        report["security_gate_requested"] = False
        report.update(extra)
        dump(report_path, report)
        task = report.get("task_id")
        sig = {"signer": "老板", "report_sha256": sha256(report_path),
               "task_id": task, "signed_at": "2026-10-19T10:00:00+08:00"}
        dump(tdir / "report.signature.json", sig)
        if (tdir / "gate.json").exists():
            (tdir / "gate.json").unlink()
        rc, out = run(finalize_args(tdir))
        decision = json.loads((tdir / "loop" / "decision.json").read_text(encoding="utf-8"))
        try:
            obj = json.loads(out)
        except Exception:  # noqa: BLE001
            obj = None
        check(f"畸形报告（{name}）→ rc≠0 且 decision 不 approved",
              rc != 0 and decision.get("status") == "proposed", (rc, out[-400:], decision.get("status")))
        check(f"畸形报告（{name}）→ 无 traceback + 单行 JSON",
              "Traceback" not in out and "\n" not in out.strip()
              and isinstance(obj, dict) and obj.get("ok") is False, out[-400:])

    # 顶层为 list 的报告：main 入口形状校验 → 单行 JSON 错误，不崩
    reset_decision(tdir)
    dump(report_path, [{"task_id": "T-20261019-CORE-01"}])
    sig = {"signer": "老板", "report_sha256": sha256(report_path),
           "task_id": "T-20261019-CORE-01", "signed_at": "2026-10-19T10:00:00+08:00"}
    dump(tdir / "report.signature.json", sig)
    rc, out = run(finalize_args(tdir))
    decision = json.loads((tdir / "loop" / "decision.json").read_text(encoding="utf-8"))
    try:
        obj = json.loads(out)
    except Exception:  # noqa: BLE001
        obj = None
    check("report 顶层为 list → rc≠0 且 decision 不 approved",
          rc != 0 and decision.get("status") == "proposed", (rc, out[-400:]))
    check("report 顶层为 list → 无 traceback + 单行 JSON",
          "Traceback" not in out and "\n" not in out.strip()
          and isinstance(obj, dict) and obj.get("ok") is False, out[-400:])


# ---------------------------------------------------------------------------
# 8. validate_report 双列表 + run_task 事件契约 / 自签兜底移除
# ---------------------------------------------------------------------------
def case_validate_report_dual(tmp):
    print("\n[8a] validate_report 双 data_refs")
    report, clean, demo_refs = build_real_report(tmp)
    errs = gates.real_report_errors(report, ROOT)
    check("真实报告夹具过可信门", not errs, "；".join(errs)[:400])
    vr = Path(tmp) / "validate"
    base = json.loads(json.dumps(report))

    def validate(rep, name):
        p = vr / f"{name}.json"
        dump(p, rep)
        return run([PY, "-B", str(ROOT / "tests" / "validate_report.py"), str(p), "--require-real"])

    rc, out = validate(base, "ok")
    check("干净真实报告 validate PASS", rc == 0 and "RESULT: PASS" in out, (rc, out[-500:]))

    top_demo = json.loads(json.dumps(base))
    top_demo["data_refs"] = demo_refs
    top_demo["decision_log_entry"]["data_refs"] = clean
    rc, out = validate(top_demo, "top-demo")
    check("顶层 data_refs 含 demo/ → FAIL",
          rc != 0 and "real data_refs 无 demo/" in out, (rc, out[-700:]))

    dl_demo = json.loads(json.dumps(base))
    dl_demo["data_refs"] = clean
    dl_demo["decision_log_entry"]["data_refs"] = demo_refs
    rc, out = validate(dl_demo, "dl-demo")
    check("decision_log_entry.data_refs 含 demo/ → FAIL",
          rc != 0 and "decision_log_entry.data_refs 无 demo/" in out, (rc, out[-700:]))


def case_run_task(tmp):
    print("\n[8b] run_task 事件契约 / 自签兜底")
    runs = Path(tmp) / "runs"
    workflow = ROOT / "runs" / "demo-result-2026-10-19.json"
    env = {"BOT_RUNS_ROOT": str(runs)}
    # 传 --signer 但没有白名单：M1.5 硬化后必须拒绝（不再"自签自批"）
    outdir1 = runs / "T-20261019-CORE-11"
    rc, out = run([PY, "-B", str(ROOT / "tools" / "run_task.py"), "--demo",
                   "--outdir", outdir1, "--workflow-result", workflow,
                   "--expected", "T+7 补料率回落至 ≤5.40%",
                   "--signer", "老板"], env=env)
    check("run_task --signer 无白名单 → 失败", rc != 0, (rc, out[-800:]))
    check("失败原因是未配置 signer 白名单",
          "未配置 signer 白名单" in out, out[-800:])
    status = json.loads((outdir1 / "status.json").read_text(encoding="utf-8"))
    check("失败写 status=failed", status.get("state") == "failed", status)

    # 不传 signer：正常组装，检查 run.log 事件契约
    outdir2 = runs / "T-20261019-CORE-12"
    rc, out = run([PY, "-B", str(ROOT / "tools" / "run_task.py"), "--demo",
                   "--outdir", outdir2, "--workflow-result", workflow,
                   "--expected", "T+7 补料率回落至 ≤5.40%"], env=env)
    check("run_task --demo rc0", rc == 0, (rc, out[-800:]))
    check("run_task 落 report.json", (outdir2 / "report.json").is_file())
    log = outdir2 / "run.log"
    recs = events.iter_events(str(log))
    check("run.log 有事件", len(recs) >= 5, len(recs))
    required = {"seq", "ts", "phase", "role", "agent", "kind", "line"}
    check("run.log 事件字段齐", all(required <= set(r) for r in recs),
          [r for r in recs if not required <= set(r)][:2])
    seqs = [r["seq"] for r in recs]
    check("run.log seq 严格递增", seqs == sorted(set(seqs)), seqs)
    check("run.log phase 合法", all(r["phase"] in events.EVENT_PHASES for r in recs),
          {r["phase"] for r in recs} - set(events.EVENT_PHASES))
    check("run.log role 合法", all(r["role"] in events.EVENT_ROLES for r in recs),
          {r["role"] for r in recs} - set(events.EVENT_ROLES))
    check("run.log kind 合法", all(r["kind"] in events.EVENT_KINDS for r in recs),
          {r["kind"] for r in recs} - set(events.EVENT_KINDS))
    check("run.log agent 是真名",
          all(r["agent"] in events.DEFAULT_AGENT_BY_ROLE.values() for r in recs),
          {r["agent"] for r in recs})
    check("run.log 含 tool 与 status 事件",
          {"tool", "status"} <= {r["kind"] for r in recs}, {r["kind"] for r in recs})
    check("run.log 含 assemble 写报告事件",
          any("report written" in r["line"] for r in recs), [r["line"] for r in recs][-3:])
    rep = json.loads((outdir2 / "report.json").read_text(encoding="utf-8"))
    check("run_task demo 报告 is_demo=true", rep.get("is_demo") is True, rep.get("is_demo"))


# ---------------------------------------------------------------------------
def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    tmp = Path(tempfile.mkdtemp(prefix="bot-core-m15-"))
    print(f"sandbox = {tmp}")
    try:
        case_paths(tmp)
        case_events(tmp)
        case_team(tmp)
        case_gate(tmp)
        case_gates_hardening()
        case_pack()
        case_finalize_gate(tmp)
        case_validate_report_dual(tmp)
        case_run_task(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n共 {CHECKS} 项检查，失败 {len(FAILS)} 项")
    if FAILS:
        for name in FAILS:
            print("  - FAILED:", name)
        print("RESULT: FAIL")
        return 1
    print("RESULT: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
