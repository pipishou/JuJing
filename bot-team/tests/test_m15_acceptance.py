#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""M1.5 独立验收测试（B7 · 黑盒 · Maker-Checker 的 Checker 侧）。

规格：`D:\\work\\产品类\\个人产品\\bot-2\\audit\\M1.5-build-spec.md` §1 A7–A12。
原则：
- 只读产品代码（core/ packs/ tools/ server/ ui/ orchestration/ 一律不写）。
- 每个用例在 %TEMP% 沙盒里复制仓库并起独立 server（随机端口），绝不写仓库 runs/。
- 纯 stdlib + subprocess；python 一律 -B；子进程 venv 绝对路径（可用 M15_PY 覆盖）。
- 逐用例打印 `CASE A<n> PASS/FAIL: <原因>`，结束 `RESULT: PASS|FAIL`，退出码 0/1。
- 支持 --case A7、--case A7,A10、--list、--keep。

判据（spec §1）：
 A7  GET /api/team 返回 employees/crews/shelf/pack；员工字段/精灵 key 对应；pack.crew_id 在 crews 中
 A8  POST /api/tasks[/<id>/data] 上传 T/T30 → runs/<id>/data/、data_ready；非法 schema/超限/穿越全拒
 A9  POST /api/tasks/<id>/gate：无 gate→finalize 409；reject→409；非法 confirmer→4xx；伪造 gate.json→拒；
     confirm→finalize 200 且 approval.by==confirmer
 A10 GET /events?since=n 事件流（s1–s5 阶段齐、s1‖s2 并行、工具级事件不丢）；SSE 断线 since 续传不重不丢；
     非法 since → 400（Lead m00628 裁决 fail-closed，覆盖 spec 原文「回退 0」）
 A11 同日两任务各自 runs/<id>、报告不覆盖、/api/tasks 同见；UI adapter 暴露 selectTask/tasks/taskId
 A12 fixture 报告 is_demo=true 且 UI 标注「演示数据」

局限（写进用例 NOTE）：
- 无浏览器：UI 只做静态代码检查，不做 DOM 渲染/点击验证。
- 不执行 DSH workflow（M2 才接）：A10 只验收 fixture runner 链路。
- 契约收紧（Lead m00628 裁决，非放宽）：非法 since 一律 400；阶段事件必须 s1–s5 齐（gate 可选）。
"""
import argparse
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
FIX = Path(__file__).resolve().parent / "fixtures" / "m15"
CASE_IDS = ["A7", "A8", "A9", "A10", "A11", "A12"]

OWNER = "老板"
ALLOWED = "老板,boss"
BOGUS_SIGNER = "伪造者-9000"

# venv python（spec 要求）；也允许 M15_PY 覆盖，缺省退回当前解释器
_PY_CANDIDATES = [
    os.environ.get("M15_PY", ""),
    r"D:\work\产品类\个人产品\bot-2\venv\Scripts\python.exe",
    sys.executable,
]


def _pick_py() -> str:
    for cand in _PY_CANDIDATES:
        if cand and Path(cand).exists():
            return cand
    return sys.executable


PY = _pick_py()

IGNORE_COPY = shutil.ignore_patterns(
    "runs", "__pycache__", "*.pyc", "*.png", ".git", ".pytest_cache",
)

SPRITE_KEYS = {"host", "analyst", "attr", "biz", "data", "quality", "resident", "craft"}
SHELF_STATES = {"coming", "not_ready", "craft", "resident", "retired", "active"}
TERMINAL_STATES = {"assembled", "awaiting_sign", "signed", "failed"}


# ---------------------------------------------------------------- 基础设施


class CaseFailure(Exception):
    """kind: not_ready（实现未交付） | test_issue（测试自身） | assert（断言失败）"""

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


def raise_problems(label: str, problems: list):
    """子项聚合：全部是「未实现」时整例报 not_ready，否则报 assert（不掩盖真断言失败）。"""
    if not problems:
        return
    if all("[not_ready]" in p for p in problems):
        raise CaseFailure("not_ready", f"{label} " + "；".join(problems))
    raise CaseFailure("assert", f"{label} " + "；".join(problems))


def tail(text, n: int = 1500) -> str:
    text = str(text or "").strip()
    return text if len(text) <= n else "…" + text[-n:]


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:  # pragma: no cover
        return f"<read error: {exc}>"


def dump_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def try_json(text):
    """从混合输出里抠出第一个 JSON 值（对象或数组均可）。"""
    if isinstance(text, (dict, list)):
        return text
    dec = json.JSONDecoder()
    text = str(text or "")
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
    # M1.5 §2.5：confirmer/signer 必须走 --allow-signers，不依赖 env fallback
    env.pop("BOT_SIGNERS", None)
    env.pop("BOT_SIGNER_ALLOWLIST", None)
    if extra:
        env.update({k: str(v) for k, v in extra.items()})
    return env


def run(args, cwd: Path, timeout: int = 300, env_extra=None):
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
        self.base = Path(tempfile.mkdtemp(prefix=f"m15-accept-{tag}-"))
        self.repo = self.base / "repo"
        shutil.copytree(ROOT, self.repo, ignore=IGNORE_COPY)
        runs = self.repo / "runs"
        runs.mkdir(exist_ok=True)
        # runner=fixture 依赖仓库红线演示数据（沙盒不复制 runs/ 以避免污染/串任务，
        # 这里只补 fixture 运行所需的单文件，绝不复制仓库历史任务目录）
        for name in ("demo-result-2026-10-19.json",):
            src = ROOT / "runs" / name
            if src.is_file():
                shutil.copy2(src, runs / name)

    def cleanup(self, keep: bool):
        if keep:
            print(f"  SANDBOX: {self.base}")
        else:
            shutil.rmtree(self.base, ignore_errors=True)


# ---------------------------------------------------------------- HTTP


def http_get(url: str, timeout: float = 8.0):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:  # 连接拒绝/超时等
        return None, str(e)


def http_post(url: str, obj, timeout: float = 60.0):
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


def get_json(url: str, timeout: float = 8.0):
    st, body = http_get(url, timeout=timeout)
    return st, try_json(body), body


def post_json(url: str, obj, timeout: float = 60.0):
    st, body = http_post(url, obj, timeout=timeout)
    return st, try_json(body), body


class LocalServer:
    """起 server/app.py（--host/--port/--root [--owner/--allow-signers]），轮询 /api/health。"""

    def __init__(self, repo: Path):
        self.repo = repo
        self.proc = None
        self.base = ""
        self.log_path = repo.parent / "server.log"
        self._fh = None
        self.identity_flags = False

    def _spawn(self, port: int, with_identity: bool):
        app = self.repo / "server" / "app.py"
        args = [str(PY), "-B", str(app), "--host", "127.0.0.1", "--port", str(port),
                "--root", str(self.repo)]
        if with_identity:
            args += ["--owner", OWNER, "--allow-signers", ALLOWED]
        self._fh = open(self.log_path, "wb")
        self.proc = subprocess.Popen(
            args, cwd=str(self.repo), stdout=self._fh, stderr=subprocess.STDOUT, env=_env(),
        )

    def _wait_health(self, wait_s: float):
        deadline = time.time() + wait_s
        last = ""
        while time.time() < deadline:
            rc = self.proc.poll()
            if rc is not None:
                return rc, last
            st, body = http_get(self.base + "/api/health", timeout=2)
            if st == 200:
                return 0, body
            last = f"{st} {tail(body, 300)}"
            time.sleep(0.3)
        return None, last

    def start(self, wait_s: float = 30.0):
        app = self.repo / "server" / "app.py"
        if not app.exists():
            not_ready("缺少 server/app.py（B5 未交付）")
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        self.base = f"http://127.0.0.1:{port}"

        # 优先带 --owner/--allow-signers（M1.5 §2.5）；旧 server 不识别时回退基础参数
        self._spawn(port, True)
        rc, last = self._wait_health(wait_s)
        if rc != 0 and rc is not None:
            log = read_text(self.log_path)
            if "unrecognized arguments" in log or "--owner" in log and "usage:" in log:
                self._close_log()
                self._spawn(port, False)
                rc, last = self._wait_health(wait_s)
            else:
                not_ready(f"server/app.py 启动即退出 rc={rc}: {tail(log)}")
        if rc is None:
            not_ready(f"/api/health {wait_s:.0f}s 内未就绪: {last}")
        self.identity_flags = True
        return self

    def _close_log(self):
        if self._fh is not None:
            try:
                self._fh.close()
            except Exception:
                pass
            self._fh = None

    def stop(self):
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except Exception:
                self.proc.kill()
        self._close_log()

    # -- 便捷 API（服务层契约见 spec §2.5） --
    def api(self, path: str) -> str:
        return self.base + path

    def create_task(self, body: dict):
        return post_json(self.api("/api/tasks"), body)

    def task(self, tid: str):
        return get_json(self.api(f"/api/tasks/{tid}"))


# ---------------------------------------------------------------- 夹具/任务辅助


def fixture(name: str) -> Path:
    p = FIX / name
    if not p.exists():
        test_issue(f"缺少夹具 {p}")
    return p


def intake() -> dict:
    obj = json.loads(fixture("intake.json").read_text(encoding="utf-8"))
    obj.pop("task_id", None)
    return obj


def data_text(name: str = "material_refill_T.json") -> str:
    return read_text(fixture(name))


def ensure_real_data(repo: Path) -> list:
    """把 demo 数据复制到非 demo/ 路径：real 门要求 data_refs 不得指向 demo/。"""
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


def task_id_of(obj):
    if not isinstance(obj, dict):
        return None
    for k in ("task_id", "id"):
        v = obj.get(k)
        if isinstance(v, str) and v:
            return v
    for k in ("task", "data"):
        v = obj.get(k)
        if isinstance(v, dict):
            for kk in ("task_id", "id"):
                if isinstance(v.get(kk), str) and v.get(kk):
                    return v[kk]
    return None


def task_state(obj):
    if not isinstance(obj, dict):
        return None
    st = obj.get("status")
    if isinstance(st, dict) and isinstance(st.get("state"), str):
        return st["state"]
    if isinstance(obj.get("state"), str):
        return obj["state"]
    for k in ("task", "data"):
        v = obj.get(k)
        if isinstance(v, dict) and isinstance(v.get("state"), str):
            return v["state"]
    return None


def find_data_ready(obj, path=""):
    """在任意响应/文件 JSON 里找到 data_ready=true 的位置。"""
    if isinstance(obj, dict):
        if obj.get("data_ready") is True:
            return path or "<root>"
        for k, v in obj.items():
            hit = find_data_ready(v, f"{path}.{k}" if path else k)
            if hit:
                return hit
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            hit = find_data_ready(v, f"{path}[{i}]")
            if hit:
                return hit
    return None


def file_names(entries):
    """task.data_files 可能是 [str] 或 [{name,...}]。"""
    names = []
    if isinstance(entries, list):
        for e in entries:
            if isinstance(e, str):
                names.append(Path(e).name)
            elif isinstance(e, dict):
                name = e.get("name") or e.get("path") or e.get("file")
                if isinstance(name, str):
                    names.append(Path(name).name)
    return names


def create_plain_task(srv: LocalServer):
    st, obj, body = srv.create_task(intake())
    if st is None:
        not_ready(f"POST /api/tasks 无响应: {tail(body, 400)}")
    if st == 404:
        not_ready("POST /api/tasks 返回 404（服务层未实现）")
    check(st in (200, 201), f"POST /api/tasks 失败 HTTP {st}: {tail(body, 600)}")
    tid = task_id_of(obj)
    check(bool(tid), f"POST /api/tasks 未返回 task_id: {tail(body, 600)}")
    return tid


def wait_state(srv: LocalServer, tid: str, timeout: float = 240.0):
    """轮询 GET /api/tasks/<id> 直到终态。"""
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        st, obj, body = srv.task(tid)
        last = (st, obj, body)
        state = task_state(obj)
        if state in TERMINAL_STATES:
            return state, obj, body
        time.sleep(0.5)
    st, obj, body = last or (None, None, "")
    return task_state(obj), obj, body


def upload_data(srv: LocalServer, tid: str, files, shapes=("files", "data_files")):
    """POST /api/tasks/<id>/data：spec 未写死字段名，按候选形状黑盒探测，返回 (st,obj,body,shape)。"""
    last = (None, None, "", None)
    for shape in shapes:
        st, obj, body = post_json(srv.api(f"/api/tasks/{tid}/data"), {shape: files}, timeout=120)
        last = (st, obj, body, shape)
        if st is not None and 200 <= st < 300:
            return last
    return last


def extract_files_resp(obj):
    """POST /data 的响应：{ok,files:[{name,sha256,size}],data_ready}。"""
    if not isinstance(obj, dict):
        return None, None
    files = obj.get("files")
    if files is None and isinstance(obj.get("data"), dict):
        files = obj["data"].get("files")
    if isinstance(files, list):
        return files, obj.get("data_ready", (obj.get("data") or {}).get("data_ready"))
    return None, None


def unwrap_events(obj):
    if isinstance(obj, list):
        return obj
    if isinstance(obj, dict):
        for k in ("events", "items", "data"):
            v = obj.get(k)
            if isinstance(v, list):
                return v
            if isinstance(v, dict) and isinstance(v.get("events"), list):
                return v["events"]
    return []


def seqs(events):
    out = []
    for e in events:
        if isinstance(e, dict) and isinstance(e.get("seq"), int):
            out.append(e["seq"])
    return out


def read_sse(url: str, max_events: int = 20, timeout: float = 20.0):
    """读 SSE：返回 {status, content_type, ids[], events[], raw_lines[]}。"""
    out = {"status": None, "content_type": "", "ids": [], "events": [], "raw": []}
    try:
        resp = urllib.request.urlopen(url, timeout=timeout)
    except urllib.error.HTTPError as e:
        out["status"] = e.code
        out["content_type"] = e.headers.get("Content-Type", "")
        return out
    except Exception as e:
        out["status"] = None
        out["raw"].append(str(e))
        return out
    out["status"] = getattr(resp, "status", 200)
    out["content_type"] = resp.headers.get("Content-Type", "")
    deadline = time.time() + timeout
    try:
        while len(out["events"]) < max_events and time.time() < deadline:
            line = resp.readline()
            if not line:
                break
            if isinstance(line, bytes):
                line = line.decode("utf-8", "replace")
            line = line.rstrip("\r\n")
            if not line:
                continue
            if len(out["raw"]) < 400:
                out["raw"].append(line)
            if line.startswith(":"):
                continue
            if line.startswith("id:"):
                out["ids"].append(line[3:].strip())
            elif line.startswith("data:"):
                payload = line[5:].strip()
                try:
                    ev = json.loads(payload)
                except Exception:
                    ev = {"_raw": payload}
                out["events"].append(ev)
    except Exception as e:
        out["raw"].append(f"<read error: {e}>")
    finally:
        try:
            resp.close()
        except Exception:
            pass
    return out


def find_gate_audit(report: dict):
    """报告内若实现选择写 audit/history/gate 列表，返回其路径（非唯一审计载体）。"""
    hits = []

    def walk(x, path=""):
        if isinstance(x, dict):
            for k, v in x.items():
                kl = str(k).lower()
                p = f"{path}.{k}" if path else str(k)
                if any(t in kl for t in ("audit", "history", "gate")):
                    if isinstance(v, list) and any(
                        isinstance(i, dict) and any(str(val) == OWNER for val in i.values())
                        for i in v
                    ):
                        hits.append(p)
                walk(v, p)
        elif isinstance(x, list):
            for i, v in enumerate(x):
                walk(v, f"{path}[{i}]")

    walk(report)
    return hits


def gate_audit_evidence(repo: Path, tid: str, report: Path):
    """A9 审计取证：§2.3 定义审计=gate.json history(append-only)+ append_event(kind=status,phase=gate)，
    外加报告 security_gate.confirmed_by。返回 (evidence 列表, 每类是否存在)。"""
    tdir = repo / "runs" / tid
    ev = []
    rep = try_json(read_text(report)) or {}
    sec = rep.get("security_gate") or {}
    rep_confirm = (sec.get("status") == "confirmed" and sec.get("confirmed_by") == OWNER)
    if rep_confirm:
        ev.append(f"report.security_gate.confirmed_by={OWNER}")
    hits = find_gate_audit(rep)
    if hits:
        ev.append("report 审计列表:" + ",".join(hits))

    gd = try_json(read_text(tdir / "gate.json")) or {}
    hist = gd.get("history") if isinstance(gd.get("history"), list) else []
    hist_ok = any(
        isinstance(h, dict) and (
            h.get("action") == "confirm"
            or h.get("confirmed_by") == OWNER
            or OWNER in json.dumps(h, ensure_ascii=False)
        )
        for h in hist
    )
    if hist_ok:
        ev.append(f"gate.json.history 含 confirm/{OWNER}")

    log_ok = False
    for line in read_text(tdir / "run.log").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            d = json.loads(line)
        except Exception:
            continue
        if str(d.get("phase")) != "gate":
            continue
        meta = d.get("meta") or {}
        if (d.get("kind") == "status"
                and (meta.get("confirmed_by") == OWNER or OWNER in str(d.get("line", "")))):
            log_ok = True
            break
    if log_ok:
        ev.append(f"run.log 含 phase=gate/status 事件（{OWNER}）")
    return ev, {"report": rep_confirm, "history": hist_ok, "event": log_ok}


# ---------------------------------------------------------------- A7


def case_a7(ctx: dict):
    sb: Sandbox = ctx["sb"]
    srv: LocalServer = ctx["srv"]
    repo = sb.repo
    notes, problems = [], []

    st, obj, body = get_json(srv.api("/api/team"))
    if st == 404:
        not_ready("GET /api/team 返回 404（§2.1 core.team + §2.5 路由未实现）")
    if st == 500:
        err = json.dumps(obj, ensure_ascii=False) if obj else body
        if any(t in err for t in ("未实现", "not implemented", "not_implemented")):
            not_ready(f"GET /api/team 500（实现未就绪）: {tail(err, 500)}")
        raise CaseFailure("assert", f"GET /api/team 500（team 数据加载失败，spec 要求 500+明确错误）: {tail(err, 600)}")
    check(st == 200, f"GET /api/team HTTP {st}: {tail(body, 600)}")
    check(isinstance(obj, dict), f"/api/team 非 JSON 对象: {tail(body, 400)}")

    def sub(name, fn):
        try:
            fn()
            notes.append(name)
        except CaseFailure as exc:
            problems.append(f"{name}: [{exc.kind}] {exc.reason}")

    team = obj

    def team_shape():
        check(team.get("ok") is not False, f"/api/team ok=false: {tail(json.dumps(team, ensure_ascii=False), 400)}")
        team_id = team.get("team_id") or (team.get("team") or {}).get("team_id")
        check(isinstance(team_id, str) and team_id, "/api/team 缺少 team_id")
        for k in ("employees", "crews", "shelf", "roster", "pack"):
            check(k in team, f"/api/team 缺少字段 {k}（spec §2.1）")
        check(isinstance(team.get("employees"), list), "employees 不是数组")
        check(isinstance(team.get("crews"), list) and team["crews"], "crews 为空/非数组")
        check(isinstance(team.get("pack"), dict) and team["pack"], "pack 为空/非对象")
        check(isinstance(team.get("shelf"), dict), "shelf 不是对象")

    sub("team 顶层字段", team_shape)

    def employees_fields():
        emps = team.get("employees") or []
        check(len(emps) >= 7, f"employees 数量 {len(emps)} < 7（spec §1 A7 要求 7 员工）")
        need = ("id", "name", "layer", "category", "state", "skills", "sample")
        ids = []
        for i, e in enumerate(emps):
            check(isinstance(e, dict), f"employees[{i}] 不是对象")
            for k in need:
                check(k in e and e[k] not in (None, "", [], {}),
                      f"employees[{i}] 缺字段 {k}: {json.dumps(e, ensure_ascii=False)[:300]}")
            check(isinstance(e.get("skills"), list) and e["skills"],
                  f"employees[{i}].skills 不是非空数组")
            ids.append(e["id"])
        check(len(ids) == len(set(ids)), f"employees.id 有重复: {ids}")
        if "warnings" in team:
            check(isinstance(team.get("warnings"), list), "warnings 不是数组")

    sub("employees 字段齐全", employees_fields)

    def sprite_mapping():
        emps = team.get("employees") or []
        # 精灵 key 的唯一事实源 = ui/sprites.js 的 PAL 调色板键（黑盒解析，不写死实现侧映射表）
        sprites_js = read_text(repo / "ui" / "sprites.js")
        check(sprites_js and not sprites_js.startswith("<read error"),
              f"读不到 ui/sprites.js: {tail(sprites_js, 200)}")
        pal = re.search(r"const\s+PAL\s*=\s*\{(.*?)\n\s*\};", sprites_js, re.S)
        check(bool(pal), "ui/sprites.js 中找不到 PAL 调色板对象（无法交叉校验精灵 key）")
        palette_keys = set(re.findall(r"^\s{4}(\w+):\s*\{", pal.group(1), re.M))
        check(len(palette_keys) >= 7,
              f"ui/sprites.js PAL 仅 {len(palette_keys)} 个角色 key {sorted(palette_keys)}，不足 7 员工配色")
        sprites = []
        for e in emps:
            sk = e.get("sprite_key")
            check(isinstance(sk, str) and sk, f"员工 {e.get('id')} 缺 sprite_key")
            check(sk in palette_keys,
                  f"员工 {e.get('id')} sprite_key={sk!r} 不在 ui/sprites.js PAL 角色 {sorted(palette_keys)}")
            sprites.append(sk)
        check(len(set(sprites)) >= 7,
              f"{len(emps)} 员工 sprite_key 仅 {len(set(sprites))} 种（{sorted(set(sprites))}），疑似映射错位/全部 fallback")
        notes.append(f"员工 sprite_key 全部命中 PAL（{sorted(set(sprites))}）")

    sub("员工 sprite_key 一一对应", sprite_mapping)

    def crews_consistency():
        crews = team.get("crews") or []
        crew_ids = [c.get("id") for c in crews if isinstance(c, dict) and c.get("id")]
        check(len(crew_ids) == len(crews), f"crews 有缺 id 的条目: {crews[:2]}")
        pack = team.get("pack") or {}
        crew_id = pack.get("crew_id") or (team.get("roster") or {}).get("crew_id")
        check(isinstance(crew_id, str) and crew_id,
              f"pack.crew_id 缺失（spec A7：否则 500+明确错误）: pack={json.dumps(pack, ensure_ascii=False)[:300]}")
        check(crew_id in crew_ids,
              f"pack.crew_id={crew_id!r} 不在 crews {crew_ids}（spec 要求 500+明确错误）")
        emp_ids = {e.get("id") for e in (team.get("employees") or [])}
        missing = []
        for c in crews:
            for step in (c.get("steps") or []):
                a = step.get("agent") if isinstance(step, dict) else None
                if a not in emp_ids:
                    missing.append((c.get("id"), a))
        if missing:
            invalid = team.get("invalid")
            check(isinstance(invalid, list) and invalid,
                  f"crews.steps.agent 不在 employees.id: {missing[:5]} 且无 invalid 列表（spec §2.1 要求 warning+invalid）")

    sub("crews/pack 一致性", crews_consistency)

    def shelf_states():
        shelf = team.get("shelf") or {}
        states = shelf.get("states")
        if isinstance(states, dict):
            got = set(states.keys())
        elif isinstance(shelf.get("states"), list):
            got = set(shelf["states"])
        else:
            got = set()
        check(SHELF_STATES.issubset(got),
              f"shelf.states 缺状态: {sorted(SHELF_STATES - got)}（spec §2.6 六态）")
        check(isinstance(shelf.get("badges"), dict) or "badges" in shelf, "shelf 缺 badges")
        check("categories" in shelf, "shelf 缺 categories")

    sub("shelf 六态", shelf_states)

    if problems:
        raise_problems("A7", problems)
    return "；".join(notes)


# ---------------------------------------------------------------- A8


def case_a8(ctx: dict):
    sb: Sandbox = ctx["sb"]
    srv: LocalServer = ctx["srv"]
    repo = sb.repo
    notes, problems = [], []
    t_text = data_text("material_refill_T.json")
    t30_text = data_text("material_refill_T30.json")

    # --- A8a：POST /api/tasks 携带 data_files（spec §2.5 表：扩展建任务） ---
    tid = None
    try:
        body = intake()
        body["data_files"] = [
            {"name": "material_refill_T.json", "content": t_text},
            {"name": "material_refill_T30.json", "content": t30_text},
        ]
        st, obj, raw = srv.create_task(body)
        check(st in (200, 201), f"POST /api/tasks + data_files HTTP {st}: {tail(raw, 800)}")
        tid = task_id_of(obj)
        check(bool(tid), f"POST /api/tasks + data_files 未返回 task_id: {tail(raw, 500)}")
        tdir = repo / "runs" / tid
        check(tdir.is_dir(), f"runs/{tid}/ 未创建")
        check((tdir / "data").is_dir(), f"runs/{tid}/data/ 未创建")
        for name, text in (("material_refill_T.json", t_text), ("material_refill_T30.json", t30_text)):
            p = tdir / "data" / name
            check(p.is_file(), f"runs/{tid}/data/{name} 未落盘")
            check(sha256_file(p) == sha256_bytes(text.encode("utf-8")),
                  f"runs/{tid}/data/{name} 内容与上传不一致")
        task_json = try_json(read_text(tdir / "task.json"))
        check(isinstance(task_json, dict), f"runs/{tid}/task.json 不可读")
        names = file_names(task_json.get("data_files"))
        check("material_refill_T.json" in names and "material_refill_T30.json" in names,
              f"task.json.data_files 未更新: {task_json.get('data_files')!r}")
        st2, obj2, raw2 = srv.task(tid)
        check(st2 == 200, f"GET /api/tasks/{tid} HTTP {st2}")
        loc = find_data_ready(obj2)
        check(bool(loc), f"status.task.data_ready 未置 true: {tail(json.dumps(obj2, ensure_ascii=False), 800)}")
        notes.append(f"嵌入 data_files: task={tid}，data_ready@{loc}，文件 sha 一致")
    except CaseFailure as exc:
        problems.append(f"A8a 建任务带数据: [{exc.kind}] {exc.reason}")

    # --- A8b：POST /api/tasks/<id>/data 追加 ---
    if tid is None:
        st, obj, raw = srv.create_task(intake())
        check(st in (200, 201), f"POST /api/tasks HTTP {st}: {tail(raw, 600)}")
        tid = task_id_of(obj)
        check(bool(tid), "POST /api/tasks 未返回 task_id")
    try:
        files = [
            {"name": "material_refill_T.json", "content": t_text},
            {"name": "material_refill_T30.json", "content": t30_text},
        ]
        st, obj, raw, shape = upload_data(srv, tid, files)
        if st == 404:
            not_ready(f"POST /api/tasks/<id>/data 返回 404（§2.5 未实现）: {tail(raw, 500)}")
        if st is None or not (200 <= st < 300):
            # 退一步：新建任务为空时 /data 可能要求已有 task 文件；仍应 4xx 而非 500
            check(st is not None and st < 500,
                  f"POST /data HTTP {st}（期望 2xx/4xx，不要 5xx）: {tail(raw, 700)}")
            raise CaseFailure("assert",
                              f"POST /data 各候选字段形状均被拒（HTTP {st}，shape={shape}）: {tail(raw, 600)}"
                              "（契约歧义：spec 只写『追加数据文件』未写字段名）")
        got, ready = extract_files_resp(obj)
        check(got is not None, f"POST /data 响应缺 files[]: {tail(raw, 600)}")
        got_map = {Path(f.get("name", f.get("path", ""))).name: f for f in got if isinstance(f, dict)}
        for name, text in (("material_refill_T.json", t_text), ("material_refill_T30.json", t30_text)):
            check(name in got_map, f"POST /data 响应 files 缺 {name}: {tail(raw, 500)}")
            check(got_map[name].get("sha256") == sha256_bytes(text.encode("utf-8")),
                  f"POST /data 返回的 {name} sha256 不等于上传内容")
            check((repo / "runs" / tid / "data" / name).is_file(), f"/data 后 runs/{tid}/data/{name} 不存在")
        check(ready is True or find_data_ready(obj), "POST /data 后 data_ready 未置 true")
        notes.append(f"/data 追加成功（字段形状 {shape}）")
    except CaseFailure as exc:
        problems.append(f"A8b /data 追加: [{exc.kind}] {exc.reason}")

    # --- A8c：非法 schema 全拒 ---
    try:
        bad_cases = {
            "空 meta 且无 records": read_text(fixture("bad_schema.json")),
            "坏 JSON": "{ this is not json ",
            "records 空数组": json.dumps({"meta": json.loads(t_text)["meta"], "records": []}, ensure_ascii=False),
            "缺 field_contract": json.dumps({"meta": {"dataset": "x", "schema_version": "1.3"},
                                             "records": [{"date": "2026-10-05"}]}, ensure_ascii=False),
        }
        for label, content in bad_cases.items():
            st, obj, raw, shape = upload_data(srv, tid, [{"name": "bad_input.json", "content": content}])
            check(st is not None and 400 <= st < 500,
                  f"非法 schema「{label}」未被 4xx 拒绝（HTTP {st}, shape={shape}）: {tail(raw, 500)}")
            check(not (repo / "runs" / tid / "data" / "bad_input.json").exists(),
                  f"非法 schema「{label}」被拒绝但仍落盘 runs/{tid}/data/bad_input.json")
        notes.append("非法 schema 4 类全 4xx")
    except CaseFailure as exc:
        problems.append(f"A8c 非法 schema: [{exc.kind}] {exc.reason}")

    # --- A8d：超限（单文件 >8MB；总 >24MB）---
    try:
        big_valid = dict(json.loads(t_text))
        big_valid["meta"] = dict(big_valid["meta"])
        big_valid["meta"]["padding_8mb"] = "x" * (8 * 1024 * 1024 + 1024)   # >8MB 单文件
        big_text = json.dumps(big_valid, ensure_ascii=False)
        check(len(big_text.encode("utf-8")) > 8 * 1024 * 1024, "测试构造错误：单文件未超 8MB")
        st, obj, raw, shape = upload_data(srv, tid, [{"name": "oversize_single.json", "content": big_text}],
                                          shapes=("files",))
        ok_single = st is not None and 400 <= st < 500
        if st is None:
            # 传输层直接断开也是拒绝，但要求 server 仍活着且未落盘
            st_h, _, _ = get_json(srv.api("/api/health"))
            ok_single = st_h == 200
        check(ok_single, f"单文件 >8MB 未被拒（HTTP {st}）: {tail(raw, 300)}")
        check(not (repo / "runs" / tid / "data" / "oversize_single.json").exists(),
              "超限单文件被拒后仍落盘")

        parts = []
        for i in range(4):   # 4 × ~7MB > 24MB 总量，单文件均 <8MB
            chunk = dict(json.loads(t_text))
            chunk["meta"] = dict(chunk["meta"])
            chunk["meta"][f"padding_{i}"] = "y" * (7 * 1024 * 1024)
            parts.append({"name": f"bulk_{i}.json", "content": json.dumps(chunk, ensure_ascii=False)})
        total = sum(len(p["content"].encode("utf-8")) for p in parts)
        check(total > 24 * 1024 * 1024, "测试构造错误：总量未超 24MB")
        st, obj, raw, shape = upload_data(srv, tid, parts, shapes=("files",))
        ok_total = st is not None and 400 <= st < 500
        if st is None:
            st_h, _, _ = get_json(srv.api("/api/health"))
            ok_total = st_h == 200
        check(ok_total, f"总上传 >24MB 未被拒（HTTP {st}）: {tail(raw, 300)}")
        bulk_files = [p for p in (repo / "runs" / tid / "data").glob("bulk_*.json")] if (repo / "runs" / tid / "data").is_dir() else []
        check(not bulk_files, f"超总量上传被拒但仍落盘: {[p.name for p in bulk_files]}")
        notes.append("超限（单 8MB+/总 24MB+）均拒且未落盘")
    except CaseFailure as exc:
        problems.append(f"A8d 超限: [{exc.kind}] {exc.reason}")
    except subprocess.TimeoutExpired as exc:
        problems.append(f"A8d 超限: [测试自身问题] 超时 {exc}")

    # --- A8e：文件名穿越（../、..\\、绝对路径、盘符、NUL 等）---
    probe_win = Path(r"C:\Windows\Temp\m15_evil_probe.json")
    win_preexisting = probe_win.exists()
    try:
        names = json.loads(read_text(fixture("traversal_names.json")))["names"]
        good_t = t_text
        for name in names:
            st, obj, raw, shape = upload_data(srv, tid, [{"name": name, "content": good_t}], shapes=("files",))
            # 4xx 是唯一合格结果；NUL 可能在 JSON/HTTP 层被拒得更早，也必须是拒绝
            check(st is not None and 400 <= st < 500,
                  f"穿越文件名 {name!r} 未被 4xx 拒绝（HTTP {st}）: {tail(raw, 400)}")
        # 越权落盘检查：沙盒内任何 evil.json 都说明穿越成功
        evil = [str(p) for p in sb.base.rglob("evil.json")]
        check(not evil, f"穿越文件名导致越权落盘: {evil[:5]}")
        if (not win_preexisting) and probe_win.exists():
            try:
                probe_win.unlink()
            except Exception:
                pass
            check(False, f"穿越文件名导致写到沙盒外: {probe_win}（已尝试删除）")
        # 合法单层子目录名应被允许（safe_relative 允许单层子目录）——只在 /data 可判定时验证
        notes.append(f"穿越文件名 {len(names)} 类全 4xx、无越权落盘")
    except CaseFailure as exc:
        problems.append(f"A8e 穿越: [{exc.kind}] {exc.reason}")

    if problems:
        raise_problems("A8", problems)
    return "；".join(notes)


# ---------------------------------------------------------------- A9


def build_irreversible_report(repo: Path, tid: str) -> Path:
    """用不可逆 workflow-result 造 real 报告：assemble --real → runs/<tid>/report.json。"""
    wf = fixture("workflow_result_irreversible.json")
    work = repo / "_a9"
    work.mkdir(exist_ok=True)
    refs = ensure_real_data(repo)
    out = repo / "runs" / tid / "report.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    p = tool(repo, "orchestration/assemble_report.py", wf, out,
             "--real", "--task-id", tid,
             "--context", "B产线补料率7日 5.1%→7.2%，不可逆换模建议待批",
             "--data-refs", ",".join(refs),
             "--expected", "T+7 补料率回落至 ≤5.40%")
    if p.returncode != 0:
        low = p.out.lower()
        if "unrecognized arguments" in low or "usage:" in low:
            test_issue(f"assemble 调用参数与当前 CLI 契约不符（B4 可能改 flags）: {tail(p.out, 800)}")
        raise CaseFailure("assert", f"assemble --real 不能产出不可逆 real 报告（rc={p.returncode}）: {tail(p.out, 900)}")
    rep = try_json(read_text(out))
    check(isinstance(rep, dict), "assemble 未产出合法 report.json")
    check(rep.get("is_demo") is False, f"assemble --real 报告 is_demo={rep.get('is_demo')!r}（期望 false）")
    return out


def plant_gate(repo: Path, tid: str, report: Path, confirmer: str,
               comment: str = "人工确认 securityGate") -> dict:
    """用产品 core.gate.confirm 走真实人工确认：gate.json + run.log phase=gate 事件 + 回写报告。

    M1.5 A9 要求 finalize 不得接受「报告字段自证」；合法确认夹具必须走真实 gate 事实源。
    """
    script = (
        "import json, sys\n"
        "from pathlib import Path\n"
        "sys.path.insert(0, %r)\n"
        "from core import gate\n"
        "rep_path = Path(%r)\n"
        "report = json.loads(rep_path.read_text(encoding='utf-8'))\n"
        "doc = gate.confirm(Path(%r), %r, %r, report)\n"
        "print(json.dumps(doc, ensure_ascii=False))\n"
    ) % (str(repo), str(report), str(repo / "runs" / tid), confirmer, comment)
    p = run(["-c", script], cwd=repo)
    check(p.returncode == 0, f"core.gate.confirm 失败 rc={p.returncode}: {tail(p.out, 700)}")
    lines = [ln for ln in (p.out or "").splitlines() if ln.strip().startswith("{")]
    return try_json(lines[-1]) if lines else {}


def write_loop(repo: Path, tid: str, report: Path) -> Path:
    loop = repo / "runs" / tid / "loop"
    args = ["--report", report, "--outdir", loop, "--approver", OWNER]
    p = tool(repo, "tools/write_loop_bundle.py", *args)
    if p.returncode != 0:
        args2 = ["--report", report, "--outdir", loop]
        p = tool(repo, "tools/write_loop_bundle.py", *args2)
    check(p.returncode == 0, f"write_loop_bundle 失败 rc={p.returncode}: {tail(p.out, 700)}")
    check((loop / "decision.json").is_file(), f"{loop}/decision.json 未生成")
    return loop


def sign_report(repo: Path, tid: str, report: Path) -> Path:
    sig = repo / "runs" / tid / "signature.json"
    p = tool(repo, "tools/sign_delivery.py", "--report", report, "--signer", OWNER, "--out", sig)
    if p.returncode != 0:
        # 旧 CLI 可能是位置参数写法
        p = tool(repo, "tools/sign_delivery.py", report, "--signer", OWNER, "--out", sig)
    check(p.returncode == 0, f"sign_delivery 失败 rc={p.returncode}: {tail(p.out, 700)}")
    check(sig.is_file(), f"sign_delivery 未产出 {sig}")
    return sig


def case_a9(ctx: dict):
    sb: Sandbox = ctx["sb"]
    srv: LocalServer = ctx["srv"]
    repo = sb.repo
    notes, problems = [], []

    def sub(name, fn):
        try:
            fn()
            notes.append(name)
        except CaseFailure as exc:
            problems.append(f"{name}: [{exc.kind}] {exc.reason}")

    # 前置：服务层 gate 路由 + 工具链
    st, obj, body = get_json(srv.api("/api/health"))
    check(st == 200, f"/api/health HTTP {st}")
    tid = create_plain_task(srv)
    report = build_irreversible_report(repo, tid)
    rep = try_json(read_text(report))
    gate = rep.get("security_gate") or {}
    requested = rep.get("security_gate_requested") is True or gate.get("status") == "pending_user_confirmation"
    check(requested, "测试夹具问题：assemble 报告未标记 securityGate（A9 需要不可逆报告）")
    loop = write_loop(repo, tid, report)
    signature = sign_report(repo, tid, report)

    def finalize():
        return post_json(srv.api(f"/api/tasks/{tid}/finalize"), {})

    def decision_status():
        d = try_json(read_text(loop / "decision.json"))
        return (d or {}).get("status")

    # --- 1) 无 gate 的不可逆报告：finalize 必 409 ---
    def no_gate():
        st, obj, raw = finalize()
        if st == 404:
            not_ready(f"POST /api/tasks/<id>/finalize 返回 404（路由缺失）: {tail(raw, 400)}")
        check(st == 409, f"无 gate 的不可逆报告 finalize 未 409（HTTP {st}）: {tail(raw, 600)}")
        check(decision_status() != "approved", "无 gate 被拒但 decision.status 已改成 approved")
        txt = json.dumps(obj, ensure_ascii=False) if obj else raw
        check(any(t in txt for t in ("securityGate", "security_gate", "gate", "Gate", "不可逆", "未确认", "确认")),
              f"无 gate finalize 409 但原因不含 gate/不可逆 字样: {tail(txt, 500)}")
    sub("无 gate → finalize 409", no_gate)

    # --- 2) reject 后 finalize 必 409 ---
    def reject_then_finalize():
        st, obj, raw = post_json(srv.api(f"/api/tasks/{tid}/gate"),
                                 {"action": "reject", "confirmer": OWNER, "comment": "数据不足，先驳回"})
        check(st in (200, 201), f"POST /gate reject HTTP {st}: {tail(raw, 600)}")
        g = repo / "runs" / tid / "gate.json"
        check(g.is_file(), f"reject 后 gate.json 未生成")
        gd = try_json(read_text(g)) or {}
        check(str(gd.get("status", "")).lower() in ("rejected", "reject") or
              "reject" in json.dumps(gd, ensure_ascii=False).lower(),
              f"gate.json 未记录 rejected: {tail(json.dumps(gd, ensure_ascii=False), 500)}")
        st2, obj2, raw2 = finalize()
        check(st2 == 409, f"reject 后 finalize 未 409（HTTP {st2}）: {tail(raw2, 600)}")
        check(decision_status() != "approved", "reject 后 finalize 被拒但 decision.status 已 approved")
    sub("reject → finalize 409", reject_then_finalize)

    # --- 3) 伪造 gate.json → 拒 ---
    def forged_gate():
        g = repo / "runs" / tid / "gate.json"
        forged = json.loads(read_text(fixture("gate_forged.json")))
        dump_json(g, forged)
        st, obj, raw = finalize()
        check(st == 409, f"伪造 gate.json（confirmer={forged.get('confirmer')}）被放行（HTTP {st}）: {tail(raw, 500)}")
        # 变体：白名单名字但无服务端历史/无 report 联动
        forged2 = dict(forged)
        forged2["confirmer"] = OWNER
        forged2["confirmed_by"] = OWNER
        forged2["history"] = []
        dump_json(g, forged2)
        st2, obj2, raw2 = finalize()
        check(st2 == 409,
              f"伪造 gate.json（白名单名 {OWNER} + 空 history/无 report 联动）被放行（HTTP {st2}）: {tail(raw2, 500)}")
        check(decision_status() != "approved", "伪造 gate 被拒但 decision.status 已 approved")
    sub("伪造 gate.json → 拒", forged_gate)

    # --- 3b) 报告字段自证 confirmed、但磁盘上无 gate.json（无人工确认载体）→ finalize 必须 fail closed ---
    # spec §2.3：gate.json 是人工确认的唯一事实源（含 append-only history + phase=gate 事件）；
    # A9「无 gate 的不可逆报告 finalize 必须 409」不因 report.security_gate 被写成 confirmed 而放行。
    def report_self_claim():
        tid2 = create_plain_task(srv)
        rep_path = build_irreversible_report(repo, tid2)
        doc = try_json(read_text(rep_path)) or {}
        claim = {"status": "confirmed", "confirmed_by": OWNER,
                 "confirmed_at": "2026-10-19T16:00:00+08:00"}
        doc["security_gate"] = dict(claim)
        dl = doc.get("decision_log_entry")
        if not isinstance(dl, dict):
            dl = {}
            doc["decision_log_entry"] = dl
        dl["security_gate"] = dict(claim)
        dump_json(rep_path, doc)
        gate_file = repo / "runs" / tid2 / "gate.json"
        check(not gate_file.exists(), f"测试前置：tid2={tid2} 不应已有 gate.json")
        write_loop(repo, tid2, rep_path)
        sign_report(repo, tid2, rep_path)
        st, obj, raw = post_json(srv.api(f"/api/tasks/{tid2}/finalize"), {})
        check(st == 409,
              f"report.security_gate 自证 confirmed 但无 gate.json，finalize 未 409（HTTP {st}）——"
              f"报告字段可伪造绕过人工门: {tail(raw, 600)}")
        dec = try_json(read_text(repo / "runs" / tid2 / "loop" / "decision.json")) or {}
        check(dec.get("status") != "approved", "报告自证 confirmed 被拒但 decision.status 已 approved")
    sub("报告自证 confirmed 无 gate.json → finalize 409", report_self_claim)

    # --- 3c) gate.json 在、status=confirmed，但 history 缺 confirm 动作（无人工确认历史）→ 拒 ---
    def gate_history_missing_confirm():
        tid3 = create_plain_task(srv)
        rep3 = build_irreversible_report(repo, tid3)
        doc = try_json(read_text(rep3)) or {}
        claim = {"status": "confirmed", "confirmed_by": OWNER,
                 "confirmed_at": "2026-10-19T16:00:00+08:00"}
        doc["security_gate"] = dict(claim)
        dl = doc.get("decision_log_entry")
        if not isinstance(dl, dict):
            dl = {}
            doc["decision_log_entry"] = dl
        dl["security_gate"] = dict(claim)
        dump_json(rep3, doc)
        dump_json(repo / "runs" / tid3 / "gate.json", {
            "task_id": tid3, "status": "confirmed", "confirmed_by": OWNER,
            "confirmed_at": "2026-10-19T16:00:00+08:00",
            "requested_at": "2026-10-19T15:00:00+08:00",
            "history": [{"action": "request", "status": "requested",
                         "requested_by": "consulting-analyst",
                         "requested_at": "2026-10-19T15:00:00+08:00"}],
        })
        write_loop(repo, tid3, rep3)
        sign_report(repo, tid3, rep3)
        st, obj, raw = post_json(srv.api(f"/api/tasks/{tid3}/finalize"), {})
        check(st == 409,
              f"gate.json status=confirmed 但 history 无 confirm 动作仍被放行（HTTP {st}）: "
              f"{tail(raw, 600)}")
        dec = try_json(read_text(repo / "runs" / tid3 / "loop" / "decision.json")) or {}
        check(dec.get("status") != "approved", "history 缺 confirm 被拒但 decision.status 已 approved")
    sub("gate.json 缺 confirm 历史 → finalize 409", gate_history_missing_confirm)

    # --- 3d) gate.json/history 合法，但 run.log 无 phase=gate 审计事件（唯一事实源断链）→ 拒 ---
    def gate_event_missing():
        tid4 = create_plain_task(srv)
        rep4 = build_irreversible_report(repo, tid4)
        plant_gate(repo, tid4, rep4, OWNER, "A9 审计事件缺失负例")
        logf = repo / "runs" / tid4 / "run.log"
        kept = []
        for line in read_text(logf).splitlines():
            try:
                d = json.loads(line)
            except Exception:
                kept.append(line)
                continue
            if str(d.get("phase")) == "gate":
                continue
            kept.append(line)
        logf.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
        write_loop(repo, tid4, rep4)
        sign_report(repo, tid4, rep4)
        st, obj, raw = post_json(srv.api(f"/api/tasks/{tid4}/finalize"), {})
        check(st == 409,
              f"gate.json 合法但 run.log 无 phase=gate 审计事件仍被放行（HTTP {st}）: "
              f"{tail(raw, 600)}")
        txt = json.dumps(obj, ensure_ascii=False) if obj else raw
        check(any(t in txt for t in ("审计", "事件", "溯源", "trace", "gate", "Gate")),
              f"缺审计事件 409 但原因未提审计/事件: {tail(txt, 500)}")
        dec = try_json(read_text(repo / "runs" / tid4 / "loop" / "decision.json")) or {}
        check(dec.get("status") != "approved", "缺审计事件被拒但 decision.status 已 approved")
    sub("run.log 缺 gate 审计事件 → finalize 409", gate_event_missing)

    # --- 4) 白名单外 confirmer → 4xx ---
    def bad_confirmer():
        for confirmer in (BOGUS_SIGNER, "外部人-9000"):
            st, obj, raw = post_json(srv.api(f"/api/tasks/{tid}/gate"),
                                     {"action": "confirm", "confirmer": confirmer, "comment": "越权确认"})
            check(st is not None and 400 <= st < 500,
                  f"白名单外 confirmer={confirmer!r} 未被 4xx 拒绝（HTTP {st}）: {tail(raw, 500)}")
            check(decision_status() != "approved", f"越权 confirmer={confirmer!r} 后 decision 被 approved")
    sub("白名单外 confirmer → 4xx", bad_confirmer)

    # --- 5) 合法 confirm → 报告/审计/gate.json → finalize 放行 ---
    def valid_confirm():
        # 先清掉伪造 gate，模拟真实流程
        g = repo / "runs" / tid / "gate.json"
        if g.exists():
            g.unlink()
        st, obj, raw = post_json(srv.api(f"/api/tasks/{tid}/gate"),
                                 {"action": "confirm", "confirmer": OWNER,
                                  "comment": "已核对停机损失与换模必要性，同意执行"})
        check(st in (200, 201), f"POST /gate confirm HTTP {st}: {tail(raw, 600)}")
        check(g.is_file(), "confirm 后 gate.json 未生成")
        gd = try_json(read_text(g)) or {}
        gd_txt = json.dumps(gd, ensure_ascii=False)
        check(OWNER in gd_txt, f"gate.json 未记录 confirmer {OWNER}: {tail(gd_txt, 500)}")
        hist = gd.get("history")
        check(isinstance(hist, list) and len(hist) >= 1,
              f"gate.json.history 不是非空数组（spec §2.3 append-only）: {tail(gd_txt, 500)}")

        rep2 = try_json(read_text(report)) or {}
        sec = rep2.get("security_gate") or {}
        check(sec.get("status") == "confirmed",
              f"confirm 后 report.security_gate.status={sec.get('status')!r}（期望 confirmed）")
        check(sec.get("confirmed_by") == OWNER,
              f"confirm 后 report.security_gate.confirmed_by={sec.get('confirmed_by')!r}（期望 {OWNER!r}）")
        check(bool(sec.get("confirmed_at") or sec.get("at")),
              f"confirm 后 report.security_gate 缺 confirmed_at: {tail(json.dumps(sec, ensure_ascii=False), 400)}")
        ev, kinds = gate_audit_evidence(repo, tid, report)
        check(kinds["report"],
              f"confirm 后报告 security_gate.confirmed_by 未写为 {OWNER!r}: {tail(json.dumps(sec, ensure_ascii=False), 400)}")
        check(kinds["history"] or kinds["event"] or find_gate_audit(rep2),
              "confirm 后未找到审计行（§2.3：gate.json.history 或 run.log phase=gate 事件，或报告 audit 列表）: "
              f"evidence={ev}")

        # confirm 改了 report 文件 → 重新签名后再 finalize（签名必须对应当前整文件）
        sign_report(repo, tid, report)
        st2, obj2, raw2 = finalize()
        if st2 == 404:
            not_ready(f"POST /finalize 返回 404（路由缺失）: {tail(raw2, 400)}")
        check(st2 == 200, f"confirm 后 finalize 未放行（HTTP {st2}）: {tail(raw2, 700)}")
        dec = try_json(read_text(loop / "decision.json")) or {}
        check(dec.get("status") == "approved",
              f"finalize 放行但 decision.status={dec.get('status')!r}（期望 approved）")
        by = ((dec.get("approval") or {}).get("by"))
        check(by == OWNER, f"decision.approval.by={by!r}（期望 {OWNER!r}）")
        st3, obj3, raw3 = srv.task(tid)
        state = task_state(obj3)
        check(state == "signed", f"finalize 后 status.state={state!r}（期望 signed）")
        notes.append(f"confirm 后 finalize approved，approval.by={by}")
    sub("合法 confirm → finalize 200/approved", valid_confirm)

    if problems:
        raise_problems("A9", problems)
    return "；".join(notes)


# ---------------------------------------------------------------- A10


def run_fixture(srv: LocalServer, repo: Path, tid: str):
    """POST /run runner=fixture → 轮询终态。"""
    st, obj, raw = post_json(srv.api(f"/api/tasks/{tid}/run"), {"runner": "fixture"})
    if st == 404:
        not_ready(f"POST /api/tasks/<id>/run 返回 404（路由缺失）: {tail(raw, 500)}")
    check(st in (200, 201, 202), f"POST /run HTTP {st}: {tail(raw, 700)}")
    state, tobj, tbody = wait_state(srv, tid, timeout=240)
    check(state != "failed",
          f"fixture runner 运行失败 state=failed: {tail(json.dumps(tobj, ensure_ascii=False), 900)}")
    report = repo / "runs" / tid / "report.json"
    check(report.is_file(), f"fixture runner 未产出 runs/{tid}/report.json（state={state}）")
    return report, state


def fetch_events(srv: LocalServer, tid: str, since=None):
    q = f"?since={since}" if since is not None else ""
    return get_json(srv.api(f"/api/tasks/{tid}/events{q}"))


def case_a10(ctx: dict):
    sb: Sandbox = ctx["sb"]
    srv: LocalServer = ctx["srv"]
    repo = sb.repo
    notes, problems = [], []
    t_text = data_text("material_refill_T.json")
    t30_text = data_text("material_refill_T30.json")

    # 建任务 + 上传数据（runner=fixture 最好有数据；上传失败不阻断事件断言但记录）
    body = intake()
    body["data_files"] = [
        {"name": "material_refill_T.json", "content": t_text},
        {"name": "material_refill_T30.json", "content": t30_text},
    ]
    st, obj, raw = srv.create_task(body)
    check(st in (200, 201), f"POST /api/tasks HTTP {st}: {tail(raw, 600)}")
    tid = task_id_of(obj)
    check(bool(tid), f"POST /api/tasks 未返回 task_id: {tail(raw, 500)}")

    report, state = run_fixture(srv, repo, tid)
    notes.append(f"fixture runner 终态={state}，report 已产出")

    def sub(name, fn):
        try:
            fn()
            notes.append(name)
        except CaseFailure as exc:
            problems.append(f"{name}: [{exc.kind}] {exc.reason}")

    full = []

    def events_shape():
        nonlocal full
        st, obj, raw = fetch_events(srv, tid, 0)
        if st == 404:
            not_ready(f"GET /events 返回 404（§2.2/§2.5 未实现）: {tail(raw, 400)}")
        check(st == 200, f"GET /events HTTP {st}: {tail(raw, 600)}")
        evs = unwrap_events(obj)
        check(bool(evs), f"/events 无事件: {tail(json.dumps(obj, ensure_ascii=False) if obj else raw, 700)}")
        full = evs
        seq = seqs(evs)
        check(len(seq) == len(evs), f"有事件缺整数 seq: {[e for e in evs[:3]]}")
        check(seq == sorted(seq) and len(set(seq)) == len(seq), f"seq 非严格递增/有重复: {seq[:20]}")
        check(seq[0] >= 1, f"seq 起始 {seq[0]}（期望 ≥1）")
        need = ("seq", "ts", "phase", "role", "agent", "kind", "line")
        for i, e in enumerate(evs[:50]):
            for k in need:
                check(k in e, f"事件[{i}] 缺字段 {k}: {json.dumps(e, ensure_ascii=False)[:300]}")
            check(isinstance(e.get("seq"), int), f"事件[{i}].seq 非整数")
            check(isinstance(e.get("phase"), str) and e["phase"], f"事件[{i}].phase 空")
            check(isinstance(e.get("agent"), str) and e["agent"], f"事件[{i}].agent 空")
            check(isinstance(e.get("kind"), str) and e["kind"], f"事件[{i}].kind 空")
            check(isinstance(e.get("line"), str) and e["line"], f"事件[{i}].line 空")
        notes.append(f"事件 {len(evs)} 条，seq {seq[0]}..{seq[-1]}")
    sub("§2.2 事件字段/seq 递增", events_shape)

    def parallel_and_tools():
        phases = {str(e.get("phase")) for e in full}
        # Lead 2026-10-19 裁决（m00628）：阶段事件必须 s1–s5 齐（s1‖s2 并行都出现），gate 可选；
        # 这是对 spec A10 的收紧，不是放宽测试。
        need = {"s1", "s2", "s3", "s4", "s5"}
        check(need <= phases,
              f"阶段事件缺 s1–s5（spec A10 收紧：s1‖s2 并行且全程五阶段都出现）："
              f"缺少 {sorted(need - phases)}，phases={sorted(phases)}")
        kinds = {str(e.get("kind")) for e in full}
        check("tool" in kinds,
              f"事件流缺工具级事件 kind=tool（spec A10『工具级事件不丢』）：kinds={sorted(kinds)}")
        notes.append(f"阶段 s1–s5 齐；kinds={sorted(kinds)}")
    sub("s1‖s2 与工具级事件", parallel_and_tools)

    def since_filter():
        mid = seqs(full)[len(full) // 2] if full else 1
        st, obj, raw = fetch_events(srv, tid, mid)
        check(st == 200, f"GET /events?since={mid} HTTP {st}: {tail(raw, 500)}")
        evs = unwrap_events(obj)
        got = seqs(evs)
        check(all(s > mid for s in got), f"since={mid} 返回了 ≤{mid} 的事件: {got[:10]}")
        expect = [s for s in seqs(full) if s > mid]
        check(got == expect, f"since={mid} 增量与全量尾部不一致（丢/重）: got={got[:10]} expect={expect[:10]}")
        notes.append(f"since={mid} 增量 {len(got)} 条，无重无丢")
    sub("since 增量", since_filter)

    def bad_since():
        # Lead 裁决（m00628）：非法 since 一律 400 JSON 错误（fail-closed），
        # 覆盖 spec 原文「回退 0」——属 Lead 裁定的 spec 收紧，不是放宽测试。
        for bad in ("abc", "-3", "1e999"):
            st, obj, raw = fetch_events(srv, tid, bad)
            check(st == 400,
                  f"非法 since={bad!r} 期望 400 JSON 错误（lead 裁决 fail-closed），实际 HTTP {st}: {tail(raw, 400)}")
            check(isinstance(obj, dict) and (obj.get("ok") is False or obj.get("error")),
                  f"非法 since={bad!r} 400 但 body 不是 JSON 错误对象: {tail(raw, 400)}")
        notes.append("非法 since 全部 400")
    sub("非法 since → 400", bad_since)

    def sse_stream():
        res = read_sse(srv.api(f"/api/tasks/{tid}/events?stream=1&since=0"), max_events=15, timeout=20)
        if res["status"] == 404:
            not_ready("GET /events?stream=1 返回 404（SSE 未实现）")
        check(res["status"] == 200, f"SSE HTTP {res['status']}")
        ct = (res["content_type"] or "").lower()
        check("text/event-stream" in ct, f"SSE Content-Type={res['content_type']!r}（期望 text/event-stream）")
        check(bool(res["events"]), f"SSE 未读到 data 事件; raw={tail('\n'.join(res['raw']), 600)}")
        ids = [int(i) for i in res["ids"] if str(i).strip().isdigit()]
        sse_seqs = seqs(res["events"])
        check(bool(ids), f"SSE 缺 id: 行; raw={tail('\n'.join(res['raw']), 600)}")
        check(ids == sorted(ids) and len(set(ids)) == len(ids), f"SSE id 非严格递增/重复: {ids}")
        check(len(sse_seqs) == len(ids), f"SSE data 事件数 {len(sse_seqs)} != id 数 {len(ids)}")
        check(sse_seqs == ids, f"SSE data.seq 与 id: 行不一致: seq={sse_seqs[:8]} ids={ids[:8]}")
        last = ids[-1]

        # 断线后在 since=last 续传：不重不丢
        st, obj, raw = fetch_events(srv, tid, last)
        check(st == 200, f"续传 GET /events?since={last} HTTP {st}: {tail(raw, 400)}")
        resumed = seqs(unwrap_events(obj))
        check(all(s > last for s in resumed), f"续传返回重复事件（≤{last}）: {resumed[:10]}")
        expect = [s for s in seqs(full) if s > last]
        check(resumed == expect, f"续传不完整: resumed={resumed[:10]} expect={expect[:10]}")
        notes.append(f"SSE ids {ids[0]}..{ids[-1]}，since 续传 {len(resumed)} 条不重不丢")
    sub("SSE 断线续传", sse_stream)

    if problems:
        raise_problems("A10", problems)
    return "；".join(notes)


# ---------------------------------------------------------------- A11


def case_a11(ctx: dict):
    sb: Sandbox = ctx["sb"]
    srv: LocalServer = ctx["srv"]
    repo = sb.repo
    notes, problems = [], []

    # --- 同日连建两任务 ---
    tids = []
    for _ in range(2):
        tid = create_plain_task(srv)
        tids.append(tid)
        check((repo / "runs" / tid).is_dir(), f"runs/{tid}/ 未创建")
    tid1, tid2 = tids
    check(tid1 != tid2, f"同日两次建任务撞号: {tid1}")

    st, obj, raw = get_json(srv.api("/api/tasks"))
    if st == 404:
        not_ready("GET /api/tasks 返回 404")
    check(st == 200, f"GET /api/tasks HTTP {st}: {tail(raw, 500)}")
    items = obj if isinstance(obj, list) else (obj.get("tasks") if isinstance(obj, dict) else None)
    check(isinstance(items, list), f"/api/tasks 未返回数组: {tail(raw, 500)}")
    listed = [task_id_of({"task": it}) or task_id_of(it) for it in items]
    check(tid1 in listed and tid2 in listed, f"/api/tasks 未同时列出两个任务: listed={listed[:10]}")
    notes.append(f"同日两任务 {tid1}/{tid2} 各自落盘并在 /api/tasks 同见")

    # --- 各字段独立：一任务 run 后另一任务不被串改 ---
    report, state = run_fixture(srv, repo, tid1)
    sha1 = sha256_file(report)
    task1_json = repo / "runs" / tid1 / "task.json"
    task1_sha = sha256_file(task1_json) if task1_json.is_file() else None

    st, obj2, raw2 = srv.task(tid2)
    check(st == 200, f"第二个任务 GET HTTP {st}")
    t2dir = repo / "runs" / tid2
    check(not (t2dir / "report.json").exists(),
          f"任务1 /run 后任务2 目录出现 report.json（跨任务串台）: {t2dir}")

    report2, state2 = run_fixture(srv, repo, tid2)
    check(tid1 != tid2, "任务 id 重合")
    check(sha256_file(report) == sha1,
          "任务2 运行后任务1 report.json 被覆盖/改动（多任务隔离失败）")
    if task1_sha is not None:
        check(sha256_file(task1_json) == task1_sha, "任务2 运行后任务1 task.json 被改写")
    rep2 = try_json(read_text(report2)) or {}
    check(rep2.get("is_demo") is True, f"任务2 fixture 报告 is_demo={rep2.get('is_demo')!r}（期望 true）")
    notes.append(f"任务1 报告在任务2 运行后 sha 未变（{sha1[:8]}…）")

    # --- /api/tasks/<id> 各字段独立 ---
    s1, o1, _ = srv.task(tid1)
    s2, o2, _ = srv.task(tid2)
    check(task_id_of(o1) == tid1 and task_id_of(o2) == tid2,
          f"/api/tasks/<id> task_id 串台: {task_id_of(o1)!r}/{task_id_of(o2)!r}")

    # --- UI：adapter 暴露 selectTask / tasks 数组 / 渲染接收 taskId（代码级断言）---
    adapter = read_text(repo / "ui" / "api-adapter.js")
    check(not adapter.startswith("<read error"), "读不到 ui/api-adapter.js")
    check("selectTask" in adapter, "api-adapter.js 未暴露 selectTask（spec §2.6/A11）")
    check("/api/tasks" in adapter, "api-adapter.js 未接 /api/tasks（任务栏数据源）")
    check(re.search(r"(this\.)?tasks\s*[:=]", adapter) is not None or "tasks =" in adapter,
          "api-adapter.js 未维护 tasks 数组/状态")
    check(adapter.count("taskId") >= 3, f"api-adapter.js 中 taskId 出现 {adapter.count('taskId')} 次（渲染函数需接收 taskId）")

    if problems:
        raise_problems("A11", problems)
    return "；".join(notes)


# ---------------------------------------------------------------- A12


def case_a12(ctx: dict):
    sb: Sandbox = ctx["sb"]
    srv: LocalServer = ctx["srv"]
    repo = sb.repo
    notes = []

    report, state = run_fixture(srv, repo, tid := create_plain_task(srv))
    rep = try_json(read_text(report)) or {}
    check(rep.get("is_demo") is True,
          f"fixture runner 报告 is_demo={rep.get('is_demo')!r}（spec §2.7/A12 要求 true，禁止冒充真实）")
    check(rep.get("task_id") in (None, tid), f"fixture 报告 task_id={rep.get('task_id')!r} 与任务 {tid} 不一致")
    notes.append(f"fixture 报告 is_demo=true（task={tid}, state={state}）")

    # /api/tasks/<id> 投影里也必须是 is_demo=true（UI 交付卡据此标注）
    st, obj, raw = srv.task(tid)
    check(st == 200, f"GET /api/tasks/{tid} HTTP {st}")
    txt = json.dumps(obj, ensure_ascii=False)
    check('"is_demo": true' in txt or '"is_demo":true' in txt,
          f"/api/tasks/<id> 未透出 is_demo=true: {tail(txt, 600)}")

    # UI 静态：必须出现「演示数据」标注（无浏览器，静态检查 + HTTP 断言）
    ui_blob = read_text(repo / "ui" / "index.html") + "\n" + read_text(repo / "ui" / "api-adapter.js")
    check("演示数据" in ui_blob, "UI 未出现「演示数据」标注（spec §2.6/§2.7/A12）")
    check("is_demo" in ui_blob or "demo" in ui_blob.lower(), "UI 未见 is_demo/demo 标记逻辑（无法区分演示与真实）")
    st, body = http_get(srv.api("/ui/index.html"))
    check(st == 200, f"GET /ui/index.html HTTP {st}")
    check("演示数据" in body, "服务端返回的 index.html 不含「演示数据」标注")
    notes.append("UI 静态含「演示数据」+ is_demo 标记（NOTE：无浏览器，未做 DOM 渲染验证）")

    return "；".join(notes)


# ---------------------------------------------------------------- main

CASES = {
    "A7": ("POST/GET 团队 API：/api/team 字段/精灵 key/crew 一致性", case_a7),
    "A8": ("接单数据：上传 T/T30 → runs/<id>/data、data_ready；非法/超限/穿越全拒", case_a8),
    "A9": ("审批门：无 gate/reject/伪造/越权 confirmer 拒；confirm 后 finalize 放行", case_a9),
    "A10": ("事件流：/events 字段/seq/s1‖s2/工具事件；SSE 续传；非法 since", case_a10),
    "A11": ("多任务：同日双任务独立/报告不覆盖//api/tasks 同见；UI selectTask", case_a11),
    "A12": ("演示标记：fixture 报告 is_demo=true 且 UI 标注「演示数据」", case_a12),
}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="M1.5 独立验收测试（黑盒）")
    ap.add_argument("--case", action="append", default=[],
                    help="只跑指定用例，如 --case A9 或 --case A7,A10")
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

    print(f"M1.5 acceptance · python={PY}")
    print(f"M1.5 acceptance · cases={selected} · sandbox=%TEMP%\\m15-accept-*")
    results = {}
    for cid in selected:
        desc, fn = CASES[cid]
        print(f"\n--- CASE {cid}: {desc}")
        sb = None
        srv = None
        t0 = time.time()
        try:
            sb = Sandbox(cid)
            srv = LocalServer(sb.repo).start()
            notes = fn({"sb": sb, "srv": srv})
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
            if srv is not None:
                srv.stop()
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
