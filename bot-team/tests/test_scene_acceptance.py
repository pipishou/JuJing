#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""B16/B18/B15/B17 场景化决策台 —— 独立验收测试（Checker 侧，黑盒）。

唯一规格源:
  <bot-team>/../docs/项目文档/02-产品方案/经营模型整合与场景化决策卡设计_2026-10-20.md
     §3.3 DEC、§3.5 U1-U5、§4.5.1 SceneSpec v1、§4.5.2 三通道、
     §4.5.3 core/scene.py 校验六规则、§4.5.4 通用渲染纪律、§4.5.5 U6-U8

覆盖:
  U1  8787 端口起 server -> GET /api/scenes -> P0 场景驾驶舱字段与 5 秒可见性
  U2  P2 决策卡（DEC §3.3 字段齐）+ 60 秒可签字（report.json 整文件 sha256 可见）
  U3  决策卡可打印/可导出，文本不含模型名/token/轨迹
  U4  T+7 回填（POST /api/tasks/<id>/outcome）后 P2/P3 透出 observed_outcome/increment_value；
      未回填显示「待回看」
  U5  空值率 > 10% -> P1 只显示降级原因与缺失字段清单，不显示建议
  U6  第 4 个场景（废品率）seed 零代码改动渲染；GET /api/scenes 返回 4 个；
      ui/index.html 不含 refill/downtime/output 之类场景常量
  U7  POST /api/scenes/derive 双模式（lead 2026-10-19 裁决）：
      校验模式（body 含 spec）非法 spec_version/缺必填 -> 400，其余五类 -> 200
      {ok:false, cannot_derive:[人话原因]}；生成模式（body 无 spec）discovered 通道
      必须 200 + scene_id（cannot_derive 计入失败），不存在的数据文件 -> 200 +
      cannot_derive（不得 500），user_prompt -> 501
  U8  同 scene_id@version 两次渲染关键字段一致、origin 可见

用法:
  python -B tests/test_scene_acceptance.py                 # 跑 U1-U8
  python -B tests/test_scene_acceptance.py --case U5       # 单跑
  python -B tests/test_scene_acceptance.py --list
  python -B tests/test_scene_acceptance.py --selfcheck     # 只做夹具自检
  python -B tests/test_scene_acceptance.py --regression    # 只跑回归套件
  python -B tests/test_scene_acceptance.py --keep          # 保留全部沙盒
  python -B tests/test_scene_acceptance.py --keep-failed   # 默认：保留失败沙盒
  python -B tests/test_scene_acceptance.py --no-keep-failed

约束（写清限制）:
  * 只用 stdlib + subprocess + urllib；python 子进程一律 -B。
  * 每个用例独立 %TEMP% 沙盒复制 $BT（排除 runs/ 等），绝不写仓库 runs/。
  * 浏览器不可用：P0/P1/P2 的「渲染」以 HTTP 契约 + ui/*.js|html 静态关键字断言
    代替真实浏览器；签字时长以 HTTP 往返计时近似。
  * external runner 的 workflow-result 由测试直接写入 runs/<id>/result.json（黑盒契约）。
  * 不执行 DSH workflow（server 不调 LLM），user_prompt 通道只验 /api/scenes/derive 契约。
  * 本文件是 B3 Checker 唯一 write scope 产物，不修改产品代码。

输出:
  逐用例 `CASE U<n> PASS|FAIL: <原因>`，最后 `RESULT: GREEN|RED`，退出码 0/1。
"""

from __future__ import annotations

import argparse
import base64
import copy
import datetime as dt
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
import urllib.parse
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIX = Path(__file__).resolve().parent / "fixtures" / "scene"


def _default_venv_py() -> Path:
    """子进程解释器：仓库同级 venv（bot-2/venv）→ 仓内 venv → 当前解释器。

    不写死某台机器的绝对路径；确需指定时用环境变量 BOT_PY（优先）。
    兜底保留：找不到 venv 就用当前解释器（本仓仍能跑）。
    """
    for rel in ("venv/Scripts/python.exe", ".venv/Scripts/python.exe",
                "venv/bin/python", ".venv/bin/python"):
        cand = ROOT.parent / rel
        if cand.is_file():
            return cand
    return Path(sys.executable)


DEFAULT_VENV_PY = _default_venv_py()
IGNORE_COPY = shutil.ignore_patterns(
    "runs", "__pycache__", "*.pyc", ".git", ".pytest_cache",
)
DEMO_FILES = (
    "demo-result-2026-10-19.json",
    "demo-report-2026-10-19.json",
    "demo-report.json",
    "dsh-tape-sample.json",
)
# demo 红线四哈希（前 16 位；B11/M1.5 基线，任何变化都是回归）
DEMO_HASH_PREFIX = {
    "material_refill_T.json": "0048fa0aad30",
    "material_refill_T30.json": "da985bce35a0",
    "demo-report-2026-10-19.json": "086c2a51af05",
    "demo-result-2026-10-19.json": "55eab00f4c42",
}
SIGNER = "老板"
FORBIDDEN_TOKENS = (
    "deepseek", "gpt-", "claude", "chatgpt", "llm", "token", "prompt",
    "轨迹", "trace",
)
SCENE_IDS = ("refill_rate_drop", "downtime_anomaly", "output_per_capita", "scrap_rate_high")
INVALID_FIXTURES = (
    "invalid_metric.json",
    "invalid_factor.json",
    "invalid_irreversible.json",
    "invalid_threshold.json",
    "invalid_spec_version.json",
    "invalid_missing_field.json",
)
LEGAL_FIXTURES = (
    "scene_refill_rate.json",
    "scene_downtime.json",
    "scene_output.json",
    "scene_scrap_rate.json",
)
DEC_REQUIRED = (
    "decision_id", "entity", "decided_at", "actor", "context",
    "attribution", "recommendation", "evidence", "sign",
)
CASES = ("U1", "U2", "U3", "U4", "U5", "U6", "U7", "U8")


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #
def _pick_python() -> str:
    # BOT_PY 优先（与 server.find_python 同口径），再兼容本文件历史键名
    cand = []
    for key in ("BOT_PY", "B16_PY", "M15_PY"):
        val = os.environ.get(key)
        if val:
            cand.append(Path(val))
    cand.append(DEFAULT_VENV_PY)
    cand.append(Path(sys.executable))
    for c in cand:
        if c and Path(c).is_file():
            return str(c)
    return sys.executable


PY = _pick_python()


class CaseFailure(Exception):
    def __init__(self, kind: str, reason: str):
        super().__init__(reason)
        self.kind = kind  # not_ready | test_issue | assert
        self.reason = reason


def not_ready(reason: str):
    raise CaseFailure("not_ready", reason)


def test_issue(reason: str):
    raise CaseFailure("test_issue", reason)


def check(cond, reason: str):
    if not cond:
        raise CaseFailure("assert", reason)


def qs(name: str, value: str) -> str:
    """查询串拼装：值一律百分号编码（非 ASCII 中文筛选项必须编码）。

    与前端 ui/api-adapter.js 的 encodeURIComponent 同口径；服务端
    urllib.parse.parse_qs 会做解码，故端到端行为一致。
    """
    return f"{urllib.parse.quote(str(name), safe='')}={urllib.parse.quote(str(value), safe='')}"


def read_text(p: Path, limit: int = 200000) -> str:
    try:
        return p.read_text(encoding="utf-8", errors="replace")[:limit]
    except OSError as exc:
        return f"<read error: {exc}>"


def load_json(p: Path):
    with p.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def dump_json(p: Path, obj):
    p.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


# ---- 枚举口径：直接取产品契约（core.scene）与行业包，不在夹具里另造一套 ----
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from core.scene import DIRECTIONS as SCENE_DIRECTIONS, GRAINS as SCENE_GRAINS  # noqa: E402

_PACK_DOC = json.loads(
    (ROOT / "packs" / "manufacturing" / "pack.json").read_text(encoding="utf-8-sig"))
PACK_METRIC_IDS = {
    str(item.get("id")) for item in (_PACK_DOC.get("metrics") or []) if isinstance(item, dict)
}


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def tail(text: str, n: int = 1200) -> str:
    text = text or ""
    return text[-n:].replace("\r", "")


def child_env() -> dict:
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env.pop("BOT_SIGNERS", None)
    env.pop("BOT_SIGNER", None)
    return env


def run_py(args, cwd: Path, timeout: int = 300):
    exe = [PY, "-B"]
    try:
        proc = subprocess.run(
            exe + [str(a) for a in args],
            cwd=str(cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            env=child_env(),
        )
    except subprocess.TimeoutExpired as exc:
        raise CaseFailure("test_issue", f"子进程超时 {timeout}s: {exc}")
    out = (proc.stdout or b"").decode("utf-8", "replace")
    err = (proc.stderr or b"").decode("utf-8", "replace")
    return proc.returncode, out, err


def _free_port(preferred=None) -> int:
    candidates = []
    if preferred:
        candidates.append(preferred)
    candidates += list(range(8870, 8920)) + [0]
    for p in candidates:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind(("127.0.0.1", p))
            port = s.getsockname()[1]
            s.close()
            return port
        except OSError:
            s.close()
            continue
    raise CaseFailure("test_issue", "无法申请本地端口")


def try_json(text: str):
    text = (text or "").strip()
    if not text:
        return None
    for i, ch in enumerate(text):
        if ch in "[{":
            try:
                obj, _ = json.JSONDecoder().raw_decode(text[i:])
                return obj
            except ValueError:
                continue
    try:
        return json.loads(text)
    except ValueError:
        return None


def http_request(method: str, url: str, data: bytes | None = None,
                 ctype: str | None = None, timeout: int = 30):
    req = urllib.request.Request(url, data=data, method=method)
    if ctype:
        req.add_header("Content-Type", ctype)
    req.add_header("Accept", "application/json, text/html;q=0.9, */*;q=0.8")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return resp.status, resp.headers.get("Content-Type", ""), raw
    except urllib.error.HTTPError as exc:
        raw = exc.read() if exc.fp else b""
        return exc.code, exc.headers.get("Content-Type", "") if exc.headers else "", raw
    except Exception as exc:  # noqa: BLE001 - 连接层失败要可读
        raise CaseFailure("not_ready", f"HTTP 请求失败 {method} {url}: {exc}")


# --------------------------------------------------------------------------- #
# 沙盒 / 本地服务
# --------------------------------------------------------------------------- #
class Sandbox:
    def __init__(self, tag: str):
        self.tag = tag
        self.root = Path(tempfile.mkdtemp(prefix=f"b16-accept-{tag}-"))
        self.repo = self.root / "repo"
        try:
            shutil.copytree(ROOT, self.repo, ignore=IGNORE_COPY)
        except OSError as exc:
            raise CaseFailure("test_issue", f"复制仓库失败: {exc}")
        (self.repo / "runs").mkdir(parents=True, exist_ok=True)
        for name in DEMO_FILES:
            src = ROOT / "runs" / name
            if src.is_file():
                shutil.copy2(src, self.repo / "runs" / name)

    def cleanup(self):
        shutil.rmtree(self.root, ignore_errors=True)


class LocalServer:
    def __init__(self, sb: Sandbox, prefer_port: int = 8787):
        self.sb = sb
        self.repo = sb.repo
        self.app = self.repo / "server" / "app.py"
        self.port = _free_port(prefer_port)
        self.base = f"http://127.0.0.1:{self.port}"
        self.proc = None
        self._log_path = sb.root / "server.log"
        self._log = None

    def __enter__(self):
        if not self.app.is_file():
            raise CaseFailure("not_ready", "缺少 server/app.py")
        self._log = self._log_path.open("wb")
        self.proc = subprocess.Popen(
            [PY, "-B", str(self.app), "--host", "127.0.0.1",
             "--port", str(self.port), "--root", str(self.repo),
             "--owner", SIGNER, "--allow-signers", f"{SIGNER},boss"],
            cwd=str(self.repo),
            stdout=self._log,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            env=child_env(),
        )
        deadline = time.time() + 30
        last = ""
        while time.time() < deadline:
            if self.proc.poll() is not None:
                last = tail(read_text(self._log_path))
                raise CaseFailure(
                    "not_ready",
                    f"server/app.py 启动即退出 rc={self.proc.returncode}: {last}")
            try:
                status, _, raw = http_request("GET", self.base + "/api/health", timeout=2)
                if status == 200:
                    return self
            except CaseFailure:
                pass
            except Exception:  # noqa: BLE001
                pass
            time.sleep(0.2)
        raise CaseFailure("not_ready", f"server 30s 内未就绪: {tail(read_text(self._log_path))}")

    def __exit__(self, exc_type, exc, tb):
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                try:
                    self.proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
        if self._log:
            try:
                self._log.close()
            except OSError:
                pass
        return False

    # -- HTTP 便捷方法 ------------------------------------------------------ #
    def get(self, path: str, timeout: int = 30):
        status, ctype, raw = http_request("GET", self.base + path, timeout=timeout)
        return status, ctype, raw

    def get_json(self, path: str, timeout: int = 30):
        status, ctype, raw = self.get(path, timeout)
        return status, try_json(raw.decode("utf-8", "replace")), raw.decode("utf-8", "replace")

    def post(self, path: str, payload=None, raw_body: bytes | None = None,
             ctype: str | None = None, timeout: int = 60):
        if raw_body is None:
            raw_body = json.dumps(payload or {}, ensure_ascii=False).encode("utf-8")
            ctype = ctype or "application/json"
        status, rctype, raw = http_request("POST", self.base + path, raw_body, ctype, timeout)
        return status, try_json(raw.decode("utf-8", "replace")), raw.decode("utf-8", "replace")

    def post_multipart(self, path: str, parts, timeout: int = 120):
        body, ctype = build_multipart(parts)
        status, rctype, raw = http_request("POST", self.base + path, body, ctype, timeout)
        return status, try_json(raw.decode("utf-8", "replace")), raw.decode("utf-8", "replace")


def build_multipart(parts) -> tuple[bytes, str]:
    """parts: [{name, value(bytes|str), filename?, ctype?}]"""
    boundary = "----b16accept" + uuid.uuid4().hex
    out = []
    for p in parts:
        out.append(f"--{boundary}\r\n".encode())
        disp = f'Content-Disposition: form-data; name="{p["name"]}"'
        if p.get("filename"):
            disp += f'; filename="{p["filename"]}"'
        out.append((disp + "\r\n").encode())
        if p.get("ctype"):
            out.append(f"Content-Type: {p['ctype']}\r\n".encode())
        out.append(b"\r\n")
        val = p.get("value", b"")
        out.append(val if isinstance(val, bytes) else str(val).encode("utf-8"))
        out.append(b"\r\n")
    out.append(f"--{boundary}--\r\n".encode())
    return b"".join(out), f"multipart/form-data; boundary={boundary}"


def walk(obj, path: str = ""):
    yield path, obj
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from walk(v, f"{path}.{k}" if path else str(k))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from walk(v, f"{path}[{i}]" if path else f"[{i}]")


def dig(obj, dotted: str):
    cur = obj
    for part in dotted.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return None
    return cur


def collect_texts(obj) -> str:
    chunks = []
    for _, v in walk(obj):
        if isinstance(v, str):
            chunks.append(v)
    return "\n".join(chunks)


def find_dec(obj):
    """在 delivery/report 里找 DEC 决策卡对象（含 decision_id 且至少 3 个 DEC 顶层键）。"""
    best = None
    for path, v in walk(obj):
        if isinstance(v, dict):
            score = sum(1 for k in DEC_REQUIRED if k in v)
            if "decision_id" in v and score >= 3:
                if best is None or score > best[1]:
                    best = (v, score, path)
    return best


# --------------------------------------------------------------------------- #
# 数据 / 任务流水线（黑盒契约）
# --------------------------------------------------------------------------- #
def build_task_csv(null_rate: float = 0.0) -> bytes:
    header = ("日期,产线,补料率,产量,模具状态,保养记录,班组,原料批次,"
              "环境温湿度,换模记录,停机记录,信息员")
    rows = [header]
    start = dt.date(2026, 9, 5)
    t_window = (5.10, 5.28, 5.54, 5.90, 6.26, 6.85, 7.20)
    for line in ("B", "C"):
        for i in range(37):
            day = start + dt.timedelta(days=i)
            if line == "B":
                refill = 5.00 + 0.004 * i
                if i >= 30:
                    refill = t_window[i - 30]
                if i == 33:
                    mold = "slight_wear"
                elif i == 35:
                    mold = "wear_detected"
                else:
                    mold = "normal"
                # 保养记录：基线窗口 09-18 一次；T 窗口（i>=30）每天一条。
                # T 窗口若整列缺失，核心三项缺一 → 按 degradation_rules 判降级（不出建议），
                # 那是 U5 的场景；U2/U3/U4 需要一个非降级的健康数据集。
                if i == 26:
                    maint = "2026-09-18 换模后保养"
                elif i >= 30:
                    maint = f"{day.isoformat()} 例行保养"
                else:
                    maint = ""
                output = 1180 + 3.2 * i
            else:
                refill = 4.80
                mold = "normal"
                maint = ""
                output = 980 + 1.0 * i
            if null_rate > 0 and (i % 3 == 0 or i % 7 == 0):
                refill_cell = ""
            else:
                refill_cell = f"{refill:.2f}"
            rec = 0.5 if i >= 30 else 0.0
            downtime = 12 + (i % 5) * 2 if line == "B" else 8 + (i % 3)
            rows.append(",".join([
                day.isoformat(),
                line,
                refill_cell,
                f"{output:.0f}",
                mold,
                maint,
                "甲班",
                f"MAT-{day.month:02d}{day.day:02d}",
                "55/60",
                f"{rec:.1f}",
                str(downtime),
                f"信息员{i % 3 + 1}",
            ]))
    return ("\n".join(rows) + "\n").encode("utf-8")


def create_task(srv: LocalServer, sb: Sandbox, title: str = "场景验收任务") -> str:
    intake = load_json(sb.repo / "templates" / "task-intake.example.json")
    intake.pop("task_id", None)
    intake["title"] = title
    intake["line"] = "B"
    intake["metric"] = "补料率"
    status, body, text = srv.post("/api/tasks", intake)
    if status in (404, 405):
        not_ready(f"POST /api/tasks 未实现 HTTP {status}: {tail(text)}")
    check(status in (200, 201), f"POST /api/tasks 应 2xx，实际 {status}: {tail(text)}")
    body = body if isinstance(body, dict) else {}
    task = body.get("task") if isinstance(body.get("task"), dict) else body
    tid = (task or {}).get("task_id") or body.get("task_id") or dig(body, "task.task_id")
    check(bool(tid), f"POST /api/tasks 未返回 task_id: {tail(text)}")
    check((sb.repo / "runs" / str(tid)).is_dir(), f"任务目录 runs/{tid}/ 未落盘")
    return str(tid)


def upload_data(srv: LocalServer, sb: Sandbox, tid: str, null_rate: float = 0.0):
    csv_bytes = build_task_csv(null_rate)
    fm = (sb.repo / "templates" / "field-map.example.yaml").read_bytes()
    parts = [
        {"name": "table", "filename": "task.csv", "value": csv_bytes, "ctype": "text/csv"},
        {"name": "field_map", "filename": "field-map.yaml", "value": fm,
         "ctype": "application/x-yaml"},
        {"name": "line", "value": "B"},
        {"name": "t_start", "value": "2026-10-05"},
        {"name": "t_end", "value": "2026-10-11"},
        {"name": "base_start", "value": "2026-09-05"},
        {"name": "base_end", "value": "2026-10-04"},
    ]
    status, body, text = srv.post_multipart(f"/api/tasks/{tid}/data", parts, timeout=180)
    if status in (404, 405):
        not_ready(f"POST /api/tasks/<id>/data 未实现 HTTP {status}: {tail(text)}")
    check(status in (200, 201), f"上传数据应 2xx，实际 {status}: {tail(text)}")
    return body, text


def state_of(body) -> str:
    if not isinstance(body, dict):
        return ""
    for key in ("state", "status"):
        val = body.get(key)
        if isinstance(val, str):
            return val
        if isinstance(val, dict):
            for k in ("state", "status"):
                if isinstance(val.get(k), str):
                    return val[k]
    return ""


def wait_state(srv: LocalServer, tid: str, want=("assembled", "awaiting_sign", "signed"),
               timeout: int = 180) -> tuple[str, dict]:
    deadline = time.time() + timeout
    last_state = ""
    last_body = {}
    while time.time() < deadline:
        status, body, text = srv.get_json(f"/api/tasks/{tid}")
        if status == 200 and isinstance(body, dict):
            last_body = body
            last_state = state_of(body)
            if last_state in want:
                return last_state, body
            if last_state == "failed":
                return last_state, body
        time.sleep(0.5)
    return last_state, last_body


def run_pipeline(srv: LocalServer, sb: Sandbox, workflow_doc, null_rate: float = 0.0,
                 sign: bool = True) -> dict:
    tid = create_task(srv, sb)
    upload_data(srv, sb, tid, null_rate=null_rate)
    run_dir = sb.repo / "runs" / tid
    dump_json(run_dir / "result.json", workflow_doc)
    status, body, text = srv.post(f"/api/tasks/{tid}/run", {"runner": "external"})
    if status in (404, 405):
        not_ready(f"POST /api/tasks/<id>/run 未实现 HTTP {status}: {tail(text)}")
    check(status in (200, 202), f"/run 应 200/202，实际 {status}: {tail(text)}")
    state, view = wait_state(srv, tid, want=("assembled", "awaiting_sign", "signed"))
    report_path = run_dir / "report.json"
    report = load_json(report_path) if report_path.is_file() else None
    sign_rc = finalize_rc = None
    sign_body = finalize_body = None
    if state in ("assembled", "awaiting_sign", "signed") and sign:
        sign_rc, sign_body, sign_text = srv.post(
            f"/api/tasks/{tid}/sign", {"signer": SIGNER})
        if sign_rc in (404, 405):
            not_ready(f"POST /sign 未实现 HTTP {sign_rc}: {tail(sign_text)}")
        finalize_rc, finalize_body, finalize_text = srv.post(
            f"/api/tasks/{tid}/finalize", {})
        if finalize_rc == 200 and report_path.is_file():
            report = load_json(report_path)
        state, view = wait_state(srv, tid, want=("signed", "awaiting_sign", "assembled"))
    delivery = None
    status, body, text = srv.get_json(f"/api/tasks/{tid}/delivery")
    if status == 200:
        delivery = body
    return {
        "tid": tid,
        "repo": sb.repo,
        "state": state,
        "view": view,
        "report_path": report_path,
        "report": report,
        "delivery": delivery,
        "sign_rc": sign_rc,
        "sign_body": sign_body,
        "finalize_rc": finalize_rc,
        "finalize_body": finalize_body,
    }


def get_dec(pipeline: dict):
    for source in (pipeline.get("delivery"), pipeline.get("report"), pipeline.get("view")):
        if not source:
            continue
        found = find_dec(source)
        if found:
            return found[0], found[2]
    return None, ""


def rel_lines(path: Path, needles, limit: int = 6):
    hits = []
    text = read_text(path)
    for i, line in enumerate(text.splitlines(), 1):
        low = line.lower()
        for n in needles:
            if n.lower() in low:
                hits.append(f"{path.name}:{i}: {line.strip()[:140]}")
                break
        if len(hits) >= limit:
            break
    return hits


def ensure_delivery_chain(pipeline: dict):
    if pipeline["state"] == "failed":
        body = pipeline.get("view") or {}
        raise CaseFailure(
            "assert",
            "任务执行失败（status=failed）: "
            f"{tail(json.dumps(body, ensure_ascii=False))}")
    if pipeline.get("report") is None:
        raise CaseFailure(
            "assert",
            f"状态 {pipeline['state']} 但 runs/<id>/report.json 未生成（state 契约不完整）")
    if pipeline.get("sign_rc") not in (None, 200):
        raise CaseFailure("assert", f"可逆场景 /sign 失败 HTTP {pipeline['sign_rc']}: "
                          f"{tail(json.dumps(pipeline.get('sign_body'), ensure_ascii=False))}")


# --------------------------------------------------------------------------- #
# 用例
# --------------------------------------------------------------------------- #
def case_u1(sb: Sandbox):
    with LocalServer(sb) as srv:
        t0 = time.time()
        status, body, text = srv.get_json("/api/scenes")
        elapsed = time.time() - t0
        if status in (404, 405):
            not_ready(f"GET /api/scenes 未实现 HTTP {status}: {tail(text)}")
        check(status == 200, f"GET /api/scenes 应 200，实际 {status}: {tail(text)}")
        entries = entries_of(body)
        check(len(entries) >= 3,
              f"GET /api/scenes 应返回 >=3 个 preset 场景，实际 {len(entries)}: {tail(text)}")
        problems = []
        seen = []
        metric_ids = []
        for idx, entry in enumerate(entries):
            meta = scene_meta(entry)
            sid = meta.get("scene_id")
            if not sid:
                problems.append(f"第 {idx} 个场景缺 scene_id")
                continue
            seen.append(sid)
            for field in ("title", "metric"):
                val = meta.get(field) or entry.get(field)
                if not val:
                    problems.append(f"{sid}: 缺 {field}")
            metric = meta.get("metric") or entry.get("metric")
            if isinstance(metric, dict):
                for k in ("id", "name"):
                    if not metric.get(k):
                        problems.append(f"{sid}: metric 缺 {k}")
                if metric.get("id"):
                    metric_ids.append(str(metric["id"]))
            ctl = meta.get("control_line") or entry.get("control_line")
            if not ctl and isinstance(metric, dict):
                ctl = metric.get("control_line")
            if not ctl:
                problems.append(f"{sid}: 缺 control_line（P0 当前值 vs 控制线）")
        if len(set(seen)) != len(seen):
            problems.append(f"场景 id 重复: {seen}")
        if len(set(metric_ids)) < 3:
            problems.append(f"3 个 preset 的 metric 应互不相同，实际 {metric_ids}")
        if not any("refill" in m for m in metric_ids):
            problems.append(f"3 个 preset 中未见补料率场景（metric id 含 refill）：{metric_ids}")
        check(not problems, "；".join(problems[:8]))
        check(elapsed <= 5.0, f"P0 5 秒可见性超预算：GET /api/scenes 耗时 {elapsed:.2f}s")
        # P0 首页静态：默认首页应能说出「场景/控制线/置信度/最近决策/发起归因」
        t0 = time.time()
        status, ctype, raw = srv.get("/")
        elapsed = time.time() - t0
        html = raw.decode("utf-8", "replace")
        check(status == 200, f"GET / 应 200，实际 {status}")
        p0_markers = ["场景", "控制线", "置信度", "最近决策", "发起归因", "场景卡"]
        hits = [m for m in p0_markers if m in html]
        check(len(hits) >= 3,
              f"P0 首页缺少核心要素，仅命中 {hits}（需 >=3：场景/控制线/置信度/最近决策/发起归因）")
        check(elapsed <= 5.0, f"P0 首页 5 秒可见性超预算：GET / 耗时 {elapsed:.2f}s")
        return (f"{len(entries)} 个场景 {seen}，字段齐；P0 首页命中 {hits}；"
                f"GET /api/scenes {elapsed:.2f}s")


def entries_of(body):
    if isinstance(body, list):
        return body
    if isinstance(body, dict):
        for key in ("scenes", "items", "data", "results", "list"):
            val = body.get(key)
            if isinstance(val, list):
                return val
        dicts = [v for v in body.values() if isinstance(v, dict)]
        if dicts and any("scene_id" in v or "spec" in v for v in dicts):
            return dicts
    return []


def scene_meta(entry) -> dict:
    if not isinstance(entry, dict):
        return {}
    meta = dict(entry)
    spec = entry.get("spec") or entry.get("scene") or entry.get("definition")
    if isinstance(spec, dict):
        merged = dict(spec)
        merged.update({k: v for k, v in entry.items() if k not in ("spec", "scene")})
        return merged
    return meta


def case_u2(sb: Sandbox):
    wf = load_json(FIX / "workflow_result.json")
    with LocalServer(sb) as srv:
        pipeline = run_pipeline(srv, sb, wf)
        ensure_delivery_chain(pipeline)
        dec, where = get_dec(pipeline)
        if not dec:
            not_ready("P2 决策卡（DEC §3.3）未在 delivery/report 中出现（实现未就绪）")
        problems = []
        for key in DEC_REQUIRED:
            if key not in dec:
                problems.append(f"DEC 缺顶层字段 {key}")
        ctx = dec.get("context") if isinstance(dec.get("context"), dict) else {}
        for key in ("metric", "control_line", "trend_7d"):
            if key not in ctx:
                problems.append(f"DEC.context 缺 {key}")
        ctl = ctx.get("control_line") if isinstance(ctx.get("control_line"), dict) else {}
        for key in ("mean", "sigma", "upper_2sigma"):
            if key not in ctl:
                problems.append(f"DEC.context.control_line 缺 {key}")
        attr = dec.get("attribution") if isinstance(dec.get("attribution"), dict) else {}
        for key in ("candidates", "scorecard", "total", "verdict",
                    "hard_guardrails", "cannot_attribute"):
            if key not in attr:
                problems.append(f"DEC.attribution 缺 {key}")
        scorecard = attr.get("scorecard")
        if isinstance(scorecard, dict):
            numeric = [v for v in scorecard.values() if isinstance(v, (int, float))]
            if len(numeric) < 6:
                problems.append(f"DEC.attribution.scorecard 不是六维（数值项 {len(numeric)}）")
        rec = dec.get("recommendation")
        rec = rec[0] if isinstance(rec, list) and rec else rec
        if not isinstance(rec, dict):
            problems.append("DEC.recommendation 缺失/形状异常")
        else:
            for key in ("action", "expected", "cost", "roi", "irreversibility"):
                if key not in rec:
                    problems.append(f"DEC.recommendation 缺 {key}")
            if "roi" in rec and not isinstance(rec.get("roi"), dict):
                problems.append("DEC.recommendation.roi 非对象")
        ev = dec.get("evidence") if isinstance(dec.get("evidence"), dict) else {}
        refs = ev.get("refs") if isinstance(ev.get("refs"), list) else []
        if not refs:
            problems.append("DEC.evidence.refs 为空（至少 1 条 sha256 证据）")
        for i, ref in enumerate(refs[:5]):
            if not isinstance(ref, dict) or not ref.get("sha256"):
                problems.append(f"DEC.evidence.refs[{i}] 缺 sha256")
                break
        sign = dec.get("sign") if isinstance(dec.get("sign"), dict) else {}
        report_sha = None
        if pipeline["report_path"].is_file():
            report_sha = sha256_file(pipeline["report_path"])
        if not sign:
            problems.append("DEC.sign 缺失")
        else:
            if not sign.get("report_sha256"):
                problems.append("DEC.sign.report_sha256 缺失")
            elif report_sha and sign.get("report_sha256") != report_sha:
                problems.append(
                    f"DEC.sign.report_sha256 与 report.json 整文件 sha256 不一致："
                    f"{sign.get('report_sha256')[:12]} != {report_sha[:12]}")
        check(not problems, "；".join(problems[:10]))
        # 60 秒可签字：HTTP 签字往返计时 + 签名哈希/证据可见
        t0 = time.time()
        status, _, text = srv.post(f"/api/tasks/{pipeline['tid']}/sign", {"signer": SIGNER})
        sign_elapsed = time.time() - t0
        check(sign_elapsed <= 60.0,
              f"签字 HTTP 往返 {sign_elapsed:.2f}s 超过 60 秒预算")
        return (f"DEC @{where or 'delivery'} 字段齐（evidence.refs={len(refs)}，"
                f"report_sha256={str(sign.get('report_sha256'))[:12]}…）；"
                f"签字往返 {sign_elapsed:.2f}s")


def case_u3(sb: Sandbox):
    wf = load_json(FIX / "workflow_result.json")
    with LocalServer(sb) as srv:
        pipeline = run_pipeline(srv, sb, wf)
        ensure_delivery_chain(pipeline)
        dec, where = get_dec(pipeline)
        check(dec is not None,
              "P2 决策卡（DEC）未出现，无法校验打印/导出内容（实现未就绪）")
        rendered = json.dumps(dec, ensure_ascii=False)
        low = rendered.lower()
        banned = [tok for tok in FORBIDDEN_TOKENS if tok in low]
        check(not banned, f"DEC 渲染文本含不该出现的模型名/内部词 {banned}")
        # 打印/导出端点（规格未定路径，做能力探测；全 404 时降级为静态检查并写清限制）
        candidates = [
            f"/api/tasks/{pipeline['tid']}/delivery-card",
            f"/api/tasks/{pipeline['tid']}/card",
            f"/api/tasks/{pipeline['tid']}/export",
            f"/api/tasks/{pipeline['tid']}/export?format=html",
            f"/api/tasks/{pipeline['tid']}/print",
        ]
        exported = None
        for path in candidates:
            status, ctype, raw = srv.get(path)
            if status == 200:
                exported = (path, ctype, raw.decode("utf-8", "replace"))
                break
        if exported:
            path, ctype, text = exported
            low_text = text.lower()
            banned2 = [tok for tok in FORBIDDEN_TOKENS if tok in low_text]
            check(not banned2, f"导出内容 {path} 含模型名/内部词 {banned2}")
            if "html" in (ctype or "").lower():
                check("@media print" in text.replace("@media  print", "@media print"),
                      f"导出 HTML {path} 缺打印样式 @media print（不可打印转发）")
            note = f"导出端点 {path} 内容干净"
        else:
            html = read_text(sb.repo / "ui" / "index.html")
            css = read_text(sb.repo / "ui" / "style.css") if (sb.repo / "ui" / "style.css").is_file() else ""
            check("@media print" in (html + css),
                  "未发现导出/打印端点，且 UI 静态样式无 @media print（无法打印转发）")
            html_low = html.lower()
            banned3 = [tok for tok in FORBIDDEN_TOKENS if tok in html_low]
            check(not banned3,
                  f"UI 静态文本含模型名/内部词 {banned3}（§3.1 不得出现）")
            note = "无导出端点（HTTP 探测全 404），降级为 UI 静态 @media print + 词表检查"
        return f"DEC 文本无模型名/token/轨迹；{note}"


def case_u4(sb: Sandbox):
    wf = load_json(FIX / "workflow_result.json")
    with LocalServer(sb) as srv:
        pipeline = run_pipeline(srv, sb, wf)
        ensure_delivery_chain(pipeline)
        tid = pipeline["tid"]
        dec, _ = get_dec(pipeline)
        check(dec is not None,
              "P2 决策卡（DEC）未出现，无法校验 T+7 回填（实现未就绪）")
        before = dec.get("outcome")
        delivery_text = json.dumps(pipeline.get("delivery") or {}, ensure_ascii=False)
        report_text = ""
        if pipeline["report_path"].is_file():
            report_text = read_text(pipeline["report_path"])
        # 未回填标记：人话「待回看」或结构化 outcome_pending=true
        status0, body0, _ = srv.get_json(f"/api/tasks/{tid}/outcome")
        outcome_pending = isinstance(body0, dict) and (
            body0.get("outcome_pending") or dig(body0, "outcome.pending"))
        unfilled_marker = ("待回看" in (delivery_text + report_text)) or bool(outcome_pending)
        check(before in (None, {}, []) or unfilled_marker,
              f"未回填时 DEC.outcome 应为空并标「待回看」，实际 outcome={before} 且未见「待回看」")
        # ---- R4 严格段（回填前）：P3 未回填条目必须标「待回看」且 filled=0 筛得出 ----
        # 在回填之前取样，才能真正验到「待回看」这条分支（回填后沙箱内已无待回看条目）。
        sp0, bp0, tp0 = srv.get_json("/api/decisions")
        check(sp0 == 200, f"回填前 GET /api/decisions 应 200，实际 {sp0}: {tail(tp0)}")
        check(isinstance(bp0, dict) and isinstance(bp0.get("decisions"), list),
              f"回填前 GET /api/decisions 形状不对：{tail(tp0)}")
        rows0 = bp0.get("decisions") or []
        mine0 = [r for r in rows0 if isinstance(r, dict) and r.get("task_id") == tid]
        check(len(mine0) == 1,
              f"回填前 P3 应含本单 1 条（task_id={tid}），实际 {len(mine0)} 条")
        row0 = mine0[0]
        check(str(row0.get("outcome_status") or "") == "待回看",
              f"回填前 P3 本单应标「待回看」，实际 {row0.get('outcome_status')!r}")
        check(row0.get("outcome_pending") is True,
              f"回填前 P3 本单 outcome_pending 应为 true，实际 {row0.get('outcome_pending')!r}")
        sf0, bf0, _ = srv.get_json("/api/decisions?filled=0")
        check(sf0 == 200, f"回填前 GET /api/decisions?filled=0 应 200，实际 {sf0}")
        ids0 = [r.get("task_id") for r in (bf0.get("decisions") or [])]
        check(tid in ids0,
              f"回填前筛选 filled=0（待回看）应含本单：{ids0}")
        sf1, bf1, _ = srv.get_json("/api/decisions?filled=1")
        check(sf1 == 200, f"回填前 GET /api/decisions?filled=1 应 200，实际 {sf1}")
        ids1 = [r.get("task_id") for r in (bf1.get("decisions") or [])]
        check(tid not in ids1,
              f"回填前筛选 filled=1（已回看）不应含本单：{ids1}")
        observed = {
            "refill_rate_avg": 6.42,
            "refill_rate_target": 7.2,
            "observed_at": "2026-10-20T09:00:00+08:00",
        }
        payload = {
            # 只用产品契约 OUTCOME_FIELDS 内的键（observed_outcome / outcome_window /
            # actual_action / override_reason / action_cost / increment_value）：
            # 未知字段一律 400（DEC 是事实源，展示字段不得混入）。
            "observed_outcome": dict(observed),
            "outcome_window": "7d",
            "actual_action": "maintenance",
            "override_reason": None,
            "action_cost": 800,
            "increment_value": 1234.5,
        }
        status, body, text = srv.post(f"/api/tasks/{tid}/outcome", payload)
        if status in (404, 405):
            not_ready(f"POST /api/tasks/<id>/outcome 未实现 HTTP {status}: {tail(text)}")
        check(status in (200, 201, 202),
              f"POST /outcome 应 2xx，实际 {status}: {tail(text)}")
        # GET /outcome 也应可回读
        got_status, got_body, got_text = srv.get_json(f"/api/tasks/{tid}/outcome")
        check(got_status == 200,
              f"GET /outcome 应 200，实际 {got_status}: {tail(got_text)}")
        status, delivery2, text2 = srv.get_json(f"/api/tasks/{tid}/delivery")
        check(status == 200,
              f"回填后 GET /delivery 应 200，实际 {status}: {tail(text2)}")
        dec2, where2 = get_dec({"delivery": delivery2})
        check(dec2 is not None,
              "回填后 delivery 中未找到更新后的 DEC 决策卡")
        outcome = dec2.get("outcome")
        check(isinstance(outcome, dict) and outcome,
              f"回填后 DEC.outcome 仍为空：{outcome}")
        problems = []
        observed = outcome.get("observed")
        if not isinstance(observed, dict):
            problems.append("DEC.outcome.observed 非对象")
        elif not observed:
            problems.append("DEC.outcome.observed 为空")
        if "actual_action" not in outcome:
            problems.append("DEC.outcome 缺 actual_action")
        inc = outcome.get("increment_value")
        if inc is None:
            problems.append("DEC.outcome 缺 increment_value")
        text_all = json.dumps(delivery2, ensure_ascii=False)
        if "1234.5" not in text_all and "1234.50" not in text_all:
            problems.append("delivery 未透出 increment_value=1234.5")
        check(not problems, "；".join(problems[:8]))

        # ---- R4 严格段：P3 决策日志（跨任务时间线）必须实现且与 P2 自洽 ----
        # 口径（设计文档 §3.5 U4 + §3.2 P3 规格）：
        #   a) GET /api/decisions 必须 200 且含本单条目；
        #   b) 回填后 P3 条目透出 observed_outcome / increment_value（与 P2 同值）；
        #   c) 未回填的条目标「待回看」；本单回填后标「已回填」；
        #   d) 筛选真实生效（filled=0/1、verdict、conclusion 关键词）；
        #   e) 跨页自洽：P3 与 P2 的 increment_value / observed_outcome 一致。
        s3, b3, t3 = srv.get_json("/api/decisions")
        check(s3 == 200, f"GET /api/decisions 应 200（R4 未实现或路由未挂），实际 {s3}: {tail(t3)}")
        check(isinstance(b3, dict) and isinstance(b3.get("decisions"), list),
              f"GET /api/decisions 返回形状不对（缺 decisions 数组）: {tail(t3)}")
        rows = b3.get("decisions") or []
        mine = [r for r in rows if isinstance(r, dict) and r.get("task_id") == tid]
        check(len(mine) == 1,
              f"P3 时间线应恰好含本单 1 条（task_id={tid}），实际 {len(mine)} 条（共 {len(rows)} 条）")
        row = mine[0]
        dec_id = str(dec2.get("decision_id") or "")
        check(dec_id and row.get("decision_id") == dec_id,
              f"P3 与 P2 的 decision_id 不一致：P3={row.get('decision_id')!r} P2={dec_id!r}")
        p3_out = row.get("outcome") if isinstance(row.get("outcome"), dict) else {}
        check(bool(p3_out.get("observed_outcome")),
              f"P3 未透出 observed_outcome（实际 {p3_out.get('observed_outcome')!r}）")
        check(p3_out.get("increment_value") == 1234.5,
              f"P3 未透出 increment_value=1234.5（实际 {p3_out.get('increment_value')!r}）")
        check(str(row.get("outcome_status") or "") == "已回填",
              f"P3 本单回填后应标「已回填」，实际 {row.get('outcome_status')!r}")
        check(row.get("outcome_pending") is False,
              f"P3 本单回填后 outcome_pending 应为 false，实际 {row.get('outcome_pending')!r}")
        # 跨页自洽：P2 的 observed/increment 与 P3 同值
        p2_inc = outcome.get("increment_value")
        check(p3_out.get("increment_value") == p2_inc,
              f"P2/P3 的 increment_value 不一致：P2={p2_inc!r} P3={p3_out.get('increment_value')!r}")
        p2_observed = outcome.get("observed")
        check(p3_out.get("observed_outcome") == p2_observed,
              f"P2/P3 的 observed_outcome 不一致：P2={p2_observed!r} P3={p3_out.get('observed_outcome')!r}")
        # 筛选真实生效：filled=0 不含本单、filled=1 含本单
        s_f0, b_f0, _ = srv.get_json("/api/decisions?filled=0")
        check(s_f0 == 200, f"GET /api/decisions?filled=0 应 200，实际 {s_f0}")
        ids_f0 = [r.get("task_id") for r in (b_f0.get("decisions") or [])]
        check(tid not in ids_f0,
              f"筛选 filled=0（待回看）不应含已回填的本单：{ids_f0}")
        s_f1, b_f1, _ = srv.get_json("/api/decisions?filled=1")
        check(s_f1 == 200, f"GET /api/decisions?filled=1 应 200，实际 {s_f1}")
        ids_f1 = [r.get("task_id") for r in (b_f1.get("decisions") or [])]
        check(tid in ids_f1,
              f"筛选 filled=1（已回填）应含本单：{ids_f1}")
        # 筛选真实生效：verdict 维度
        # 关键：沙箱内可能只有本单一条，正向断言（筛出来都同档）会空转；
        # 必须用「不存在的档位 → 0 条」的反向断言才咬得住 no-op 变异。
        verdict = str(row.get("verdict") or "")
        check(bool(verdict), f"P3 条目缺 verdict 字段，无法验筛选：{row.get('verdict')!r}")
        s_v, b_v, _ = srv.get_json(f"/api/decisions?{qs('verdict', verdict)}")
        check(s_v == 200, f"GET /api/decisions?verdict={verdict} 应 200，实际 {s_v}")
        vs = [(r.get("task_id"), r.get("verdict")) for r in (b_v.get("decisions") or [])]
        check(all(v == verdict for _, v in vs),
              f"筛选 verdict={verdict} 后混入了别的档位：{vs}")
        check(any(t == tid for t, _ in vs),
              f"筛选 verdict={verdict} 未命中本单：{vs}")
        s_vx, b_vx, _ = srv.get_json(f"/api/decisions?{qs('verdict', '__不存在的档位__')}")
        check(s_vx == 200, f"GET /api/decisions?verdict=<不存在> 应 200，实际 {s_vx}")
        vxs = [r.get("task_id") for r in (b_vx.get("decisions") or [])]
        check(not vxs,
              f"筛选 verdict=<不存在的档位> 仍返回条目（筛选未生效）：{vxs}")
        # 场景/实体维度同样用反向断言（正向断言在单条沙箱里会空转）
        for dim, key in (("scene", "scene_id"), ("entity", "entity")):
            val = str(row.get(key) or "")
            if not val:
                continue
            s_d, b_d, _ = srv.get_json(f"/api/decisions?{qs(dim, val)}")
            check(s_d == 200, f"GET /api/decisions?{dim}={val} 应 200，实际 {s_d}")
            check(any(r.get("task_id") == tid for r in (b_d.get("decisions") or [])),
                  f"筛选 {dim}={val!r} 未命中本单")
            s_dx, b_dx, _ = srv.get_json(f"/api/decisions?{qs(dim, '__不存在的筛选值__')}")
            check(s_dx == 200, f"GET /api/decisions?{dim}=<不存在> 应 200，实际 {s_dx}")
            dxs = [r.get("task_id") for r in (b_dx.get("decisions") or [])]
            check(not dxs,
                  f"筛选 {dim}=<不存在的值> 仍返回条目（筛选未生效）：{dxs}")
        # 筛选真实生效：conclusion 关键词（用本单结论里的因素名）
        conclusion = str(row.get("conclusion") or "")
        factor = conclusion.split("（")[0].split(" / ")[0].strip()
        if factor:
            s_c, b_c, _ = srv.get_json(f"/api/decisions?{qs('conclusion', factor)}")
            check(s_c == 200, f"GET /api/decisions?conclusion={factor} 应 200，实际 {s_c}")
            cs = [r.get("task_id") for r in (b_c.get("decisions") or [])]
            check(tid in cs, f"筛选 conclusion={factor!r} 未命中本单：{cs}")
            s_cx, b_cx, _ = srv.get_json(f"/api/decisions?{qs('conclusion', '__不存在的结论词__')}")
            check(s_cx == 200, f"GET /api/decisions?conclusion=<不存在> 应 200，实际 {s_cx}")
            cxs = [r.get("task_id") for r in (b_cx.get("decisions") or [])]
            check(tid not in cxs,
                  f"筛选 conclusion=<不存在的词> 仍命中本单（筛选未生效）：{cxs}")
        # 未回填的决策标「待回看」：本单回填前另开一单来验
        pending_note = ""
        if any(r.get("outcome_pending") for r in rows if isinstance(r, dict)):
            pend = [r for r in rows if isinstance(r, dict) and r.get("outcome_pending")][0]
            check(str(pend.get("outcome_status") or "") == "待回看",
                  f"未回填条目应标「待回看」，实际 {pend.get('outcome_status')!r}")
            pending_note = f"待回看 {len(pend.get('outcome_status') or '')} 条已验"
        else:
            pending_note = "回填后本单标「已回填」，「待回看」已在回填前取样段严格验证"
        # 跨页渲染自洽（浏览器实测抓到过真缺陷）：回填值按契约允许是 dict，
        # P2 若直接 String(obj) 会渲染成 [object Object]，与 P3 的 JSON 不一致。
        # 口径：P2 与 P3 必须都走同一个取值器 deskOutcomeText（断言只收紧不放宽）。
        ui_html = read_text(sb.repo / "ui" / "index.html")
        check("deskOutcomeText(outcome.observed_outcome)" in ui_html,
              "P2 未用 deskOutcomeText 渲染 observed_outcome（dict 会被渲染成 [object Object]）")
        check("deskOutcomeText(out.observed_outcome)" in ui_html,
              "P3 未用 deskOutcomeText 渲染 observed_outcome（与 P2 口径不一致）")
        check("deskEsc(outcome.observed_outcome" not in ui_html,
              "P2 仍直接 deskEsc(outcome.observed_outcome)（dict → [object Object]）")
        # P0↔P1 任务锚点自洽（浏览器实测抓到过真缺陷）：P0 卡显示「最近决策」（spec.latest），
        # 而 P1 读工作台当前任务；同一场景多单时两端会显示不同数字（实测 4.8% vs 7.2%）。
        # 裁定：点卡片即把当前任务切到该场景最近一单（与 P3「打开决策卡」同机制）。
        _oc = ui_html.split("function openScene(", 1)
        check(len(_oc) == 2, "ui/index.html 缺少 openScene（P0 场景卡点击入口）")
        _oc_body = _oc[1].split("function renderP1(", 1)[0] if len(_oc) == 2 else ""
        # 只查关键字会被「if (false && ...)」这种死分支变异逃逸（实测已逃逸过一次），
        # 故锁死守卫形态本身：必须是活的 if + 真实调用，不接受被短路或被常量门禁。
        check("if (latestId && adapter && adapter.selectTask && latestId !== curId) {" in _oc_body,
              "openScene 的「切最近一单」守卫被改（死分支/常量门禁），P0 卡与 P1 会显示不同任务的数字")
        check("adapter.selectTask(latestId)" in _oc_body,
              "openScene 未真正调用 adapter.selectTask(latestId)（P0→P1 任务锚点未同步）")
        return (f"回填后 DEC.outcome.observed 非空、actual_action={outcome.get('actual_action')}、"
                f"increment_value={inc}；P3 /api/decisions 200 且本单自洽（decision_id={dec_id}、"
                f"observed/increment 与 P2 同值）；筛选 filled/verdict/conclusion 均生效；{pending_note}")


def case_u5(sb: Sandbox):
    wf = load_json(FIX / "workflow_result.json")
    with LocalServer(sb) as srv:
        pipeline = run_pipeline(srv, sb, wf, null_rate=0.30)
        ensure_delivery_chain(pipeline)
        dec, where = get_dec(pipeline)
        check(dec is not None,
              "空值率>10% 时未产出决策卡/降级态（实现未就绪或降级契约缺失）")
        text_all = json.dumps(dec, ensure_ascii=False)
        if pipeline.get("delivery"):
            text_all += json.dumps(pipeline["delivery"], ensure_ascii=False)
        if pipeline["report_path"].is_file():
            text_all += read_text(pipeline["report_path"])
        problems = []
        rec = dec.get("recommendation")
        if isinstance(rec, list):
            if rec:
                problems.append(f"空值率>10% 仍给出 {len(rec)} 条建议（P1 应不显示建议）")
        elif isinstance(rec, dict):
            if rec.get("action"):
                problems.append(f"空值率>10% 仍给出建议 action={rec.get('action')}")
        elif rec not in (None, [], {}):
            problems.append(f"DEC.recommendation 形状异常: {rec!r}")
        markers = ["空值率", "缺失", "数据完整", "降级", "cannot_attribute", "数据不足"]
        hits = [m for m in markers if m in text_all]
        if not hits:
            problems.append("未见降级原因/缺失字段清单（P1 应只显示降级原因）")
        attr = dec.get("attribution") if isinstance(dec.get("attribution"), dict) else {}
        sc = attr.get("scorecard") if isinstance(attr.get("scorecard"), dict) else {}
        low_dim = None
        for key in ("data_completeness", "completeness"):
            if key in sc and isinstance(sc[key], (int, float)) and sc[key] <= 3:
                low_dim = f"{key}={sc[key]}"
                break
        hard = attr.get("hard_guardrails")
        if low_dim is None and not (isinstance(hard, list) and hard):
            problems.append("评分卡未体现数据完整度降级（data_completeness/guardrails 均无）")
        complete = sc.get("completeness", sc.get("data_completeness"))
        check(not problems, "；".join(problems[:8]))
        return (f"空值率 30%：建议区为空，命中降级理由 {hits[:3]}"
                f"{('，data_completeness=' + str(complete)) if complete is not None else ''}")


def case_u6(sb: Sandbox):
    seed = load_json(FIX / "scene_scrap_rate.json")
    scene_dir = sb.repo / "packs" / "manufacturing" / "scenes"
    scene_dir.mkdir(parents=True, exist_ok=True)
    dump_json(scene_dir / "scene_scrap_rate.json", seed)
    with LocalServer(sb) as srv:
        status, body, text = srv.get_json("/api/scenes")
        if status in (404, 405):
            not_ready(f"GET /api/scenes 未实现 HTTP {status}: {tail(text)}")
        check(status == 200, f"GET /api/scenes 应 200，实际 {status}: {tail(text)}")
        entries = entries_of(body)
        sids = [scene_meta(e).get("scene_id") for e in entries]
        check(len(entries) >= 4,
              f"seed 第 4 个场景后 GET /api/scenes 应 >=4 个，实际 {len(entries)}：{sids}")
        check("scrap_rate_high" in sids,
              f"seed 的废品率场景未出现在 /api/scenes：{sids}")
        # 通用渲染器：前端不得写死任何场景常量。
        # 扫描口径＝场景/指标的**规范 id**（refill_rate / downtime_minutes /
        # output_per_capita / scrap_rate），不是泛词（如 refill）——
        # "material_refill_T.json" 是取数文件命名契约，不是场景常量。
        index_path = sb.repo / "ui" / "index.html"
        adapter_path = sb.repo / "ui" / "api-adapter.js"
        problems = []
        for p in (index_path, adapter_path):
            if not p.is_file():
                not_ready(f"缺少 {p.relative_to(sb.repo)}")
        index_text = read_text(index_path)
        adapter_text = read_text(adapter_path)
        for token in ("refill_rate", "downtime_minutes", "output_per_capita", "scrap_rate"):
            hits = rel_lines(index_path, [token])
            if hits:
                problems.append(f"ui/index.html 写死场景常量 '{token}' -> {hits[:2]}")
            hits2 = rel_lines(adapter_path, [token])
            if hits2:
                problems.append(f"ui/api-adapter.js 写死场景常量 '{token}' -> {hits2[:2]}")
        combined = index_text + "\n" + adapter_text
        renderer_markers = ["/api/scenes", "scene_id", "spec_version", "control_line", "场景"]
        markers = [m for m in renderer_markers if m in combined]
        if "/api/scenes" not in combined:
            problems.append("前端未消费 GET /api/scenes（找不到 /api/scenes 调用）")
        if "场景" not in combined:
            problems.append("前端无场景化文案（找不到「场景」）")
        if len(markers) < 3:
            problems.append(f"通用渲染器关键字不足：命中 {markers}（需 >=3）")
        check(not problems, "；".join(problems[:8]))
        # HTTP 首页为场景列表渲染入口
        status, ctype, raw = srv.get("/")
        html = raw.decode("utf-8", "replace")
        check(status == 200 and "场景" in html, "GET / 未呈现场景化首页")
        return (f"第 4 场景 scrap_rate_high 零代码改动可见（{sids}）；"
                f"通用渲染关键字 {markers}；index.html 无场景常量")


def case_u7(sb: Sandbox):
    """U7 双模式契约（lead 2026-10-19 裁决）：
    校验模式 = body 含 spec -> 非法 spec_version/缺必填 400，其余五类 200 {ok:false,cannot_derive};
    生成模式 = body 无 spec（prompt/metric_id/data_ref）-> preset 命中 / cannot_derive / user_prompt 501。
    """
    with LocalServer(sb) as srv:
        legal = load_json(FIX / "scene_refill_rate.json")
        status, body, text = srv.post("/api/scenes/derive", {"spec": legal})
        if status in (404, 405):
            not_ready(f"POST /api/scenes/derive 未实现 HTTP {status}: {tail(text)}")
        check(status in (200, 201) and not _is_rejection(body),
              f"校验模式正控失败：合法 spec 被拒 HTTP {status}: {tail(text, 300)}")
        problems = []
        schema_level = {"invalid_spec_version.json", "invalid_missing_field.json"}
        statuses = {}
        for name in INVALID_FIXTURES:
            spec = load_json(FIX / name)
            st, resp, txt = srv.post("/api/scenes/derive", {"spec": spec})
            statuses[name] = st
            if st in (404, 405):
                not_ready(f"POST /api/scenes/derive 未实现 HTTP {st}: {tail(txt)}")
            rejected = st in (400, 422) or _is_rejection(resp)
            if not rejected:
                problems.append(f"{name}: 非法 SceneSpec 未被拒（HTTP {st}）: {tail(txt, 200)}")
                continue
            if name in schema_level and st != 400:
                problems.append(
                    f"{name}: 契约要求 schema 级拒绝=400，实际 HTTP {st} "
                    f"（{tail(json.dumps(resp, ensure_ascii=False), 200)}）")
            if st in (200, 201) and not (isinstance(resp, dict) and (
                    resp.get("ok") is False or resp.get("cannot_derive"))):
                problems.append(f"{name}: 200 必须带 ok:false/cannot_derive，实际 {tail(txt, 160)}")
            reason = _reason_text(resp, txt)
            if "Traceback" in (txt or "") or "traceback" in reason.lower():
                problems.append(f"{name}: 返回 traceback 而非人话原因")
            if len(reason.strip()) < 6:
                problems.append(f"{name}: 拒绝原因过短/缺失（人话原因要求）: {reason!r}")
            else:
                human = [w for w in ("spec_version", "metric", "factor", "action",
                                     "threshold", "pack", "字段", "指标", "因子",
                                     "动作", "阈值", "版本", "必填", "不认识",
                                     "不支持", "无法", "拒绝", "无法派生") if w in reason]
                if not human:
                    problems.append(f"{name}: 原因未指向具体规则（{reason[:80]}）")
        # 非法 spec 不得混入场景列表
        st_list, body_list, _ = srv.get_json("/api/scenes")
        if st_list == 200:
            sids = [scene_meta(e).get("scene_id") for e in entries_of(body_list)]
            for name in INVALID_FIXTURES:
                spec = load_json(FIX / name)
                if spec.get("scene_id") in sids:
                    problems.append(f"{name}: 非法 spec 混入 /api/scenes 列表")
        # 生成模式（body 无 spec）：discovered 通道必须真的派生成功（200 + scene_id）。
        # 收紧依据：R1 修复前 server 用「乱试三种签名」掩盖 core.scene.discover 签名不符，
        # U7 又把 cannot_derive 记成合法通过 → 绿测说通过、实际 discovered 全坏（公理 5）。
        # 现在 discovered 必须 200 且带 scene_id；cannot_derive 一律计入 problems。
        gen_notes = []
        st_d, resp_d, txt_d = srv.post(
            "/api/scenes/derive",
            {"metric_id": "refill_rate", "data_ref": "demo/material_refill_T.json"})
        if st_d in (404, 405):
            not_ready(f"POST /api/scenes/derive 未实现 HTTP {st_d}: {tail(txt_d)}")
        if st_d >= 500:
            problems.append(f"discovered 通道异常 HTTP {st_d}: {tail(txt_d, 200)}")
        elif st_d not in (200, 201):
            problems.append(f"discovered 通道应 200，实际 HTTP {st_d}: {tail(txt_d, 200)}")
        else:
            scene_id = resp_d.get("scene_id") if isinstance(resp_d, dict) else None
            if not scene_id and isinstance(resp_d, dict):
                scene_id = dig(resp_d, "scene.scene_id") or dig(resp_d, "spec.scene_id")
            if not scene_id:
                problems.append(
                    f"discovered 通道 200 但无 scene_id（cannot_derive 计入失败）: {tail(txt_d, 200)}")
            elif isinstance(resp_d, dict) and resp_d.get("channel") != "discovered":
                problems.append(
                    f"discovered 通道 channel 应为 discovered，实际 {resp_d.get('channel')!r}")
            else:
                gen_notes.append(f"discovered->200 {scene_id}")
        # 负例：data_ref 指向不存在的文件 -> 200 + cannot_derive（人话原因），不得 500
        st_n, resp_n, txt_n = srv.post(
            "/api/scenes/derive", {"data_ref": "demo/__not_exist__.json"})
        if st_n in (404, 405):
            not_ready(f"POST /api/scenes/derive 未实现 HTTP {st_n}: {tail(txt_n)}")
        if st_n >= 500:
            problems.append(f"不存在的数据文件不得 500，实际 HTTP {st_n}: {tail(txt_n, 200)}")
        elif st_n not in (200, 201) or not _is_rejection(resp_n):
            problems.append(
                f"不存在的数据文件应 200 + cannot_derive，实际 HTTP {st_n}: {tail(txt_n, 200)}")
        else:
            gen_notes.append(f"不存在文件->{st_n} cannot_derive")
        # user_prompt 通道：契约允许 501（T0 server 内不调 LLM，由 M2 DSH s0_scene 承接）
        st_p, resp_p, txt_p = srv.post(
            "/api/scenes/derive", {"prompt": "帮我盯一下 B 线补料率"})
        if st_p in (404, 405):
            not_ready(f"POST /api/scenes/derive 未实现 HTTP {st_p}: {tail(txt_p)}")
        if st_p >= 500 and st_p != 501:
            problems.append(f"user_prompt 异常 HTTP {st_p}: {tail(txt_p, 200)}")
        elif st_p == 501:
            gen_notes.append("user_prompt->501(user_prompt 未接 LLM，契约允许)")
        elif st_p in (200, 201) and not _is_rejection(resp_p):
            gen_notes.append(f"user_prompt->{st_p} 200")
        else:
            gen_notes.append(f"user_prompt->{st_p} cannot_derive")
        check(not problems, "；".join(problems[:10]))
        return (f"校验模式 6 类非法全拒 status={statuses}，合法正控通过；"
                f"生成模式 {gen_notes}")


def _is_rejection(body) -> bool:
    if not isinstance(body, dict):
        return False
    if body.get("ok") is False:
        return True
    if body.get("cannot_derive"):
        return True
    if body.get("status") in ("rejected", "cannot_derive", "invalid"):
        return True
    return bool(body.get("error") or body.get("errors") or body.get("reasons"))


def _reason_text(body, raw_text: str) -> str:
    if isinstance(body, dict):
        for key in ("reason", "error", "message", "cannot_derive", "reasons", "errors"):
            val = body.get(key)
            if isinstance(val, str):
                return val
            if isinstance(val, list):
                return "；".join(str(v) for v in val)
    return raw_text or ""


def case_u8(sb: Sandbox):
    with LocalServer(sb) as srv:
        def snapshot():
            status, body, text = srv.get_json("/api/scenes")
            if status in (404, 405):
                not_ready(f"GET /api/scenes 未实现 HTTP {status}: {tail(text)}")
            check(status == 200, f"GET /api/scenes 应 200，实际 {status}: {tail(text)}")
            out = {}
            for entry in entries_of(body):
                meta = scene_meta(entry)
                sid = meta.get("scene_id")
                if not sid:
                    continue
                ver = meta.get("version")
                key = f"{sid}@{ver}"
                out[key] = {
                    "title": meta.get("title"),
                    "metric": meta.get("metric") or entry.get("metric"),
                    "actions": meta.get("actions"),
                    "control_line": meta.get("control_line"),
                    "origin": meta.get("origin") or entry.get("origin"),
                }
            return out

        first = snapshot()
        check(len(first) >= 3, f"场景列表快照过少：{list(first)}")
        time.sleep(0.4)
        second = snapshot()
        problems = []
        if set(first) != set(second):
            problems.append(f"两次渲染场景集合不一致：{sorted(set(first) ^ set(second))}")
        for key in sorted(set(first) & set(second)):
            if json.dumps(first[key], ensure_ascii=False, sort_keys=True) != \
                    json.dumps(second[key], ensure_ascii=False, sort_keys=True):
                problems.append(f"{key}: scene_id@version 两次关键字段不一致")
                break
        allowed = {"preset", "user_prompt", "discovered"}
        for key, meta in first.items():
            origin = meta.get("origin")
            if origin not in allowed:
                problems.append(f"{key}: origin 缺失或非法（{origin!r}，应 ∈ {sorted(allowed)}）")
            if not meta.get("title"):
                problems.append(f"{key}: 缺 title")
        # 详情端点：若实现，两次 GET 亦应一致
        note = "详情端点未实现（仅列表）"
        for key in list(first)[:1]:
            sid, _, ver = key.partition("@")
            for path in (f"/api/scenes/{sid}?version={ver}", f"/api/scenes/{sid}/{ver}"):
                st, body, txt = srv.get_json(path)
                if st == 200:
                    a = json.dumps(body, ensure_ascii=False, sort_keys=True)
                    st2, body2, _ = srv.get_json(path)
                    b = json.dumps(body2, ensure_ascii=False, sort_keys=True)
                    if a != b:
                        problems.append(f"{path}: 两次渲染详情不一致")
                    note = f"详情端点 {path} 稳定，origin 可见"
                    break
        check(not problems, "；".join(problems[:8]))
        return (f"{len(first)} 个场景两次渲染关键字段一致、origin 可见"
                f"（{sorted({m['origin'] for m in first.values()})}）；{note}")


CASES_MAP = {
    "U1": case_u1,
    "U2": case_u2,
    "U3": case_u3,
    "U4": case_u4,
    "U5": case_u5,
    "U6": case_u6,
    "U7": case_u7,
    "U8": case_u8,
}


# --------------------------------------------------------------------------- #
# fixtures 自检
# --------------------------------------------------------------------------- #
def selfcheck() -> int:
    problems = []
    for name in LEGAL_FIXTURES:
        p = FIX / name
        if not p.is_file():
            problems.append(f"缺合法夹具 {name}")
            continue
        spec = load_json(p)
        required = ("spec_version", "scene_id", "pack_id", "version", "origin", "title",
                    "question", "metric", "entity_scope", "factors_ref", "actions",
                    "guardrails", "data_requirements", "scorecard",
                    "output_contract_ref", "generated_by", "generated_at", "evidence_refs")
        for key in required:
            if key not in spec:
                problems.append(f"{name}: 缺 {key}")
        if spec.get("spec_version") != "scene.v1":
            problems.append(f"{name}: spec_version != scene.v1")
        if spec.get("origin") not in ("preset", "user_prompt", "discovered"):
            problems.append(f"{name}: origin 非法 {spec.get('origin')!r}")
        metric = spec.get("metric") or {}
        for key in ("id", "name", "unit", "grain", "direction", "control_line"):
            if key not in metric:
                problems.append(f"{name}: metric 缺 {key}")
        # 枚举口径统一（core.scene.GRAINS / DIRECTIONS，pack 为单一事实源）：
        # 夹具不得自造 "line/day" / "higher_is_better" 这类非规范值
        if metric.get("grain") not in SCENE_GRAINS:
            problems.append(f"{name}: metric.grain={metric.get('grain')!r} 不在规范枚举 {list(SCENE_GRAINS)}")
        if metric.get("direction") not in SCENE_DIRECTIONS:
            problems.append(
                f"{name}: metric.direction={metric.get('direction')!r} 不在规范枚举 {list(SCENE_DIRECTIONS)}")
        if metric.get("id") not in PACK_METRIC_IDS:
            problems.append(f"{name}: metric.id={metric.get('id')!r} 不在 pack.metrics 目录 {sorted(PACK_METRIC_IDS)}")
        sc = spec.get("scorecard") or {}
        if sc.get("scale") != 100:
            problems.append(f"{name}: scorecard.scale 应为 100")
        if sc.get("dims_ref") != "pack.scorecard_six_dim":
            problems.append(f"{name}: dims_ref 应为 pack.scorecard_six_dim")
        if sc.get("hard_guardrails_ref") != "pack.scorecard_hard_guardrails":
            problems.append(
                f"{name}: hard_guardrails_ref 应为 pack.scorecard_hard_guardrails（B16 正名）")
        if not isinstance(spec.get("actions"), list) or not spec["actions"]:
            problems.append(f"{name}: actions 为空")
        for i, act in enumerate(spec.get("actions") or []):
            for key in ("id", "label", "cost_anchor_key", "reversible"):
                if key not in act:
                    problems.append(f"{name}: actions[{i}] 缺 {key}")
        for i, req in enumerate(spec.get("data_requirements") or []):
            for key in ("field", "required", "quality"):
                if key not in req:
                    problems.append(f"{name}: data_requirements[{i}] 缺 {key}")
        if "current_value" not in metric:
            problems.append(f"{name}: metric 缺 current_value（应为 null 占位）")
        # spec 内不得出现数值阈值常量（阈值/ROI 必须引用 pack 键）
        raw = p.read_text(encoding="utf-8")
        for bad in ("5.40", "7.2", "1200", "800", "500"):
            if bad in raw:
                problems.append(f"{name}: spec 内出现数值常量 {bad}（阈值/ROI 应引用 pack 键）")
    for name in INVALID_FIXTURES:
        p = FIX / name
        if not p.is_file():
            problems.append(f"缺非法夹具 {name}")
            continue
        try:
            spec = load_json(p)
        except ValueError as exc:
            problems.append(f"{name}: JSON 解析失败 {exc}")
            continue
        if name == "invalid_metric.json" and spec["metric"]["id"] == "magic_quality_index":
            continue
        if name == "invalid_factor.json" and spec["factors_ref"]["pack_field"] == "weather_factors":
            continue
        if name == "invalid_irreversible.json" and \
                spec["actions"][0]["id"] == "permanent_line_stop":
            continue
        if name == "invalid_threshold.json" and \
                spec["metric"]["control_line"]["params"].get("target") == 9.99:
            continue
        if name == "invalid_spec_version.json" and spec["spec_version"] == "scene.v2":
            continue
        if name == "invalid_missing_field.json" and "title" not in spec:
            continue
        problems.append(f"{name}: 未按预期构造非法点（夹具自身问题）")
    wf = FIX / "workflow_result.json"
    if not wf.is_file():
        problems.append("缺 workflow_result.json")
    else:
        try:
            doc = load_json(wf)
        except ValueError as exc:
            problems.append(f"workflow_result.json: JSON 解析失败 {exc}")
            doc = {}
        if "attribution" not in doc or "advice" not in doc:
            problems.append("workflow_result.json 缺 attribution/advice（assemble 输入契约）")
    if problems:
        for p in problems:
            print(f"SELFCHECK FAIL: {p}")
        print(f"RESULT: FAIL（{len(problems)} 项）")
        return 1
    print(f"SELFCHECK PASS: 合法夹具 {len(LEGAL_FIXTURES)}，非法夹具 "
          f"{len(INVALID_FIXTURES)}，workflow_result.json 1")
    print("RESULT: PASS")
    return 0


# --------------------------------------------------------------------------- #
# 回归套件（阶段二用；阶段一不跑）
# --------------------------------------------------------------------------- #
REGRESSION_SUITES = [
    ("B11 A13-A16", ["tests/test_b12_acceptance.py"]),
    ("M1 A1-A6", ["tests/test_m1_acceptance.py"]),
    ("M1.5 A7-A12", ["tests/test_m15_acceptance.py"]),
    ("m1_smoke", ["tests/m1_smoke.py"]),
    ("core_m15", None),  # 特殊处理：直接跑或 pytest
    ("validate_structure", ["tests/validate_structure.py"]),
    ("validate_report demo", ["tests/validate_report.py", "runs/demo-report-2026-10-19.json"]),
]


def run_regression() -> int:
    sb = Sandbox("reg")
    problems = []
    evidence = []
    try:
        for name, cmd in REGRESSION_SUITES:
            if cmd is None:
                candidates = [
                    ["tests/test_core_m15.py"],
                    ["-m", "pytest", "-q", "tests/test_core_m15.py"],
                ]
                ok = False
                last = ""
                for cand in candidates:
                    rc, out, err = run_py(cand, sb.repo, timeout=900)
                    last = tail(out + "\n" + err)
                    if rc == 0:
                        ok = True
                        break
                if not ok:
                    problems.append(f"{name} 回归失败: {last}")
                    evidence.append(f"{name}=FAIL")
                else:
                    evidence.append(f"{name}=PASS")
                continue
            rc, out, err = run_py(cmd, sb.repo, timeout=900)
            if rc != 0:
                problems.append(f"{name} 回归失败 rc={rc}: {tail(out + err)}")
                evidence.append(f"{name}=FAIL")
            else:
                m = re.search(r"RESULT:\s*(PASS|GREEN|FAIL|RED)", out)
                if m and m.group(1) in ("FAIL", "RED"):
                    problems.append(f"{name} 输出 {m.group(1)}: {tail(out)}")
                    evidence.append(f"{name}=FAIL")
                else:
                    evidence.append(f"{name}=PASS")
            if name == "M1 A1-A6":
                if "CASE A4 PASS" not in out:
                    problems.append("A4（100 分制）未 PASS：" + tail(out))
                elif "total=100.0" not in out:
                    problems.append("A4 PASS 但输出未见 total=100.0（100 分制契约）")
        # demo 红线四哈希
        for rel, prefix in DEMO_HASH_PREFIX.items():
            path = sb.repo / ("runs" if rel.startswith("demo-") else "demo") / rel
            if not path.is_file():
                problems.append(f"红线文件缺失: {path}")
                continue
            actual = sha256_file(path)
            if not actual.startswith(prefix):
                problems.append(f"{rel} 哈希变化: {actual[:16]} != {prefix}")
    finally:
        sb.cleanup()
    for e in evidence:
        print(f"CASE REG {e}")
    if problems:
        for p in problems:
            print(f"CASE REG FAIL: {p}")
        print("RESULT: RED")
        return 1
    print("CASE REG PASS: 回归套件全绿 + demo 四哈希不变")
    print("RESULT: GREEN")
    return 0


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    ap = argparse.ArgumentParser(description="B16/B18/B15/B17 场景验收（U1-U8）")
    ap.add_argument("--case", action="append", default=[],
                    help="只跑指定用例，可重复或逗号分隔，如 --case U2,U4")
    ap.add_argument("--list", action="store_true", help="列出用例")
    ap.add_argument("--selfcheck", action="store_true", help="只做夹具自检")
    ap.add_argument("--regression", action="store_true", help="只跑回归套件")
    ap.add_argument("--keep", action="store_true", help="保留全部沙盒")
    ap.add_argument("--keep-failed", dest="keep_failed", action="store_true",
                    default=True, help="保留失败沙盒（默认）")
    ap.add_argument("--no-keep-failed", dest="keep_failed", action="store_false")
    ap.add_argument("--timeout", type=int, default=1800)
    args = ap.parse_args(argv)

    if args.list:
        for c in CASES:
            print(f"{c}  {CASES_MAP[c].__doc__ or ''}".rstrip())
        print("REG  回归套件（--regression）")
        return 0
    if args.selfcheck:
        return selfcheck()
    if args.regression and not args.case:
        return run_regression()

    wanted = []
    for raw in args.case:
        for part in str(raw).split(","):
            part = part.strip().upper()
            if part:
                wanted.append(part)
    if not wanted:
        wanted = list(CASES)
    for c in wanted:
        if c not in CASES_MAP:
            print(f"未知用例 {c}（可用：{', '.join(CASES)}）")
            return 2

    results = []
    for c in wanted:
        sb = None
        started = time.time()
        try:
            sb = Sandbox(c)
            detail = CASES_MAP[c](sb)
            elapsed = time.time() - started
            print(f"CASE {c} PASS: {detail}（{elapsed:.1f}s）")
            results.append((c, True))
        except CaseFailure as exc:
            elapsed = time.time() - started
            print(f"CASE {c} FAIL[{exc.kind}]: {exc.reason}"
                  + (f"（{elapsed:.1f}s）" if elapsed else ""))
            results.append((c, False))
        except Exception as exc:  # noqa: BLE001 - 测试自身异常必须可见
            elapsed = time.time() - started
            print(f"CASE {c} FAIL[test_issue]: 未捕获异常 {exc!r}"
                  f"（{elapsed:.1f}s）\n{traceback.format_exc()[-2000:]}")
            results.append((c, False))
        finally:
            if sb is not None:
                failed = results and results[-1][0] == c and not results[-1][1]
                if args.keep or (failed and args.keep_failed):
                    print(f"  SANDBOX {sb.root}")
                else:
                    sb.cleanup()
    if args.regression:
        rc = run_regression()
        results.append(("REG", rc == 0))
    passed = [c for c, ok in results if ok]
    failed = [c for c, ok in results if not ok]
    if failed:
        print(f"RESULT: RED（{len(passed)} PASS / {len(failed)} FAIL：{', '.join(failed)}）")
        return 1
    print(f"RESULT: GREEN（{len(passed)} PASS：{', '.join(passed)}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
