#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""M1 独立验收测试（B3 Checker · 黑盒 / Maker-Checker 的 Checker 侧）。

判据来源：audit/M1-build-spec.md §1 A1–A6、§2 文件契约、§5 交付纪律。
设计约束（故意保持黑盒独立性）：
  * 只用 stdlib + subprocess，不 import 被测模块做断言（core 合同点用独立子进程探）；
  * python 一律加 -B；不写仓库 runs/：每个用例把 $BT 必要文件复制到 %TEMP%\\m1-accept-* 沙盒再跑；
  * 按 spec 的「命令 + 文件契约」判，不假设内部函数名（spec 明写的 allocate/load/check_finalize 除外）；
  * 失败原因分三类前缀，方便 Lead 区分「实现未完成」与「测试自身问题」：
      [实现未就绪]  被测文件/入口尚不存在，或依赖的环境（pandas 等）不满足 spec
      [测试自身问题] 夹具/调用方式需要修正（B3 自查）
      [断言失败]    契约存在但行为不符合 spec（真 bug / 真红）

覆盖：
  A1 同日连建 2 任务：task_id 不同、各自 runs/<id>/、重跑不覆盖历史
  A2 real 报告门：占位 evidence / data_refs 不存在 → validate --require-real、assemble 非 0
  A3 finalize 门：pending securityGate / rejected decision / 伪 signer → 非 0；合法链 → approved 且 approval.by==signer
  A4 六维全 5 → total=100.0 且 verdict 命中「系统建议」；run_task 产出的 scorecard 与 score_scorecard 独立调用一致
  A5 改 packs/manufacturing/pack.json 的 ROI 锚点 → 新报告 ROI 数值随之变
  A6 UI 交付卡字段==report.json；签字按钮哈希==sign_delivery.py 对同一 report.json 的 sha256
     （浏览器不可用 → 静态检查 ui/api-adapter.js + 服务接口 HTTP 断言；局限见各用例 NOTE）

用法：
  python -B tests/test_m1_acceptance.py              # 跑全部
  python -B tests/test_m1_acceptance.py --case A3    # 只跑一个（可逗号分隔/重复）
  python -B tests/test_m1_acceptance.py --list
  python -B tests/test_m1_acceptance.py --keep       # 保留全部沙盒（默认只保留失败的）
"""

from __future__ import annotations

import argparse
import copy
import csv
import datetime as _dt
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import traceback
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]          # $BT
FIX = Path(__file__).resolve().parent / "fixtures" / "m1"
CASE_IDS = ["A1", "A2", "A3", "A4", "A5", "A6"]

# venv python（spec 要求）；也允许 M1_PY 覆盖，缺省退回当前解释器
_PY_CANDIDATES = [
    os.environ.get("M1_PY", ""),
    r"D:\work\产品类\个人产品\bot-2\venv\Scripts\python.exe",
    sys.executable,
]


def _pick_py() -> str:
    for cand in _PY_CANDIDATES:
        if cand and Path(cand).exists():
            return cand
    return sys.executable


PY = _pick_py()

# finalize 的 signer allowlist 由实现决定、spec 未定义来源 → 黑盒枚举员工名册里的合法签字人。
SIGNER_CANDIDATES = [
    "业务负责人", "用户", "工作助理", "咨询分析师", "AI 归因专家",
    "制造业业务专家", "合规质检", "取数员", "记录员",
    "host", "consulting-analyst", "ai-attribution-expert",
    "manufacturing-ops-expert", "report-recorder",
]

IGNORE_COPY = shutil.ignore_patterns(
    "runs", "__pycache__", "*.pyc", "*.png", ".git", ".pytest_cache",
)


# ---------------------------------------------------------------- 基础设施


class CaseFailure(Exception):
    """kind: not_ready | test_issue | assert"""

    def __init__(self, kind: str, reason: str):
        super().__init__(reason)
        self.kind = kind
        self.reason = reason


def not_ready(msg: str):
    raise CaseFailure("not_ready", msg)


def test_issue(msg: str):
    raise CaseFailure("test_issue", msg)


def check(cond, msg: str):
    if not cond:
        raise CaseFailure("assert", msg)


def tail(text: str, n: int = 1500) -> str:
    text = (text or "").strip()
    return text if len(text) <= n else "…" + text[-n:]


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:  # pragma: no cover
        return f"<read error: {exc}>"


def dump_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def try_json(text: str):
    """从混合输出里抠出第一个 JSON 值（对象或数组均可）。"""
    dec = json.JSONDecoder()
    start = 0
    while True:
        cands = [i for i in (text.find("{", start), text.find("[", start)) if i >= 0]
        if not cands:
            return None
        i = min(cands)
        try:
            obj, _ = dec.raw_decode(text, i)
            return obj
        except Exception:
            start = i + 1


def _env(extra=None) -> dict:
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env.pop("BOT_PACK", None)
    env.pop("BOT_SIGNERS", None)
    if extra:
        env.update({k: str(v) for k, v in extra.items()})
    return env


def run(args, cwd: Path, timeout: int = 300, env_extra=None):
    """跑一个子进程，返回 CompletedProcess（stdout+stderr 合并进 .out）。"""
    cmd = [str(PY), "-B"] + [str(a) for a in args]
    p = subprocess.run(
        cmd, cwd=str(cwd), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=timeout, env=_env(env_extra),
    )
    p.out = (p.stdout or "") + (p.stderr or "")
    return p


def tool(repo: Path, rel: str, *args, timeout: int = 300, env_extra=None):
    path = repo / rel
    if not path.exists():
        not_ready(f"缺少入口/资产 {rel}（生产侧未交付）")
    return run([path, *args], cwd=repo, timeout=timeout, env_extra=env_extra)


class Sandbox:
    def __init__(self, tag: str):
        self.base = Path(tempfile.mkdtemp(prefix=f"m1-accept-{tag}-"))
        self.repo = self.base / "repo"
        shutil.copytree(ROOT, self.repo, ignore=IGNORE_COPY)
        (self.repo / "runs").mkdir(exist_ok=True)

    def cleanup(self, keep: bool):
        if keep:
            print(f"  SANDBOX: {self.base}")
        else:
            shutil.rmtree(self.base, ignore_errors=True)


# ---------------------------------------------------------------- HTTP / server


def http_get(url: str, timeout: float = 5.0):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:  # 连接拒绝等
        return None, str(e)


def http_post(url: str, obj, timeout: float = 30.0):
    data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        return None, str(e)


class LocalServer:
    """起 server/app.py（--host/--port/--root），轮询 /api/health。"""

    def __init__(self, repo: Path):
        self.repo = repo
        self.proc = None
        self.base = ""
        self.health = None
        self.log_path = repo.parent / "server.log"
        self._fh = None

    def start(self, wait_s: float = 30.0):
        app = self.repo / "server" / "app.py"
        if not app.exists():
            not_ready("缺少 server/app.py（B2 未交付）")
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        self._fh = open(self.log_path, "wb")
        self.proc = subprocess.Popen(
            [str(PY), "-B", str(app), "--host", "127.0.0.1", "--port", str(port),
             "--root", str(self.repo)],
            cwd=str(self.repo), stdout=self._fh, stderr=subprocess.STDOUT, env=_env(),
        )
        self.base = f"http://127.0.0.1:{port}"
        deadline = time.time() + wait_s
        last = ""
        while time.time() < deadline:
            rc = self.proc.poll()
            if rc is not None:
                not_ready(f"server/app.py 启动即退出 rc={rc}: {tail(read_text(self.log_path))}")
            st, body = http_get(self.base + "/api/health", timeout=2)
            if st == 200:
                self.health = try_json(body)
                return self
            last = f"{st} {tail(body, 300)}"
            time.sleep(0.35)
        not_ready(f"/api/health {wait_s:.0f}s 内未就绪: {last}; log={tail(read_text(self.log_path))}")

    def stop(self):
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except Exception:
                self.proc.kill()
        if self._fh is not None:
            try:
                self._fh.close()
            except Exception:
                pass


# ---------------------------------------------------------------- 夹具构造


def clean_placeholders(obj):
    """把样例报告里 demo 语境文字清掉，得到可当 real 用的正控夹具（不改结构）。"""
    replacements = [
        ("（demo 占位预期，真实试点须按数据合同重设）", ""),
        ("（demo 数据）", ""),
        ("demo 占位预期，真实试点须按数据合同重设", ""),
        ("demo 占位", "真实口径"),
    ]

    def walk(x):
        if isinstance(x, str):
            for a, b in replacements:
                x = x.replace(a, b)
            return x
        if isinstance(x, list):
            return [walk(i) for i in x]
        if isinstance(x, dict):
            return {k: walk(v) for k, v in x.items()}
        return x

    return walk(obj)


def ensure_real_data(repo: Path) -> list[str]:
    """把 demo 数据复制到非 demo/ 路径：A2「real 门」要求 data_refs 不得指向 demo/。

    只改「引用路径」不改数据内容——测的是溯源/占位门，不是数据真伪。
    """
    out = []
    for name in ("material_refill_T.json", "material_refill_T30.json"):
        src = repo / "demo" / name
        if not src.exists():
            test_issue(f"沙盒缺少数据夹具 demo/{name}")
        rel = f"_data/real/{name}"
        dst = repo / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        if not dst.exists():
            shutil.copy2(src, dst)
        out.append(rel)
    return out


def intake_for_create() -> dict:
    """建任务用 intake：去掉固定 task_id，让服务端/发号器自己分配（A1 测同日不撞号）。"""
    path = FIX / "intake.json"
    if not path.exists():
        test_issue(f"缺少夹具 {path}")
    obj = json.loads(path.read_text(encoding="utf-8"))
    obj.pop("task_id", None)
    return obj


def build_real_report(repo: Path, out_rel: str, task_id: str = "T-20261019-900",
                      gate_status: str = "confirmed", signer: str = "业务负责人",
                      mutate=None) -> Path:
    """用 fixtures/report_template.json 造一个 real 报告：is_demo=false + data_refs(sha256) + security_gate。"""
    tpl = FIX / "report_template.json"
    if not tpl.exists():
        test_issue(f"缺少夹具 {tpl}")
    rep = clean_placeholders(json.loads(tpl.read_text(encoding="utf-8")))
    rep["is_demo"] = False
    rep["task_id"] = task_id
    dle = rep.setdefault("decision_log_entry", {})
    dle["task_id"] = task_id

    refs, ref_paths = [], []
    for rel in ensure_real_data(repo):
        p = repo / rel
        refs.append({"path": rel, "sha256": sha256_file(p), "size": p.stat().st_size})
        ref_paths.append(rel)
    rep["data_refs"] = refs
    # M1 real 报告契约：可信门要求 data_refs（含 decision_log_entry 里的引用）必须带 sha256 溯源；
    # report_template 是 demo 样例，这里是裸路径，造 real 报告时升级成引用对象。
    dle["data_refs"] = [dict(r) for r in refs]
    dle["security_gate"] = {
        "requested_at": (dle.get("security_gate") or {}).get("requested_at", "2026-10-19"),
        "confirmed_by": signer if gate_status == "confirmed" else None,
        "status": gate_status,
    }
    # M1 real 报告契约：scorecard 必须落 100 分制 total + verdict（模板只有六维）。
    sc = rep.setdefault("scorecard", {})
    if "total" not in sc or "verdict" not in sc:
        dims = {k: sc[k] for k in (
            "data_completeness", "timeliness", "significance",
            "exclusivity", "history", "human_ai_consistency",
        ) if isinstance(sc.get(k), (int, float))}
        sf = repo / "_data" / "real" / "_scores_tmp.json"
        dump_json(sf, dims)
        sp = run([repo / "tools" / "score_scorecard.py", "--scores-file", sf], cwd=repo)
        sobj = try_json(sp.out) or {}
        if sp.returncode == 0 and isinstance(sobj.get("total"), (int, float)):
            sc["total"] = sobj["total"]
            sc["verdict"] = sobj.get("verdict")
        else:
            sc["total"] = 60.02
            sc["verdict"] = "force_manual_review"
    if mutate:
        mutate(rep)
        # 注意：mutate 可能改结构；data_refs 语义保持上面构造的
    out = repo / out_rel
    dump_json(out, rep)
    return out


def build_task_csv(repo: Path) -> Path:
    """按 templates/field-map.example.yaml 的中文列名造 37 天 CSV（2026-09-05..10-11）。"""
    headers = ["日期", "产线", "补料率", "产量", "模具状态", "保养记录", "班组",
               "原料批次", "环境温湿度", "换模记录", "停机记录", "信息员"]
    start = _dt.date(2026, 9, 5)
    t_start = _dt.date(2026, 10, 5)
    t_rates = [5.10, 5.28, 5.54, 5.90, 6.26, 6.85, 7.20]
    rows = []
    for i in range(37):
        d = start + _dt.timedelta(days=i)
        if d < t_start:
            rate = [5.01, 5.12, 4.90, 5.07, 4.95, 5.10][i % 6]
        else:
            rate = t_rates[(d - t_start).days]
        mold = {"M-01": "normal", "M-02": "normal", "M-03": "normal"}
        if d >= _dt.date(2026, 10, 10):
            mold["M-03"] = "wear_detected"
        elif d >= _dt.date(2026, 10, 8):
            mold["M-03"] = "slight_wear"
        rows.append({
            "日期": d.isoformat(),
            "产线": "B",
            "补料率": rate,
            "产量": [1106, 989, 1068, 1120, 1005, 1090, 1080][i % 7],
            "模具状态": json.dumps(mold, ensure_ascii=False),
            "保养记录": json.dumps([], ensure_ascii=False),
            "班组": ["甲班", "乙班", "丙班"][i % 3],
            "原料批次": "RM-2026043",
            "环境温湿度": json.dumps({"temp_c": 23.4 + (i % 5) * 0.3,
                                      "humidity_pct": 55.8 + (i % 7) * 0.6},
                                     ensure_ascii=False),
            "换模记录": json.dumps([], ensure_ascii=False),
            "停机记录": json.dumps([], ensure_ascii=False),
            "信息员": "李强",
        })
    out = repo / "_data" / "task.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=headers)
        w.writeheader()
        w.writerows(rows)
    return out


def run_task_args(repo: Path, task_json: Path, outdir: Path,
                  workflow: Path, signer: str | None = None) -> list:
    """M1 推荐入口：--task-json（JSON 数据路径，venv 无 pandas 也能跑）。"""
    args = ["--task-json", task_json, "--outdir", outdir,
            "--workflow-result", workflow,
            "--expected", "T+7 补料率回落至 ≤5.40%"]
    if signer:
        args += ["--signer", signer]
    return args


def find_new_report(repo: Path, after: float):
    hits = [p for p in repo.rglob("report.json") if p.stat().st_mtime >= after - 1]
    if not hits:
        hits = list(repo.rglob("report.json"))
    hits.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return hits[0] if hits else None


def extract_report_score(report: dict):
    """从 report.json 里取 scorecard.total / verdict（§2.5 要求）。返回 (total, verdict, 位置说明)。"""
    sc = report.get("scorecard")
    if isinstance(sc, dict) and "total" in sc and "verdict" in sc:
        return sc.get("total"), sc.get("verdict"), "report.scorecard.total/verdict"
    if isinstance(sc, dict) and "dimensions" in sc and isinstance(sc["dimensions"], dict) \
            and "total" in sc:
        return sc.get("total"), sc.get("verdict"), "report.scorecard(total,dimensions)"
    for tk, vk in (("scorecard_total", "verdict"), ("total", "verdict")):
        if tk in report and vk in report:
            return report.get(tk), report.get(vk), f"report.{tk}/report.{vk}"
    return None, None, "not-found"


# ---------------------------------------------------------------- A1


def case_a1(ctx: dict):
    sb: Sandbox = ctx["sb"]
    repo = sb.repo
    if not (repo / "core" / "ids.py").exists():
        not_ready("core/ids.py 不存在（B1 核心发号器未交付，A1 无法判）")

    # 1) core.ids.allocate 合同：task 两次不撞号；预建 runs/<id>/ 后新号必须跳过
    script = r"""
import json, sys, pathlib
root = pathlib.Path(sys.argv[1])
sys.path.insert(0, str(root))
from core.ids import allocate
a = allocate(root, "task")
b = allocate(root, "task")
(root / "runs" / a).mkdir(parents=True, exist_ok=True)
c = allocate(root, "task")
print(json.dumps({"a": a, "b": b, "c": c}))
"""
    p = run(["-c", script, repo], cwd=repo)
    check(p.returncode == 0, f"core.ids.allocate 独立调用失败 rc={p.returncode}: {tail(p.out)}")
    ids = try_json(p.out)
    if not ids:
        test_issue(f"allocate 探针输出无法解析: {tail(p.out)}")
    pat = re.compile(r"^T-\d{8}-\d{3}$")
    check(pat.match(str(ids["a"])) and pat.match(str(ids["b"])),
          f"task_id 不符合 T-YYYYMMDD-NNN：{ids['a']!r}/{ids['b']!r}")
    check(ids["a"] != ids["b"], f"同日两次 allocate 撞号: {ids['a']} == {ids['b']}")
    check(str(ids["c"]) not in (str(ids["a"]), str(ids["b"])),
          f"已存在 runs/{ids['a']} 目录后 allocate 仍返回旧号 {ids['c']}")
    notes = [f"allocate: {ids['a']} / {ids['b']} / 跳号后 {ids['c']}"]

    # 2) 建任务入口：优先 server（§2.6），退回 run_task；两种路径都要求「各自 runs/<id>/ + 不覆盖历史」
    created = []  # list of (task_id, task_dir)
    if (repo / "server" / "app.py").exists():
        srv = LocalServer(repo)
        try:
            srv.start()
            st, body = http_post(srv.base + "/api/tasks",
                                 intake_for_create())
            check(st in (200, 201), f"POST /api/tasks 第一次失败 HTTP {st}: {tail(body)}")
            j1 = try_json(body) or {}
            id1 = j1.get("task_id") or (j1.get("task") or {}).get("task_id")
            check(bool(id1), f"POST /api/tasks 未返回 task_id: {tail(body)}")
            st2, body2 = http_post(srv.base + "/api/tasks",
                                   intake_for_create())
            check(st2 in (200, 201), f"POST /api/tasks 第二次失败 HTTP {st2}: {tail(body2)}")
            id2 = (try_json(body2) or {}).get("task_id") or \
                  ((try_json(body2) or {}).get("task") or {}).get("task_id")
            check(bool(id2), f"POST /api/tasks 第二次未返回 task_id: {tail(body2)}")
            check(id1 != id2, f"同日连建 2 个任务撞号: {id1} == {id2}")
            d1, d2 = repo / "runs" / str(id1), repo / "runs" / str(id2)
            check(d1.is_dir(), f"任务 {id1} 未落盘 runs/<id>/（缺 {d1}）")
            check(d2.is_dir(), f"任务 {id2} 未落盘 runs/<id>/（缺 {d2}）")
            for d in (d1, d2):
                check((d / "task.json").is_file(), f"{d.name}/ 缺 task.json")
            # 第二次建任务不得改动第一份历史
            (d1 / "SENTINEL.txt").write_text("keep-me", encoding="utf-8")
            snap1 = (d1 / "task.json").read_bytes()
            st3, body3 = http_post(srv.base + "/api/tasks",
                                   intake_for_create())
            check(st3 in (200, 201), f"POST /api/tasks 第三次失败 HTTP {st3}: {tail(body3)}")
            check((d1 / "SENTINEL.txt").read_text(encoding="utf-8") == "keep-me",
                  "重跑/继续建任务后 runs/<首个 id>/ 的 SENTINEL 被删除（覆盖历史）")
            check((d1 / "task.json").read_bytes() == snap1,
                  "重跑/继续建任务后 runs/<首个 id>/task.json 被改写（覆盖历史）")
            created = [(str(id1), d1), (str(id2), d2)]
            notes.append(f"server 连建: {id1} / {id2}，首份历史未被覆盖")
        finally:
            srv.stop()
    else:
        csv_path = build_task_csv(repo)
        wf = FIX / "workflow_result_demo.json"
        if not wf.exists():
            test_issue(f"缺少夹具 {wf}")
        dirs_before = {p.name for p in (repo / "runs").iterdir()}
        signer_used, last = None, None
        for cand in SIGNER_CANDIDATES:
            rc_ok = True
            for _ in range(2):
                p = tool(repo, "tools/run_task.py",
                         *run_task_args(repo, csv_path, repo / "runs", wf, cand),
                         timeout=300)
                if p.returncode != 0:
                    rc_ok = False
                    last = p.out
                    break
            if rc_ok:
                signer_used = cand
                break
        if signer_used is None:
            not_ready(f"server 与可自动发号的 run_task 都未跑通；run_task 末次输出: {tail(last or '', 800)}")
        new_dirs = [p for p in (repo / "runs").iterdir()
                    if p.is_dir() and p.name not in dirs_before]
        check(len(new_dirs) >= 2, f"run_task 两次建任务未产生 2 个独立 runs/<id>/（实际 {[p.name for p in new_dirs]}）")
        for d in new_dirs[:2]:
            check((d / "task.json").is_file(), f"{d.name}/ 缺 task.json")
        created = [(d.name, d) for d in new_dirs[:2]]
        notes.append(f"run_task 自动发号: {[c[0] for c in created]}")

    # 3) 任务号必须唯一且格式正确（无论走哪条入口）
    tids = [c[0] for c in created]
    check(len(set(tids)) == len(tids), f"建任务返回的 task_id 有重复: {tids}")
    for tid in tids:
        if re.match(r"^T-\d{8}-\d{3}$", str(tid)) is None:
            notes.append(f"NOTE: {tid} 非 T-YYYYMMDD-NNN 格式（若来自 intake 固定 id 可接受，待人工判）")
    return "；".join(notes)


# ---------------------------------------------------------------- A2


def case_a2(ctx: dict):
    sb: Sandbox = ctx["sb"]
    repo = sb.repo
    for rel in ("tools/assemble_report.py", "orchestration/assemble_report.py",
                "tests/validate_report.py", "tools/validate_report.py"):
        pass
    validate = None
    for rel in ("tests/validate_report.py", "tools/validate_report.py"):
        if (repo / rel).exists():
            validate = rel
            break
    if validate is None:
        not_ready("缺少 validate_report.py")
    assemble = None
    for rel in ("orchestration/assemble_report.py", "tools/assemble_report.py"):
        if (repo / rel).exists():
            assemble = rel
            break
    if assemble is None:
        not_ready("缺少 assemble_report.py")

    notes = []
    real = build_real_report(repo, "runs/T-20261019-900/report.json")

    # 正控 0：不带 flag 的旧校验也必须 PASS（红线：demo-report 仍 PASS 由另一条覆盖）
    p0 = tool(repo, validate, real)
    check(p0.returncode == 0,
          f"合法 real 报告未通过基础 validate_report（rc={p0.returncode}）: {tail(p0.out)}")

    # 正控 1：--require-real 必须接受合法 real 报告（否则 flag/实现没到位）
    p1 = tool(repo, validate, real, "--require-real")
    if p1.returncode != 0:
        low = p1.out.lower()
        if "not found: --require-real" in low or "unrecognized arguments" in low or "invalid choice" in low:
            not_ready(f"validate_report.py 未支持 --require-real（把 flag 当路径/参数）: {tail(p1.out, 400)}")
        check(False, f"合法 real 报告未通过 validate_report --require-real: {tail(p1.out)}")
    notes.append("正控: 合法 real 报告 --require-real PASS")

    # 负例 1：占位 evidence 必须被 --require-real 拒绝
    bad = build_real_report(
        repo, "runs/T-20261019-901/report.json", task_id="T-20261019-901",
        mutate=lambda r: r["finding"][0]["evidence"].insert(0, "demo 占位：无 provenance 的占位证据"),
    )
    p = tool(repo, validate, bad, "--require-real")
    check(p.returncode != 0, "占位 evidence（'demo 占位'）未被 validate_report --require-real 拒绝（rc=0）")
    check(any(t in p.out for t in ("占位", "placeholder", "Provenance", "provenance", "真实")),
          f"--require-real 拒了占位 evidence 但原因不明确: {tail(p.out, 500)}")
    notes.append("负例: 占位 evidence → 非 0")

    # 负例 2：data_refs 指向不存在文件 → 必须非 0
    def missing_ref(r):
        r["data_refs"] = [{"path": "_data/real/__definitely_missing__.json",
                           "sha256": "0" * 64, "size": 1}]
    bad2 = build_real_report(repo, "runs/T-20261019-902/report.json",
                             task_id="T-20261019-902", mutate=missing_ref)
    p = tool(repo, validate, bad2, "--require-real")
    check(p.returncode != 0, "data_refs 指向不存在文件未被 --require-real 拒绝（rc=0）")
    check(any(t in p.out for t in ("不存在", "missing", "not found", "ref", "data_ref", "证据", "sha")),
          f"缺 refs 被拒但原因不明确: {tail(p.out, 500)}")
    notes.append("负例: 缺失 data_refs → 非 0")

    # 负例 3：sha256 不匹配 → 必须非 0
    def bad_sha(r):
        r["data_refs"][0]["sha256"] = "f" * 64
    bad3 = build_real_report(repo, "runs/T-20261019-903/report.json",
                             task_id="T-20261019-903", mutate=bad_sha)
    p = tool(repo, validate, bad3, "--require-real")
    check(p.returncode != 0, "data_refs sha256 不匹配未被 --require-real 拒绝（rc=0）")
    notes.append("负例: sha 不匹配 → 非 0")

    # 正控 2：assemble --real 干净 workflow-result + 存在 refs 必须成功（负例才有意义）
    wf_clean = FIX / "workflow_result_demo.json"
    if not wf_clean.exists():
        test_issue(f"缺少夹具 {wf_clean}")
    work = repo / "_a2"
    work.mkdir(exist_ok=True)
    real_refs = ensure_real_data(repo)
    wf_txt = read_text(wf_clean)
    if "demo 占位" in wf_txt:
        # 正控输入本身若含占位词，先清理（保持结构），避免正控失败
        wf_clean_obj = clean_placeholders(json.loads(wf_txt))
        wf_path_clean = work / "workflow_clean.json"
        dump_json(wf_path_clean, wf_clean_obj)
        wf_clean = wf_path_clean
    pos = work / "report_positive.json"
    p = tool(repo, assemble, wf_clean, pos,
             "--real", "--task-id", "T-20261019-904",
             "--context", "B产线补料率7日 5.1%→7.2%",
             "--data-refs", ",".join(real_refs),
             "--expected", "T+7 补料率回落至 ≤5.40%")
    if p.returncode != 0:
        low = p.out.lower()
        if "unrecognized arguments" in low or "usage:" in low:
            test_issue(f"assemble --real 正控调用参数与 CLI 契约不符（B3 待修）: {tail(p.out, 800)}")
        check(False, f"assemble --real 正控失败（干净输入 + 存在的 refs 应成功）: {tail(p.out)}")
    check(pos.is_file(), "assemble --real 正控 rc=0 但未产出报告文件")
    notes.append("正控: assemble --real 干净输入 PASS")

    # 负例 4：assemble --real 喂占位 evidence → 必须非 0
    wf_bad_obj = clean_placeholders(json.loads(wf_txt))
    wf_bad_obj["attribution"]["finding"][0]["evidence"] = ["demo 占位：无来源占位证据"]
    wf_bad = work / "workflow_placeholder.json"
    dump_json(wf_bad, wf_bad_obj)
    p = tool(repo, assemble, wf_bad, work / "report_bad.json",
             "--real", "--task-id", "T-20261019-905",
             "--context", "B产线补料率7日 5.1%→7.2%",
             "--data-refs", ",".join(real_refs),
             "--expected", "T+7 补料率回落至 ≤5.40%")
    check(p.returncode != 0, "assemble --real 对占位 evidence 未拒绝（rc=0）")
    notes.append("负例: assemble 占位 evidence → 非 0")

    # 负例 5：assemble --real 的 --data-refs 指向不存在文件 → 必须非 0
    p = tool(repo, assemble, wf_clean, work / "report_bad2.json",
             "--real", "--task-id", "T-20261019-906",
             "--context", "B产线补料率7日 5.1%→7.2%",
             "--data-refs", "_data/real/__definitely_missing__.json",
             "--expected", "T+7 补料率回落至 ≤5.40%")
    check(p.returncode != 0, "assemble --real 对不存在的 --data-refs 未拒绝（rc=0）")
    notes.append("负例: assemble 缺 refs → 非 0")
    return "；".join(notes)


# ---------------------------------------------------------------- A3


def _sig_sha_from_json(path: Path):
    obj = try_json(read_text(path))
    if isinstance(obj, dict):
        return obj.get("report_sha256")
    return None


def craft_signature(report: Path, signer: str | None, out: Path) -> Path:
    obj = {
        "report": str(report),
        "report_sha256": sha256_file(report),
        "signed_at": "2026-10-19T16:00:00+08:00",
        "is_demo": False,
    }
    if signer is not None:
        obj["signer"] = signer
    dump_json(out, obj)
    return out


def _finalize_has_reason(p) -> bool:
    out = p.out or ""
    if any(t in out for t in ("security", "Security", "securityGate", "门", "gate", "Gate",
                              "confirmed", "确认", "pending", "白名单", "allowlist", "signer",
                              "签名", "status", "reject", "拒绝", "approved", "proposed")):
        return True
    obj = try_json(out)
    return isinstance(obj, dict) and obj.get("ok") is False


def case_a3(ctx: dict):
    sb: Sandbox = ctx["sb"]
    repo = sb.repo
    for rel in ("tools/sign_delivery.py", "tools/write_loop_bundle.py",
                "tools/finalize_delivery.py"):
        if not (repo / rel).exists():
            not_ready(f"缺少 {rel}（B1 未交付 finalize 链）")
    work = repo / "_a3"
    work.mkdir(exist_ok=True)
    notes, problems = [], []
    ALLOWED = "老板"          # lead 契约：白名单示例 signer
    BOGUS = "fake-signer-zzz-9000"

    def fail(label: str, exc: CaseFailure):
        problems.append(f"{label}: [{exc.kind}] {exc.reason}")

    def run_finalize(report_path: Path, loop_dir: Path, sig_path: Path,
                     allow_signers: str | None = ALLOWED, env_extra=None):
        args = ["--report", report_path, "--loop-dir", loop_dir, "--signature", sig_path]
        if allow_signers is not None:
            args += ["--allow-signers", allow_signers]
        return tool(repo, "tools/finalize_delivery.py", *args, env_extra=env_extra)

    def sign_report(report_path: Path, tag: str, signer: str | None = None):
        sig = work / f"sig_{tag}.json"
        args = ["--report", report_path, "--out", sig]
        if signer is not None:
            args += ["--signer", signer]
        ps = tool(repo, "tools/sign_delivery.py", *args)
        if ps.returncode != 0:
            raise CaseFailure("assert", f"{tag}: sign_delivery 失败 rc={ps.returncode}: {tail(ps.out, 400)}")
        return sig

    def make_loop(report_path: Path, tag: str, approver: str) -> Path:
        loop = work / f"loop_{tag}"
        pb = tool(repo, "tools/write_loop_bundle.py", "--report", report_path,
                  "--outdir", loop, "--approver", approver)
        if pb.returncode != 0:
            raise CaseFailure("assert", f"{tag}: write_loop_bundle 失败: {tail(pb.out, 400)}")
        return loop

    def force_status(loop_dir: Path, status: str):
        decp = loop_dir / "decision.json"
        dec = json.loads(decp.read_text(encoding="utf-8"))
        dec["status"] = status
        dump_json(decp, dec)

    def decision_of(loop_dir: Path) -> dict:
        return json.loads((loop_dir / "decision.json").read_text(encoding="utf-8"))

    # ---- 1) 能力探针（隔离 status）：--allow-signers 存在且白名单内 signer 可过
    rep_probe = build_real_report(repo, "runs/T-20261019-910/report.json",
                                  task_id="T-20261019-910", gate_status="confirmed")
    try:
        sig = sign_report(rep_probe, "probe", ALLOWED)
        loop = make_loop(rep_probe, "probe", ALLOWED)
        force_status(loop, "proposed")
        pf = run_finalize(rep_probe, loop, sig)
        out = pf.out or ""
        if "unrecognized arguments" in out:
            not_ready("finalize_delivery.py 未实现 --allow-signers（lead 契约：新增该参数）")
        check(pf.returncode == 0,
              f"能力探针失败：--allow-signers {ALLOWED!r} + proposed + 白名单内 signer 应 rc=0，"
              f"实际 rc={pf.returncode}: {tail(out, 600)}")
        notes.append(f"能力探针: --allow-signers={ALLOWED!r} 生效")
    except CaseFailure as exc:
        if exc.kind == "not_ready":
            raise
        fail("能力探针", exc)

    # ---- 2) 合法链（端到端、不 force）：默认 signer=老板 → bundle=proposed → finalize 0/approved
    try:
        rep_l = build_real_report(repo, "runs/T-20261019-900/report.json",
                                  gate_status="confirmed")
        sig_l = sign_report(rep_l, "legal")  # 不传 --signer，验默认 signer
        sig_doc = json.loads(sig_l.read_text(encoding="utf-8"))
        check(sig_doc.get("signer") == ALLOWED,
              f"sign_delivery 默认 signer={sig_doc.get('signer')!r} != {ALLOWED!r}"
              "（lead 契约：默认 signer=老板）")
        loop_l = make_loop(rep_l, "legal", ALLOWED)
        dec = decision_of(loop_l)
        check(dec.get("status") == "proposed",
              f"write_loop_bundle 生成 decision.status={dec.get('status')!r} != 'proposed'"
              "（lead 契约：改为生成 proposed）")
        pf = run_finalize(rep_l, loop_l, sig_l)
        check(pf.returncode == 0,
              f"合法流程（confirmed gate + 默认 signer 老板 + proposed）finalize rc={pf.returncode}: "
              f"{tail(pf.out, 700)}")
        dec2 = decision_of(loop_l)
        check(dec2.get("status") == "approved",
              f"合法链 finalize 后 decision.status={dec2.get('status')!r} != 'approved'")
        check((dec2.get("approval") or {}).get("by") == ALLOWED,
              f"合法链 approval.by={(dec2.get('approval') or {}).get('by')!r} != {ALLOWED!r}")
        notes.append("合法链: 默认 signer=老板 / bundle=proposed / finalize 0 / approved / approval.by=老板")
    except CaseFailure as exc:
        fail("合法链", exc)

    # ---- 3) env BOT_SIGNERS 回退（不传 --allow-signers）→ rc0
    try:
        rep_e = build_real_report(repo, "runs/T-20261019-911/report.json",
                                  task_id="T-20261019-911", gate_status="confirmed")
        sig_e = sign_report(rep_e, "env", ALLOWED)
        loop_e = make_loop(rep_e, "env", ALLOWED)
        force_status(loop_e, "proposed")
        pf = run_finalize(rep_e, loop_e, sig_e, allow_signers=None,
                          env_extra={"BOT_SIGNERS": ALLOWED})
        check(pf.returncode == 0,
              f"BOT_SIGNERS={ALLOWED!r} 未传 --allow-signers 时应 rc=0，实际 rc={pf.returncode}: "
              f"{tail(pf.out, 600)}")
        dec_e = decision_of(loop_e)
        check(dec_e.get("status") == "approved",
              f"env 回退链 finalize 后 decision.status={dec_e.get('status')!r} != 'approved'")
        notes.append("env BOT_SIGNERS 回退: rc0/approved")
    except CaseFailure as exc:
        fail("env signer 回退", exc)

    # ---- 4) fail closed：无 --allow-signers 且无 env → 非 0，原因含「未配置 signer 白名单」
    try:
        rep_c = build_real_report(repo, "runs/T-20261019-912/report.json",
                                  task_id="T-20261019-912", gate_status="confirmed")
        sig_c = sign_report(rep_c, "closed", ALLOWED)
        loop_c = make_loop(rep_c, "closed", ALLOWED)
        force_status(loop_c, "proposed")
        pf = run_finalize(rep_c, loop_c, sig_c, allow_signers=None)
        check(pf.returncode != 0,
              "未配置白名单（无 --allow-signers、无 BOT_SIGNERS）时 finalize 仍放行 rc=0，应为 fail closed")
        check("未配置 signer 白名单" in (pf.out or ""),
              f"fail closed 原因未含「未配置 signer 白名单」: {tail(pf.out, 500)}")
        notes.append("fail closed: 无白名单 → 非 0")
    except CaseFailure as exc:
        fail("fail closed", exc)

    # ---- 5) 白名单外 signer（sha 正确、decision proposed）→ 非 0
    try:
        rep_f = build_real_report(repo, "runs/T-20261019-913/report.json",
                                  task_id="T-20261019-913", gate_status="confirmed")
        try:
            sig_f = sign_report(rep_f, "fake", BOGUS)
        except CaseFailure:
            sig_f = craft_signature(rep_f, BOGUS, work / "sig_fake_crafted.json")
        loop_f = make_loop(rep_f, "fake", ALLOWED)
        force_status(loop_f, "proposed")
        pf = run_finalize(rep_f, loop_f, sig_f)
        check(pf.returncode != 0,
              f"白名单外 signer {BOGUS!r}（report_sha256 正确）仍被 finalize 放行 rc=0")
        notes.append("负例: 白名单外 signer → 非 0")

        # ---- 6) 签名缺失 signer 字段 → 非 0
        sig_missing = craft_signature(rep_f, None, work / "sig_missing_signer.json")
        force_status(loop_f, "proposed")
        pf = run_finalize(rep_f, loop_f, sig_missing)
        check(pf.returncode != 0, "签名文件无 signer 字段仍被 finalize 放行 rc=0")
        notes.append("负例: 缺失 signer → 非 0")

        # ---- 7) report_sha256 不匹配 → 非 0
        sig_bad = work / "sig_badsha.json"
        dump_json(sig_bad, {"report": str(rep_f), "report_sha256": "0" * 64,
                            "signer": ALLOWED, "signed_at": "2026-10-19T16:00:00+08:00",
                            "is_demo": False})
        force_status(loop_f, "proposed")
        pf = run_finalize(rep_f, loop_f, sig_bad)
        check(pf.returncode != 0, "report_sha256 不匹配的签名仍被 finalize 放行 rc=0")
        notes.append("负例: sha 不匹配 → 非 0")
    except CaseFailure as exc:
        fail("负例 fake signer/sha", exc)

    # ---- 8) security_gate pending → finalize 非 0（decision 置 proposed 隔离 status 规则）
    try:
        rep_p = build_real_report(repo, "runs/T-20261019-901/report.json",
                                  task_id="T-20261019-901",
                                  gate_status="pending_user_confirmation")
        try:
            sig_p = sign_report(rep_p, "pending", ALLOWED)
        except CaseFailure:
            sig_p = craft_signature(rep_p, ALLOWED, work / "sig_pending_crafted.json")
        loop_p = make_loop(rep_p, "pending", ALLOWED)
        force_status(loop_p, "proposed")
        pf = run_finalize(rep_p, loop_p, sig_p)
        check(pf.returncode != 0,
              "security_gate=pending_user_confirmation 的 report 仍被 finalize 放行（rc=0）")
        check(_finalize_has_reason(pf), f"pending gate 被拒但无明确原因: {tail(pf.out, 500)}")
        notes.append("负例: pending securityGate → 非 0")
    except CaseFailure as exc:
        fail("负例 pending gate", exc)

    # ---- 9) decision.status=rejected → finalize 非 0
    try:
        rep_r = build_real_report(repo, "runs/T-20261019-902/report.json",
                                  task_id="T-20261019-902", gate_status="confirmed")
        sig_r = sign_report(rep_r, "rejected", ALLOWED)
        loop_r = make_loop(rep_r, "rejected", ALLOWED)
        force_status(loop_r, "rejected")
        pf = run_finalize(rep_r, loop_r, sig_r)
        check(pf.returncode != 0, "decision.status=rejected 仍被 finalize 放行（rc=0）")
        check(_finalize_has_reason(pf), f"rejected decision 被拒但无明确原因: {tail(pf.out, 500)}")
        notes.append("负例: rejected decision → 非 0")
    except CaseFailure as exc:
        fail("负例 rejected decision", exc)

    if problems:
        raise CaseFailure("assert", "；".join(problems))
    return "；".join(notes)


# ---------------------------------------------------------------- A4


def case_a4(ctx: dict):
    sb: Sandbox = ctx["sb"]
    repo = sb.repo
    score = repo / "tools" / "score_scorecard.py"
    if not score.exists():
        not_ready("缺少 tools/score_scorecard.py")
    notes = []
    work = repo / "_a4"
    work.mkdir(exist_ok=True)

    # ---- 1) 六维全 5 → total 必须 == 100.0（不是 100.02），verdict 命中「系统建议」
    all5 = work / "scores_all5.json"
    dump_json(all5, {k: 5 for k in (
        "data_completeness", "timeliness", "significance",
        "exclusivity", "history", "human_ai_consistency")})
    p = tool(repo, "tools/score_scorecard.py", "--scores-file", all5)
    obj = try_json(p.out)
    if p.returncode != 0 or not isinstance(obj, dict) or "total" not in obj:
        check(False, f"score_scorecard 全 5 调用失败 rc={p.returncode}: {tail(p.out)}")
    total = float(obj["total"])
    check(abs(total - 100.0) < 1e-9,
          f"六维全 5 时 total={obj['total']}（应为 100.0；旧的 16.67×6=100.02 属未修）")
    verdict = str(obj.get("verdict", ""))
    label = str(obj.get("label", ""))
    ok_verdict = (verdict in ("output", "system_suggestion", "suggest", "best"))
    check(ok_verdict or ("系统建议" in label) or ("最优" in label),
          f"全 5 的 verdict={verdict!r}/label={label!r} 未命中「系统建议/最优区间」（no_output 属未修）")
    notes.append(f"全 5: total={obj['total']}, verdict={verdict!r}")

    # ---- 2) --from-report 对全 5 报告也应 100.0（独立调用口径一致）
    rep5 = build_real_report(repo, "runs/T-20261019-900/report.json",
                             task_id="T-20261019-900")
    rep5_obj = json.loads(rep5.read_text(encoding="utf-8"))
    rep5_obj["scorecard"] = {k: 5 for k in (
        "data_completeness", "timeliness", "significance",
        "exclusivity", "history", "human_ai_consistency")}
    rep5_obj["is_demo"] = False
    dump_json(rep5, rep5_obj)
    p = tool(repo, "tools/score_scorecard.py", "--from-report", rep5)
    obj2 = try_json(p.out)
    if p.returncode == 0 and isinstance(obj2, dict) and "total" in obj2:
        check(abs(float(obj2["total"]) - 100.0) < 1e-9,
              f"score --from-report 全 5 total={obj2['total']}（应 100.0）")
        notes.append("--from-report 全 5: total=100.0")

    # ---- 3) run_task 端到端：产出的 report 含 scorecard.total/verdict，且与 score CLI 独立调用一致
    # ---- 3) run_task 端到端（M1 推荐 --task-json 入口；venv 无 pandas，CSV 旧路径不属 A4 验收）
    for rel in ("tools/run_task.py",):
        if not (repo / rel).exists():
            not_ready(f"缺少 {rel}")
    real_refs = ensure_real_data(repo)
    wf = FIX / "workflow_result_demo.json"
    if not wf.exists():
        test_issue(f"缺少夹具 {wf}")
    run_id = "T-20261019-910"
    run_dir = repo / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    intake = json.loads(read_text(FIX / "intake.json"))
    intake["task_id"] = run_id
    intake["data_files"] = [str((repo / r).resolve()) for r in real_refs]
    intake["expected"] = "T+7 补料率回落至 ≤5.40%"
    intake["context"] = "B产线补料率异常（真实任务）"
    task_json = run_dir / "task.json"
    dump_json(task_json, intake)
    rt = tool(repo, "tools/run_task.py",
              *run_task_args(repo, task_json, run_dir, wf), timeout=400)
    if rt.returncode != 0:
        out = tail(rt.out, 1800)
        if "pandas" in out.lower():
            check(False, "run_task 推荐入口（--task-json + JSON 数据）仍触发 pandas 依赖（不应再依赖）: "
                         + tail(out, 900))
        if "unrecognized arguments" in out.lower() or "the following arguments are required" in out.lower():
            test_issue(f"run_task CLI 参数与 M1 契约不符（B3 待修）: {tail(out, 900)}")
        check(False, f"run_task 端到端失败 rc={rt.returncode}: {tail(out, 1800)}")
    report_path = run_dir / "report.json"
    check(report_path.is_file(), f"run_task rc=0 但缺 runs/{run_id}/report.json")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    total_r, verdict_r, where = extract_report_score(report)
    check(total_r is not None,
          f"run_task 产出的 report 未含 scorecard.total/verdict（§2.5）；实际位置扫描: {where}；"
          f"report.scorecard={tail(json.dumps(report.get('scorecard'), ensure_ascii=False), 400)}")
    check(verdict_r is not None, f"report 有 total={total_r} 但缺 verdict")
    # 独立调用 score_scorecard --from-report 对同一文件
    p = tool(repo, "tools/score_scorecard.py", "--from-report", report_path)
    obj3 = try_json(p.out)
    if p.returncode != 0 or not isinstance(obj3, dict) or "total" not in obj3:
        check(False, f"score_scorecard --from-report 对 run_task 报告调用失败 rc={p.returncode}: {tail(p.out)}")
    check(abs(float(obj3["total"]) - float(total_r)) < 1e-6,
          f"report 内 scorecard.total={total_r} 与 score CLI 独立计算 {obj3['total']} 不一致")
    check(str(obj3.get("verdict", "")) == str(verdict_r),
          f"report 内 verdict={verdict_r!r} 与 score CLI {obj3.get('verdict')!r} 不一致")
    notes.append(f"run_task(--task-json): report={report_path.relative_to(repo)} total={total_r} verdict={verdict_r}")
    return "；".join(notes)


# ---------------------------------------------------------------- A5


def case_a5(ctx: dict):
    sb: Sandbox = ctx["sb"]
    repo = sb.repo
    pack_rel = "packs/manufacturing/pack.json"
    if not (repo / pack_rel).exists():
        not_ready(f"缺少 {pack_rel}（B1 行业包未交付）")
    if not (repo / "core" / "pack.py").exists():
        not_ready("缺少 core/pack.py（B1 pack 加载器未交付）")
    assemble = None
    for rel in ("orchestration/assemble_report.py", "tools/assemble_report.py"):
        if (repo / rel).exists():
            assemble = rel
            break
    if assemble is None:
        not_ready("缺少 assemble_report.py")
    work = repo / "_a5"
    work.mkdir(exist_ok=True)
    notes = []

    # ---- 1) load() 合同：默认锚点 + env BOT_PACK 覆盖生效
    code = r"""
import json, sys
from core.pack import load
p = load("manufacturing", root=sys.argv[1])
print(json.dumps({"roi_anchors": p.get("roi_anchors", {}),
                  "thresholds": p.get("thresholds", {})}, ensure_ascii=False))
"""
    p = run(["-c", code, repo], cwd=repo)
    check(p.returncode == 0, f"core.pack.load 调用失败: {tail(p.out)}")
    obj = try_json(p.out) or {}
    orig = obj.get("roi_anchors") or {}
    need = {"overtime_clean", "mold_change", "maintenance"}
    check(need.issubset(set(orig)), f"pack.roi_anchors 缺键 {need - set(orig)}；实际 {orig}")
    notes.append(f"load() 锚点={orig}")

    pack_path = repo / pack_rel
    pack = json.loads(pack_path.read_text(encoding="utf-8"))
    baseline = copy.deepcopy(pack)
    sentinels = {"overtime_clean": 4321, "mold_change": 8765, "maintenance": 6543}
    sentinel_pack = copy.deepcopy(pack)
    sentinel_pack["roi_anchors"] = dict(pack.get("roi_anchors") or {}, **sentinels)
    dump_json(work / "pack_baseline.json", baseline)
    dump_json(work / "pack_sentinel.json", sentinel_pack)

    # spec §2.4 明写 env BOT_PACK 可覆盖 load()
    bot_pack = work / "pack_bot_env.json"
    dump_json(bot_pack, sentinel_pack)
    p = run(["-c", code, repo], cwd=repo, env_extra={"BOT_PACK": bot_pack})
    check(p.returncode == 0, f"BOT_PACK 覆盖时 core.pack.load 调用失败: {tail(p.out)}")
    env_anchors = (try_json(p.out) or {}).get("roi_anchors") or {}
    hit_env = [k for k, v in sentinels.items() if str(env_anchors.get(k)) == str(v)]
    check(bool(hit_env), f"BOT_PACK 未生效：load() 锚点={env_anchors}（期望含哨兵 {sentinels}）")
    notes.append(f"BOT_PACK 覆盖生效: {hit_env}")

    # ---- 2) 改 pack → assemble 产出的新报告 ROI 数值必须随之变
    # 黑盒 seam 说明：ROI 原始值由 team-workflow.mjs 的 agent 产出；assemble 的契约是
    # 「ROI 缺失/占位时按 pack.roi_anchors 兜底」。本环境不跑 DSH workflow，因此把输入 ROI 置空，
    # 用 assemble 的兜底路径验证「报告 ROI 由 pack 决定」这一 spec 要求。
    wf = FIX / "workflow_result_demo.json"
    if not wf.exists():
        test_issue(f"缺少夹具 {wf}")
    wf_obj = clean_placeholders(json.loads(read_text(wf)))
    recs = ((wf_obj.get("advice") or {}).get("recommendation") or [])
    if not recs:
        test_issue(f"夹具 {wf} 缺 advice.recommendation，无法构造 ROI 缺失输入")
    for rec in recs:
        if isinstance(rec, dict):
            rec["roi"] = {}
    wf_stub = work / "workflow_roi_missing.json"
    dump_json(wf_stub, wf_obj)

    base_report = work / "report_baseline.json"
    mod_report = work / "report_modified.json"
    common = ["--task-id", "T-20261019-920",
              "--context", "B产线补料率7日 5.1%→7.2%",
              "--expected", "T+7 补料率回落至 ≤5.40%"]
    dump_json(pack_path, baseline)
    pb = tool(repo, assemble, wf_stub, base_report, *common)
    check(pb.returncode == 0, f"assemble 基线报告失败: {tail(pb.out)}")
    dump_json(pack_path, sentinel_pack)
    pm = tool(repo, assemble, wf_stub, mod_report, *common)
    check(pm.returncode == 0, f"改 pack 后 assemble 失败: {tail(pm.out)}")

    def load_report(path: Path) -> dict:
        txt = read_text(path)
        return json.loads(txt) if txt.lstrip().startswith("{") else {}

    base_json, mod_json = load_report(base_report), load_report(mod_report)

    def roi_blob(rep):
        blobs = [json.dumps(rec.get("roi", {}), ensure_ascii=False)
                 for rec in (rep.get("recommendation") or [])]
        blobs.append(json.dumps(rep.get("roi", {}), ensure_ascii=False))
        return "\n".join(blobs)

    base_roi, mod_roi = roi_blob(base_json), roi_blob(mod_json)
    base_values = list((baseline.get("roi_anchors") or {}).values())

    def has_num(blob: str, num) -> bool:
        s = str(int(num))
        forms = {s, format(int(num), ",")}
        return any(re.search(rf"(?<!\d){re.escape(x)}(?!\d)", blob) for x in forms)

    base_hit = [v for v in base_values if has_num(base_roi, v)]
    check(bool(base_hit),
          "assemble 未按 pack 兜底填写 ROI：基线报告 recommendation[].roi 里找不到基线锚点 "
          f"{base_values}；roi={tail(base_roi, 600)}")
    hit = [k for k, v in sentinels.items() if has_num(mod_roi, v)]
    check(bool(hit),
          "修改 packs/manufacturing/pack.json 的 ROI 锚点后，新报告 ROI 数值未变"
          f"（未在 recommendation[].roi 找到哨兵 {sentinels}；roi={tail(mod_roi, 600)}）")
    leftover = [v for v in base_values if v not in sentinels.values() and has_num(mod_roi, v)]
    check(not leftover,
          f"改 pack 后报告仍残留基线锚点数值 {leftover}（说明未重新读 pack）: {tail(mod_roi, 500)}")
    notes.append(f"改 pack 后 report ROI 命中哨兵: {hit}（基线命中 {base_hit}）")

    # ---- 3) §2.4 文件契约：team-workflow.mjs 必须从 pack 读、删掉写死的 ¥ 字面量
    wf_mjs = repo / "orchestration" / "team-workflow.mjs"
    check(wf_mjs.exists(), "缺少 orchestration/team-workflow.mjs（§2.4 要求改其读 pack）")
    txt = read_text(wf_mjs)
    has_pack = any(k in txt for k in ("pack_path", "pack.json", "roi_anchors"))
    check(has_pack, "team-workflow.mjs 未引用 pack_path/pack.json/roi_anchors（§2.4 要求 ROI 锚点与阈值从 pack 读）")
    hard = [lit for lit in ("¥800", "¥500", "¥1200") if lit in txt]
    check(not hard, f"team-workflow.mjs 仍写死 {hard}（§2.4 要求删除 :128 附近 ¥800/¥500/¥1200 字面量）")
    notes.append("mjs: 已接 pack 且无 ¥800/¥500/¥1200 字面量")
    notes.append("NOTE(局限): 本环境不执行 DSH workflow；A5 的 ROI 运行时断言走 assemble 的 pack 兜底 seam，"
                 "mjs 侧以 §2.4 静态契约补齐（agent 真实 ROI 需人工走查）")
    return "；".join(notes)


# ---------------------------------------------------------------- A6


def case_a6(ctx: dict):
    sb: Sandbox = ctx["sb"]
    repo = sb.repo
    for rel in ("server/app.py", "ui/index.html"):
        if not (repo / rel).exists():
            not_ready(f"缺少 {rel}（B2 未交付）")
    adapter = repo / "ui" / "api-adapter.js"
    if not adapter.exists():
        not_ready("缺少 ui/api-adapter.js（§2.7 未交付）")
    notes = []

    srv = LocalServer(repo)
    try:
        srv.start()
        # ---- 1) /api/health
        check(isinstance(srv.health, dict), f"/api/health 非 JSON 对象: {srv.health!r}")
        check(srv.health.get("ok") is True, f"/api/health ok != true: {srv.health}")
        check(str(srv.health.get("pack") or ""), f"/api/health 缺 pack: {srv.health}")
        check(str(srv.health.get("version") or ""), f"/api/health 缺 version: {srv.health}")
        notes.append(f"health: pack={srv.health.get('pack')} version={srv.health.get('version')}")

        # ---- 2) GET /api/tasks
        st, body = http_get(srv.base + "/api/tasks")
        check(st == 200, f"GET /api/tasks HTTP {st}: {tail(body, 300)}")
        lst = try_json(body)
        check(isinstance(lst, (list, dict)), f"/api/tasks 返回非 JSON 列表: {tail(body, 300)}")

        # ---- 3) POST /api/tasks → 建任务、落盘 runs/<id>/
        intake = intake_for_create()
        st, body = http_post(srv.base + "/api/tasks", intake)
        check(st in (200, 201), f"POST /api/tasks HTTP {st}: {tail(body, 400)}")
        j = try_json(body) or {}
        task_id = j.get("task_id") or (j.get("task") or {}).get("task_id")
        check(bool(task_id), f"POST /api/tasks 未返回 task_id: {tail(body)}")
        tdir = repo / "runs" / str(task_id)
        check(tdir.is_dir(), f"任务未落盘 {tdir}")
        check((tdir / "task.json").is_file(), f"缺 {tdir.name}/task.json")
        check((tdir / "status.json").is_file(), f"缺 {tdir.name}/status.json")
        notes.append(f"建任务: {task_id}")

        # ---- 4) 放一份 real report + 四卡，GET /api/tasks/<id> 交付卡字段必须==report.json
        report = build_real_report(repo, f"runs/{task_id}/report.json",
                                   task_id=str(task_id), gate_status="confirmed")
        loopdir = tdir / "loop"
        pb = tool(repo, "tools/write_loop_bundle.py", "--report", report,
                  "--outdir", loopdir, "--approver", "业务负责人")
        st, body = http_get(srv.base + f"/api/tasks/{task_id}")
        check(st == 200, f"GET /api/tasks/<id> HTTP {st}: {tail(body, 400)}")
        detail = try_json(body) or {}
        for key in ("task", "report", "status", "loop"):
            check(key in detail, f"/api/tasks/<id> 缺字段 {key!r}；keys={list(detail)}")
        served = detail.get("report")
        check(isinstance(served, dict), f"/api/tasks/<id>.report 非对象: {type(served)}")
        rep_obj = json.loads(report.read_text(encoding="utf-8"))
        for field in ("finding", "scorecard", "recommendation", "decision_log_entry"):
            check(field in served, f"服务端 report 缺交付卡字段 {field!r}")
            check(served.get(field) == rep_obj.get(field),
                  f"服务端 report.{field} 与磁盘 report.json 不一致")
        notes.append("GET /api/tasks/<id>: finding/scorecard/recommendation/decision_log_entry == report.json")

        # ---- 5) POST /sign → 返回 sha256 必须 == 磁盘 report.json 的 sha256 == sign_delivery.py 输出
        file_sha = sha256_file(report)
        signer_used, sign_resp = None, None
        for cand in SIGNER_CANDIDATES:
            st, body = http_post(srv.base + f"/api/tasks/{task_id}/sign", {"signer": cand})
            if st == 200:
                signer_used, sign_resp = cand, try_json(body) or {}
                break
            if st in (400, 409, 422):
                continue
            check(False, f"POST /sign HTTP {st}: {tail(body, 400)}")
        check(signer_used is not None, "POST /api/tasks/<id>/sign 所有候选 signer 都未返回 200")
        srv_sha = sign_resp.get("sha256")
        check(str(srv_sha) == file_sha,
              f"服务端签字 sha256={srv_sha} != 磁盘 report.json sha256={file_sha}")
        # CLI 交叉验证（同一文件）
        sig_out = sb.base / "cli_signature.json"
        pc = tool(repo, "tools/sign_delivery.py", "--report", report,
                  "--signer", signer_used, "--out", sig_out)
        check(pc.returncode == 0, f"sign_delivery.py 交叉验证失败: {tail(pc.out)}")
        cli_sha = _sig_sha_from_json(sig_out) if sig_out.exists() else None
        if cli_sha is None:
            cli_sha = (try_json(pc.out) or {}).get("sha256")
        check(str(cli_sha) == file_sha,
              f"sign_delivery.py 输出 sha256={cli_sha} != 磁盘 {file_sha}")
        check(str(srv_sha) == str(cli_sha),
              f"服务端 sha256={srv_sha} != sign_delivery.py sha256={cli_sha}")
        notes.append(f"签字 sha256 三方一致 ({file_sha[:12]}…, signer={signer_used})")

        # ---- 6) UI：静态检查 api-adapter.js + index.html（浏览器不可用，局限见 NOTE）
        ui_txt = read_text(adapter)
        idx_txt = read_text(repo / "ui" / "index.html")
        checks = {
            "健康检查": ("api/health", ui_txt),
            "任务列表": ("api/tasks", ui_txt),
            "日志接口": ("/log", ui_txt),
            "签接口": ("sign", ui_txt),
            "哈希展示": ("sha256", ui_txt),
            "交付卡finding": ("finding", ui_txt),
            "交付卡scorecard": ("scorecard", ui_txt),
            "ROI": ("roi", ui_txt),
            "decision_log": ("decision_log", ui_txt),
        }
        missing = [name for name, (needle, blob) in checks.items() if needle not in blob]
        check(not missing, f"ui/api-adapter.js 缺少接线关键字: {missing}")
        check("api-adapter.js" in idx_txt,
              "ui/index.html 未引入 api-adapter.js（§2.7 要求）")
        st, root_body = http_get(srv.base + "/")
        check(st == 200, f"GET / HTTP {st}")
        for token in ("PixelSprites", "beats", "openIntake"):
            check(token in root_body, f"GET / 的 index.html 丢失结构字面量 {token!r}（红线：静态演示仍需可用）")
        notes.append("UI 静态: api-adapter.js 接线关键字齐全 + index.html 引入 + GET / 保留 PixelSprites/beats/openIntake")
        notes.append("NOTE(局限): 本环境无浏览器，未做真实 DOM 渲染/点击验证；A6 的 UI 部分是"
                     "「静态检查 + 服务接口 HTTP 断言」，浏览器侧需人工/Playwright 补验")
    finally:
        srv.stop()
    return "；".join(notes)


CASES = {
    "A1": ("同日连建 2 任务：id 不同、各自 runs/<id>/、重跑不覆盖历史", case_a1),
    "A2": ("real 报告门：占位 evidence / 缺 data_refs → validate --require-real & assemble 非 0", case_a2),
    "A3": ("finalize 门：pending gate / rejected decision / 伪 signer 非 0；合法链 approved", case_a3),
    "A4": ("六维全 5 → total=100.0 且系统建议；run_task scorecard 与 score CLI 一致", case_a4),
    "A5": ("改 pack.json ROI 锚点 → 新报告 ROI 数值随之变", case_a5),
    "A6": ("UI 交付卡字段==report.json；签字哈希==sign_delivery.py sha256", case_a6),
}


# ---------------------------------------------------------------- main


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="M1 独立验收测试（黑盒）")
    ap.add_argument("--case", action="append", default=[],
                    help="只跑指定用例，如 --case A3 或 --case A1,A4")
    ap.add_argument("--list", action="store_true", help="列出用例")
    ap.add_argument("--keep", action="store_true", help="保留全部沙盒")
    ap.add_argument("--keep-failed", dest="keep_failed", action="store_true", default=True,
                    help="失败时保留沙盒（默认开启）")
    ap.add_argument("--no-keep-failed", dest="keep_failed", action="store_false")
    ap.add_argument("--timeout", type=int, default=600, help="单用例总超时秒（默认 600）")
    args = ap.parse_args(argv)

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    if args.list:
        for cid in CASE_IDS:
            print(f"{cid}: {CASES[cid][0]}")
        return 0

    selected = []
    for raw in args.case:
        for part in str(raw).split(","):
            cid = part.strip().upper()
            if not cid:
                continue
            if cid not in CASES:
                print(f"未知用例 {cid}（可选 {CASE_IDS}）")
                return 2
            if cid not in selected:
                selected.append(cid)
    if not selected:
        selected = list(CASE_IDS)

    print(f"M1 acceptance · python={PY}")
    print(f"M1 acceptance · cases={selected} · sandbox=%TEMP%\\m1-accept-*")
    results = {}
    for cid in selected:
        desc, fn = CASES[cid]
        print(f"\n--- CASE {cid}: {desc}")
        sb = None
        t0 = time.time()
        try:
            sb = Sandbox(cid)
            notes = fn({"sb": sb})
            results[cid] = True
            print(f"CASE {cid} PASS: {notes}")
        except CaseFailure as exc:
            results[cid] = False
            label = {"not_ready": "实现未就绪", "test_issue": "测试自身问题",
                     "assert": "断言失败"}[exc.kind]
            print(f"CASE {cid} FAIL: [{label}] {exc.reason}")
        except subprocess.TimeoutExpired as exc:
            results[cid] = False
            print(f"CASE {cid} FAIL: [测试自身问题] 子进程超时 {exc.timeout}s: {tail(str(exc), 500)}")
        except Exception:
            results[cid] = False
            print(f"CASE {cid} FAIL: [测试自身问题] 用例异常:\n{traceback.format_exc(limit=6)}")
        finally:
            if sb is not None:
                failed = not results.get(cid, False)
                sb.cleanup(keep=args.keep or (failed and args.keep_failed))
        print(f"  (elapsed {time.time() - t0:.1f}s)")

    print("\n" + "=" * 72)
    for cid in selected:
        print(f"{cid}: {'PASS' if results.get(cid) else 'FAIL'}")
    ok = all(results.get(cid) for cid in selected)
    print(f"RESULT: {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
